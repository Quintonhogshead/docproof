"""The one JSON envelope every galley-facing CLI verb's ``--json`` shares.

    {"findings": [...], "cost": {"total_usd": float, "by_model": {...}},
     "ledger": {"total": int, "gaps": [...], "unruled": [...],
                "degraded": [...]},
     "checkpoint": "<path or null>"}

``findings`` rows are :func:`docproof.checkpoint.finding_to_dict` shape (or
already-dict rows, passed through unchanged — a caller reading a findings file
back off disk need not round-trip it through `Finding` first).

``cost`` is summed PER MODEL, via ``Usage.by_model`` and
``providers/catalog.cost_of_usage`` — never a mixed run's total priced at one
model's flat rate, the historical bug that under-counted an expensive model's
share by up to ~40x (see docs/cost-accounting notes). Sapling's per-character
bill rides `usage.sapling_cost` and is folded in under its own `by_model` key
since it is not priced through the token catalog at all.

``ledger`` mirrors the fields :mod:`docproof.run_checkpoint` already persists
— the same :class:`~docproof.models.CoverageLedger`, so a ``--json`` reader
and a resumed run agree on what "reviewed" means.

``checkpoint`` is the resumable checkpoint file's path, if one is still on
disk when the envelope is built, else ``null``. On a normal, fully completed
run this is almost always ``null`` — the checkpoint that made the run
resumable is deleted the moment the deliverable is written; a caller sees a
path here chiefly when reporting on a job that did not finish.

A verb with nothing to say for one of these keys still emits it, zeroed or
empty — never omitted — so a consumer can destructure the envelope without a
presence check first. Verb-specific fields (galley audit's hypotheses, galley
seed's answer-key paths, sweep's flagged/remaining counts, …) ride alongside
the four canonical keys, merged in via `extra`; the four always win if a name
collides, so a caller can never accidentally shadow the contract.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import CoverageLedger, Finding, Usage
from .providers import estimate_cost


def _cost_dict(usage: Usage | None, fallback_model: str, *,
               batch: bool = False) -> dict[str, Any]:
    by_model: dict[str, float] = {}
    total = 0.0
    for model_id, tk in (getattr(usage, "by_model", None) or {}).items():
        label = model_id or fallback_model
        c = estimate_cost(
            label,
            input_tokens=tk.get("input_tokens", 0),
            output_tokens=tk.get("output_tokens", 0),
            cache_read_tokens=tk.get("cache_read_input_tokens", 0),
            cache_write_tokens=tk.get("cache_creation_input_tokens", 0),
            batch=batch) or 0.0
        by_model[label] = by_model.get(label, 0.0) + c
        total += c
    sapling_cost = float(getattr(usage, "sapling_cost", 0.0) or 0.0)
    if sapling_cost:
        by_model["sapling"] = by_model.get("sapling", 0.0) + sapling_cost
        total += sapling_cost
    return {"total_usd": round(total, 4),
            "by_model": {k: round(v, 4) for k, v in by_model.items()}}


def _ledger_dict(coverage: CoverageLedger | None) -> dict[str, Any]:
    if coverage is None:
        return {"total": 0, "gaps": [], "unruled": [], "degraded": []}
    return {
        "total": coverage.total,
        "gaps": [dataclasses.asdict(g) for g in coverage.gaps],
        "unruled": [dataclasses.asdict(w) for w in coverage.unruled],
        "degraded": [dataclasses.asdict(s) for s in coverage.degraded],
    }


def _finding_rows(findings: Sequence[Finding | Mapping]) -> list[dict]:
    from .checkpoint import finding_to_dict
    return [f if isinstance(f, dict) else finding_to_dict(f) for f in findings]


def build_envelope(*, findings: Sequence[Finding | Mapping] = (),
                   usage: Usage | None = None, fallback_model: str = "",
                   batch: bool = False, coverage: CoverageLedger | None = None,
                   checkpoint: str | Path | None = None,
                   extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the envelope. See module docstring for the shape and for how
    `extra` (verb-specific fields) composes with it."""
    envelope = {
        "findings": _finding_rows(findings),
        "cost": _cost_dict(usage, fallback_model, batch=batch),
        "ledger": _ledger_dict(coverage),
        "checkpoint": str(checkpoint) if checkpoint else None,
    }
    if extra:
        return {**extra, **envelope}
    return envelope


__all__ = ["build_envelope"]
