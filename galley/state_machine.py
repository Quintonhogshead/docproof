"""Persist forward-only run transitions and verify recorded inputs and
artifacts on resume. Timestamps are supplied by callers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

STATE_SCHEMA_VERSION = 1

# Transitions follow this order; a run may skip optional stages.
RUN_STATES = (
    "intake",
    "profiled",
    "plan_approved",
    "mechanical_complete",
    "copyedit_complete",
    "audited",
    "settled",
    "certified",
    "delivered",
)
_ORDER = {s: i for i, s in enumerate(RUN_STATES)}


class ArtifactHash(BaseModel):
    path: str
    sha256: str


class RunStateRecord(BaseModel):
    state: str
    at: str = ""
    by: str = ""
    note: str = ""
    source_sha256: str = ""
    config_sha256: str = ""
    artifacts: list[ArtifactHash] = Field(default_factory=list)


class StateError(RuntimeError):
    """A transition that would move backward, or to an unknown state."""


class RunStateMachine(BaseModel):
    state_schema_version: int = STATE_SCHEMA_VERSION
    history: list[RunStateRecord] = Field(default_factory=list)
    # Source identity recorded by seed_workspace. Changed content requires
    # an explicit revision transition.
    source_sha256: str = ""
    source_id: str = ""
    source_name: str = ""
    revision: int = 1

    @property
    def current(self) -> str:
        return self.history[-1].state if self.history else ""

    def _index(self, state: str) -> int:
        if state not in _ORDER:
            raise StateError(f"unknown run state {state!r}; expected one of "
                             f"{RUN_STATES}")
        return _ORDER[state]

    def advance(self, to_state: str, *, at: str = "", by: str = "",
                note: str = "", source_sha256: str = "",
                config_sha256: str = "",
                artifacts: list[ArtifactHash] | None = None) -> RunStateRecord:
        """Append a transition and return it. Allow the current state or a
        later state; reject backward and unknown transitions.
        """
        target = self._index(to_state)
        if self.history and target < self._index(self.current):
            raise StateError(
                f"cannot move backward from {self.current!r} to {to_state!r}; "
                f"the state machine is forward-only")
        rec = RunStateRecord(state=to_state, at=at, by=by, note=note,
                             source_sha256=source_sha256,
                             config_sha256=config_sha256,
                             artifacts=list(artifacts or []))
        self.history.append(rec)
        return rec

    def reached(self, state: str) -> bool:
        """Whether the run has reached at least ``state``."""
        if not self.history:
            return False
        return self._index(self.current) >= self._index(state)

    def verify_resume(self, *, source_sha256: str = "",
                      config_sha256: str = "",
                      artifact_hasher=None) -> list[str]:
        """Return mismatches against recorded source/config hashes and artifact
        hashes.

        Compare inputs with all stamped transitions. A hash supplied on only
        one side is a mismatch. When artifact_hasher is supplied, check
        every recorded path against its latest hash, allowing later stages
        to replace earlier versions.
        """
        if not self.history:
            return ["no recorded state to resume from"]
        last = self.history[-1]
        out: list[str] = []
        for label, current, attr in (("source", source_sha256, "source_sha256"),
                                     ("config", config_sha256, "config_sha256")):
            stamped = [r for r in self.history if getattr(r, attr)]
            if current and not stamped:
                out.append(
                    f"no {label} hash was stamped at {last.state!r}; "
                    f"re-advance with --source/--config")
                continue
            if stamped and not current:
                out.append(
                    f"a {label} hash was stamped at {stamped[-1].state!r} but "
                    f"resume supplied none to compare; recompute it and pass "
                    f"--source/--config")
                continue
            seen: set[str] = set()
            for r in stamped:
                recorded = getattr(r, attr)
                if recorded == current or recorded in seen:
                    continue
                seen.add(recorded)
                out.append(
                    f"{label} changed since {r.state!r}: now {current[:12]}"
                    f"…, recorded {recorded[:12]}…")
        if artifact_hasher is not None:
            # A later stage may replace an artifact at the same path.
            artifacts = {art.path: art for record in self.history
                         for art in record.artifacts}
            for art in artifacts.values():
                try:
                    now = artifact_hasher(art.path)
                except OSError:
                    out.append(f"artifact missing: {art.path}")
                    continue
                if now != art.sha256:
                    out.append(f"artifact changed: {art.path}")
        return out

    def save(self, path: str | Path) -> None:
        from docproof.utils.files import write_atomic
        write_atomic(Path(path), self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "RunStateMachine":
        return cls.model_validate_json(
            Path(path).read_text(encoding="utf-8"))


def hash_artifact(path: str | Path) -> str:
    """sha256 of a file — the ``artifact_hasher`` verify_resume expects."""
    from galley.manifest import sha256_file
    return sha256_file(path)


__all__ = [
    "STATE_SCHEMA_VERSION", "RUN_STATES", "ArtifactHash", "RunStateRecord",
    "RunStateMachine", "StateError", "hash_artifact",
]
