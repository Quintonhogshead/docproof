"""Batched model judgment with exact candidate-ID coverage validation."""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from .candidate_ledger import (
    IncompleteCandidateVerdicts, validate_candidate_verdict_coverage)
from .candidate_models import ScreeningPacket, Verdict
from .models import Usage
from .providers import Provider
from .providers.base import strict_json_schema


class _Decision(BaseModel):
    candidate_id: str
    corrected_text: str | None = None
    confidence: str = "low"
    reason_code: str = "model_judgment"
    explanation: str = ""
    evidence_used: list[str] = Field(default_factory=list)
    meaning_change_risk: str = "low"


class _Judgments(BaseModel):
    pass_ids: list[str] = Field(default_factory=list)
    errors: list[_Decision] = Field(default_factory=list)
    uncertain: list[_Decision] = Field(default_factory=list)
    deferred: list[_Decision] = Field(default_factory=list)


_SYSTEM = """\
You are screening explicit proofreading candidates, not searching for new
problems. Judge every supplied candidate exactly once. Return its candidate_id
in one and only one group: pass_ids, errors, uncertain, or deferred. A pass
means the anchored text is correct for this candidate type. An error needs the
smallest safe corrected_text: the exact replacement for observed_text at the
candidate's primary anchor, never a rewritten sentence or paragraph. Uncertain
means author intent or house style is needed. Deferred means stronger or broader
context is needed. Supply a short
reason_code. Explain errors, uncertain decisions, and deferrals only. Never
omit an ID, invent an ID, rewrite surrounding prose, or silently suppress a
plausible problem.\
"""


def candidate_judgment_prompt() -> str:
    return _SYSTEM


def candidate_judgment_fingerprint() -> str:
    return hashlib.sha256(_SYSTEM.encode("utf-8")).hexdigest()


def parse_candidate_judgments(packet: ScreeningPacket, parsed: dict, *,
                              judge_id: str) -> list[Verdict]:
    body = _Judgments.model_validate(parsed)
    returned = (list(body.pass_ids)
                + [row.candidate_id for row in body.errors]
                + [row.candidate_id for row in body.uncertain]
                + [row.candidate_id for row in body.deferred])
    validate_candidate_verdict_coverage(packet.candidate_ids, returned)
    verdicts = [Verdict(
        candidate_id=candidate_id, decision="pass", confidence="high",
        reason_code="model_pass", judge_id=judge_id)
        for candidate_id in body.pass_ids]
    verdicts.extend(_verdict(row, "error", judge_id) for row in body.errors)
    verdicts.extend(_verdict(row, "uncertain", judge_id)
                    for row in body.uncertain)
    verdicts.extend(_verdict(row, "defer", judge_id)
                    for row in body.deferred)
    by_id = {verdict.candidate_id: verdict for verdict in verdicts}
    return [by_id[candidate_id] for candidate_id in packet.candidate_ids]


