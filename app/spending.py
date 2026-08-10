"""A standing record of what finished jobs cost, so clearing them keeps the bill.

Spending is otherwise pure arithmetic over live Job records (see `app.usage`):
the moment a job's folder is deleted its contribution to every total vanishes
with it. This ledger is the one place a job's cost outlives its folder. A
snapshot is taken just before a terminal job is removed, holds exactly the
fields `build_usage` reads off a job, and is fed back into `build_usage`
alongside the live jobs it no longer sits among.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("docproof.app.spending")


@dataclass(frozen=True)
class LedgerEntry:
    """A finished job reduced to what `build_usage` reads off it.

    Attributes, not a dict: `build_usage` reaches for `job.model`, `job.state`
    and the rest directly, so a snapshot has to answer the same way a Job does.
    `state` is always "done" so the row always counts. `cost` is final —
    computed while the results folder still existed — so nothing here re-reads
    disk. `results_dir` is empty and present only so the one usage path that
    still reaches for it can't trip over a missing attribute.
    """
    id: str
    filename: str
    kind: str
    model: str
    mode: str
    source: str
    owner_id: str
    created_at: str
    words: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    api_calls: int
    cost: float
    state: str = "done"
    results_dir: str = ""

    @classmethod
    def from_job(cls, job, numbers: dict) -> "LedgerEntry":
        """Snapshot a live job whose final `numbers` (from `usage._totals_for`)
        have already been computed against its still-present results folder."""
        return cls(
            id=job.id, filename=job.filename, kind=job.kind, model=job.model,
            mode=job.mode, source=getattr(job, "source", None) or "app",
            owner_id=getattr(job, "owner_id", ""), created_at=job.created_at,
            words=job.words or 0,
            input_tokens=numbers["input_tokens"],
            output_tokens=numbers["output_tokens"],
            cache_read_tokens=numbers["cache_read_tokens"],
            cache_write_tokens=numbers["cache_write_tokens"],
            api_calls=numbers["api_calls"], cost=numbers["cost"])


class SpendingLedger:
    """Append-only spend snapshots on disk, one JSON object per line."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, LedgerEntry]:
        """Every row, keyed by job id. A later line for the same id wins, so a
        re-recorded job reads back as one entry, not two."""
        entries: dict[str, LedgerEntry] = {}
        if not self.path.is_file():
            return entries
        known = set(LedgerEntry.__dataclass_fields__)
        for line in self.path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Skipping unreadable ledger line: %s", e)
                continue
            row = LedgerEntry(**{k: v for k, v in data.items() if k in known})
            entries[row.id] = row
        return entries

    def record(self, entry: LedgerEntry) -> None:
        """Append one snapshot. Idempotent by id: recording a job already in the
        ledger changes nothing, so clearing the same job twice never
        double-counts."""
        with self._lock:
            if entry.id in self._read():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry)) + "\n")

    def entries(self, owner_id: str | None = None) -> list[LedgerEntry]:
        """The recorded jobs, optionally only one owner's — the web build scopes
        by owner the same way `JobStore.all` does; the desktop build passes
        nothing and sees the lot."""
        rows = self._read().values()
        if owner_id is None:
            return list(rows)
        return [r for r in rows if r.owner_id == owner_id]


def merge_live(live_jobs, ledger_entries) -> list:
    """Live jobs plus every ledger row whose job no longer exists.

    A live job is authoritative — its numbers are current, a ledger row is only
    the last thing known about a job that has since been deleted — so on an id
    clash the live job wins and the stale row is dropped. Feed the result to
    `build_usage` (or sum its `cost`) to count spending that outlived its jobs.
    """
    seen = {j.id for j in live_jobs}
    return [*live_jobs, *(e for e in ledger_entries if e.id not in seen)]
