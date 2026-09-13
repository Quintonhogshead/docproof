"""Read retained run evidence without treating snapshots as cumulative work.

Older drivers overwrote their invocation ledger on resume. Findings snapshots
and session result receipts can still explain work absent from that ledger,
but they cannot reconstruct a complete execution history.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _source_key(source: Any, workspace: Path) -> str:
    if not isinstance(source, str) or not source:
        return ""
    path = Path(source)
    return str((path if path.is_absolute() else workspace / path).resolve())


@dataclass
class FindingsSnapshot:
    paths: list[str]
    generated_at: str
    applied: int
    queries: int
    rounds: int | None
    decisions: int | None
    selected: bool = False


@dataclass
class SessionReceipt:
    paths: list[str]
    duration_ms: float | None
    num_turns: int | None
    status: str
    phase: str = ""
    started_at: str = ""
    timing_source: str = "CLI result"


@dataclass
class RunHistory:
    snapshots: list[FindingsSnapshot] = field(default_factory=list)
    sessions: list[SessionReceipt] = field(default_factory=list)
    streams_without_result: list[str] = field(default_factory=list)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _last_result(path: Path) -> dict[str, Any] | None:
    result = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
                    result = obj
    except OSError:
        pass
    return result


def _result_key(result: dict[str, Any]) -> str:
    return "result:" + str(result.get("uuid") or _fingerprint(result))


def _legacy_started_at(stream_path: Path) -> str:
    log_path = stream_path.with_name(
        stream_path.name.removesuffix(".stream.jsonl") + ".log")
    try:
        with log_path.open(encoding="utf-8", errors="replace") as log:
            first = log.readline(1024)
    except OSError:
        return ""
    match = re.match(r"# galley driver: phase \S+ at (\S+) ", first)
    return match.group(1) if match else ""


def _coordinator_resources(path: Path) -> dict[str, dict[str, Any]]:
    """Coordinator measurements only; per-model usage rows are not sessions.