def judge_screening_packet(
        packet: ScreeningPacket, provider: Provider, *, model: str,
        max_tokens: int, usage: Usage | None = None,
        max_missing_retries: int = 1,
        retry_log: list[dict] | None = None) -> list[Verdict]:
    """Judge a packet and retry only missing or structurally invalid IDs."""
    remaining = packet
    verdicts: dict[str, Verdict] = {}
    attempts = 0
    while remaining.candidates:
        result = provider.complete_structured(
            model=model, system=_SYSTEM, user=remaining.prompt_payload(),
            schema=strict_json_schema(_Judgments),
            schema_name="candidate_screening_verdicts", max_tokens=max_tokens)
        if usage is not None:
            usage.add(result.usage, model=model)
        if result.parsed is None:
            if attempts >= max_missing_retries:
                raise IncompleteCandidateVerdicts(
                    missing=remaining.candidate_ids)
            if retry_log is not None:
                retry_log.append({
                    "attempt": attempts + 1,
                    "missing_ids": list(remaining.candidate_ids),
                    "invalid_ids": [],
                    "retried_ids": list(remaining.candidate_ids),
                    "failure": "empty_or_truncated_response",
                })
            attempts += 1
            continue
        try:
            body = _Judgments.model_validate(result.parsed)
        except Exception:
            if attempts >= max_missing_retries:
                raise
            if retry_log is not None:
                retry_log.append({
                    "attempt": attempts + 1,
                    "missing_ids": [],
                    "invalid_ids": list(remaining.candidate_ids),
                    "retried_ids": list(remaining.candidate_ids),
                    "failure": "schema_validation_failure",
                })
            attempts += 1
            continue
        returned = (list(body.pass_ids)
                    + [row.candidate_id for row in body.errors]
                    + [row.candidate_id for row in body.uncertain]
                    + [row.candidate_id for row in body.deferred])
        missing: set[str] = set()
        try:
            validate_candidate_verdict_coverage(
                remaining.candidate_ids, returned)
        except IncompleteCandidateVerdicts as exc:
            # A duplicate is a real candidate answered ambiguously; an unknown
            # is a hallucinated ID that maps to no candidate. Neither is
            # grounds to abandon the whole packet: drop the unknowns, treat the
            # duplicated candidates as unanswered, and retry the focused
            # remainder alongside any missing IDs. (Previously this raised,
            # losing every candidate in the packet to one judge slip.)
            missing.update(exc.missing)
            missing.update(exc.duplicate)
        known = set(remaining.candidate_ids)
        invalid = _invalid_decision_ids(body, remaining)
        unresolved = (missing | invalid) & known
        if unresolved:
            if attempts >= max_missing_retries:
                if invalid:
                    raise ValueError(
                        "invalid candidate verdict(s): "
                        + ",".join(sorted(invalid)))
                raise IncompleteCandidateVerdicts(missing=missing)
            answered_ids = set(returned) - unresolved
            answered = remaining.subset(answered_ids)
            if answered.candidates:
                partial = _partial_payload(body, set(answered.candidate_ids))
                rows = parse_candidate_judgments(
                    answered, partial, judge_id=model)
                verdicts.update((row.candidate_id, row) for row in rows)
            if retry_log is not None:
                retry_log.append({
                    "attempt": attempts + 1,
                    "missing_ids": sorted(missing),
                    "invalid_ids": sorted(invalid),
                    "retried_ids": sorted(unresolved),
                })
            remaining = remaining.subset(unresolved)
            attempts += 1
            continue
        rows = parse_candidate_judgments(
            remaining, result.parsed, judge_id=model)
        verdicts.update((row.candidate_id, row) for row in rows)
        break
    validate_candidate_verdict_coverage(packet.candidate_ids, verdicts)
    return [verdicts[candidate_id] for candidate_id in packet.candidate_ids]


def _verdict(row: _Decision, decision: str, judge_id: str) -> Verdict:
    risk = (row.meaning_change_risk if row.meaning_change_risk
            in {"low", "medium", "high"} else "low")
    confidence = (row.confidence if row.confidence
                  in {"low", "medium", "high"} else "low")
    explanation = row.explanation.strip() or (
        "The model did not supply the required explanation.")
    return Verdict(
        candidate_id=row.candidate_id, decision=decision,
        corrected_text=row.corrected_text, confidence=confidence,
        reason_code=(row.reason_code.strip() or "model_judgment"),
        explanation=explanation, evidence_used=tuple(row.evidence_used),
        judge_id=judge_id, meaning_change_risk=risk)


def _partial_payload(body: _Judgments, wanted: set[str]) -> dict:
    return {
        "pass_ids": [candidate_id for candidate_id in body.pass_ids
                     if candidate_id in wanted],
        "errors": [row.model_dump() for row in body.errors
                   if row.candidate_id in wanted],
        "uncertain": [row.model_dump() for row in body.uncertain
                      if row.candidate_id in wanted],
        "deferred": [row.model_dump() for row in body.deferred
                     if row.candidate_id in wanted],
    }


def _invalid_decision_ids(body: _Judgments,
                          packet: ScreeningPacket) -> set[str]:
    """IDs whose returned row violates the compact verdict contract."""
    by_id = {row.candidate_id: row for row in packet.candidates}
    invalid = set()
    for row in body.errors:
        item = by_id.get(row.candidate_id)
        if (row.corrected_text is None or not row.explanation.strip()
                or (item is not None
                    and row.corrected_text == item.observed_text)):
            invalid.add(row.candidate_id)
    for row in tuple(body.uncertain) + tuple(body.deferred):
        if not row.explanation.strip():
            invalid.add(row.candidate_id)
    return invalid
