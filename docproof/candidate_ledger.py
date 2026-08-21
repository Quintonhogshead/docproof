"""Append-only accounting for every generated and screened candidate."""
from __future__ import annotations

import gzip
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .candidate_models import (
    Candidate, CandidateStatus, PENDING_CANDIDATE_STATES,
    TERMINAL_CANDIDATE_STATES, Verdict, utc_now)


class CandidateLedgerError(RuntimeError):
    pass


class IncompleteCandidateVerdicts(CandidateLedgerError):
    """A model packet did not partition exactly the IDs it received."""

    def __init__(self, *, missing=(), duplicate=(), unknown=()):
        self.missing = tuple(sorted(missing))
        self.duplicate = tuple(sorted(duplicate))
        self.unknown = tuple(sorted(unknown))
        parts = []
        if self.missing:
            parts.append(f"missing={','.join(self.missing)}")
        if self.duplicate:
            parts.append(f"duplicate={','.join(self.duplicate)}")
        if self.unknown:
            parts.append(f"unknown={','.join(self.unknown)}")
        super().__init__("incomplete candidate verdict coverage: "
                         + "; ".join(parts))


class ScreeningEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_kind: Literal["transition", "observation"] = "transition"
    candidate_id: str = Field(min_length=1)
    status: CandidateStatus
    stage: str = Field(min_length=1)
    judge_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    explanation: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    input_record: dict[str, Any] = Field(default_factory=dict)
    output_record: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0.0)
    verdict: Verdict | None = None
    candidate: Candidate | None = None
    created_at: datetime = Field(default_factory=utc_now)


_ALLOWED: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.GENERATED: frozenset({
        CandidateStatus.NEEDS_SCREENING, CandidateStatus.NEEDS_MODEL_JUDGMENT,
        CandidateStatus.PACKET_READY, CandidateStatus.PASS,
        CandidateStatus.ERROR, CandidateStatus.UNCERTAIN,
        CandidateStatus.DEFERRED, CandidateStatus.DUPLICATE,
        CandidateStatus.INVALID,
    }),
    CandidateStatus.NEEDS_SCREENING: frozenset({
        CandidateStatus.NEEDS_MODEL_JUDGMENT, CandidateStatus.PACKET_READY,
        CandidateStatus.PASS, CandidateStatus.ERROR,
        CandidateStatus.UNCERTAIN, CandidateStatus.DEFERRED,
        CandidateStatus.INVALID,
    }),
    CandidateStatus.NEEDS_MODEL_JUDGMENT: frozenset({
        CandidateStatus.PACKET_READY, CandidateStatus.PASS,
        CandidateStatus.ERROR, CandidateStatus.UNCERTAIN,
        CandidateStatus.DEFERRED, CandidateStatus.INVALID,
    }),
    CandidateStatus.PACKET_READY: frozenset({
        CandidateStatus.PASS, CandidateStatus.ERROR,
        CandidateStatus.UNCERTAIN, CandidateStatus.DEFERRED,
        CandidateStatus.INVALID,
    }),
    CandidateStatus.PASS: frozenset(),
    CandidateStatus.ERROR: frozenset(),
    CandidateStatus.UNCERTAIN: frozenset(),
    CandidateStatus.DEFERRED: frozenset(),
    CandidateStatus.DUPLICATE: frozenset(),
    CandidateStatus.INVALID: frozenset(),
}


