"""Render editorial letters, style sheets, and verification reports from case
files and run evidence. No model or network calls; expose every unresolved
query. render_all writes the letter and style sheet.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from galley.casefile import CaseFile
from galley.contracts import Manuscript, WaveRecord

#: The run evidence the letter and the report read: the journal's loader,
#: which already knows every artifact a run leaves behind.
from galley.journal import JournalSources, _CHECK_RE




def _money(value: float) -> str:
    """Format a dollar amount to cents, e.g. ``$1.23``."""

    return f"${value:,.2f}"


def _n(value: int) -> str:
    return f"{int(value):,}"


def _clip(text: Any, limit: int = 220) -> str:
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _recall_rate(recall: Any) -> float | None:
    """Pull an overall recall rate out of a RecallEstimate-like object or dict.

    Tolerant of shape: accepts a mapping or an object, and looks for any of a
    few conventional names for the headline rate. Returns ``None`` when nothing
    usable is present so the caller can simply omit the confidence figure.
    """

    if recall is None:
        return None
    for key in ("overall", "overall_rate", "rate", "recall"):
        value: Any = None
        if isinstance(recall, dict):
            if key in recall:
                value = recall[key]
        elif hasattr(recall, key):
            value = getattr(recall, key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _recall_book(recall: Any) -> str:
    """The book a recall estimate was measured on, or ``""`` when it doesn't
    say (a bare ``RecallEstimate``, a dict without ``book``)."""

    if recall is None:
        return ""
    if isinstance(recall, dict):
        return str(recall.get("book") or "")
    return str(getattr(recall, "book", "") or "")


def _pct(rate: float) -> str:
    """Render a rate as a percentage; accepts either 0..1 or 0..100 input."""

    pct = rate * 100.0 if rate <= 1.0 else rate
    return f"{pct:.1f}%"


def _wave_action_lines(wave: WaveRecord) -> list[str]:
    """Summarize each action dict a wave dispatched into a bullet line."""

    lines: list[str] = []
    for action in wave.actions:
        adapter = action.get("adapter") or action.get("action") or "action"
        scope = action.get("scope")
        cost = action.get("cost_usd")
        added = action.get("findings_added")
        parts = [f"**{adapter}**"]
        if scope:
            parts.append(f"scope `{scope}`")
        if added is not None:
            parts.append(f"{added} finding(s)")
        if cost is not None:
            try:
                parts.append(_money(float(cost)))
            except (TypeError, ValueError):
                pass
        lines.append("  - " + ", ".join(parts))
    return lines


def _is_duplicate(verdict: Any) -> bool:
    """An arbitration verdict that merged a re-find into the finding it
    duplicates (``galley.adjudicate.arbitrate``) — never an open question."""

    return (verdict.judge == "arbitrator"
            and verdict.reason.startswith("duplicate of "))


def _coverage_notes(cf: CaseFile) -> list[str]:
    """Every coverage note across every action of every wave, in wave order."""

    notes: list[str] = []
    for wave in cf.waves:
        for action in wave.actions:
            raw = action.get("coverage_notes") or []
            if isinstance(raw, str):
                raw = [raw]
            for note in raw:
                notes.append(f"wave {wave.index}: {note}")
    return notes




def run_evidence(run_dir: str | Path, workspace: str | Path | None = None
                 ) -> JournalSources:
    """Load everything the letter and the report read from a run directory
    (findings, verify artifacts, settlement, outcome) and, when given, its
    workspace (profile, approval, plan, certificate). A missing artifact is
    simply absent; the renderers say so rather than invent a figure."""
    return JournalSources.load(run_dir, workspace)


def _rows(src: JournalSources | None) -> list[dict[str, Any]]:
    env = src.envelope if src is not None else None
    if not isinstance(env, dict):
        return []
    return [r for r in (env.get("findings") or []) if isinstance(r, dict)]


def _state_of(row: Mapping[str, Any]) -> tuple[str, str]:
    from galley.settle import terminal_state
    return terminal_state(row)


def applied_rows(src: JournalSources | None) -> list[dict[str, Any]]:
    return [r for r in _rows(src) if _state_of(r)[0] == "applied"]


def query_rows(src: JournalSources | None) -> list[dict[str, Any]]:
    """The author questions that reach the margin: query rows that were not
    withheld."""
    out = []
    for r in _rows(src):
        state, reason = _state_of(r)
        if state == "query" and reason != "withheld":
            out.append(r)
    return out


def delivered_docx(src: JournalSources | None) -> Path | None:
    if src is None:
        return None
    from galley.verify import deliverable_docx
    return deliverable_docx(src.run_dir)


def comment_count(src: JournalSources | None) -> int | None:
    """Margin comments on the delivered document, or the query-row count when
    there is no document to count on, or None when neither exists."""
    if src is None:
        return None
    from galley.manifest import delivered_comment_count
    n = delivered_comment_count(src.run_dir)
    if n is not None:
        return n
    rows = query_rows(src)
    return len(rows) if _rows(src) else None


def _docx_media(path: Path | None) -> int | None:
    if path is None or not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as z:
            return sum(1 for n in z.namelist() if n.startswith("word/media/"))
    except (OSError, zipfile.BadZipFile):
        return None


def _comment_anchors(path: Path | None) -> tuple[int, int] | None:
    """(range starts, range ends) in the delivered document — paired when the
    two counts agree."""
    if path is None or not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    return (len(re.findall(rb"<w:commentRangeStart\b", xml)),
            len(re.findall(rb"<w:commentRangeEnd\b", xml)))


def _source_path(src: JournalSources | None) -> Path | None:
    if src is None:
        return None
    appr = src.approval if isinstance(src.approval, dict) else {}
    raw = appr.get("source") or ""
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return p
    if src.workspace is not None and (src.workspace / p).is_file():
        return src.workspace / p
    return None


def certificate_checks(src: JournalSources | None
                       ) -> list[tuple[str, str, str]]:
    """``(status, name, detail)`` for every line of the certificate, in
    order. Empty when no certificate was recorded."""
    if src is None or not src.certificate:
        return []
    out: list[tuple[str, str, str]] = []
    for line in src.certificate.splitlines():
        m = _CHECK_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip(), (m.group(3) or "").strip()))
    return out


#: Plain-English labels for the finding families a proofread applies. Keys
#: are error types (typed passes, sweeps, import rows); anything unlisted is
#: shown as its key with the underscores spaced out.
FAMILY_LABELS: dict[str, str] = {
    "sweep_time_of_day": "clock times in the house form (h:mm AM/PM)",
    "sweep_ellipsis": "ellipses set the house way",
    "sweep_dash": "dashes (unspaced em dash; en dash in ranges)",
    "sweep_stacked_punctuation": "stacked terminal punctuation (one mark)",
    "sweep_quote_punctuation": "commas and periods inside closing quotes",
    "sweep_nested_quote": "nested quotation marks",
    "sweep_dialogue_tag": "dialogue-tag punctuation",
    "sweep_doubled_word": "doubled words (the the)",
    "sweep_terminal_period": "missing terminal periods",
    "sweep_double_period": "doubled periods",
    "sweep_space_before_comma": "stray space before a comma",
    "sweep_space_before_period": "stray space before a period",
    "serial_comma": "the serial comma",
    "comma_splice": "comma splices",
    "compound_sentence_comma": "commas between independent clauses",
    "spelling": "misspellings",
    "spellscan": "misspellings",
    "typo": "typos",
    "capitalization": "capitalization",
    "number_style": "numbers in the house style (spelled to one hundred)",
    "numbers": "numbers in the house style (spelled to one hundred)",
    "hyphenation": "hyphenation (Merriam-Webster)",
    "apostrophe": "apostrophes and possessives",
    "subject_verb_agreement": "subject-verb agreement",
    "tense_shift": "isolated tense slips",
    "pronoun_agreement": "pronoun agreement",
    "chapter_label": "chapter and part labels renumbered in sequence",
    "chapter_labels": "chapter and part labels renumbered in sequence",
    "galley_read": "the practitioner's own reading",
    "galley_settle": "the finished-text reread (settled residuals)",
    "imported_edit": "imported corrections (bespoke sweeps, fleet reads)",
    "curated_fix": "curated corrections",
    "languagetool": "LanguageTool punctuation and spacing",
    "repair": "broken sentences repaired as a unit",
    "recurrence": "the same fix at its other sites",
    "consistency": "spelling made consistent across the book",
}


def family_label(error_type: str) -> str:
    key = str(error_type or "").strip()
    if key in FAMILY_LABELS:
        return FAMILY_LABELS[key]
    if key.startswith("sweep_") and key in FAMILY_LABELS:
        return FAMILY_LABELS[key]
    return key.replace("_", " ") or "(untyped)"


def families(rows: Iterable[Mapping[str, Any]]) -> list[tuple[str, int]]:
    counts = Counter(str(r.get("error_type") or "") for r in rows)
    return counts.most_common()


#: Question texts that are about a name or a spelling the author must settle
#: — the style sheet's "pending" list is drawn from these.
_NAME_QUESTION_RE = re.compile(
    r"\b(spell(?:ed|ing|ings)?|surname|name|brand|product|title|caption)\b",
    re.IGNORECASE)


def _question_text(row: Mapping[str, Any]) -> str:
    return " ".join(str(row.get("explanation") or row.get("note") or "").split())


def grouped_questions(rows: Sequence[Mapping[str, Any]]
                      ) -> list[tuple[str, str, list[str], list[str]]]:
    """Distinct author questions: ``(question, quoted span, para ids,
    finding ids)``, one per distinct (question, span) — a family collapsed to
    one comment shows its sites, never one bullet per site."""
    groups: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        q = _question_text(r)
        span = " ".join(str(r.get("original_text") or "").split())
        key = (q, span if len(span) <= 80 else span[:80])
        if key not in groups:
            groups[key] = ([], [])
            order.append(key)
        pids, fids = groups[key]
        pid = str(r.get("para_id") or "")
        fid = str(r.get("finding_id") or "")
        if pid and pid not in pids:
            pids.append(pid)
        if fid and fid not in fids:
            fids.append(fid)
    return [(q, span, groups[(q, span)][0], groups[(q, span)][1])
            for q, span in order]




def _section_what_ran(cf: CaseFile) -> list[str]:
    out = ["## What ran and what it cost", ""]
    if not cf.waves:
        out.append("_No waves were recorded._")
        out.append("")
        return out
    for wave in sorted(cf.waves, key=lambda w: w.index):
        out.append(f"### Wave {wave.index}")
        span = " → ".join(x for x in (wave.started_at, wave.ended_at) if x)
        if span:
            out.append(f"- Timing: {span}")
        out.append(f"- Spend: {_money(wave.spend_usd)}")
        out.append(f"- Findings added: {wave.findings_added}")
        action_lines = _wave_action_lines(wave)
        if action_lines:
            out.append("- Dispatched:")
            out.extend(action_lines)
        else:
            out.append("- Dispatched: (no actions recorded)")
        out.append("")
    return out


def _section_spend(cf: CaseFile) -> list[str]:
    out = ["## Spend curve", ""]
    out.append("| Wave | Spend | Cumulative |")
    out.append("| --- | --- | --- |")
    cumulative = 0.0
    for wave in sorted(cf.waves, key=lambda w: w.index):
        cumulative += wave.spend_usd
        out.append(f"| {wave.index} | {_money(wave.spend_usd)} | {_money(cumulative)} |")
    out.append("")
    out.append(f"**Total spend: {_money(cf.budget.spent_usd)}**")
    if cf.budget.charges:
        out.append("")
        out.append("Ledger charges:")
        for charge in cf.budget.charges:
            out.append(
                f"- wave {charge.wave}: {charge.label} — {_money(charge.cost_usd)}"
            )
    out.append("")
    return out


def _section_coverage(cf: CaseFile) -> list[str]:
    out = ["## Coverage holes", ""]
    notes = _coverage_notes(cf)
    if not notes:
        out.append("Coverage was clean — no gaps were flagged by any adapter.")
    else:
        out.append(
            "The following gaps were flagged during the run; a human should "
            "weigh whether they need a targeted pass:"
        )
        out.append("")
        for note in notes:
            out.append(f"- {note}")
    out.append("")
    return out


def _section_queries(cf: CaseFile, heading: str = "## Open queries") -> list[str]:
    """Every unresolved query — from a query verdict or a query-routed finding.

    Nothing is deduplicated away that would hide a distinct query: verdicts are
    keyed by finding id, and a finding whose id never received a query verdict
    but whose own confidence marks it a query is surfaced too.
    """

    out = [heading, ""]
    finding_by_id = {f.id: f for f in cf.findings}

    # Query and panel-reject verdicts both route to the margin. Duplicate
    # re-finds were merged and are not author questions.
    query_verdicts = [
        v for v in cf.verdicts
        if v.ruling in ("query", "reject") and not _is_duplicate(v)
    ]
    verdict_ids = {v.finding_id for v in query_verdicts}

    if not query_verdicts:
        out.append("No open queries — every finding was adjudicated to a decision.")
        out.append("")
        return out

    out.append(
        "These need an editorial decision before delivery. Each is listed so "
        "none is silently dropped:"
    )
    out.append("")
    for verdict in query_verdicts:
        finding = finding_by_id.get(verdict.finding_id)
        head = f"- **{verdict.finding_id}**"
        if finding is not None:
            head += f" ({finding.error_type})"
            if finding.find:
                head += f": `{finding.find}` → `{finding.replace}`"
        reason = verdict.reason or (finding.note if finding else "") or "(no reason recorded)"
        out.append(f"{head} — {reason}")
    out.append("")

    # Findings whose id carries no query verdict at all but that name themselves
    # a query in their note/confidence still deserve a line, not silence.
    orphans = [
        f
        for f in cf.findings
        if f.id not in verdict_ids
        and (f.confidence == "query" or f.error_type == "query")
    ]
    if orphans:
        out.append("Additionally routed to query with no separate verdict:")
        for finding in orphans:
            note = finding.note or "(no note)"
            out.append(f"- **{finding.id}** ({finding.error_type}) — {note}")
        out.append("")
    return out


def _section_confidence(
    cf: CaseFile, ms: Manuscript | None, recall: Any
) -> list[str]:
    out = ["## Confidence", ""]
    rate = _recall_rate(recall)
    gauge_book = _recall_book(recall)
    if rate is not None and gauge_book and cf.book and gauge_book != cf.book:
        # A gauge measured on a different manuscript says nothing about this
        # one; presenting it as this book's recall would be a false figure.
        rate = None
    if rate is not None:
        gauge = (
            "Against our seeded gauge"
            if gauge_book and gauge_book == cf.book
            else "Against our seeded gauge (a cross-book gauge — measured on a "
            "manuscript other than this one, not on this run)"
        )
        out.append(
            f"{gauge}, estimated recall is **{_pct(rate)}**. "
            "Read this honestly: a seeded gauge only measures the error classes "
            "we know to seed, so it cannot see blind spots outside our own "
            "taxonomy. The true miss rate is at least this high."
        )
    else:
        out.append(
            "No recall estimate was supplied for this run. Absence of a figure "
            "is not a clean bill of health — a seeded gauge can only see error "
            "classes inside our own taxonomy."
        )
    out.append("")

    if ms is not None and ms.chapters:
        out.append("Per-chapter finding counts:")
        out.append("")
        counts: dict[int, int] = {}
        para_to_chapter: dict[str, int] = {}
        for chapter in ms.chapters:
            for pid in chapter.para_ids:
                para_to_chapter[pid] = chapter.index
        for finding in cf.findings:
            idx = para_to_chapter.get(finding.span.para_id)
            if idx is not None:
                counts[idx] = counts.get(idx, 0) + 1
        for chapter in ms.chapters:
            out.append(
                f"- {chapter.title or f'Chapter {chapter.index}'}: "
                f"{counts.get(chapter.index, 0)} finding(s)"
            )
        out.append("")
    return out




def _summary_line(src: JournalSources, cf: CaseFile) -> str:
    edits = len(applied_rows(src))
    comments = comment_count(src)
    parts = [f"**{_n(edits)} tracked correction(s)**"]
    if comments is not None:
        parts.append(f"**{_n(comments)} margin comment(s)**")
    text = "The proof contains " + " and ".join(parts) + "."
    oc = src.outcome if isinstance(src.outcome, dict) else {}
    if oc.get("outcome") == "done":
        text += (" Every finding the run raised ended as a tracked edit, a "
                 "recorded drop, or a question below; nothing is pending.")
    elif oc.get("outcome") == "needs_human":
        text += (" **This book needs a human proofreader**: "
                 f"{_clip(oc.get('reason'), 400)}")
    return text


def _section_choices(src: JournalSources, cf: CaseFile) -> list[str]:
    out = ["## Choices and reasons", ""]
    prof = src.profile if isinstance(src.profile, dict) else {}
    appr = src.approval if isinstance(src.approval, dict) else {}
    scope = []
    if prof.get("genre"):
        scope.append(f"read as {str(prof['genre']).replace('_', ' ')}")
    if prof.get("variant"):
        scope.append(f"{str(prof['variant']).upper()} English")
    if appr.get("mechanical_only"):
        scope.append("mechanical proofreading only — spelling, grammar, "
                     "punctuation, and consistency, never wording for its "
                     "own sake")
    if scope:
        out.append("The book was " + "; ".join(scope) + ". Its voice — "
                   "conversational phrasing, deliberate fragments, rhetorical "
                   "repetition, remembered speech — is the author's and was "
                   "left alone.")
        out.append("")

    fams = families(applied_rows(src))
    if fams:
        out.append("Corrections by kind (every one a rejectable tracked "
                   "change):")
        out.append("")
        out.append("| Kind | Count |")
        out.append("| --- | --- |")
        for key, n in fams[:14]:
            out.append(f"| {family_label(key)} | {_n(n)} |")
        rest = sum(n for _k, n in fams[14:])
        if rest:
            out.append(f"| other | {_n(rest)} |")
        out.append("")

    st = src.settlement if isinstance(src.settlement, dict) else {}
    counts = st.get("counts") if isinstance(st.get("counts"), dict) else {}
    if counts:
        raised = len(st.get("residuals_seen") or []) or sum(counts.values())
        bits = [f"{_n(counts.get(k, 0))} {label}" for k, label in (
            ("add", "fixed as new corrections"),
            ("absorb", "folded into an existing correction"),
            ("revise", "corrected an earlier correction"),
            ("drop", "dropped with the reason recorded"),
            ("query", "sent to you as questions"))
            if counts.get(k)]
        out.append(f"The reread of the finished text raised {_n(raised)} "
                   f"item(s) over {st.get('rounds', '?')} round(s): "
                   + ", ".join(bits) + ". Every drop carries its reason in "
                   "the decision log.")
        out.append("")

    if cf.style_sheet:
        out.append("Rulings recorded during the run:")
        out.append("")
        for r in cf.style_sheet:
            out.append(f"- **{r.subject}** — {r.decision}"
                       + (f" ({r.rationale})" if r.rationale else ""))
        out.append("")

    labels = [r for r in applied_rows(src)
              if str(r.get("error_type") or "").startswith("chapter_label")]
    if labels:
        out.append(f"Chapter and part labels were renumbered in sequence "
                   f"({_n(len(labels))} heading(s)), as tracked edits — a "
                   "label out of order is mechanics, not a question.")
        out.append("")
    if len(out) == 2:
        out.append("_No run evidence beyond the findings was recorded._")
        out.append("")
    return out


def _section_decisions(src: JournalSources, cf: CaseFile) -> list[str]:
    out = ["## Decisions still needed", ""]
    rows = query_rows(src)
    if not rows:
        out.append("None — every finding was decided. Nothing is waiting on "
                   "you beyond reviewing the tracked changes.")
        out.append("")
        return out
    groups = grouped_questions(rows)
    out.append(f"Please review the {_n(len(rows))} comment(s) in Word. Each "
               f"asks something only you can answer — a fact, an intent, an "
               f"identity — and none changes the text until you decide. The "
               f"distinct questions, {_n(len(groups))} in all:")
    out.append("")
    for q, span, pids, fids in groups:
        where = ", ".join(pids[:4]) + ("…" if len(pids) > 4 else "")
        site = f" ({_n(len(pids))} sites)" if len(pids) > 1 else ""
        head = f"- **{where}**{site}"
        if span:
            head += f" `{_clip(span, 70)}`"
        out.append(f"{head} — {_clip(q, 400)}"
                   + (f" [{', '.join(fids[:3])}]" if fids else ""))
    out.append("")
    return out


def _skipped_in_prose(checks: Sequence[tuple[str, str, str]]) -> str:
    skipped = [(name, detail) for status, name, detail in checks
               if status.lower() == "skip"]
    if not skipped:
        return ""
    names = "; ".join(f"{name} ({_clip(detail, 90)})" if detail else name
                      for name, detail in skipped)
    return (f"The certificate did not run {len(skipped)} check(s), and says "
            f"so rather than passing them silently: {names}.")


def _section_verification(src: JournalSources, cf: CaseFile,
                          ms: Manuscript | None, recall: Any) -> list[str]:
    out = ["## Verification and limits", ""]
    cv = src.change_verify if isinstance(src.change_verify, dict) else {}
    fw = src.finished_walk if isinstance(src.finished_walk, dict) else {}
    st = src.settlement if isinstance(src.settlement, dict) else {}
    lines: list[str] = []
    if cv.get("ran"):
        lines.append(f"Every applied correction ({_n(cv.get('applied_edits') or 0)}) "
                     f"was re-read in its finished sentence; "
                     f"{_n(len(cv.get('problems') or []))} were flagged and "
                     f"every flag was settled.")
    if fw.get("ran"):
        unread = fw.get("unread_paragraphs") or []
        lines.append(f"The finished text was proofread again as you will read "
                     f"it: {_n(fw.get('paragraphs') or 0)} paragraph(s) read, "
                     f"{_n(len(fw.get('residuals') or []))} residual(s) "
                     f"raised, all settled"
                     + (f"; {_n(len(unread))} paragraph(s) could not be read "
                        f"and are named in the decision log" if unread
                        else "") + ".")
    if st.get("rounds"):
        conv = st.get("convergence") if isinstance(st.get("convergence"),
                                                   dict) else {}
        tail = ""
        if conv.get("last_new_items") is not None:
            tail = (f" The last round raised {_n(conv['last_new_items'])} "
                    f"new item(s)"
                    + (", which is quiet." if conv.get("quiet")
                       else " — still noisy, which is why this book is "
                            "flagged for a human proofreader."))
        lines.append(f"Settlement took {st['rounds']} round(s)." + tail)
    if lines:
        out.extend(lines)
        out.append("")
    checks = certificate_checks(src)
    if checks:
        failed = [n for s_, n, _d in checks if s_.lower() == "fail"]
        passed = sum(1 for s_, _n_, _d in checks if s_.lower() == "pass")
        out.append(f"The structural certificate ran {len(checks)} check(s): "
                   f"{passed} passed"
                   + (f", **{len(failed)} failed** ({', '.join(failed)})"
                      if failed else ", none failed") + ". "
                   + _skipped_in_prose(checks))
        out.append("")
    else:
        out.append("No structural certificate was recorded for this run.")
        out.append("")
    notes = _coverage_notes(cf)
    if notes:
        out.append("Coverage gaps flagged during the run:")
        for note in notes:
            out.append(f"- {note}")
        out.append("")
    out.append("These reads are dependent — the same lanes that made the "
               "corrections re-read the result — so they give no defensible "
               "statistical estimate of the errors that remain. Treat the "
               "proof as thorough, not as proven clean.")
    rate = _recall_rate(recall)
    if rate is not None and (not _recall_book(recall)
                             or _recall_book(recall) == cf.book):
        out.append(f" Against our seeded gauge the estimated recall is "
                   f"{_pct(rate)}; the gauge sees only the error classes we "
                   f"know to seed, so the true miss rate is at least that "
                   f"high.")
    out.append("")
    return out


def _section_preparation(src: JournalSources) -> list[str]:
    env = src.envelope if isinstance(src.envelope, dict) else {}
    norm = env.get("normalization") if isinstance(env.get("normalization"),
                                                  dict) else {}
    if not norm or not norm.get("ran"):
        return []
    out = ["## Preparation disclosure", ""]
    out.append(f"Before any tracked edit, the engine normalized "
               f"{_n(norm.get('spaces') or 0)} space run(s) and curled "
               f"{_n(norm.get('quotes') or 0)} quotation mark(s) across "
               f"{_n(norm.get('paragraphs') or 0)} paragraph(s). Those "
               "preparation changes are not tracked: rejecting every revision "
               "returns that normalized text, not every original spacing "
               "character. The supplied original is unchanged and remains "
               "the comparison copy.")
    out.append("")
    return out


def _section_closing(src: JournalSources) -> list[str]:
    oc = src.outcome if isinstance(src.outcome, dict) else {}
    if not oc:
        return []
    verdict = oc.get("outcome")
    if verdict == "done":
        text = ("**Outcome: done.** This is a proofreading hand-off for your "
                "review; the comments above and the production notes in the "
                "verification report are what remain before publication.")
    elif verdict == "needs_human":
        text = (f"**Outcome: needs a human proofreader.** {_clip(oc.get('reason'), 500)}")
    else:
        text = f"**Outcome: {verdict}.** {_clip(oc.get('reason'), 500)}"
    return [text, ""]




def render_letter(
    cf: CaseFile,
    out_dir: str | Path,
    *,
    ms: Manuscript | None = None,
    recall: Any = None,
    evidence: JournalSources | None = None,
) -> Path:
    """Render the editorial cover letter to ``out_dir/letter.md``; return the path.

    With ``evidence`` (see :func:`run_evidence`) the letter is the
    proofreader's letter: summary, what ran and cost, choices and reasons,
    decisions still needed, verification and limits, preparation disclosure,
    outcome. Without it the letter is the case file's own ledger."""

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    title = cf.book or "the manuscript"
    if evidence is not None:
        lines.append(f"# Proofreading letter — {title}")
        lines.append("")
        lines.append(_summary_line(evidence, cf))
        lines.append("")
        lines.extend(_section_what_ran(cf))
        lines.extend(_section_spend(cf))
        lines.extend(_section_choices(evidence, cf))
        lines.extend(_section_decisions(evidence, cf))
        # The case file's own query ledger stays — by finding id — so nothing a
        # verdict routed to the margin can hide behind the grouped list.
        lines.extend(_section_queries(cf, heading="### Query ledger (by finding id)"))
        lines.extend(_section_verification(evidence, cf, ms, recall))
        lines.extend(_section_preparation(evidence))
        lines.extend(_section_closing(evidence))
    else:
        lines.append(f"# Editorial letter — {title}")
        lines.append("")
        lines.append(
            "This letter reports what our proofreading pass did: which waves ran, "
            "what they cost, where coverage may be thin, and every question left "
            "open for you. It is a record of the run, not a set of new decisions."
        )
        lines.append("")
        lines.extend(_section_what_ran(cf))
        lines.extend(_section_spend(cf))
        lines.extend(_section_coverage(cf))
        lines.extend(_section_queries(cf))
        lines.extend(_section_confidence(cf, ms, recall))

    out_file = out_path / "letter.md"
    out_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_file


