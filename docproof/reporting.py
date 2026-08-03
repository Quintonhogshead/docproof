from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import DocumentModel, Finding, Usage, index_paragraphs

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


def write_findings_json(path: Path, *, doc: DocumentModel,
                        findings: list[Finding], usage: Usage,
                        cfg: Config, applied_ids: tuple[str, ...]) -> None:
    applied = set(applied_ids)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": doc.source_path,
        "config": {"model": cfg.api.model,
                   "error_types": list(cfg.error_type_keys),
                   "error_type_passes": [list(g) for g in cfg.error_type_groups],
                   "min_confidence": cfg.min_confidence,
                   "revision_author": cfg.revision_author},
        "usage": dataclasses.asdict(usage),
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
                     findings: list[Finding], usage: Usage,
                     cfg: Config, applied_ids: tuple[str, ...]) -> None:
    paras = index_paragraphs(doc)
    applied = [f for f in findings if f.finding_id in set(applied_ids)]
    low = [f for f in findings if f.status == "skipped_low_confidence"]
    rejected = [f for f in findings if f.status.startswith("rejected")]

    L: list[str] = []
    L.append(f"# docproof review — {Path(doc.source_path).name}\n")
    passes = cfg.error_type_groups
    L.append(f"*{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
             f"model `{cfg.api.model}` · {len(cfg.error_type_keys)} error "
             f"type(s) in {len(passes)} pass(es) · gate: {cfg.min_confidence}*\n")
    L.append("Passes: " + "; ".join(" + ".join(g) for g in passes) + "\n")

    stats = _tally(findings)
    L.append(f"**{len(applied)} change(s) applied** as tracked revisions "
             f"(author *{cfg.revision_author}*). Findings by status: " +
             ", ".join(f"{k} {v}" for k, v in stats.items()) +
             f". Paragraphs reviewed: {len(doc.paragraphs)}; "
             f"skipped: {len(doc.skipped)}.\n")

    by_type = _tally_types(findings)
    if by_type:
        L.append("Findings by error type: " +
                 ", ".join(f"{k} {v}" for k, v in by_type.items()) + "\n")

    if applied:
        L.append("## Applied changes\n")
        for f in applied:
            loc = paras[f.para_id].location
            L.append(f"**{f.para_id}** ({loc}, {f.error_type}, "
                     f"{f.confidence} confidence) — {f.explanation}\n")
            L.append(f"> {f.original_text}\n>\n> → {f.corrected_text}\n")

    if low:
        L.append("## Possibly intentional — for your judgment\n")
        L.append("These anchored cleanly but sat below the confidence gate. In "
                 "fiction that usually means the model read the passage as "
                 "deliberate: dialogue rhythm, dialect, voice, a name that only "
                 "looks misspelled. Nothing was changed in the document.\n")
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
    if cfg.pricing.input_per_mtok and cfg.pricing.output_per_mtok:
        est = ((usage.input_tokens + usage.cache_creation_input_tokens)
               * cfg.pricing.input_per_mtok
               + usage.output_tokens * cfg.pricing.output_per_mtok) / 1_000_000
        L.append(f"Estimated cost (upper bound — cache reads bill below the "
                 f"input rate): **${est:.4f}**\n")

    L.append("---\nOpen the reviewed `.docx` in Word → **Review** tab → walk "
             "the changes with Accept/Reject. Every change carries a margin "
             "comment explaining itself.\n")
    path.write_text("\n".join(L), encoding="utf-8")
    log.info("Wrote %s", path)