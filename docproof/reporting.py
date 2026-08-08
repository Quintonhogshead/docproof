from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import DocumentModel, Finding, Usage, index_paragraphs
from .providers import estimate_cost

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
                        normalization=None, audit=None, consistency=None
                        ) -> None:
    applied = set(applied_ids)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": doc.source_path,
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
                        "candidates": [c.word for c in spell.candidates]}
                       if spell is not None else None),
        "usage": dataclasses.asdict(usage),
        "batch": batch,
        "stats": _tally(findings),
        "stats_by_error_type": _tally_types(findings),
        "skipped_paragraphs": [{"para_id": pid, "reason": r}
                               for pid, r in doc.skipped],
        "findings": [
            {**dataclasses.asdict(f),
             "anchor": dataclasses.asdict(f.anchor) if f.anchor else None,
             "applied": f.finding_id in applied}
            for f in findings
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    log.info("Wrote %s", path)


def write_summary_md(path: Path, *, doc: DocumentModel,
                     findings: list[Finding], usage: Usage, cfg: Config,
                     applied_ids: tuple[str, ...], batch: bool = False,
                     fmt=None, sweeps=None, spell=None, normalization=None,
                     audit=None, consistency=None) -> None:
    paras = index_paragraphs(doc)
    applied = [f for f in findings if f.finding_id in set(applied_ids)]
    low = [f for f in findings if f.status == "skipped_low_confidence"]
    queries = [f for f in findings if f.status == "query"]
    rejected = [f for f in findings if f.status.startswith("rejected")]
    in_margin = (f", and each is a margin {fmt.comment_noun} in the reviewed "
                 f"file" if cfg.query_comments else "")

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
            L.append(f"**Treated as this author's own** ({len(spell.lexicon)}) "
                     f"— sent to every pass as words never to flag or "
                     f"'correct': {shown}{more}\n")
        if spell.candidates:
            shown = ", ".join(c.word for c in spell.candidates[:40])
            more = (f" …and {len(spell.candidates) - 40} more"
                    if len(spell.candidates) > 40 else "")
            L.append(f"**Given to the model to look at** "
                     f"({len(spell.candidates)}) — used once, not written as "
                     f"a name, and unknown: {shown}{more}\n")

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

    if queries:
        L.append("## Queries — questions, not corrections\n")
        L.append(f"{len(queries)} finding(s) from types that ask rather than "
                 f"correct, because the answer is the author's to make — "
                 f"where a line of dialogue belongs is not a punctuation fix. "
                 f"Nothing here changed the document, and each is a margin "
                 f"{fmt.comment_noun} in the reviewed file.\n"
                 if cfg.query_comments else
                 f"{len(queries)} finding(s) from types that ask rather than "
                 f"correct. They appear here only — `query_comments` is off, "
                 f"so they were not written into the manuscript.\n")
        for f in queries:
            L.append(f"- **{f.para_id}** ({f.error_type}): "
                     f"{f.original_text!r} — {f.explanation}")
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
    est = estimate_cost(cfg.api.model,
                        input_tokens=usage.input_tokens
                        + usage.cache_creation_input_tokens,
                        output_tokens=usage.output_tokens,
                        batch=batch)
    if est is None and cfg.pricing.input_per_mtok and cfg.pricing.output_per_mtok:
        est = ((usage.input_tokens + usage.cache_creation_input_tokens)
               * cfg.pricing.input_per_mtok
               + usage.output_tokens * cfg.pricing.output_per_mtok) / 1_000_000
    if est is not None:
        note = "batch rates" if batch else "upper bound"
        L.append(f"Estimated cost ({note} — cache reads bill below the "
                 f"input rate): **${est:.4f}**\n")

    if fmt is None:                       # callers that predate format support
        from .formats import DOCX as fmt
    closing = fmt.review_instructions
    if cfg.comments:
        closing += f" Every change carries a {fmt.comment_noun} explaining itself."
    L.append(f"---\n{closing}\n")
    path.write_text("\n".join(L), encoding="utf-8")
    log.info("Wrote %s", path)