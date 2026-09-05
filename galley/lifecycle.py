"""Append-only lifecycle history for findings.

Each finding gets a content-derived stable key and append-only history of states:

    detected → verified → held → promoted / rejected → queried → merged →
    delivered  (or dropped)

Each transition records its wave, actor, and note. Timestamps are caller-supplied
for deterministic reconstruction; the ledger complements the case file.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from galley.contracts import _known

LEDGER_SCHEMA_VERSION = 1

# The states a finding moves through. Ordered roughly by progression; a finding
# need not touch every one (a rejected finding never merges).
LIFECYCLE_STATES = (
    "detected",     # a detector/sweep raised it
    "verified",     # an ensemble verifier / gate affirmed it
    "held",         # parked (low confidence, awaiting a later wave)
    "promoted",     # accepted for application
    "rejected",     # ruled a false positive / declined
    "queried",      # routed to the author as a margin question, not an edit
    "merged",       # written into the tracked-changes document
    "delivered",    # shipped in the deliverable the author received
    "dropped",      # discarded (deduped away, overflow, artifact)
)

# How a Finding.status (docproof/models.py STATUSES) maps to a terminal
# lifecycle state when a ledger is RECONSTRUCTED from a finished run.
_STATUS_TO_STATE = {
    "pending": "detected",
    "validated": "merged",
    "query": "queried",
    "rejected_no_anchor": "rejected",
    "rejected_overlap": "dropped",
    "rejected_duplicate": "dropped",
    "rejected_noop": "dropped",
    "rejected_oversized": "rejected",
    "rejected_by_verifier": "rejected",
    "skipped_low_confidence": "held",
}


def stable_key(para_id: str, original_text: str, error_type: str) -> str:
    """A content-derived key for a finding, stable across waves and runs: the
    same (paragraph, original text, error type) hashes to the same key, so two
    waves raising the same finding share one lifecycle and a duplicate is
    visible. Whitespace in the original is collapsed so a trivial re-quote does
    not fork the identity."""
    norm = " ".join((original_text or "").split())
    blob = f"{para_id}\x1f{norm}\x1f{error_type}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LifecycleEvent:
    state: str
    wave: int = 0
    by: str = ""            # the lane/detector/judge that moved it
    note: str = ""
    at: str = ""            # optional caller-supplied timestamp (never a clock)

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "wave": self.wave, "by": self.by,
                "note": self.note, "at": self.at}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "LifecycleEvent":
        d = _known(cls, data)
        return cls(state=str(d.get("state", "")), wave=int(d.get("wave", 0)),
                   by=str(d.get("by", "")), note=str(d.get("note", "")),
                   at=str(d.get("at", "")))


@dataclass
class FindingLifecycle:
    finding_id: str
    key: str = ""
    events: list[LifecycleEvent] = field(default_factory=list)

    @property
    def state(self) -> str:
        return self.events[-1].state if self.events else ""

    def to_json(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, "key": self.key,
                "state": self.state,
                "events": [e.to_json() for e in self.events]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FindingLifecycle":
        d = _known(cls, data)
        return cls(finding_id=str(d.get("finding_id", "")),
                   key=str(d.get("key", "")),
                   events=[LifecycleEvent.from_json(e)
                           for e in (d.get("events", []) or [])])


class Ledger:
    """An append-only lifecycle store keyed by finding id."""

    def __init__(self) -> None:
        self._by_id: dict[str, FindingLifecycle] = {}

    def record(self, finding_id: str, state: str, *, key: str = "",
               wave: int = 0, by: str = "", note: str = "", at: str = "") -> None:
        """Append a state transition for `finding_id`, creating its lifecycle on
        first sight. A state outside LIFECYCLE_STATES is refused (a typo must not
        become a silent new state). Recording the SAME state twice in a row is a
        no-op, so an idempotent re-run does not bloat the history."""
        if state not in LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state {state!r}; expected one "
                             f"of {LIFECYCLE_STATES}")
        lc = self._by_id.get(finding_id)
        if lc is None:
            lc = FindingLifecycle(finding_id=finding_id, key=key)
            self._by_id[finding_id] = lc
        elif key and not lc.key:
            lc.key = key
        if lc.events and lc.events[-1].state == state and \
                lc.events[-1].wave == wave:
            return
        lc.events.append(LifecycleEvent(state=state, wave=wave, by=by,
                                        note=note, at=at))

    def history(self, finding_id: str) -> FindingLifecycle | None:
        return self._by_id.get(finding_id)

    def state_of(self, finding_id: str) -> str:
        lc = self._by_id.get(finding_id)
        return lc.state if lc else ""

    def by_state(self) -> dict[str, int]:
        """A histogram of current states across every finding."""
        out: dict[str, int] = {}
        for lc in self._by_id.values():
            out[lc.state] = out.get(lc.state, 0) + 1
        return out

    def duplicates(self) -> dict[str, list[str]]:
        """Stable keys carried by more than one finding id — the duplicate
        report. An empty dict means every finding is unique by content."""
        by_key: dict[str, list[str]] = {}
        for lc in self._by_id.values():
            if lc.key:
                by_key.setdefault(lc.key, []).append(lc.finding_id)
        return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}

    def __len__(self) -> int:
        return len(self._by_id)

    def to_json(self) -> dict[str, Any]:
        return {"ledger_schema_version": LEDGER_SCHEMA_VERSION,
                "findings": [lc.to_json() for lc in self._by_id.values()]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Ledger":
        led = cls()
        for row in (data.get("findings", []) or []):
            lc = FindingLifecycle.from_json(row)
            led._by_id[lc.finding_id] = lc
        return led

    def save(self, path: str | Path) -> None:
        from docproof.utils.files import write_atomic
        write_atomic(Path(path),
                     json.dumps(self.to_json(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def reconstruct_from_findings(envelope: dict[str, Any], *, wave: int = 1,
                              by: str = "") -> Ledger:
    """Build a ledger from a finished run's findings.json envelope: every
    finding is recorded ``detected`` then moved to the terminal state its
    ``status``/``force_query`` implies (a validated finding → merged, a query →
    queried, a rejection → rejected/dropped). The stable key groups any
    content-duplicates so ``duplicates()`` and ``by_state()`` are populated
    immediately."""
    led = Ledger()
    rows = envelope.get("findings", []) if isinstance(envelope, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("finding_id", "")) or str(row.get("id", ""))
        if not fid:
            continue
        key = stable_key(str(row.get("para_id", "")),
                         str(row.get("original_text", row.get("find", ""))),
                         str(row.get("error_type", "")))
        detector = by or str(row.get("detector", "") or row.get("lane", ""))
        led.record(fid, "detected", key=key, wave=wave, by=detector)
        status = str(row.get("status", "") or "")
        if row.get("force_query") and status not in ("query",):
            led.record(fid, "queried", key=key, wave=wave, by=detector,
                       note="force_query")
            continue
        terminal = _STATUS_TO_STATE.get(status)
        if terminal and terminal != "detected":
            led.record(fid, terminal, key=key, wave=wave, by=detector,
                       note=status)
    return led


__all__ = [
    "LEDGER_SCHEMA_VERSION", "LIFECYCLE_STATES", "LifecycleEvent",
    "FindingLifecycle", "Ledger", "stable_key", "reconstruct_from_findings",
]