The resource ledger may contain several model rows for one coordinator result.
Their duration/turn metadata is the same inclusive session measurement, so we
retain one measurement per receipt rather than summing model rows.
"""
    receipts = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(row, dict) or row.get("reused")
                        or not str(row.get("operation_id", "")).startswith("coordinator-")
                        or row.get("status") in {"started", "pending", "recovering"}):
                    continue
                metadata = row.get("metadata")
                if isinstance(metadata, dict) and row.get("receipt_id"):
                    receipts[str(row["receipt_id"])] = metadata
    except OSError:
        pass
    return receipts


def _reservation_timestamp(value: Any) -> str:
    nanoseconds = _number(value)
    if nanoseconds is None:
        return ""
    try:
        return datetime.fromtimestamp(nanoseconds / 1_000_000_000,
                                      tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def load_history(workspace: Path, selected_run: Path,
                 envelope: dict[str, Any] | None) -> RunHistory:
    """Collect source-matched snapshots and distinct retained CLI receipts.

    Identical copied snapshots/receipts share a row. We deliberately do not add
    settlement round counts: a later snapshot may contain earlier rounds again.
    Session totals are receipt totals, not elapsed time or a claim of all work.
    """
    history = RunHistory()
    runs = workspace / "runs"
    source_key = _source_key((envelope or {}).get("source"), workspace)
    snapshots: dict[str, FindingsSnapshot] = {}
    if source_key:
        for path in sorted(runs.rglob("findings.json")):
            env = _load(path)
            if not env or _source_key(env.get("source"), workspace) != source_key:
                continue
            # A matching filename is not enough when the artifacts explicitly
            # identify different source revisions.
            wanted_hash = (envelope or {}).get("source_sha256")
            recorded_hash = env.get("source_sha256")
            if wanted_hash and recorded_hash and wanted_hash != recorded_hash:
                continue
            settlement = _load(path.parent / "settlement.json")
            key = _fingerprint([env, settlement])
            label = str(path.parent.relative_to(workspace))
            selected = path.parent.resolve() == selected_run.resolve()
            if key in snapshots:
                snapshots[key].paths.append(label)
                snapshots[key].selected |= selected
                continue
            raw_rows = env.get("findings")
            if not isinstance(raw_rows, list):
                continue
            rows = [r for r in raw_rows if isinstance(r, dict)]
            records = (settlement or {}).get("records")
            rounds = _count((settlement or {}).get("rounds"))
            snapshots[key] = FindingsSnapshot(
                paths=[label], generated_at=str(env.get("generated_at") or ""),
                applied=sum(r.get("applied") is True for r in rows),
                queries=sum(r.get("queried") is True for r in rows),
                rounds=rounds,
                decisions=len(records) if isinstance(records, list) else None,
                selected=selected,
            )
    history.snapshots = sorted(snapshots.values(),
                               key=lambda item: (item.generated_at, item.paths))

    sessions: dict[str, SessionReceipt] = {}
    accounted_streams: set[Path] = set()
    budget_path = runs / "driver" / "execution-budget.json"
    budget = _load(budget_path) or {}
    resources = _coordinator_resources(runs / "driver" / "resources.jsonl")
    wanted_hash = (envelope or {}).get("source_sha256")
    matching_source = (not wanted_hash or not budget.get("source_sha256")
                       or wanted_hash == budget["source_sha256"])
    attempts = budget.get("attempts")
    for attempt in (attempts if matching_source and isinstance(attempts, list) else []):
        if not isinstance(attempt, dict) or not attempt.get("id"):
            continue
        key = "execution:" + str(attempt["id"])
        if key in sessions:
            continue
        log = Path(str(attempt.get("log") or ""))
        if not log.is_absolute():
            log = workspace / log
        stream_path = log.with_suffix(".stream.jsonl")
        accounted_streams.add(stream_path.resolve())
        result = _last_result(stream_path)
        metadata = resources.get(str(attempt["id"])) or {}
        phase = str(attempt.get("phase") or "")
        status = str(attempt.get("status") or "status not recorded")
        completed = status in {"completed", "recovered"}
        # Running rows charge their full reservation as a guard against fresh
        # allowances on restart. Those numbers are not measured work.
        elapsed = _number(attempt.get("seconds")) if completed else None
        # Even completed row.turns may be a charged ceiling: the driver uses
        # the remaining allowance when the coordinator omitted actual usage.
        # Read the native completion measurement instead.
        turns = _count((result or {}).get("num_turns"))
        if turns is None:
            turns = _count(metadata.get("num_turns"))
        if phase.startswith("code-"):
            turns = None  # A code operation has no coordinator-agent turns.
        try:
            label = str(log.relative_to(workspace))
        except ValueError:
            label = str(log)
        sessions[key] = SessionReceipt(
            paths=[f"runs/driver/execution-budget.json#{attempt['id']}", label],
            duration_ms=elapsed * 1000 if elapsed is not None else None,
            num_turns=turns, status=status, phase=phase,
            started_at=_reservation_timestamp(attempt.get("reserved_at_ns")),
            timing_source="CLI result (reconciled)" if status == "recovered"
                          else "execution ledger clock",
        )
    for path in sorted((runs / "driver").rglob("*.attempt.json")):
        attempt = _load(path)
        if not attempt or not attempt.get("attempt_id"):
            continue
        stream_name = attempt.get("stream")
        if isinstance(stream_name, str) and stream_name:
            accounted_streams.add((path.parent / stream_name).resolve())
        key = "attempt:" + str(attempt["attempt_id"])
        label = str(path.relative_to(workspace))
        if key in sessions:
            sessions[key].paths.append(label)
            continue
        elapsed = _number(attempt.get("elapsed_s"))
        turns = _count(attempt.get("num_turns"))
        status = str(attempt.get("status") or "status not recorded")
        if attempt.get("limit"):
            status += f" ({attempt['limit']})"
        elif attempt.get("returncode"):
            status += f" (exit {attempt['returncode']})"
        sessions[key] = SessionReceipt(
            paths=[label],
            duration_ms=elapsed * 1000 if elapsed is not None else None,
            num_turns=turns,
            status=status, phase=str(attempt.get("phase") or ""),
            started_at=str(attempt.get("started_at") or ""),
            timing_source="driver clock",
        )
    # Archived/copied streams can sit at a second path without an accompanying
    # copied attempt receipt. Deduplicate their completion event as well.
    accounted_results = {_result_key(result) for path in accounted_streams
                         if (result := _last_result(path)) is not None}
    for path in sorted((runs / "driver").rglob("*.stream.jsonl")):
        if path.resolve() in accounted_streams:
            continue
        result = _last_result(path)
        label = str(path.relative_to(workspace))
        if result is None:
            history.streams_without_result.append(label)
            continue
        # The same result is commonly copied into a recovery/archive folder.
        # A result UUID identifies an event, not just the reusable session.
        key = _result_key(result)
        if key in accounted_results:
            continue
        if key in sessions:
            sessions[key].paths.append(label)
            continue
        turns = _count(result.get("num_turns"))
        sessions[key] = SessionReceipt(
            paths=[label], duration_ms=_number(result.get("duration_ms")),
            num_turns=turns,
            status=str(result.get("subtype") or "status not recorded"),
            phase=path.name.removesuffix(".stream.jsonl"),
            started_at=_legacy_started_at(path),
        )
    history.sessions = sorted(sessions.values(),
                              key=lambda item: (item.started_at, item.paths))
    return history
