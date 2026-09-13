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
import sys
import os
import signal

from docproof.utils.files import write_atomic


class ExecutionBudgetError(ValueError):
    pass


class ActiveExecutionError(ExecutionBudgetError):
    def __init__(self, phase, key, *, known_active, remaining_seconds):
        super().__init__(f"{phase}: an active or unknown process owns this operation")
        self.phase, self.key = phase, key
        self.known_active = known_active
        self.remaining_seconds = remaining_seconds


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
        self.reconcile()
        with self._locked() as data:
            limits = self._limits(data, phase, turns, seconds)
            used_turns, used_seconds = self._totals(data, phase)
            return limits["turns"] - used_turns, limits["seconds"] - used_seconds

    def reserve(self, phase: str, turns: int, seconds: float, *, log_path: Path):
        from galley.process_receipt import clock_identity, process_identity
        self.reconcile()
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
            if Path(log_path).with_suffix(".process.json").exists():
                raise ExecutionBudgetError("Execution requires a fresh unique process receipt")
            key = uuid.uuid4().hex
            data["attempts"].append({"id": key, "phase": phase,
                "status": "running", "turns": left_turns, "seconds": left_seconds,
                "reserved_turns": left_turns, "reserved_seconds": left_seconds,
                "reserved_at_ns": time.time_ns(),
                "reserved_monotonic_ns": time.monotonic_ns(),
                "clock_id": clock_identity(), "owner": process_identity(),
                "log": str(Path(log_path).resolve())})
            return key, left_turns, left_seconds

    @staticmethod
    def _active(row):
        from galley.process_receipt import process_alive
        identities = [row.get("owner")]
        if row.get("child"):
            identities.append(row["child"])
        states = [process_alive(identity) for identity in identities]
        if True in states:
            return True
        return None if None in states else False

    def active(self, key: str) -> bool | None:
        """Whether a reservation still has a live owner/command (None=unknown).

        A missing process identity never proves death. Completed reservations
        return False; active or unidentifiable attempts must not be relaunched.
        """
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if row is None:
                raise ExecutionBudgetError("Unknown execution reservation")
            return self._active(row) if row["status"] == "running" else False

    def assert_available(self, phase: str) -> None:
        """Reconcile dead attempts before an engine changes its operation receipt."""
        self.reconcile()
        with self._locked() as data:
            for row in data["attempts"]:
                if row["phase"] != phase or row["status"] != "running":
                    continue
                active = self._active(row)
                remaining = None
                # A matching live kernel process cannot span a reboot, so its
                # monotonic reservation timestamp remains comparable even on
                # an OS that will not expose a separate boot identifier.
                if active is True and type(row.get("reserved_monotonic_ns")) is int:
                    elapsed = (time.monotonic_ns() - row["reserved_monotonic_ns"]) / 1e9
                    if elapsed >= 0:
                        remaining = max(0.0, row["reserved_seconds"] - elapsed)
                raise ActiveExecutionError(phase, row["id"],
                    known_active=active is True, remaining_seconds=remaining)

    def wait_available(self, phase: str, *, sleep=time.sleep, on_wait=None):
        """Rejoin known-live work within its original deadline, without reserving.

        No recovery attempt or model call is made. Unknown ownership remains a
        block; an existing process reaching its own ceiling remains a real cap.
        ``on_wait`` receives the active-operation exception once per attempt.
        """
        announced = set()
        while True:
            try:
                self.assert_available(phase)
                return
            except ActiveExecutionError as exc:
                if not exc.known_active or exc.remaining_seconds is None:
                    raise
                if exc.remaining_seconds <= 0:
                    raise ExecutionBudgetError(
                        f"{phase}: existing operation reached its reserved deadline") from exc
                if exc.key not in announced:
                    announced.add(exc.key)
                    if on_wait is not None:
                        on_wait(exc)
                sleep(min(1.0, exc.remaining_seconds))

    def terminate_process(self, key: str) -> None:
        """Stop a registered command on explicit driver timeout/cancellation.

        Call in the actual subprocess executor's finally block, before finish
        or reconcile. A coordinator crash never calls this: its supervisor can
        still finish useful work. The stop flag closes the delayed-child race.
        """
        from galley.process_receipt import process_alive, process_identity
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if row is None:
                raise ExecutionBudgetError("Unknown execution reservation")
            if row.get("process_protocol") != 1:
                return
            row["stop_requested"] = True
            child = row.get("child")
        # Persist the closed launch gate before signals or any operation that
        # can fail. A late child then refuses to exec even if this caller dies.
        if not child:
            return
        current = process_identity(child["pid"])
        if (child.get("birth") and current.get("birth") and
                current["birth"] != child["birth"]):
            return  # The PID now belongs to someone else.
        if process_alive(child) is False and not current.get("dead"):
            return
        if not child.get("birth") or child.get("birth") != current.get("birth"):
            raise ExecutionBudgetError("Cannot safely identify the active command to terminate")
        try:
            if os.getpgid(child["pid"]) != child["pid"]:
                raise ExecutionBudgetError("Command is no longer in its own process group")
            os.killpg(child["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass

    def command_argv(self, key: str, argv: list[str]) -> list[str]:
        """Wrap a code-only command in the independent receipt supervisor.

        Call after reserve(), before Popen. Reconciliation can safely close a
        dead coordinator's unlaunched reservation: the supervisor and command
        both check its live status under the budget lock before doing work.
        Windows retains its existing native job containment and parent receipt.
        """
        from galley.process_receipt import command_hash
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if row is None or row["status"] != "running" or row["reserved_turns"]:
                raise ExecutionBudgetError("Command requires a running code-only reservation")
            if os.name == "nt":
                return list(argv)
            fingerprint = command_hash(argv)
            if row.get("command_sha256") not in (None, fingerprint):
                raise ExecutionBudgetError("Command differs from its reserved operation")
            row.update(process_protocol=1, command_sha256=fingerprint)
        return [sys.executable, "-m", "galley.process_receipt", "--budget",
                str(self.path.resolve()), "--source", self.source_sha256,
                "--key", key, "--", *argv]

    def claim_process(self, key: str, argv: list[str], *, child: bool = False):
        from galley.process_receipt import command_hash, process_identity
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if (row is None or row["status"] != "running" or row.get("stop_requested") or
                    row.get("process_protocol") != 1 or
                    row.get("command_sha256") != command_hash(argv)):
                raise ExecutionBudgetError("Command reservation is closed or does not match")
            if child:
                if not row.get("supervisor_claimed") or row.get("child"):
                    raise ExecutionBudgetError("Command child has no unique supervisor claim")
                row["child"] = process_identity()
            else:
                if row.get("supervisor_claimed"):
                    raise ExecutionBudgetError("Command supervisor already claimed this reservation")
                row.update(owner=process_identity(), supervisor_claimed=True,
                           process_started_at_ns=time.time_ns())
            return dict(row)

    def complete_process(self, key: str, *, seconds: float, returncode: int,
                         limit: str | None = None):
        """Save completion before releasing the reservation, independently of parent."""
        if not math.isfinite(seconds) or seconds < 0 or type(returncode) is not int:
            raise ExecutionBudgetError("Invalid process completion")
        with self._locked() as data:
            row = next((r for r in data["attempts"] if r["id"] == key), None)
            if row is None or row.get("process_protocol") != 1:
                raise ExecutionBudgetError("Unknown command reservation")
            if row["status"] != "running":
                return
            # Called by the supervisor after wait() has reaped its command.
            # That exit receipt is stronger than probing a now-reused PID or
            # asking an OS sandbox that may deny process inspection.
            receipt = {"schema_version": 1, "reservation_id": key,
                "source_sha256": self.source_sha256,
                "command_sha256": row["command_sha256"],
                "seconds": seconds, "returncode": returncode, "limit": limit,
                "finished_at_ns": time.time_ns()}
            write_atomic(Path(row["log"]).with_suffix(".process.json"),
                         json.dumps(receipt, indent=2, allow_nan=False))
            row.update(status="completed", turns=0, seconds=seconds,
                       usage_known=True, completion=receipt)

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

    def reconcile(self, parse_result=None):
        """Recover measured completions, or charge a dead code owner's upper bound.

        This never releases unmeasured model turns or changes a spending ledger.
        A dead code command without an exit receipt is abandoned, not successful;
        its output/checkpoint still needs the engine's normal validation.
        """
        from galley.process_receipt import clock_identity
        with self._locked() as data:
            for row in data["attempts"]:
                if row["status"] != "running":
                    continue
                if row.get("process_protocol") == 1:
                    receipt_path = Path(row["log"]).with_suffix(".process.json")
                    try:
                        completion = json.loads(receipt_path.read_text("utf-8"))
                    except (OSError, ValueError):
                        completion = None
                    if (isinstance(completion, dict) and
                            completion.get("schema_version") == 1 and
                            completion.get("reservation_id") == row["id"] and
                            completion.get("source_sha256") == self.source_sha256 and
                            completion.get("command_sha256") == row.get("command_sha256") and
                            type(completion.get("returncode")) is int and
                            type(completion.get("seconds")) in (int, float) and
                            math.isfinite(completion["seconds"]) and completion["seconds"] >= 0 and
                            type(completion.get("finished_at_ns")) is int and
                            completion["finished_at_ns"] >= row.get("reserved_at_ns", 0)):
                        row.update(status="recovered", turns=0,
                                   seconds=completion["seconds"], usage_known=True,
                                   completion=completion)
                        continue
                if row["reserved_turns"] == 0 and self._active(row) is False:
                    # A monotonic interval on the same boot bounds all possible
                    # command time, including an unrecorded exit. Downtime on a
                    # different boot gives no such bound, so keep the ceiling.
                    elapsed = row["reserved_seconds"]
                    if (row.get("clock_id") and row["clock_id"] == clock_identity()
                            and type(row.get("reserved_monotonic_ns")) is int):
                        bound = (time.monotonic_ns() - row["reserved_monotonic_ns"]) / 1e9
                        if bound >= 0:
                            elapsed = min(elapsed, bound)
                    row.update(status="abandoned", turns=0, seconds=elapsed,
                               usage_known=True, completion_known=False,
                               reconciled_at_ns=time.time_ns())
                    continue
                if row["reserved_turns"] == 0:
                    continue  # A nested Claude event is not command completion.
                if parse_result is None:
                    continue
                path = Path(row["log"]).with_suffix(".stream.jsonl")
                if (not path.is_file() or not row.get("reserved_at_ns")
                        or path.stat().st_mtime_ns < row["reserved_at_ns"]):
                    continue
                try:
                    result = parse_result(path.read_text("utf-8", errors="replace"))
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(result, dict):
                    continue
                turns, ms = result.get("num_turns"), result.get("duration_ms")
                if (type(turns) is not int or turns < 0 or
                        not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0):
                    continue
                row.update(status="recovered", turns=turns, seconds=ms / 1000,
                           usage_known=True)
