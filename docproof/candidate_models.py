"""Typed contracts for candidate generation and screening.

Candidates are deliberately separate from :class:`docproof.models.Finding`.
A candidate is an obligation to make a decision; only the mature Finding ->
validator -> tracked-change path is allowed to alter a manuscript.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    NEEDS_SCREENING = "needs_screening"
    NEEDS_MODEL_JUDGMENT = "needs_model_judgment"
    PACKET_READY = "packet_ready"
    PASS = "pass"
    ERROR = "error"
    UNCERTAIN = "uncertain"
    DEFERRED = "deferred"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


TERMINAL_CANDIDATE_STATES = frozenset({
    CandidateStatus.PASS,
    CandidateStatus.ERROR,
    CandidateStatus.UNCERTAIN,
    CandidateStatus.DEFERRED,
    CandidateStatus.DUPLICATE,
    CandidateStatus.INVALID,
})

PENDING_CANDIDATE_STATES = frozenset(
    set(CandidateStatus) - TERMINAL_CANDIDATE_STATES)


class CandidateAnchor(BaseModel):
    """A stable Word-aware anchor, including registered virtual locations."""

    model_config = ConfigDict(extra="forbid")

    document_part: str = Field(min_length=1)
    paragraph_id: str | None = None
    run_index: int | None = Field(default=None, ge=0)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    object_id: str | None = None
    virtual_location: str | None = None

    @model_validator(mode="after")
    def _valid_location(self):
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("start_offset and end_offset must be supplied together")
        if (self.start_offset is not None and self.end_offset is not None
                and self.end_offset < self.start_offset):
            raise ValueError("end_offset must be at or after start_offset")
        if not any((self.paragraph_id, self.object_id, self.virtual_location)):
            raise ValueError(
                "an anchor needs a paragraph_id, object_id, or virtual_location")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class Candidate(BaseModel):
    """One explicit potential error site generated without a paid model call."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    candidate_type: str = Field(min_length=1)
    generator_id: str = Field(min_length=1)
    # Deduplication keeps the first generator as ``generator_id`` for compact
    # reports and all contributing generators here for complete provenance.
    generator_ids: tuple[str, ...] = ()
    anchors: tuple[CandidateAnchor, ...] = Field(min_length=1)
    observed_text: str | None = None
    candidate_correction: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    context_recipe: tuple[str, ...] = ("current paragraph",)
    risk_prior: float = Field(default=0.5, ge=0.0, le=1.0)
    meaning_change_risk: Literal["low", "medium", "high"] = "low"
    channel_preference: Literal["edit", "query", "either"] = "either"
    status: CandidateStatus = CandidateStatus.GENERATED
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _generator_provenance(self):
        generators = tuple(dict.fromkeys(
            (self.generator_id,) + tuple(self.generator_ids)))
        if generators != self.generator_ids:
            object.__setattr__(self, "generator_ids", generators)
        return self

    def canonical_key(self) -> str:
        """Canonical identity used for deterministic IDs and deduplication."""
        payload = {
            "candidate_type": self.candidate_type,
            "anchors": [anchor.canonical() for anchor in self.anchors],
            "observed_text": normalize_candidate_text(self.observed_text),
            "candidate_correction": normalize_candidate_text(
                self.candidate_correction),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))


class Verdict(BaseModel):
    """A supported decision for exactly one candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    decision: Literal[
        "pass", "error", "uncertain", "defer", "invalid", "duplicate"]
    corrected_text: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    reason_code: str = Field(min_length=1)
    explanation: str | None = None
    evidence_used: tuple[str, ...] = ()
    judge_id: str = Field(min_length=1)
    meaning_change_risk: Literal["low", "medium", "high"] = "low"
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _explain_nontrivial_decisions(self):
        if self.decision in {"error", "uncertain", "defer"} \
                and not (self.explanation or "").strip():
            raise ValueError(
                "error, uncertain, and defer verdicts require an explanation")
        return self


class ScreeningContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str = Field(min_length=1)
    text: str
    paragraph_ids: tuple[str, ...] = ()
    recipes: tuple[str, ...] = ()


class ScreeningPacketItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    candidate_type: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    observed_text: str | None = None
    candidate_correction: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    anchors: tuple[dict[str, Any], ...]
    meaning_change_risk: Literal["low", "medium", "high"] = "low"
    channel_preference: Literal["edit", "query", "either"] = "either"


class ScreeningPacket(BaseModel):
    """A compact group of compatible candidates with shared context."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(min_length=1)
    contexts: tuple[ScreeningContext, ...]
    # Internal focused-retry subsets may be empty while the caller determines
    # that there were no valid rows to retain. Packet builders still emit only
    # non-empty packets.
    candidates: tuple[ScreeningPacketItem, ...]

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(row.candidate_id for row in self.candidates)

    def subset(self, candidate_ids) -> "ScreeningPacket":
        wanted = set(candidate_ids)
        rows = tuple(row for row in self.candidates
                     if row.candidate_id in wanted)
        context_ids = {row.context_id for row in rows}
        contexts = tuple(context for context in self.contexts
                         if context.context_id in context_ids)
        return ScreeningPacket(
            packet_id=screening_packet_id(row.candidate_id for row in rows),
            contexts=contexts, candidates=rows)

    def prompt_payload(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          separators=(",", ":"))


def normalize_candidate_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def deterministic_candidate_id(candidate_type: str,
                               anchors: tuple[CandidateAnchor, ...],
                               observed_text: str | None = None,
                               candidate_correction: str | None = None) -> str:
    payload = {
        "candidate_type": candidate_type,
        "anchors": [anchor.canonical() for anchor in anchors],
        "observed_text": normalize_candidate_text(observed_text),
        "candidate_correction": normalize_candidate_text(candidate_correction),
    }
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return "C-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def screening_packet_id(candidate_ids) -> str:
    material = "\x1f".join(candidate_ids)
    return "CSP-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
