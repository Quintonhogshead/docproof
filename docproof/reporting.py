from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import DocumentModel, Finding, Usage, index_paragraphs
from .providers import cost_of_usage, estimate_cost, lookup

log = logging.getLogger("docproof.reporting")


def _tally(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return dict(sorted(counts.items()))


def _tally_types(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.error_type] = counts.get(f.error_type, 0) + 1
    return dict(sorted(counts.items()))


def scripted_check_rows(sweeps, findings: list[Finding],
                        applied_ids) -> list[dict]:
    """The scripted-check report: for each sweep, what it flagged, how much of
    that actually reached the document, and what its patterns still match.

    `flagged` and `applied` can differ legitimately — a sweep edit that
    overlaps an earlier one is rejected — so both are reported rather than one
    standing in for the other."""
    applied = set(applied_ids)
    rows = []
    for report in sweeps or ():
        mine = [f for f in findings if f.error_type == report.key]
        rows.append({
            "key": report.key,
            "name": report.name,
            "flagged": report.flagged,
            "applied": sum(1 for f in mine if f.finding_id in applied),
            "remaining": report.remaining,
        })
    return rows


def write_findings_json(path: Path, *, doc: DocumentModel,
                        findings: list[Finding], usage: Usage, cfg: Config,
                        applied_ids: tuple[str, ...],
                        batch: bool = False, sweeps=None, spell=None,
                        normalization=None, audit=None, consistency=None,
                        coverage=None, smoothing=None, chapter_continuity=None,
                        queried_ids: tuple[str, ...] = (),
                        unplaced_ids: tuple[str, ...] = ()) -> None:
    applied = set(applied_ids)
    queried = set(queried_ids)
    unplaced = set(unplaced_ids)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": doc.source_path,
        # `error_types` is the CONFIGURED registry keys and nothing else. It
        # can never name a pass that labels its findings with a free-form
        # string — the sweeps, LanguageTool, Sapling, the low-confidence valve,
        # continuity, consistency — so a reader must not take absence from this
        # list as "that pass did not run". See docproof/labels.py, which owns
        # that question and answers it from positive evidence instead.
        "config": {"model": cfg.api.model,
                   "error_types": list(cfg.error_type_keys),
                   "error_type_passes": [list(g) for g in cfg.error_type_groups],
                   "sweeps": list(cfg.sweeps),
                   "min_confidence": cfg.min_confidence,
                   "revision_author": cfg.revision_author},
        "scripted_checks": scripted_check_rows(sweeps, findings, applied_ids),
        "normalization": ({"ran": normalization.ran,
                           "quotes": normalization.quotes,
                           "spaces": normalization.spaces,
                           "paragraphs": normalization.paragraphs,
                           "left_straight": normalization.ambiguous}
                          if normalization is not None else None),
        "audit": ({"ran": audit.ran, "passed": audit.passed,
                   "checked": audit.checked,
                   "mismatches": [m.para_id for m in audit.mismatches],
                   "missing": list(audit.missing)}
                  if audit is not None else None),
        "consistency": ({"ran": consistency.ran,
                         "terms": [{"key": t.key, "dominant": t.dominant,
                                    "forms": dict(t.counts),
                                    "outliers": len(t.outliers)}
                                   for t in consistency.terms],
                         "names": [{"key": n.key, "dominant": n.dominant,
                                    "forms": dict(n.counts),
                                    "outliers": len(n.outliers),
                                    "enforced": n.enforce}
                                   for n in consistency.names]}
                        if consistency is not None else None),
        "spell_scan": ({"available": spell.available, "tokens": spell.tokens,
                        "unique": spell.unique, "unknown": spell.unknown,
                        "lexicon": list(spell.lexicon),
                        "candidates": [c.word for c in spell.candidates],
                        "recurring": [c.word for c in spell.recurring]}
                       if spell is not None else None),
        "usage": dataclasses.asdict(usage),
        "batch": batch,
        "coverage": ({"total": coverage.total, "reviewed": coverage.reviewed,
                      "gaps": [{"pass": g.pass_label, "chunk_id": g.chunk_id,
                                "para_ids": list(g.para_ids)}
                               for g in coverage.gaps],
                      # Candidates sent to a batched pass that never came back
                      # with a verdict — see CoverageLedger.unruled.
                      "unruled_total": coverage.unruled_total,
                      "unruled": [{"pass": r.label, "asked": r.asked,
                                   "answered": r.answered, "lost": r.lost,
                                   "truncated_calls": r.truncated_calls}
                                  for r in coverage.unruled],
                      # Whole passes that fell open and produced nothing — see
                      # CoverageLedger.degraded.
                      "degraded": [{"pass": d.label, "reason": d.reason}
                                   for d in coverage.degraded]}
                     if coverage is not None else None),
        # What the smoothing pass did, for anything scoring it. `unjudged` is
        # the one that must not be inferred: a pass that proposed suggestions
        # and delivered none looks identical to a restrained one from the
        # outside, and on this pass silence is the DESIRED output almost
        # everywhere — which is exactly why silence must never be the
        # unexamined reading. The prompt fingerprints are here so two runs can
        # be told apart: a prompt change moves the output more than any knob.
        "smoothing": (dataclasses.asdict(smoothing)
                      if smoothing is not None else None),
        # What the chapter-continuity pass did, for the same reason and with the
        # same accounting identity — proposed == kept + withheld + below_floor +
        # refused + unjudged — plus the models and prompt fingerprints that say
        # the manuscript was actually read on a run whose ordinary output is
        # nothing.
        "chapter_continuity": (dataclasses.asdict(chapter_continuity)
                               if chapter_continuity is not None else None),
        "stats": _tally(findings),
        "stats_by_error_type": _tally_types(findings),
        "skipped_paragraphs": [{"para_id": pid, "reason": r}
                               for pid, r in doc.skipped],
        # `applied` says a tracked change was written. `queried` and `unplaced`
        # say the same for the OTHER channel, and neither is derivable from
        # `status`: a finding can be status "query" and still never reach the
        # reader, because both reassemblers require a truthy anchor, refuse a
        # paragraph outside the main story part, and can fail to attach the
        # comment itself. Without these, anything counting questions off this
        # file counts the ones that were GENERATED and calls them delivered.
        "findings": [
            {**dataclasses.asdict(f),
             "anchor": dataclasses.asdict(f.anchor) if f.anchor else None,
             "applied": f.finding_id in applied,
             "queried": f.finding_id in queried,
             "unplaced": f.finding_id in unplaced}
            for f in findings
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    log.info("Wrote %s", path)


def _gap_preview(gap, paras) -> str:
    """A human handle for an unreviewed section: the paragraph span and a
    snippet of where it starts, so an editor can find it in the manuscript."""
    ids = [pid for pid in gap.para_ids if pid in paras]
    if not ids:
        return gap.chunk_id
    first = paras[ids[0]]
    text = " ".join(first.text.split())
    snippet = text[:60] + ("…" if len(text) > 60 else "")
    span = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
    return f"{span}: “{snippet}”"


def _settings_section(cfg: Config, batch: bool) -> list[str]:
    """Everything this run was configured to do — every pass, on or off — so the
    report says not just what changed but what was even looked for. A reader who
    can't tell whether the spell scan or the model review ran is exactly how a
    continuity-only run got mistaken for a full proofread."""
    def on(flag: bool) -> str:
        return "on" if flag else "off"

    n_pass = len(cfg.error_type_groups)
    glossary = f"on (`{cfg.glossary.model}`)" if cfg.glossary.enabled else "off"
    sapling = "off"
    if cfg.sapling.enabled:
        sapling = "on" + (f" ({cfg.sapling.variety})" if cfg.sapling.variety
                          else "")
    passes = [
        ("Glossary read", glossary),
        ("Story sheet", on(cfg.storysheet.enabled)),
        ("Real-word typo adjudication", on(cfg.adjudicate.enabled)),
        ("Rewrite-and-compare", on(cfg.rewrite.enabled)),
        ("LanguageTool floor", on(cfg.languagetool.enabled)),
        ("Sapling grammar check", sapling),
        ("Consistency scan", on(cfg.consistency.enabled)),
        ("Spell scan", on(cfg.spellcheck.enabled)),
        ("Continuity read", on(cfg.continuity.enabled)),
        ("Chapter continuity", on(cfg.chapter_continuity.enabled)),
        ("Smoothing", on(cfg.smoothing.enabled)),
    ]
    L = ["## Settings used\n"]
    L.append(f"- **Reviewer:** `{cfg.api.model}`"
             + (f", effort {cfg.api.effort}" if cfg.api.effort else "")
             + f", confidence gate {cfg.min_confidence}")
    L.append(f"- **Timing:** "
             f"{'overnight (batch rates)' if batch else 'right now'}")
    if cfg.rounds.count and cfg.rounds.count > 1:
        L.append(f"- **Review rounds:** {cfg.rounds.count}")
    L.append("- **Model error-type passes:** "
             + (f"{n_pass} pass(es) over {len(cfg.error_type_keys)} error "
                f"type(s)" if n_pass else "none"))
    L.append("- **House-style sweeps:** "
             + (f"on ({len(cfg.sweeps)} rule(s))" if cfg.sweeps else "off"))
    L.append("- **Passes:** "
             + " · ".join(f"{name} {state}" for name, state in passes))
    L.append(f"- **Writes:** margin comments {on(cfg.comments)}, "
             f"change reasons {on(cfg.report_explanations)}, "
             f"reject-all audit {cfg.audit}")
    L.append("")
    return L


def write_summary_md(path: Path, *, doc: DocumentModel,
                     findings: list[Finding], usage: Usage, cfg: Config,
                     applied_ids: tuple[str, ...], batch: bool = False,
                     fmt=None, sweeps=None, spell=None, normalization=None,
                     audit=None, consistency=None, coverage=None,
                     judges=None, smoothing=None, chapter_continuity=None) -> None:
    paras = index_paragraphs(doc)
    applied = [f for f in findings if f.finding_id in set(applied_ids)]
    low = [f for f in findings if f.status == "skipped_low_confidence"]
    queries = [f for f in findings if f.status == "query"]
    # The "Queries" section is for genuine questions — an asking pass, a name
    # pair — which always reach the margin. A withheld edit (a judge's or the
    # verifier's, "Not applied: …") is a different animal: it has its own gate
    # section below, and by default it stays out of the document, so listing it
    # here as a margin question would double-count it and overstate what the
    # file carries.
    genuine_queries = [f for f in queries if not f.withheld]
    # Oversized findings are a real catch with too large a fix; they get their
    # own section (and a margin comment) rather than the terse rejected list.
    oversized = [f for f in findings if f.status == "rejected_oversized"]
    rejected = [f for f in findings if f.status.startswith("rejected")
                and f.status != "rejected_oversized"]
    # The below-gate and oversized findings reach the margin only when
    # not_applied_comments puts declined corrections there AND query_comments
    # still admits these two kinds. Off by default, they live here and in the
    # change log, not in the document — so only claim the comment when it exists.
    in_margin = (f", and each is a {fmt.comment_noun} in the reviewed "
                 f"file" if (cfg.query_comments and cfg.not_applied_comments)
                 else "")

    L: list[str] = []
    L.append(f"# docproof review — {Path(doc.source_path).name}\n")
    passes = cfg.error_type_groups
    L.append(f"*{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
             f"model `{cfg.api.model}` · {len(cfg.error_type_keys)} error "
             f"type(s) in {len(passes)} pass(es) · gate: {cfg.min_confidence}*\n")
    L.append("Passes: " + "; ".join(" + ".join(g) for g in passes) + "\n")

    # Lead with the sentence a writer actually wants: how much changed, and
    # what kind of mistake it mostly was.
    by_type = _tally_types(applied)
    headline = (f"**{len(applied)} change(s) applied** as tracked revisions "
                f"across {len({f.para_id for f in applied})} paragraph(s)")
    if by_type:
        top, count = max(by_type.items(), key=lambda kv: kv[1])
        headline += f", most often {top} ({count})"
    L.append(headline + f". Author of the revisions: *{cfg.revision_author}*.\n")

    stats = _tally(findings)
    L.append(f"Findings by status: " +
             ", ".join(f"{k} {v}" for k, v in stats.items()) +
             f". Paragraphs reviewed: {len(doc.paragraphs)}; "
             f"skipped: {len(doc.skipped)}.\n")

    L += _settings_section(cfg, batch)

    if coverage is not None:
        L.append("## Coverage\n")
        # Whole passes that fell open and produced nothing — the most severe hole,
        # because unlike a gap the pass did not run at all. A dead or unkeyed
        # judge/continuity/glossary model lands here, and a "done" run that
        # skipped a paid pass otherwise reads exactly like one that ran it clean.
        if coverage.degraded:
            L.append(f"**{len(coverage.degraded)} pass(es) did not run** and were "
                     f"skipped — their findings are entirely absent, not clean. "
                     f"Check the model's API key:\n")
            for d in coverage.degraded:
                L.append(f"- **{d.label}** — {d.reason}")
            L.append("")
        if coverage.complete:
            L.append(f"All {coverage.total} pass×section unit(s) were reviewed "
                     f"— no section was lost to a provider refusal, a truncated "
                     f"reply, or a dropped result.\n")
        else:
            L.append(f"**{len(coverage.gaps)} of {coverage.total} pass×section "
                     f"unit(s) could not be reviewed**, even after a retry: the "
                     f"model refused, ran out of output room, or returned "
                     f"nothing. The paragraphs below were **not checked** for "
                     f"the listed error type(s) — treat them as unreviewed, not "
                     f"as clean:\n")
            for g in coverage.gaps:
                L.append(f"- **{g.pass_label}** — {len(g.para_ids)} "
                         f"paragraph(s), {_gap_preview(g, paras)}")
            L.append("")
        # The same hole one size down: individual candidates a batched pass sent
        # out and never got a verdict for. It has to be said plainly, because
        # the alternative reading of a short result is the flattering one — that
        # the model looked at everything and found little worth changing.
        if coverage.unruled:
            L.append(f"**{coverage.unruled_total} candidate(s) never got a "
                     f"verdict.** They were sent to the model and paid for, but "
                     f"the reply hit its token ceiling or came back incomplete, "
                     f"so they were neither corrected nor ruled harmless. They "
                     f"are missing from the counts below, not counted as "
                     f"'nothing to fix':\n")
            for r in coverage.unruled:
                L.append(f"- **{r.label}** — {r.summary()}")
            L.append("")

    if by_type:
        L.append("Applied changes by error type: " +
                 ", ".join(f"{k} {v}" for k, v in by_type.items()) + "\n")

    if normalization is not None and normalization.ran and normalization.total:
        L.append("## Applied without tracked changes\n")
        L.append(f"House style allows exactly two edits outside the "
                 f"tracked-changes system, because neither changes what the "
                 f"book says: **{normalization.quotes} straight quote(s) "
                 f"curled** and **{normalization.spaces} run(s) of spaces "
                 f"collapsed**, across {normalization.paragraphs} paragraph(s). "
                 f"These are not in the change list below and cannot be "
                 f"rejected in Word.\n")
        if normalization.ambiguous:
            L.append(f"{normalization.ambiguous} straight mark(s) were **left "
                     f"alone** because the text does not settle which way they "
                     f"curl — usually a single quote that could be a nested "
                     f"quotation or a dialect elision. Curling one the wrong "
                     f"way would ship silently, so they are left for a human "
                     f"to set.\n")

    if audit is not None and audit.ran:
        L.append("## Reject-all audit\n")
        if audit.passed:
            L.append(f"**Passed.** Rejecting every tracked change reproduces "
                     f"all {audit.checked} ingested paragraph(s) exactly, so "
                     f"nothing reached the manuscript without a revision mark "
                     f"around it. The two normalizations above are applied "
                     f"before this baseline is taken and are counted "
                     f"separately.\n")
        else:
            L.append(f"**FAILED.** {audit.summary()}.\n")
            if cfg.audit == "strict":
                L.append("**No reviewed document was written.** Something "
                         "changed the manuscript without wrapping it in a "
                         "tracked change, so accepting or rejecting the "
                         "revisions would not return the author's text. This "
                         "file and `findings.json` are the diagnosis; ignore "
                         "the instructions at the end of this report.\n")
            for m in audit.mismatches[:10]:
                L.append(f"```\n{m.describe()}\n```\n")
            for para_id in audit.missing[:10]:
                L.append(f"- `{para_id}` is missing from the output entirely\n")

    if consistency is not None and consistency.terms:
        L.append("## Terms written more than one way\n")
        L.append(f"{len(consistency.terms)} term(s) appear in more than one "
                 f"form across the whole manuscript — the check a "
                 f"paragraph-by-paragraph read cannot make. Each is a question, "
                 f"not a correction: which form the book uses is the author's "
                 f"to settle.\n")
        for t in consistency.terms:
            forms = ", ".join(f"`{f}` ({n})" for f, n in t.counts.most_common())
            L.append(f"- {forms} — most often **{t.dominant}**")
        L.append("")

    if consistency is not None and consistency.names:
        L.append("## Names spelled more than one way\n")
        L.append("Capitalized words that never appear lowercased and differ "
                 "only in their diacritics — one name, spelled two ways. "
                 "Where one spelling clearly owns the book, the strays were "
                 "corrected as tracked changes (reject them if the spellings "
                 "are different characters); the rest are questions in the "
                 "margins.\n")
        for nd in consistency.names:
            forms = ", ".join(f"`{f}` ({n})" for f, n in nd.counts.most_common())
            what = ("corrected" if nd.enforce else "asked about")
            L.append(f"- {forms} — {len(nd.outliers)} occurrence(s) of the "
                     f"minority spelling {what}; the book uses "
                     f"**{nd.dominant}**")
        L.append("")

    rows = scripted_check_rows(sweeps, findings, applied_ids)
    if rows:
        L.append("## Scripted checks\n")
        L.append("Each rule below ran as a pattern sweep over every paragraph "
                 "rather than as a read, so these counts are exact rather than "
                 "an impression. *Remaining* is what the sweep's own patterns "
                 "still match after its fixes are applied: zero means the rule "
                 "is fully executed, and nothing of that kind is left in the "
                 "manuscript.\n")
        for r in rows:
            line = (f"- **{r['name']}** (`{r['key']}`): {r['flagged']} flagged, "
                    f"{r['applied']} applied, {r['remaining']} remaining")
            if r["applied"] < r["flagged"]:
                line += (f" — {r['flagged'] - r['applied']} overlapped an "
                         f"earlier change and were left to it")
            L.append(line)
        L.append("")

    if spell is not None and spell.available:
        L.append("## Dictionary scan\n")
        L.append(f"{spell.tokens:,} words read, {spell.unique:,} distinct, "
                 f"{spell.unknown:,} not in the dictionary. Nothing here was "
                 f"changed — the scan classifies, it does not correct.\n")
        if spell.lexicon:
            shown = ", ".join(spell.lexicon[:40])
            more = (f" …and {len(spell.lexicon) - 40} more"
                    if len(spell.lexicon) > 40 else "")
            L.append(f"**Protected as names** ({len(spell.lexicon)}) "
                     f"— written as names, so sent to every pass as words never "
                     f"to flag or 'correct': {shown}{more}\n")
        if spell.candidates:
            shown = ", ".join(c.word for c in spell.candidates[:40])
            more = (f" …and {len(spell.candidates) - 40} more"
                    if len(spell.candidates) > 40 else "")
            L.append(f"**Given to the model to look at** "
                     f"({len(spell.candidates)}) — used seldom or coming apart "
                     f"into an ordinary word plus an ending, and unknown: "
                     f"{shown}{more}\n")
        if spell.recurring:
            shown = ", ".join(c.word for c in spell.recurring[:40])
            more = (f" …and {len(spell.recurring) - 40} more"
                    if len(spell.recurring) > 40 else "")
            L.append(f"**Noted as repeated vocabulary** "
                     f"({len(spell.recurring)}) — unknown, used more than once, "
                     f"not a name: shown to the model as evidence, not "
                     f"protection: {shown}{more}\n")

    if applied:
        # Grouped by kind rather than by document order: reading twenty
        # comma splices together is how you notice a habit.
        L.append("## Applied changes\n")
        for error_type in sorted(by_type, key=lambda k: (-by_type[k], k)):
            L.append(f"### {error_type} — {by_type[error_type]}\n")
            for f in (x for x in applied if x.error_type == error_type):
                loc = paras[f.para_id].location
                detail = f" — {f.explanation}" if f.explanation else ""
                L.append(f"**{f.para_id}** ({loc}, "
                         f"{f.confidence} confidence){detail}\n")
                L.append(f"> {f.original_text}\n>\n> → {f.corrected_text}\n")

    if genuine_queries:
        L.append("## Queries — questions, not corrections\n")
        L.append(f"{len(genuine_queries)} finding(s) from types that ask rather "
                 f"than correct, because the answer is the author's to make — "
                 f"where a line of dialogue belongs is not a punctuation fix. "
                 f"Nothing here changed the document, and each is a "
                 f"{fmt.comment_noun} in the reviewed file.\n")
        for f in genuine_queries:
            L.append(f"- **{f.para_id}** ({f.error_type}): "
                     f"{f.original_text!r} — {f.explanation}")
        L.append("")

    # The smoothing pass reports its own volume, because the number it chose NOT
    # to show is not visible anywhere else. A cap the author cannot see is
    # indistinguishable from a pass that simply found little, and those are very
    # different facts about their manuscript.
    # Rendered when the pass proposed anything OR when a reading pass failed —
    # the second half matters because a run whose every window truncated
    # proposes nothing, and gating solely on `proposed` would hide that outage
    # behind the same silence the pass produces on a clean read.
    if smoothing is not None and (smoothing.proposed or smoothing.windows_failed):
        L.append("## Language smoothing\n")
        if smoothing.proposed:
            L.append(f"{smoothing.kept} suggestion(s) in the margin, from "
                     f"{smoothing.proposed} the line-editing pass proposed. These "
                     f"are questions of taste, not corrections: every one is a "
                     f"{fmt.comment_noun} and none of them changed the text.\n")
            if smoothing.withheld:
                L.append(f"**{smoothing.withheld} further suggestion(s) withheld**"
                         f" — this manuscript's cap is {smoothing.cap}, and the "
                         f"least confident past that were dropped rather than "
                         f"crowd the margin.\n")
            if smoothing.unjudged:
                # Silence here would read as restraint. It is not: these are
                # candidates nobody ruled on, so the count above is a floor.
                L.append(f"**{smoothing.unjudged} of them were never ruled on** — "
                         f"the reviewing model's reply came back truncated or "
                         f"unreadable, so those suggestions were neither offered "
                         f"nor refused. This pass ran incompletely; treat the "
                         f"count above as a floor rather than a finding.\n")
        if smoothing.windows_failed:
            # The read itself, not the judge — a whole window of the manuscript
            # went unread. Reported for the same reason as `unjudged`: on this
            # pass a shortfall is invisible against its ordinary silence.
            L.append(f"**{smoothing.windows_failed} of {smoothing.windows} "
                     f"reading pass(es) did not complete** — the line editor's "
                     f"reply came back truncated or unreadable, so those parts "
                     f"of the manuscript were never read for smoothing. Any "
                     f"count above is a floor rather than a full read.\n")

    # Chapter continuity reports its own volume for the same reason as smoothing:
    # the per-chapter cap and the judge's refusals are not visible in the findings
    # that survived, and on this pass silence is the ordinary output — so a
    # restrained run and a failed one have to be told apart.
    cc = chapter_continuity
    # Print when the pass proposed something OR when a read failed: a run where
    # every chapter read failed proposes nothing, and that is the one time the
    # section matters most — hiding it would report the failure as restraint.
    if cc is not None and (cc.proposed or cc.read_failed):
        L.append("## Chapter continuity\n")
        L.append(f"{cc.kept} question(s) in the margin, from {cc.proposed} "
                 f"in-chapter break(s) the read proposed across {cc.chapters} "
                 f"chapter(s). These are questions, not corrections: every one is "
                 f"a {fmt.comment_noun} and none of them changed the text.\n")
        if cc.refused:
            L.append(f"The judge set aside {cc.refused} as not genuine breaks — "
                     f"a device the reader is meant to hold open, or a "
                     f"contradiction the chapter resolves between its two "
                     f"sentences.\n")
        if cc.withheld:
            L.append(f"**{cc.withheld} further question(s) withheld** — the cap is "
                     f"{cc.cap} per chapter, and the least confident past that "
                     f"were dropped rather than crowd one chapter's margin.\n")
        if cc.unjudged:
            # As with smoothing: candidates nobody ruled on, so the count is a floor.
            L.append(f"**{cc.unjudged} of them were never ruled on** — the judge's "
                     f"reply came back truncated or unreadable, so those breaks "
                     f"were neither raised nor set aside. This pass ran "
                     f"incompletely; treat the count above as a floor.\n")
        if cc.read_failed:
            # A chapter never read earns no queries the same way a clean one does;
            # only this line tells the two apart.
            L.append(f"**{cc.read_failed} chapter(s) could not be read** — the "
                     f"read failed, was refused, or truncated, so any break in "
                     f"those chapters was missed entirely. Treat the count above "
                     f"as covering only the chapters that were read.\n")

    # Each judge gate gets its own section, because these are a different animal
    # from the queries above: every one of them is a correction the run was going
    # to make and then withheld, so the author is owed both the change it would
    # have made and the reason it was held.
    for report in (judges or []):
        if not report.checked:
            continue
        # Matched on the change itself, not the finding id alone: ids are only
        # unique per source, so a namesake from another pass must not be listed
        # here as something the gate held back.
        held_key = {(f.finding_id, f.para_id, f.corrected_text)
                    for f in report.withheld}
        held = [f for f in queries
                if (f.finding_id, f.para_id, f.corrected_text) in held_key]
        L.append(f"## The {report.spec.label}\n")
        L.append(f"{report.checked} change(s) went to this check, each asked a "
                 f"single question: {report.spec.question}\n")
        # The pass fails open, so a judge that could not answer leaves changes
        # applied. That is the one thing a reader of this section must not have
        # to guess at: an unread change looks exactly like an approved one.
        if report.unread:
            L.append(f"**{report.unread} of them got no answer** — the model "
                     f"refused, timed out, or replied unusably — and those were "
                     f"applied WITHOUT being read. Treat this run's "
                     f"{report.spec.label} as incomplete.\n")
        if not report.n_withheld:
            L.append(f"{report.answered} were read and all of them passed, so "
                     f"nothing was held back.\n")
        else:
            L.append(f"{report.n_withheld} did not clearly pass, so they were "
                     f"NOT applied; each is listed below with the reason. "
                     f"Nothing here changed the document.\n")
        for f in held:
            L.append(f"- **{f.para_id}** ({f.error_type}): "
                     f"{f.original_text!r}\n"
                     f"  → would have become {f.corrected_text!r}\n"
                     f"  — {f.explanation}")
        if held:
            L.append("")

    if low:
        L.append("## Possibly intentional — for your judgment\n")
        L.append(f"These anchored cleanly but sat below the confidence gate. "
                 f"In fiction that usually means the model read the passage as "
                 f"deliberate: dialogue rhythm, dialect, voice, a name that "
                 f"only looks misspelled. Nothing was changed in the "
                 f"document{in_margin}.\n")
        for f in low:
            L.append(f"- **{f.para_id}** ({f.error_type}): "
                     f"{f.original_text!r} — {f.explanation}")
        L.append("")

    if oversized:
        L.append("## Caught, but too large to auto-correct\n")
        L.append(f"{len(oversized)} finding(s) name a real problem whose "
                 f"suggested fix rewrites more than a minimal proofreading edit "
                 f"should — a run-on split into two sentences, a restructured "
                 f"list. Applying that as a tracked change would be the model "
                 f"rewriting rather than correcting, so it is left for you to "
                 f"make by hand{in_margin}.\n")
        for f in oversized:
            suggestion = (f" → {f.corrected_text}"
                          if f.corrected_text
                          and f.corrected_text != f.original_text else "")
            L.append(f"- **{f.para_id}** ({f.error_type}): "
                     f"{f.original_text!r}{suggestion}")
        L.append("")

    if rejected:
        L.append("## Rejected by the validator\n")
        for f in rejected:
            L.append(f"- `{f.finding_id}` {f.status} ({f.para_id}): "
                     f"{f.original_text[:70]!r}")
        L.append("")

    L.append("## Usage\n")
    L.append(f"API calls: {usage.api_calls} · input tokens: "
             f"{usage.input_tokens:,} (+{usage.cache_creation_input_tokens:,} "
             f"cache-write, {usage.cache_read_input_tokens:,} cache-read) · "
             f"output tokens: {usage.output_tokens:,}\n")
    # Summed at each model's own rate from the per-model breakdown, so a mixed
    # OpenAI/Anthropic run is priced right rather than at the detector's rate.
    est = cost_of_usage(usage, fallback_model=cfg.api.model, batch=batch)
    if est is None and cfg.pricing.input_per_mtok and cfg.pricing.output_per_mtok:
        est = ((usage.input_tokens + usage.cache_creation_input_tokens)
               * cfg.pricing.input_per_mtok
               + usage.output_tokens * cfg.pricing.output_per_mtok) / 1_000_000
    # Sapling bills per character with no tokens, so it is added on top of the
    # model estimate — otherwise the total would silently omit a pass a person
    # paid for. Broken out on its own line so the sum is legible.
    sapling_cost = getattr(usage, "sapling_cost", 0.0) or 0.0
    if est is not None or sapling_cost:
        note = "batch rates" if batch else "per model"
        total = (est or 0.0) + sapling_cost
        L.append(f"Estimated cost ({note}): **${total:.4f}**\n")
        # Break the bill down by model when the run recorded per-model usage:
        # this is where a reader sees that the dear Anthropic reads, not the
        # cheap detector, are what a review actually costs.
        by_model = getattr(usage, "by_model", None) or {}
        for model_id, tk in sorted(
                by_model.items(),
                key=lambda kv: cost_of_usage(
                    {"by_model": {kv[0]: kv[1]}}, fallback_model=cfg.api.model,
                    batch=batch) or 0.0, reverse=True):
            c = cost_of_usage({"by_model": {model_id: tk}},
                              fallback_model=cfg.api.model, batch=batch)
            if c is None:
                continue
            name = lookup(model_id).display if lookup(model_id) else \
                (model_id or "unknown model")
            L.append(f"- {name}: ${c:.4f} "
                     f"({tk.get('output_tokens', 0):,} output tokens)\n")
        if sapling_cost:
            L.append(f"- Sapling grammar check: ${sapling_cost:.4f} "
                     f"({getattr(usage, 'sapling_chars', 0):,} characters at "
                     f"${cfg.sapling.cost_per_1k_chars:g}/1k)\n")

    if fmt is None:                       # callers that predate format support
        from .formats import DOCX as fmt
    closing = fmt.review_instructions
    if cfg.comments:
        closing += f" Every change carries a {fmt.comment_noun} explaining itself."
    L.append(f"---\n{closing}\n")
    path.write_text("\n".join(L), encoding="utf-8")
    log.info("Wrote %s", path)