"""Durable reservations for coordinator turns and executable review operations.

A crash does not grant a fresh allowance. An unfinished reservation remains
charged at its ceiling until its actual completion receipt is reconciled.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import uuid
import time

from docproof.utils.files import write_atomic


class ExecutionBudgetError(ValueError):
    pass


class ExecutionBudget:
    def __init__(self, path: Path, source_sha256: str):
        self.path = Path(path)
        self.source_sha256 = source_sha256
        if not source_sha256:
            raise ExecutionBudgetError("Execution budget requires a source identity")

    @contextmanager
    def _locked(self):
        from docproof.platform_io import flock, LOCK_EX, LOCK_UN
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            flock(lock, LOCK_EX)
            if self.path.exists():
                data = json.loads(self.path.read_text("utf-8"))
                if (data.get("schema_version") != 1 or
                        data.get("source_sha256") != self.source_sha256):
                    raise ExecutionBudgetError("Execution budget belongs to another source revision")
            else:
                data = {"schema_version": 1, "source_sha256": self.source_sha256,
                        "limits": {}, "attempts": []}
            try:
                yield data
                write_atomic(self.path, json.dumps(data, indent=2, allow_nan=False))
            finally:
                flock(lock, LOCK_UN)

    @staticmethod
    def _totals(data, phase):
        rows = [r for r in data["attempts"] if r["phase"] == phase]
        return (sum(r["turns"] for r in rows), sum(r["seconds"] for r in rows))

    @staticmethod
    def _grants(data, phase):
        return [g for g in data.get("grants", []) if g["phase"] == phase]

    @classmethod
    def _limits(cls, data, phase, turns, seconds):
        """The phase's effective ceiling: the original cap (never enlarged by
        a later, more generous request) plus every continuation grant."""
        if (type(turns) is not int or turns < 0 or not math.isfinite(seconds)
                or seconds <= 0):
            raise ExecutionBudgetError("Invalid execution budget")
        old = data["limits"].get(phase, {"turns": turns, "seconds": seconds})
        if bool(turns) != bool(old["turns"]):
            raise ExecutionBudgetError("Cannot change the turn-budget mode on resume")
        base = {"turns": min(turns, old["turns"]),
                "seconds": min(seconds, old["seconds"])}
        data["limits"][phase] = base
        grants = cls._grants(data, phase)
        return {"turns": base["turns"] + sum(g["turns"] for g in grants),
                "seconds": base["seconds"] + sum(g["seconds"] for g in grants)}

    def grants(self, phase: str) -> int:
        """How many continuation grants the phase has already received."""
        with self._locked() as data:
            return len(self._grants(data, phase))

    def extend(self, phase: str, turns: int, seconds: float, *, reason: str,
               max_grants: int) -> int:
        """Grant a bounded continuation on top of the phase's ceiling.

        A session that ran out of turns or time while measurably advancing
        the book is not a failure; it is unfinished work. The grant is
        durable, so a resumed run cannot be granted again at every poll:
        after ``max_grants`` the phase is exhausted for good."""
        if (type(turns) is not int or turns < 0 or not math.isfinite(seconds)
                or seconds < 0 or (turns <= 0 and seconds <= 0)):
            raise ExecutionBudgetError("Invalid continuation grant")
        with self._locked() as data:
            grants = self._grants(data, phase)
            if len(grants) >= max_grants:
                raise ExecutionBudgetError(
                    f"{phase}: continuation grants exhausted ({len(grants)} of {max_grants})")
            data.setdefault("grants", []).append({
                "phase": phase, "turns": turns, "seconds": seconds,
                "reason": reason[:400], "granted_at_ns": time.time_ns()})
            return len(grants) + 1

    def remaining(self, phase: str, turns: int, seconds: float):
        with self._locked() as data:
            limits = self._limits(data, phase, turns, seconds)
            used_turns, used_seconds = self._totals(data, phase)
            return limits["turns"] - used_turns, limits["seconds"] - used_seconds

    def reserve(self, phase: str, turns: int, seconds: float, *, log_path: Path):
        with self._locked() as data:
            limits = self._limits(data, phase, turns, seconds)
            if any(r["phase"] == phase and r["status"] == "running"
                   for r in data["attempts"]):
                raise ExecutionBudgetError(
                    f"{phase}: an interrupted or active operation needs receipt reconciliation")
            used_turns, used_seconds = self._totals(data, phase)
            left_turns = limits["turns"] - used_turns
            left_seconds = limits["seconds"] - used_seconds
            if left_seconds <= 0 or (turns and left_turns <= 0):
                raise ExecutionBudgetError(f"{phase}: durable execution budget exhausted")
            if Path(log_path).with_suffix(".stream.jsonl").exists():
                raise ExecutionBudgetError("Execution requires a fresh unique stream path")
            key = uuid.uuid4().hex
            data["attempts"].append({"id": key, "phase": phase,
                "status": "running", "turns": left_turns, "seconds": left_seconds,
                "reserved_turns": left_turns, "reserved_seconds": left_seconds,
                "reserved_at_ns": time.time_ns(), "log": str(log_path)})
            return key, left_turns, left_seconds

    def finish(self, key: str, *, turns: int | None, seconds: float,
               status: str):
        if not math.isfinite(seconds) or seconds < 0:
            raise ExecutionBudgetError("Invalid elapsed time")
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if row is None:
                raise ExecutionBudgetError("Unknown execution reservation")
            if row["status"] != "running":
                return
            row.update(status=status,
                       turns=turns if type(turns) is int and turns >= 0 else row["turns"],
                       seconds=seconds, usage_known=turns is not None)

    def reconcile(self, parse_result):
        """Recover completed coordinator attempts after the parent was interrupted."""
        with self._locked() as data:
            for row in data["attempts"]:
                if row["status"] != "running":
                    continue
                path = Path(row["log"]).with_suffix(".stream.jsonl")
                if (not path.is_file() or not row.get("reserved_at_ns")
                        or path.stat().st_mtime_ns < row["reserved_at_ns"]):
                    continue
                result = parse_result(path.read_text("utf-8", errors="replace"))
                if not result:
                    continue
                turns, ms = result.get("num_turns"), result.get("duration_ms")
                if (type(turns) is not int or turns < 0 or
                        not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0):
                    continue
                row.update(status="recovered", turns=turns, seconds=ms / 1000,
                           usage_known=True)