class CandidateLedger:
    """Current-state projection over an immutable, auditable event stream."""

    def __init__(self) -> None:
        self._candidates: dict[str, Candidate] = {}
        self._canonical_ids: dict[str, str] = {}
        self._statuses: dict[str, CandidateStatus] = {}
        self._events: list[ScreeningEvent] = []

    def __len__(self) -> int:
        return len(self._candidates)

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return tuple(self._candidates.values())

    @property
    def events(self) -> tuple[ScreeningEvent, ...]:
        return tuple(self._events)

    def has(self, candidate_id: str) -> bool:
        return candidate_id in self._candidates

    def candidate(self, candidate_id: str) -> Candidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise CandidateLedgerError(
                f"unknown candidate {candidate_id}") from exc

    def status(self, candidate_id: str) -> CandidateStatus:
        try:
            return self._statuses[candidate_id]
        except KeyError as exc:
            raise CandidateLedgerError(
                f"unknown candidate {candidate_id}") from exc

    def register(self, candidate: Candidate) -> tuple[str, bool]:
        """Register once and merge provenance for canonical duplicates.

        Returns ``(canonical_id, created)``. A duplicate generator observation
        is recorded without creating a second screening obligation.
        """
        canonical = candidate.canonical_key()
        existing_id = self._canonical_ids.get(canonical)
        if existing_id is not None:
            existing = self._candidates[existing_id]
            generators = tuple(dict.fromkeys(
                existing.generator_ids + candidate.generator_ids))
            evidence = dict(existing.evidence)
            merged = list(evidence.get("merged_generator_evidence", []))
            merged.append({
                "generator_id": candidate.generator_id,
                "evidence": candidate.evidence,
            })
            evidence["merged_generator_evidence"] = merged
            self._candidates[existing_id] = existing.model_copy(update={
                "generator_ids": generators, "evidence": evidence})
            self.observe(
                existing_id, stage="deduplication", judge_id="candidate-ledger",
                reason_code="canonical_duplicate_merged",
                explanation="Equivalent anchors, type, text, and correction were merged.",
                evidence={"merged_candidate_id": candidate.candidate_id,
                          "generator_id": candidate.generator_id})
            return existing_id, False

        if candidate.candidate_id in self._candidates:
            raise CandidateLedgerError(
                f"candidate id {candidate.candidate_id} was reused for a "
                "different canonical candidate")
        generated = candidate.model_copy(update={
            "status": CandidateStatus.GENERATED})
        self._candidates[generated.candidate_id] = generated
        self._canonical_ids[canonical] = generated.candidate_id
        self._statuses[generated.candidate_id] = CandidateStatus.GENERATED
        self._append(
            generated.candidate_id, CandidateStatus.GENERATED,
            stage="generation", judge_id=generated.generator_id,
            reason_code="candidate_generated", candidate=generated,
            output_record={"status": CandidateStatus.GENERATED.value})
        return generated.candidate_id, True

    def transition(self, candidate_id: str, status: CandidateStatus, *,
                   stage: str, judge_id: str, reason_code: str,
                   explanation: str = "", evidence: dict | None = None,
                   input_record: dict | None = None,
                   output_record: dict | None = None,
                   cost_usd: float = 0.0,
                   verdict: Verdict | None = None) -> ScreeningEvent:
        current = self.status(candidate_id)
        if status == current:
            raise CandidateLedgerError(
                f"candidate {candidate_id} is already {status.value}")
        if status not in _ALLOWED[current]:
            raise CandidateLedgerError(
                f"invalid candidate transition for {candidate_id}: "
                f"{current.value} -> {status.value}")
        self._statuses[candidate_id] = status
        self._candidates[candidate_id] = self._candidates[candidate_id].model_copy(
            update={"status": status})
        return self._append(
            candidate_id, status, stage=stage, judge_id=judge_id,
            reason_code=reason_code, explanation=explanation,
            evidence=evidence, input_record=input_record,
            output_record=output_record, cost_usd=cost_usd, verdict=verdict)

    def observe(self, candidate_id: str, *, stage: str, judge_id: str,
                reason_code: str, explanation: str = "",
                evidence: dict | None = None,
                input_record: dict | None = None,
                output_record: dict | None = None,
                cost_usd: float = 0.0,
                verdict: Verdict | None = None) -> ScreeningEvent:
        return self._append(
            candidate_id, self.status(candidate_id), stage=stage,
            judge_id=judge_id, reason_code=reason_code,
            explanation=explanation, evidence=evidence,
            input_record=input_record, output_record=output_record,
            cost_usd=cost_usd, verdict=verdict,
            event_kind="observation")

    def apply_verdict(self, verdict: Verdict, *, stage: str,
                      cost_usd: float = 0.0) -> ScreeningEvent:
        target = {
            "pass": CandidateStatus.PASS,
            "error": CandidateStatus.ERROR,
            "uncertain": CandidateStatus.UNCERTAIN,
            "defer": CandidateStatus.DEFERRED,
            "invalid": CandidateStatus.INVALID,
            "duplicate": CandidateStatus.DUPLICATE,
        }[verdict.decision]
        return self.transition(
            verdict.candidate_id, target, stage=stage,
            judge_id=verdict.judge_id, reason_code=verdict.reason_code,
            explanation=verdict.explanation or "",
            evidence={"evidence_used": list(verdict.evidence_used)},
            output_record=verdict.model_dump(mode="json", exclude_none=True),
            cost_usd=cost_usd, verdict=verdict)

    def apply_packet_verdicts(self, submitted_ids: Iterable[str],
                              verdicts: Iterable[Verdict], *,
                              stage: str, cost_usd: float = 0.0) -> None:
        rows = tuple(verdicts)
        validate_candidate_verdict_coverage(
            submitted_ids, (row.candidate_id for row in rows))
        per_candidate = cost_usd / len(rows) if rows else 0.0
        for row in rows:
            self.apply_verdict(row, stage=stage, cost_usd=per_candidate)

    def projection(self) -> dict[str, CandidateStatus]:
        return dict(self._statuses)

    def state_counts(self) -> dict[str, int]:
        counts = Counter(status.value for status in self._statuses.values())
        return dict(sorted(counts.items()))

    def pending_ids(self) -> tuple[str, ...]:
        return tuple(candidate_id for candidate_id, status
                     in self._statuses.items()
                     if status in PENDING_CANDIDATE_STATES)

    def terminal_ids(self) -> tuple[str, ...]:
        return tuple(candidate_id for candidate_id, status
                     in self._statuses.items()
                     if status in TERMINAL_CANDIDATE_STATES)

    def assert_accounted(self) -> None:
        if set(self._candidates) != set(self._statuses):
            raise CandidateLedgerError("candidate/status projection mismatch")
        event_ids = {event.candidate_id for event in self._events}
        missing = set(self._candidates) - event_ids
        unknown = event_ids - set(self._candidates)
        if missing or unknown:
            raise CandidateLedgerError(
                f"ledger event mismatch: missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}")

    def write_jsonl(self, path: Path) -> None:
        self.assert_accounted()
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if path.suffix == ".gz" else Path.open
        with opener(path, "wt", encoding="utf-8") as stream:
            for event in self._events:
                stream.write(event.model_dump_json(exclude_none=True) + "\n")

    def _append(self, candidate_id: str, status: CandidateStatus, *,
                stage: str, judge_id: str, reason_code: str,
                explanation: str = "", evidence: dict | None = None,
                input_record: dict | None = None,
                output_record: dict | None = None,
                cost_usd: float = 0.0, verdict: Verdict | None = None,
                candidate: Candidate | None = None,
                event_kind: str = "transition") -> ScreeningEvent:
        event = ScreeningEvent(
            sequence=len(self._events) + 1, event_kind=event_kind,
            candidate_id=candidate_id, status=status, stage=stage,
            judge_id=judge_id, reason_code=reason_code,
            explanation=explanation, evidence=evidence or {},
            input_record=input_record or {}, output_record=output_record or {},
            cost_usd=cost_usd, verdict=verdict, candidate=candidate)
        self._events.append(event)
        return event


def validate_candidate_verdict_coverage(submitted_ids: Iterable[str],
                                        returned_ids: Iterable[str]) -> None:
    submitted = tuple(submitted_ids)
    returned = tuple(returned_ids)
    expected = set(submitted)
    counts = Counter(returned)
    missing = expected - set(returned)
    unknown = set(returned) - expected
    duplicate = {candidate_id for candidate_id, count in counts.items()
                 if count != 1}
    duplicate |= {candidate_id for candidate_id, count
                  in Counter(submitted).items() if count != 1}
    if missing or unknown or duplicate or len(expected) != len(submitted):
        raise IncompleteCandidateVerdicts(
            missing=missing, unknown=unknown, duplicate=duplicate)
