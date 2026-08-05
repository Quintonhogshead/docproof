"""What DocProof has cost, added up.

Every finished job records its own token counts, so this is arithmetic over job
records rather than a re-read of every results folder. Jobs that finished before
records carried usage are filled in from their findings.json / prep.json once,
here, so a dashboard opened after an upgrade isn't full of blanks.

Every figure is an estimate from published per-token rates. The vendor's invoice
is the invoice; this is for spotting that a run cost ten times what the last one
did, which is the question people actually have.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docproof.providers import estimate_cost, lookup

log = logging.getLogger("docproof.app.usage")

RECENT_LIMIT = 40
WINDOW_DAYS = 30


def _totals_for(job, read_usage) -> dict:
    """One job's numbers, from the record or from what it left on disk."""
    if job.api_calls or job.input_tokens or job.output_tokens:
        cost = job.cost
        if cost is None:
            cost = estimate_cost(job.model,
                                 input_tokens=job.input_tokens
                                 + job.cache_write_tokens,
                                 output_tokens=job.output_tokens,
                                 batch=job.mode == "batch")
        return {"input_tokens": job.input_tokens,
                "output_tokens": job.output_tokens,
                "cache_read_tokens": job.cache_read_tokens,
                "cache_write_tokens": job.cache_write_tokens,
                "api_calls": job.api_calls, "cost": cost or 0.0}

    if not job.results_dir or not Path(job.results_dir).is_dir():
        return _empty()
    found = read_usage(job.results_dir)
    if found is None:
        return _empty()
    usage, cost = found
    if cost is None:
        cost = estimate_cost(
            job.model,
            input_tokens=usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            batch=job.mode == "batch")
    return {"input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
            "api_calls": usage.get("api_calls", 0), "cost": cost or 0.0}


def _empty() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "api_calls": 0, "cost": 0.0}


def _add(into: dict, other: dict) -> None:
    for key, value in other.items():
        into[key] = into.get(key, 0) + value


def _when(job) -> datetime | None:
    try:
        return datetime.fromisoformat(job.created_at)
    except (TypeError, ValueError):
        return None


def build_usage(jobs, read_usage) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    totals = _empty()
    window = _empty()
    by_model: dict[str, dict] = defaultdict(_empty)
    by_kind: dict[str, dict] = defaultdict(_empty)
    by_source: dict[str, dict] = defaultdict(_empty)
    by_month: dict[str, dict] = defaultdict(_empty)
    documents = words = 0
    unfinished = 0
    recent = []

    for job in jobs:
        if job.state != "done":
            if job.state not in ("failed",):
                unfinished += 1
            # A failed prep still burned tokens getting there, so it counts.
            if not (job.api_calls or job.input_tokens):
                continue
        numbers = _totals_for(job, read_usage)
        if not numbers["api_calls"] and not numbers["input_tokens"]:
            continue

        _add(totals, numbers)
        _add(by_model[job.model], numbers)
        _add(by_kind[job.kind], numbers)
        # getattr, not job.source: this function's whole virtue is that it
        # runs over any iterable of job-shaped things, and an older record
        # predates the field.
        _add(by_source[_source(job)], numbers)
        documents += 1
        words += job.words or 0

        when = _when(job)
        if when is not None:
            _add(by_month[when.strftime("%Y-%m")], numbers)
            if when >= cutoff:
                _add(window, numbers)

        if len(recent) < RECENT_LIMIT:
            recent.append({
                "id": job.id, "filename": job.filename, "kind": job.kind,
                "model": job.model, "display": _display(job.model),
                "mode": job.mode, "state": job.state,
                "source": _source(job),
                "created_at": job.created_at, "words": job.words,
                **numbers,
            })

    return {
        "totals": {**totals, "jobs": documents, "words": words},
        "window": {**window, "days": WINDOW_DAYS},
        "unfinished": unfinished,
        "by_model": [{"model": model, "display": _display(model), **numbers}
                     for model, numbers in
                     sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])],
        "by_kind": [{"kind": kind, **numbers}
                    for kind, numbers in sorted(by_kind.items())],
        "by_source": [{"source": source, **numbers}
                      for source, numbers in sorted(by_source.items())],
        "by_month": [{"month": month, **numbers}
                     for month, numbers in sorted(by_month.items())][-12:],
        "recent": recent,
        "note": ("Estimated from published per-token rates, including the "
                 "batch discount where it applied. Your provider's bill is "
                 "the last word."),
    }


def _source(job) -> str:
    """Which door a job came in by, tolerant of records made before there
    was more than one."""
    return getattr(job, "source", None) or "app"


def _display(model: str) -> str:
    info = lookup(model)
    return info.display if info else model
