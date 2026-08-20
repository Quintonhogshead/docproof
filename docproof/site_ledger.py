"""Append-only examination ledger with strict current-state projections."""
from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from .site_models import (ExaminationSite, LedgerEvent, LedgerState,
                          PENDING_STATES, TERMINAL_STATES, Verdict)


class LedgerInvariantError(RuntimeError):
    pass


class IncompleteVerdicts(LedgerInvariantError):
    """A judgment response did not account for exactly the submitted sites."""

    def __init__(self, *, missing=(), duplicate=(), unknown=()):
        self.missing = tuple(sorted(missing))
        self.duplicate = tuple(sorted(duplicate))
        self.unknown = tuple(sorted(unknown))
        details = []
        if self.missing:
            details.append(f"missing={','.join(self.missing)}")
        if self.duplicate:
            details.append(f"duplicate={','.join(self.duplicate)}")
        if self.unknown:
            details.append(f"unknown={','.join(self.unknown)}")
        super().__init__("incomplete verdict coverage: " + "; ".join(details))


_ALLOWED: dict[LedgerState, frozenset[LedgerState]] = {
    LedgerState.GENERATED: frozenset({
        LedgerState.LOCALLY_PASSED, LedgerState.LOCALLY_CONFIRMED,
        LedgerState.NEEDS_JUDGMENT, LedgerState.MODEL_PASSED,
        LedgerState.MODEL_CONFIRMED, LedgerState.UNCERTAIN,
        LedgerState.QUERY, LedgerState.REJECTED,
    }),
    LedgerState.LOCALLY_CONFIRMED: frozenset({
        LedgerState.NEEDS_JUDGMENT, LedgerState.MODEL_CONFIRMED,
        LedgerState.UNCERTAIN, LedgerState.EDIT, LedgerState.QUERY,
        LedgerState.REJECTED,
    }),
    LedgerState.NEEDS_JUDGMENT: frozenset({
        LedgerState.MODEL_PASSED, LedgerState.MODEL_CONFIRMED,
        LedgerState.UNCERTAIN, LedgerState.ESCALATED,
        LedgerState.QUERY, LedgerState.REJECTED,
    }),
    LedgerState.MODEL_CONFIRMED: frozenset({
        LedgerState.UNCERTAIN, LedgerState.ESCALATED, LedgerState.EDIT,
        LedgerState.QUERY,
        LedgerState.REJECTED,
    }),
    LedgerState.UNCERTAIN: frozenset({
        LedgerState.ESCALATED, LedgerState.QUERY, LedgerState.REJECTED,
    }),
    LedgerState.ESCALATED: frozenset({
        LedgerState.MODEL_PASSED, LedgerState.MODEL_CONFIRMED,
        LedgerState.UNCERTAIN, LedgerState.QUERY, LedgerState.REJECTED,
    }),
    LedgerState.EDIT: frozenset({
        LedgerState.APPLIED, LedgerState.QUERY, LedgerState.REJECTED,
    }),
    # Terminal states intentionally have no outgoing transitions.  Reconsidering
    # a decision creates a new site/event stream instead of rewriting history.
    LedgerState.LOCALLY_PASSED: frozenset(),
    LedgerState.MODEL_PASSED: frozenset(),
    LedgerState.QUERY: frozenset(),
    LedgerState.REJECTED: frozenset(),
    LedgerState.APPLIED: frozenset(),
}


