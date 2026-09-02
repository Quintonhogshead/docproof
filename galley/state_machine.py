"""The resumable run state machine — explicit states, verified on resume.

A Galley run moves through named states: intake → profiled → plan_approved →
mechanical_complete → copyedit_complete → audited → certified → delivered.
Recording them explicitly (in ``state.json`` beside the run) lets a resumed
session VERIFY where it is from artifacts and hashes rather than trusting a
session instruction that says "the mechanical wave is done." Resume recomputes
the source and config hashes and re-hashes every recorded artifact; if anything
changed since the state was written, ``verify_resume`` reports the mismatch and
the caller stops rather than building on a moved foundation.

Two rules, matching galley/casefile.py:

* **Forward only.** A state may re-enter itself (idempotent) or advance to a
  LATER state; it can never move backward. History is append-only.
* **Hash-anchored.** Every transition stamps the source and config hashes it was
  made against, plus the artifacts it produced, so a later state can prove the
  inputs have not changed underneath it.

Timestamps are supplied by the caller (never read from a clock), so a
reconstructed machine is deterministic and testable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

STATE_SCHEMA_VERSION = 1

# The ordered run states. Index in this tuple is the ordering used for the
# forward-only rule; a run need not touch every one (a proofread-only job skips
# copyedit_complete).
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
        """Append a transition to ``to_state``. Refuses a move to an earlier
        state (forward-only); re-entering the current state is allowed (an
        idempotent re-run of a stage). Returns the appended record."""
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
        """Check that the run can safely resume from its current state. Returns
        a list of human-readable mismatches; an empty list means it is safe to
        continue. Compares the CURRENT source/config hashes (recompute them and
        pass them in) against EVERY recorded state that stamped one — not only
        the latest, so an early wave's hash still anchors a run whose later
        stages were advanced without one — and, when an
        ``artifact_hasher(path) -> sha256`` is given, re-hashes every recorded
        artifact to confirm none changed or vanished.

        A hash present on one side and absent on the other is a mismatch, not
        a vacuous pass: a resume that supplies a hash no stage ever stamped has
        nothing to prove the inputs against (re-advance with --source/--config),
        and a resume that supplies none against a stamped stage is declining to
        check. Only when neither side carries a hash is there nothing to say."""
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
            for art in last.artifacts:
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
