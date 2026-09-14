"""Per-stage wall-clock, tokens and failures of a fixed proofread.

Read from the durable evidence a run already keeps — call receipts, the
budget ledger and the per-attempt transport ledgers — so every run can be
compared with the last one without instrumenting the readers. Diagnostics
only: nothing here writes into `calls/`, and a missing or partial ledger
yields a row with blanks, never an invented number.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

TOKEN_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "thinking_tokens")


def fixed_directory(path) -> Path:
    """Accept a workspace or its runs/fixed directory."""
    path = Path(path)
    if (path / "runs" / "fixed" / "calls").is_dir():
        return path / "runs" / "fixed"
    if (path / "calls").is_dir():
        return path
    raise FileNotFoundError(f"{path} holds no fixed proofread (no calls directory)")


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _when(value):
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def attempts(directory) -> list[dict]:
    """One row per recorded attempt of every fixed call."""
    directory = fixed_directory(directory)
    budget = _load(directory / "calls" / "budget.json").get("entries", {})
    rows = []
    for receipt_path in sorted((directory / "calls" / "calls").glob("*/receipt.json")):
        receipt = _load(receipt_path)
        sha = receipt.get("request_sha256") or receipt_path.parent.name
        for attempt_dir in sorted((receipt_path.parent / "attempts").glob("[0-9]*")):
            attempt = int(attempt_dir.name)
            started = finished = None
            ledger = attempt_dir / "transport-usage.jsonl"
            if ledger.exists():
                for line in ledger.read_text("utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    when = event.get("recorded_at")
                    if event.get("status") == "started":
                        started = started or when
                    elif when:
                        finished = when
            entry = budget.get(f"{sha}:{attempt}", {})
            final = attempt == receipt.get("attempt")
            usage = entry.get("usage") or (receipt.get("usage") if final else None) or {}
            status = entry.get("status") or (receipt.get("status") if final else "unknown")
            rows.append({"request_sha256": sha, "attempt": attempt, "final": final,
                         "stage": receipt.get("stage", "?"), "model": receipt.get("model", "?"),
                         "effort": receipt.get("effort", "?"), "transport": receipt.get("transport"),
                         "status": status,
                         "failure_category": receipt.get("failure_category") if final else None,
                         "started": started, "finished": finished,
                         "seconds": ((_when(finished) - _when(started)).total_seconds()
                                     if _when(started) and _when(finished) else None),
                         "usage": {k: usage.get(k) for k in TOKEN_FIELDS},
                         "api_usd": entry.get("api_usd")})
    return rows


def summarize(directory) -> dict:
    """Stage rows in order of first start, plus run totals."""
    rows = attempts(directory)
    stages = {}
    for row in rows:
        key = (row["stage"], row["model"], row["effort"])
        stage = stages.setdefault(key, {
            "stage": row["stage"], "model": row["model"], "effort": row["effort"],
            "attempts": 0, "failed_attempts": 0, "in_flight": 0, "first_started": None, "last_finished": None,
            "seconds": [], "api_usd": 0.0, "tokens": {k: 0 for k in TOKEN_FIELDS}})
        stage["attempts"] += 1
        if row["status"] in {"started", "unknown"}:
            stage["in_flight"] += 1          # a running workspace, or an unresolved submission
        elif row["status"] not in {"completed", "reused"}:
            stage["failed_attempts"] += 1
        for k in TOKEN_FIELDS:
            stage["tokens"][k] += row["usage"].get(k) or 0
        stage["api_usd"] += row["api_usd"] or 0.0
        if row["seconds"] is not None:
            stage["seconds"].append(row["seconds"])
        for field, value, pick in (("first_started", row["started"], min), ("last_finished", row["finished"], max)):
            if value and (stage[field] is None or pick(value, stage[field]) == value):
                stage[field] = value
    ordered = []
    for stage in sorted(stages.values(), key=lambda s: (s["first_started"] or "~", s["stage"], s["model"])):
        seconds = sorted(stage.pop("seconds"))
        first, last = _when(stage["first_started"]), _when(stage["last_finished"])
        stage.update({
            "span_seconds": (last - first).total_seconds() if first and last else None,
            "median_seconds": seconds[len(seconds) // 2] if seconds else None,
            "max_seconds": seconds[-1] if seconds else None,
            "api_usd": round(stage["api_usd"], 4)})
        ordered.append(stage)
    starts = [_when(r["started"]) for r in rows if _when(r["started"])]
    ends = [_when(r["finished"]) for r in rows if _when(r["finished"])]
    totals = {k: sum(s["tokens"][k] for s in ordered) for k in TOKEN_FIELDS}
    return {"directory": str(fixed_directory(directory)),
            "run": {"attempts": len(rows),
                    "failed_attempts": sum(s["failed_attempts"] for s in ordered),
                    "in_flight": sum(s["in_flight"] for s in ordered),
                    "first_started": min(starts).isoformat() if starts else None,
                    "last_finished": max(ends).isoformat() if ends else None,
                    "span_seconds": (max(ends) - min(starts)).total_seconds() if starts and ends else None,
                    "api_usd": round(sum(s["api_usd"] for s in ordered), 4),
                    "tokens": totals},
            "stages": ordered}


def render(report: dict) -> str:
    run = report["run"]
    span = run["span_seconds"]
    lines = [f"fixed proofread: {report['directory']}",
             f"attempts {run['attempts']}  failed {run['failed_attempts']}  "
             + (f"in flight {run['in_flight']}  " if run.get('in_flight') else "")
             + f"span {span / 60:.1f} min  api ${run['api_usd']:.2f}  "
             f"input {run['tokens']['input_tokens'] / 1000:.0f}k  "
             f"cache read {run['tokens']['cache_read_input_tokens'] / 1000:.0f}k  "
             f"cache write {run['tokens']['cache_creation_input_tokens'] / 1000:.0f}k  "
             f"output {run['tokens']['output_tokens'] / 1000:.0f}k"
             if span is not None else f"attempts {run['attempts']}  (no transport timestamps)",
             f"{'stage':40}{'model':18}{'eff':6}{'n':>5}{'fail':>5}{'span m':>8}{'med s':>7}{'max s':>7}"
             f"{'in+cache k':>12}{'out k':>8}{'think k':>9}{'usd':>7}"]
    for s in report["stages"]:
        t = s["tokens"]
        lines.append(
            f"{s['stage'][:39]:40}{s['model'][:17]:18}{s['effort'][:5]:6}{s['attempts']:5d}{s['failed_attempts']:5d}"
            f"{(s['span_seconds'] or 0) / 60:8.1f}{(s['median_seconds'] or 0):7.0f}{(s['max_seconds'] or 0):7.0f}"
            f"{(t['input_tokens'] + t['cache_read_input_tokens'] + t['cache_creation_input_tokens']) / 1000:12.0f}"
            f"{t['output_tokens'] / 1000:8.0f}{t['thinking_tokens'] / 1000:9.0f}{s['api_usd']:7.2f}")
    return "\n".join(lines)


def write_timeline(directory) -> Path:
    """Write timeline.json beside result.json; never inside calls/."""
    directory = fixed_directory(directory)
    report = summarize(directory)
    target = directory / "timeline.json"
    target.write_text(json.dumps(report, indent=2, sort_keys=True), "utf-8")
    return target


__all__ = ["attempts", "fixed_directory", "render", "summarize", "write_timeline"]