#: The house conventions every proofread applies. Listed on every derived
#: style sheet so the author can see what the book was proofed to, with the
#: count of sites the run touched where a family matches.
HOUSE_CONVENTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Lists", "Serial comma in ordinary comma-separated lists; deliberate "
              "repeated conjunctions preserved.", ("serial_comma",)),
    ("Dialogue", "U.S. quotation punctuation: periods and commas inside "
                 "closing marks; one terminal mark; speech tags distinguished "
                 "from action beats.",
     ("sweep_quote_punctuation", "sweep_dialogue_tag",
      "sweep_stacked_punctuation", "sweep_nested_quote")),
    ("Dashes and ellipses", "Unspaced em dashes; the house nonbreaking-space "
                            "ellipsis. Compound hyphens are never turned into "
                            "dashes.", ("sweep_dash", "sweep_ellipsis")),
    ("Clock times", "`h:mm AM` / `h:mm PM` — the stated time is never "
                    "changed.", ("sweep_time_of_day",)),
    ("Numbers", "Whole numbers spelled out through one hundred; numerals "
                "above; percentages as `40 percent`. A number that "
                "contradicts another is a question, never an edit.",
     ("number_style", "numbers")),
    ("Spelling", "Merriam-Webster, U.S. forms (toward, among, gray, "
                 "judgment); closed compounds the dictionary carries are "
                 "closed.", ("spelling", "spellscan", "typo", "imported_edit",
                             "hyphenation")),
    ("Comma splices", "Two independent clauses joined by a comma take a "
                      "semicolon, a period, or a conjunction; never left.",
     ("comma_splice",)),
    ("Compound sentences", "A comma before a conjunction only when both "
                           "sides are independent clauses — a compound "
                           "predicate takes none.",
     ("compound_sentence_comma",)),
    ("Chapter labels", "Numbered consecutively in the dominant style; a gap "
                       "or a mangled label is renumbered, not queried.",
     ("chapter_label", "chapter_labels")),
    ("Layout boundaries", "Sentences are read across photographs and manual "
                          "breaks; no punctuation is added merely because a "
                          "paragraph ends at an image.",
     ("sweep_terminal_period",)),
)


