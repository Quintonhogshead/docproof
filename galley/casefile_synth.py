"""Build a CaseFile from a bare `docproof review`/`replay` run (P1-6).

`galley letter` and `galley calibrate` want a `casefile.json`, but only the
multi-wave orchestrator and the hosted-app job path write one — a plain CLI
`review`/`replay` writes `findings.json` and the deliverable, never a case file,
so the letter and style sheet could not be produced on their output without
hand-authoring the file (Purpura beta). This synthesizes the case file the
letter needs from the artifacts a bare run DOES leave: `findings.json` (the
findings + the cost envelope) and, when present, `summary.md` for the book name.

It is a faithful projection, not a fabrication: one wave summarizing the run, the
run's own findings and their channel verdicts, and the run's recorded spend. No
new editorial decisions are invented — the style sheet stays empty (a plain run
records no bound rulings), exactly as the letter renders when none exist.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from galley.casefile import CaseFile
from galley.contracts import GFinding, Provenance, Span, Verdict, WaveRecord


def _ruling_for(row: dict[str, Any]) -> str:
    """The case-file ruling for a finished finding's channel, in the case
    file's own vocabulary (:data:`galley.contracts.RULINGS`): an applied edit
    held its span (``keep``), a margin comment is a ``query``, anything
    rejected a ``reject``."""
    status = row.get("status")
    if row.get("force_query") or row.get("queried") or status == "query":
        return "query"
    if status in ("validated", "applied") or row.get("applied") is True:
        return "keep"
    return "reject"


def casefile_from_run(run_dir: str | Path, *, book: str = "") -> CaseFile:
    """A CaseFile projected from a finished run directory. Raises FileNotFoundError
    when there is no findings.json to build from."""
    run = Path(run_dir)
    path = run / "findings.json"
    payload = json.loads(path.read_text(encoding="utf-8"))   # FileNotFoundError propagates
    rows = payload.get("findings", []) if isinstance(payload, dict) else []
    cost = float(((payload.get("cost") or {}).get("total_usd")) or 0.0)

    if not book:
        summary = run / "summary.md"
        if summary.exists():
            first = summary.read_text(encoding="utf-8").splitlines()[:1]
            if first:
                book = first[0].lstrip("# ").strip()
        book = book or (payload.get("source") and Path(payload["source"]).stem) \
            or run.name

    findings: list[GFinding] = []
    verdicts: list[Verdict] = []
    edits = queries = 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        fid = row.get("finding_id") or f"f-{i:04d}"
        anchor = row.get("anchor") or {}
        span = Span(para_id=row.get("para_id", ""),
                    start=int(anchor.get("start", 0) or 0),
                    end=int(anchor.get("end", 0) or 0))
        findings.append(GFinding(
            id=fid, error_type=row.get("error_type", "unknown"), span=span,
            find=row.get("original_text", ""), replace=row.get("corrected_text", ""),
            note=row.get("explanation", ""),
            confidence=row.get("confidence", "medium"),
            provenance=Provenance(detector=str(row.get("chunk_id", "")), wave=1)))
        ruling = _ruling_for(row)
        if ruling == "query":
            queries += 1
        elif ruling == "keep":
            edits += 1
        verdicts.append(Verdict(finding_id=fid, ruling=ruling,
                                reason=row.get("explanation", ""), wave=1))

    # The scope is the orchestrator's JSON shape (an empty scope = the whole
    # book), so ``galley.calibration.record_run`` can price this run like any
    # other rather than skipping a scope it cannot resolve.
    wave = WaveRecord(
        index=1,
        actions=({"adapter": "review",
                  "scope": {"chapters": [], "para_ids": [], "error_groups": [],
                            "model": str(payload.get("model") or ""), "passes": 1},
                  "findings_added": edits + queries, "cost_usd": cost},),
        spend_usd=cost, findings_added=len(findings))

    cf = CaseFile(book=book, findings=findings, verdicts=verdicts, waves=[wave])
    cf.budget.charge("review", cost, wave=1)
    return cf


__all__ = ["casefile_from_run"]
