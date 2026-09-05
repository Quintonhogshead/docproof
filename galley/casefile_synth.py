"""Synthesize a CaseFile from artifacts produced by a bare review or replay.

It projects ``findings.json`` and optional ``summary.md`` into the one-wave
shape consumed by the letter and calibration commands; it invents no rulings.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docproof.editmap import collapse_region_siblings
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


# The cost-bearing artifacts a run directory can hold besides findings.json:
# settle's ledger and the two verify gates, each with the same
# `cost: {total_usd}` field the findings envelope carries.
_COST_ARTIFACTS = ("settlement.json", "change_verify.json",
                   "finished_walk.json")


def _cost_of(path: Path) -> float:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(payload, dict):
        return 0.0
    cost = payload.get("cost") or {}
    try:
        return float(cost.get("total_usd") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def workspace_waves(workspace: str | Path) -> list[WaveRecord]:
    """One wave per run directory under `<workspace>/runs/` that holds a
    findings.json, in the order the runs were made, each charged with that
    run's findings.json cost PLUS its settle/verify artifacts' costs, and
    naming the lanes (error types) that ran. `galley letter` on a $0 replay
    build reported "$0.00 spent, 1 wave" (Georgis, 2026-09-04) because the
    final build's own envelope is $0 by design — the money was spent in the
    ladder and verify runs beside it."""
    runs = Path(workspace) / "runs"
    if not runs.is_dir():
        return []
    dirs = sorted((d for d in runs.iterdir()
                   if d.is_dir() and (d / "findings.json").is_file()),
                  key=lambda d: (d / "findings.json").stat().st_mtime)
    waves: list[WaveRecord] = []
    for i, d in enumerate(dirs, 1):
        try:
            payload = json.loads((d / "findings.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        rows = payload.get("findings", []) if isinstance(payload, dict) else []
        rows = [r for r in rows if isinstance(r, dict)]
        cost = _cost_of(d / "findings.json")
        extras = {name: _cost_of(d / name) for name in _COST_ARTIFACTS
                  if (d / name).is_file()}
        lanes = sorted({str(r.get("error_type") or "") for r in rows} - {""})
        actions = [{"adapter": "review", "run": d.name, "lanes": lanes,
                    "scope": {"chapters": [], "para_ids": [],
                              "error_groups": [],
                              "model": str(payload.get("model") or ""),
                              "passes": 1},
                    "findings_added": len(rows), "cost_usd": cost}]
        for name, c in extras.items():
            actions.append({"adapter": name.split(".")[0], "run": d.name,
                            "cost_usd": c, "findings_added": 0})
        waves.append(WaveRecord(index=i, actions=tuple(actions),
                                spend_usd=cost + sum(extras.values()),
                                findings_added=len(rows)))
    return waves


def casefile_from_run(run_dir: str | Path, *, book: str = "") -> CaseFile:
    """A CaseFile projected from a finished run directory. Raises FileNotFoundError
    when there is no findings.json to build from."""
    run = Path(run_dir)
    path = run / "findings.json"
    payload = json.loads(path.read_text(encoding="utf-8"))   # FileNotFoundError propagates
    rows = payload.get("findings", []) if isinstance(payload, dict) else []
    # One GFinding per decision: a row the validator split into minimal
    # regions is reported as lettered siblings, which are not separate
    # findings to adjudicate (docproof.editmap.collapse_region_siblings).
    rows, _folded = collapse_region_siblings(rows)
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