def _derived_style_sections(src: JournalSources, cf: CaseFile) -> list[str]:
    out: list[str] = []
    prof = src.profile if isinstance(src.profile, dict) else {}
    appr = src.approval if isinstance(src.approval, dict) else {}
    counts = Counter(str(r.get("error_type") or "") for r in applied_rows(src))

    out.append("## Voice and scope")
    out.append("")
    scope = "U.S. English mechanical proofreading"
    if prof.get("variant"):
        scope = f"{str(prof['variant']).upper()} English mechanical proofreading"
    if prof.get("genre"):
        scope += f" of a {str(prof['genre']).replace('_', ' ')}"
    if appr.get("mechanical_only"):
        scope += " (mechanical scope only — no copy-editing lane ran)"
    out.append(scope + ". The author's voice, remembered speech, deliberate "
               "fragments, repetition, coinages, and transliterations are "
               "preserved; a clear expression is never rewritten to be more "
               "formal or concise.")
    out.append("")

    out.append("## Mechanical conventions")
    out.append("")
    out.append("| Feature | Treatment | Sites this run |")
    out.append("| --- | --- | --- |")
    for feature, treatment, keys in HOUSE_CONVENTIONS:
        n = sum(counts.get(k, 0) for k in keys)
        out.append(f"| {feature} | {treatment} | {_n(n) if n else '—'} |")
    out.append("")

    names = prof.get("proper_nouns") if isinstance(prof.get("proper_nouns"),
                                                   list) else []
    protect = []
    for entry in names:
        if isinstance(entry, dict) and entry.get("name"):
            protect.append(str(entry["name"]))
        elif isinstance(entry, str):
            protect.append(entry)
    # Names, not vocabulary: the profile's harvest is capitalized tokens, so
    # a lowercase medical term or a two-letter abbreviation is not a name.
    protect = [n_ for n_ in protect
               if not n_.endswith(("'s", "’s")) and len(n_) > 2
               and n_[0].isupper() and not n_.isupper()]
    if protect:
        out.append("## Names and terms")
        out.append("")
        out.append("Preserved as the book establishes them: "
                   + ", ".join(protect[:40])
                   + ("…" if len(protect) > 40 else "") + ".")
        out.append("")

    pending = [r for r in query_rows(src) if _NAME_QUESTION_RE.search(
        _question_text(r))]
    if pending:
        out.append("## Unresolved, pending the author")
        out.append("")
        out.append("These are not house spellings yet — each is a comment in "
                   "the proof:")
        out.append("")
        for q, span, pids, _fids in grouped_questions(pending):
            out.append(f"- {_clip(span, 60) or pids[0]} ({', '.join(pids[:3])}) "
                       f"— {_clip(q, 220)}")
        out.append("")

    zones = prof.get("intent_zones") if isinstance(prof.get("intent_zones"),
                                                   list) else []
    if zones:
        out.append("## Protected passages")
        out.append("")
        for z in zones[:20]:
            if isinstance(z, dict):
                label = z.get("name") or z.get("id") or z.get("label") or "zone"
                out.append(f"- {_clip(label, 80)}"
                           + (f" — {_clip(z.get('reason') or z.get('note'), 120)}"
                              if (z.get("reason") or z.get("note")) else ""))
            else:
                out.append(f"- {_clip(z, 80)}")
        out.append("")

    out.append("## Review conventions")
    out.append("")
    budget = appr.get("comment_budget") or prof.get("comment_budget")
    delivered = comment_count(src)
    line = ("Every text correction is a rejectable tracked change. Comments "
            "ask; they never alter the text.")
    if budget:
        line += (f" The comment ceiling for this book was {_n(int(budget))}"
                 + (f"; the proof carries {_n(delivered)}." if delivered is not None
                    else "."))
    out.append(line)
    out.append("")
    return out


