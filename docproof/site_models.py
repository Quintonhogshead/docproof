"""Typed records for the examination-graph shadow pipeline.

An examination site is an obligation to make a decision.  It is deliberately
separate from :class:`docproof.models.Finding`: a finding is something the
existing edit pipeline may act on, while a site also represents text that was
looked at and passed, text still waiting for judgment, and questions that must
not become automatic edits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LedgerState(str, Enum):
    GENERATED = "generated"
    LOCALLY_PASSED = "locally_passed"
    LOCALLY_CONFIRMED = "locally_confirmed"
    NEEDS_JUDGMENT = "needs_judgment"
    MODEL_PASSED = "model_passed"
    MODEL_CONFIRMED = "model_confirmed"
    UNCERTAIN = "uncertain"
    ESCALATED = "escalated"
    EDIT = "edit"
    QUERY = "query"
    REJECTED = "rejected"
    APPLIED = "applied"


TERMINAL_STATES = frozenset({
    LedgerState.LOCALLY_PASSED,
    LedgerState.MODEL_PASSED,
    LedgerState.QUERY,
    LedgerState.REJECTED,
    LedgerState.APPLIED,
})

PENDING_STATES = frozenset(set(LedgerState) - TERMINAL_STATES)


class SiteAnchor(BaseModel):
    """A Word-aware location, with fields reserved for the lossless IR phases.

    Phase one can fill the OOXML part, paragraph id, and character offsets from
    DocProof's current document model.  Run, field, object, page, and geometry
    coordinates remain optional until their corresponding ingest layers exist;
    they are not fabricated in shadow reports.
    """

    model_config = ConfigDict(extra="forbid")

    part: str
    paragraph_id: str | None = None
    run_index: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    object_id: str | None = None
    page_number: int | None = None
    bounding_box: tuple[float, float, float, float] | None = None
    virtual_location: dict[str, Any] | None = None

    @field_validator("end_offset")
    @classmethod
    def _ordered_offsets(cls, value, info):
        start = info.data.get("start_offset")
        if value is not None and start is not None and value < start:
            raise ValueError("end_offset must be at or after start_offset")
        return value


class ExaminationSite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: str = Field(min_length=1)
    site_type: str = Field(min_length=1)
    anchors: tuple[SiteAnchor, ...] = Field(min_length=1)
    generator: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    context_recipe: tuple[str, ...] = ("current paragraph",)
    risk_prior: float = Field(default=0.5, ge=0.0, le=1.0)
    meaning_change_risk: Literal["low", "medium", "high"] = "low"
    status: LedgerState = LedgerState.GENERATED
    created_at: datetime = Field(default_factory=utc_now)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_id: str = Field(min_length=1)
    decision: Literal["pass", "error", "uncertain", "defer"]
    correction: str | None = None
    explanation: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    judge: str = Field(min_length=1)
    evidence_used: tuple[Any, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class LedgerEvent(BaseModel):
    """One immutable journal row.

    The first event embeds the site definition.  Later events carry only the
    transition and its evidence, keeping the JSONL journal append-friendly while
    the in-memory ledger maintains the current-state projection.
    """

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_kind: Literal["transition", "observation"] = "transition"
    site_id: str = Field(min_length=1)
    state: LedgerState
    actor: str = Field(min_length=1)
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    verdict: Verdict | None = None
    site: ExaminationSite | None = None
    created_at: datetime = Field(default_factory=utc_now)
