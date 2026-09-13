"""Persistent, progress-aware recovery without replenishing execution budgets."""
from __future__ import annotations

import errno
import hashlib
import json
import re
import uuid
from pathlib import Path

from docproof.utils.files import write_atomic


def transient_failure(error, _seen=None) -> bool:
    """Transport and temporary I/O failures can resume the same operation.

    Evidence, authority and spending failures are never classified by a loose
    'error' substring. A nested transport error retains its original type.
    """
    seen = set() if _seen is None else _seen
    if id(error) in seen:
        return False
    seen.add(id(error))
    text = str(error).lower()
    if any(term in text for term in (
            "budget exhausted", "budget exceeded", "wall-clock cap", "turn cap",
            "different evidence", "different source", "hash mismatch", "tampered",
            "credentials", "unauthorized", "invalid api key", "authentication",
            "usage limit", "rate limit", "another", "already owns")):
        return False
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status in (408, 425, 500, 502, 503, 504, 529):
        return True
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    if isinstance(error, OSError) and error.errno in (
            errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT, errno.ECONNRESET,
            errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE):
        return True
    if re.search(r"\b(?:temporar(?:y|ily)|connection reset|connection refused|"
                 r"connection error|connection failed|service unavailable|"
                 r"bad gateway|gateway timeout|overloaded|network error)\b", text):
        return True
    cause = getattr(error, "__cause__", None)
    return bool(cause is not None and cause is not error and transient_failure(cause, seen))


_VOLATILE = frozenset({"at", "generated_at", "updated_at", "started_at", "finished_at",
                       "elapsed_s", "duration_ms", "usage", "cost", "verification_pair_id"})


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def progress_fingerprint(workspace: Path) -> str:
    """Evidence changed, rather than a new log line or consumption receipt."""
    names = {"findings.json", "settlement.json", "finished_walk.json",
             "change_verify.json", "editmap.json", "astra-review.json"}
    paths = [workspace / name for name in ("state.json", "profile.json", "PLAN.md", "approval.json")]
    for path in (workspace / "runs").rglob("*.json"):
        if "driver" in path.relative_to(workspace).parts:
            continue
        if path.name in names or "checkpoint" in path.name:
            paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if path.suffix == ".json":
            try:
                value = json.loads(data)
                if path.name == "state.json" and isinstance(value, dict):
                    history = value.get("history", [])
                    history = history if isinstance(history, list) else []
                    value = {"source_sha256": value.get("source_sha256"),
                             "states": {r["state"]: {k: r.get(k) for k in
                                 ("source_sha256", "config_sha256", "results_run")}
                                 for r in history if isinstance(r, dict) and isinstance(r.get("state"), str)}}
                data = json.dumps(_stable(value), sort_keys=True).encode()
            except ValueError:
                pass  # A repaired parse error is real progress too.
        digest.update(str(path.relative_to(workspace)).encode())
        digest.update(data)
    return digest.hexdigest()


class RecoveryLedger:
    """Recovery stops only after repeated lack of progress or a real budget cap.

    Counts survive service restarts. Runtime caps are enforced independently by
    ExecutionBudget; this ledger cannot authorize more time, turns, or spend.
    """
    def __init__(self, workspace: Path, source_sha256: str):
        self.path = workspace / "runs/driver/recovery-policy.json"
        self.source_sha256 = source_sha256

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "source_sha256": self.source_sha256, "phases": {}}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("source_sha256") not in (None, self.source_sha256):
            raise ValueError("Recovery evidence belongs to a different source")
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("phases"), dict)
                or any(not isinstance(row, dict) or any(
                    type(row.get(k)) is not int or row[k] < 0 for k in ("stalled", "attempts"))
                    for row in data["phases"].values())):
            # This is retry bookkeeping, never reading proof or spend authority.
            # Preserve the damaged file; execution/resource caps still govern.
            self.path.rename(self.path.with_name("recovery-policy.damaged-" + uuid.uuid4().hex + ".json"))
            return {"schema_version": 1, "source_sha256": self.source_sha256,
                    "phases": {}, "recovered_bookkeeping": True}
        return data

    def failed(self, phase: str, *, before: str, after: str, reason: str,
               transient: bool = False) -> bool:
        data = self._load()
        row = data["phases"].setdefault(phase, {
            "stalled": 2 if data.get("recovered_bookkeeping") else 0, "attempts": 0})
        # New evidence since a prior invocation also gives recovery useful work.
        progressed = before != after or (row.get("progress") and row["progress"] != before)
        row["stalled"] = 0 if progressed else row["stalled"] + 1
        row["attempts"] += 1
        row.update(progress=after, reason=str(reason)[-2000:], status="recovering")
        allowed = row["stalled"] < (4 if transient else 3)
        if not allowed:
            row["status"] = "stalled"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps(data, indent=2))
        return allowed

    def complete(self, phase: str):
        data = self._load()
        if phase in data["phases"]:
            data["phases"][phase].update(status="complete", stalled=0)
            write_atomic(self.path, json.dumps(data, indent=2))