class ExaminationLedger:
    """Mutable projection over an immutable sequence of ledger events."""

    def __init__(self) -> None:
        self._sites: dict[str, ExaminationSite] = {}
        self._states: dict[str, LedgerState] = {}
        self._events: list[LedgerEvent] = []

    @property
    def sites(self) -> tuple[ExaminationSite, ...]:
        return tuple(self._sites.values())

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._sites)

    def has(self, site_id: str) -> bool:
        return site_id in self._sites

    def site(self, site_id: str) -> ExaminationSite:
        try:
            return self._sites[site_id]
        except KeyError as exc:
            raise LedgerInvariantError(f"unknown examination site {site_id}") from exc

    def state(self, site_id: str) -> LedgerState:
        try:
            return self._states[site_id]
        except KeyError as exc:
            raise LedgerInvariantError(f"unknown examination site {site_id}") from exc

    def register(self, site: ExaminationSite, *, actor: str | None = None,
                 initial_state: LedgerState | None = None,
                 reason: str = "") -> bool:
        """Register a site once; identical retries are idempotent.

        A repeated id with a different definition is a hash collision or a
        generator bug and is refused.  An optional initial transition records
        the local generator's immediate decision without folding it into the
        generated row.
        """
        existing = self._sites.get(site.site_id)
        if existing is not None:
            if existing.model_dump(exclude={"created_at", "status"}) != \
                    site.model_dump(exclude={"created_at", "status"}):
                raise LedgerInvariantError(
                    f"site id {site.site_id} was reused for a different site")
            return False
        generated = site.model_copy(update={"status": LedgerState.GENERATED})
        self._sites[site.site_id] = generated
        self._states[site.site_id] = LedgerState.GENERATED
        self._append(site.site_id, LedgerState.GENERATED,
                     actor or site.generator, reason="site generated",
                     site=generated)
        target = initial_state or site.status
        if target != LedgerState.GENERATED:
            self.transition(site.site_id, target, actor=actor or site.generator,
                            reason=reason)
        return True

    def transition(self, site_id: str, state: LedgerState, *, actor: str,
                   reason: str = "", evidence: dict | None = None,
                   verdict: Verdict | None = None) -> LedgerEvent:
        current = self.state(site_id)
        if state == current:
            raise LedgerInvariantError(
                f"site {site_id} is already in state {state.value}")
        if state not in _ALLOWED[current]:
            raise LedgerInvariantError(
                f"invalid ledger transition for {site_id}: "
                f"{current.value} -> {state.value}")
        self._states[site_id] = state
        self._sites[site_id] = self._sites[site_id].model_copy(
            update={"status": state})
        return self._append(site_id, state, actor, reason, evidence, verdict)

    def apply_verdict(self, verdict: Verdict) -> LedgerEvent:
        target = {
            "pass": LedgerState.MODEL_PASSED,
            "error": LedgerState.MODEL_CONFIRMED,
            "uncertain": LedgerState.UNCERTAIN,
            "defer": LedgerState.ESCALATED,
        }[verdict.decision]
        return self.transition(
            verdict.site_id, target, actor=verdict.judge,
            reason=verdict.explanation or verdict.decision,
            evidence={"correction": verdict.correction,
                      "confidence": verdict.confidence},
            verdict=verdict)

    def apply_packet_verdicts(self, submitted_ids: Iterable[str],
                              verdicts: Iterable[Verdict]) -> None:
        verdicts = tuple(verdicts)
        validate_verdict_coverage(submitted_ids, (v.site_id for v in verdicts))
        for verdict in verdicts:
            self.apply_verdict(verdict)

    def record_observation(self, site_id: str, *, actor: str, reason: str,
                           evidence: dict | None = None,
                           verdict: Verdict | None = None) -> LedgerEvent:
        """Append independent evidence without changing the state projection.

        Phase 1B compares a blind site judge with the production reviewer. A
        production finding observed after a model pass is disagreement evidence,
        not permission to rewrite the judge's terminal state.
        """
        return self._append(
            site_id, self.state(site_id), actor, reason, evidence, verdict,
            event_kind="observation")

    def projection(self) -> dict[str, LedgerState]:
        return dict(self._states)

    def state_counts(self) -> dict[str, int]:
        counts = Counter(s.value for s in self._states.values())
        return dict(sorted(counts.items()))

    def unresolved_ids(self) -> tuple[str, ...]:
        return tuple(site_id for site_id, state in self._states.items()
                     if state in PENDING_STATES)

    def assert_accounted(self) -> None:
        """Every site has a state and every event points at its own site."""
        if set(self._sites) != set(self._states):
            raise LedgerInvariantError("site/state projection mismatch")
        event_site_ids = {event.site_id for event in self._events}
        unknown = event_site_ids - set(self._sites)
        if unknown:
            raise LedgerInvariantError(
                f"ledger events reference unknown sites: {sorted(unknown)[:5]}")
        no_events = set(self._sites) - event_site_ids
        if no_events:
            raise LedgerInvariantError(
                f"sites have no append-only events: {sorted(no_events)[:5]}")

    def write_jsonl(self, path: Path) -> None:
        self.assert_accounted()
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if path.suffix == ".gz" else Path.open
        with opener(path, "wt", encoding="utf-8") as stream:
            for event in self._events:
                stream.write(event.model_dump_json(exclude_none=True) + "\n")

    def _append(self, site_id: str, state: LedgerState, actor: str,
                reason: str = "", evidence: dict | None = None,
                verdict: Verdict | None = None,
                site: ExaminationSite | None = None,
                event_kind: str = "transition") -> LedgerEvent:
        event = LedgerEvent(
            sequence=len(self._events) + 1, site_id=site_id, state=state,
            actor=actor, reason=reason, evidence=evidence or {},
            verdict=verdict, site=site, event_kind=event_kind)
        self._events.append(event)
        return event


def validate_verdict_coverage(submitted_ids: Iterable[str],
                              returned_ids: Iterable[str]) -> None:
    submitted = tuple(submitted_ids)
    returned = tuple(returned_ids)
    expected = set(submitted)
    counts = Counter(returned)
    missing = expected - set(returned)
    duplicate = {site_id for site_id, n in counts.items() if n != 1}
    unknown = set(returned) - expected
    if missing or duplicate or unknown or len(expected) != len(submitted):
        # Duplicate submitted ids are a caller bug and reported through the same
        # invariant as duplicate returned ids.
        duplicate |= {site_id for site_id, n in Counter(submitted).items()
                      if n != 1}
        raise IncompleteVerdicts(missing=missing, duplicate=duplicate,
                                 unknown=unknown)


def read_jsonl(path: Path) -> list[dict]:
    """Small diagnostic helper; replay is intentionally not implicit yet."""
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]