def render_style_sheet(cf: CaseFile, out_dir: str | Path, *,
                       evidence: JournalSources | None = None) -> Path:
    """Render the per-book style sheet to ``out_dir/style-sheet.md``; return the path.

    With ``evidence`` the sheet is derived from the run — conventions with
    site counts, names preserved, spellings pending the author — and the
    recorded rulings (``cf.style_sheet``) are appended. Without it, only the
    rulings."""

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    title = cf.book or "the manuscript"
    lines.append(f"# Style sheet — {title}")
    lines.append("")

    if evidence is not None:
        lines.extend(_derived_style_sections(evidence, cf))

    if not cf.style_sheet:
        if evidence is None:
            lines.append(
                "_No rulings were recorded for this book yet._ The first ruling on "
                "each variant binds the book; once decisions are made they will be "
                "logged here with their rationale and cited authority."
            )
            lines.append("")
    else:
        lines.append("## Rulings recorded" if evidence is not None else "")
        if evidence is not None:
            lines.append("")
        lines.append(
            "Each entry is a binding decision for this book, with the authority "
            "cited. The first ruling on a variant binds it."
        )
        lines.append("")
        lines.append("| Subject | Decision | Rationale | Source | Wave |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ruling in cf.style_sheet:
            lines.append(
                f"| {ruling.subject} | {ruling.decision} | "
                f"{ruling.rationale or '—'} | {ruling.source or '—'} | "
                f"{ruling.wave} |"
            )
        lines.append("")

    out_file = out_path / "style-sheet.md"
    out_file.write_text("\n".join(l for l in lines).rstrip() + "\n",
                        encoding="utf-8")
    return out_file


def render_verification_report(evidence: JournalSources, out_dir: str | Path,
                               *, cf: CaseFile | None = None) -> Path:
    """Render ``out_dir/verification.md`` from the run's evidence; return the
    path. Every figure is read from an artifact; a missing artifact is named,
    never estimated."""
    src = evidence
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    book = (cf.book if cf is not None and cf.book else "") or \
        (src.workspace.name if src.workspace is not None else src.run_dir.name)

    lines: list[str] = [f"# Verification report — {book}", ""]
    edits = len(applied_rows(src))
    comments = comment_count(src)
    lines.append(f"The delivered proof has **{_n(edits)} tracked correction(s)**"
                 + (f" and **{_n(comments)} comment(s)**" if comments is not None
                    else "") + ". Every figure below is read from the run's "
                 "own artifacts; a check that could not run is named as "
                 "skipped, never counted as passed.")
    lines.append("")

    checks = certificate_checks(src)
    lines.append("## Structural certificate")
    lines.append("")
    if checks:
        lines.append("| Check | Result | Detail |")
        lines.append("| --- | --- | --- |")
        for status, name, detail in checks:
            lines.append(f"| {name} | {status.upper()} | {_clip(detail, 140)} |")
        lines.append("")
        prose = _skipped_in_prose(checks)
        if prose:
            lines.append(prose)
            lines.append("")
        failed = [n for s_, n, _d in checks if s_.lower() == "fail"]
        lines.append("**Result: " + ("FAILED — " + ", ".join(failed)
                                     if failed else "passed, zero failing "
                                     "checks") + ".**")
    else:
        lines.append("No certificate was recorded (`docproof galley certify` "
                     "did not write runs/certify.txt). The checks below are "
                     "the run's own artifacts only.")
    lines.append("")

    cv = src.change_verify if isinstance(src.change_verify, dict) else {}
    fw = src.finished_walk if isinstance(src.finished_walk, dict) else {}
    st = src.settlement if isinstance(src.settlement, dict) else {}
    lines.append("## Reading and settlement")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("| --- | --- |")
    if cv.get("ran"):
        lines.append(f"| Applied corrections re-read in context | "
                     f"{_n(cv.get('applied_edits') or 0)} read; "
                     f"{_n(len(cv.get('problems') or []))} flagged; "
                     f"{'all settled' if cv.get('settled') else 'NOT settled'} |")
    else:
        lines.append("| Applied corrections re-read in context | not run |")
    if fw.get("ran"):
        unread = fw.get("unread_paragraphs") or []
        lines.append(f"| Finished text proofread again | "
                     f"{_n(fw.get('paragraphs') or 0)} paragraph(s) read; "
                     f"{_n(len(fw.get('residuals') or []))} residual(s); "
                     f"{_n(len(unread))} unread |")
    else:
        lines.append("| Finished text proofread again | not run |")
    if st:
        counts = st.get("counts") if isinstance(st.get("counts"), dict) else {}
        lines.append(f"| Residual settlement | {st.get('rounds', '?')} round(s); "
                     + ", ".join(f"{k} {_n(v)}" for k, v in sorted(counts.items()))
                     + f"; open {_n(len(st.get('open') or []))} |")
    else:
        lines.append("| Residual settlement | no settlement.json |")
    env = src.envelope if isinstance(src.envelope, dict) else {}
    stats = env.get("stats") if isinstance(env.get("stats"), dict) else {}
    if stats:
        lines.append("| Finding states | "
                     + ", ".join(f"{k} {_n(v)}" for k, v in sorted(stats.items())
                                 if isinstance(v, int)) + " |")
    skipped = env.get("skipped_paragraphs")
    if isinstance(skipped, (list, dict)) and skipped:
        lines.append(f"| Paragraphs the engine does not review | "
                     f"{_n(len(skipped))} (front matter, contents, captions "
                     f"by style) |")
    lines.append("")

    docx = delivered_docx(src)
    lines.append("## Delivered file")
    lines.append("")
    if docx is not None:
        from galley.manifest import sha256_file
        lines.append(f"- File: `{docx.name}`")
        lines.append(f"- SHA-256: `{sha256_file(docx)}`")
        media = _docx_media(docx)
        source = _source_path(src)
        src_media = _docx_media(source) if source is not None else None
        if media is not None:
            if src_media is not None:
                same = media == src_media
                lines.append(f"- Embedded media: {_n(media)} file(s) delivered, "
                             f"{_n(src_media)} in the supplied original — "
                             + ("preserved in number" if same
                                else "**COUNT DIFFERS — check the images**"))
            else:
                lines.append(f"- Embedded media: {_n(media)} file(s) (the "
                             f"original was not available to compare)")
        anchors = _comment_anchors(docx)
        if anchors is not None:
            starts, ends = anchors
            lines.append(f"- Comment anchors: {_n(starts)} range start(s), "
                         f"{_n(ends)} range end(s) — "
                         + ("paired" if starts == ends else "**UNPAIRED**"))
        if source is not None:
            lines.append(f"- Supplied original: `{source.name}` (unchanged; "
                         f"SHA-256 `{sha256_file(source)}`)")
    else:
        lines.append("No tracked-changes .docx was found in the run directory "
                     "to fingerprint.")
    lines.append("")

    norm = env.get("normalization") if isinstance(env.get("normalization"),
                                                  dict) else {}
    if norm.get("ran"):
        lines.append("## Untracked preparation")
        lines.append("")
        lines.append(f"{_n(norm.get('spaces') or 0)} space normalization(s) and "
                     f"{_n(norm.get('quotes') or 0)} quote curl(s) across "
                     f"{_n(norm.get('paragraphs') or 0)} paragraph(s) were "
                     "applied before tracking began. Rejecting every revision "
                     "restores that normalized text, not every original "
                     "spacing character; the supplied original remains "
                     "available for comparison.")
        lines.append("")

    lines.append("## Production notes and limits")
    lines.append("")
    fields = [n for s_, n, d in checks if n.startswith("unresolved fields")]
    field_detail = next((d for s_, n, d in checks
                         if n.startswith("unresolved fields")), "")
    if field_detail:
        lines.append(f"- {_clip(field_detail, 300)}")
    lines.append("- Tracked revisions change pagination; the contents page "
                 "and page references need their production check after "
                 "the revisions are accepted.")
    lines.append("- The reads above are dependent — the lanes that made the "
                 "corrections re-read the result — so there is no "
                 "defensible statistical estimate of the errors remaining "
                 "book-wide.")
    oc = src.outcome if isinstance(src.outcome, dict) else {}
    if oc.get("outcome"):
        lines.append(f"- Outcome recorded: **{oc['outcome']}** — "
                     f"{_clip(oc.get('reason'), 300)}")
    lines.append("")

    out_file = out_path / "verification.md"
    out_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_file


def render_all(cf: CaseFile, out_dir: str | Path, **kw: Any) -> tuple[Path, Path]:
    """Write the letter and the style sheet; return ``(letter_path, style_sheet_path)``.

    Keyword arguments (``ms``, ``recall``, ``evidence``) are forwarded to
    :func:`render_letter`; ``evidence`` also to :func:`render_style_sheet`.
    The verification report is rendered separately by
    :func:`render_verification_report`, since it needs run evidence.
    """

    letter = render_letter(cf, out_dir, **kw)
    style = render_style_sheet(cf, out_dir, evidence=kw.get("evidence"))
    return letter, style


__all__ = [
    "FAMILY_LABELS",
    "HOUSE_CONVENTIONS",
    "applied_rows",
    "certificate_checks",
    "comment_count",
    "family_label",
    "grouped_questions",
    "query_rows",
    "render_all",
    "render_letter",
    "render_style_sheet",
    "render_verification_report",
    "run_evidence",
]
