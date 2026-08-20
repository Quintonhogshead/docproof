"""Batched site judgment with exact response-coverage validation."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .models import Usage
from .providers import Provider
from .providers.base import strict_json_schema
from .site_ledger import IncompleteVerdicts, validate_verdict_coverage
from .site_models import Verdict
from .site_packet import JudgmentPacket


class _Decision(BaseModel):
    site_id: str
    correction: str = ""
    explanation: str = ""
    confidence: str = "low"


class _Judgments(BaseModel):
    pass_ids: list[str] = Field(default_factory=list)
    errors: list[_Decision] = Field(default_factory=list)
    uncertain: list[_Decision] = Field(default_factory=list)
    defer_ids: list[str] = Field(default_factory=list)


_SYSTEM = """\
You are resolving explicit proofreading examination sites. Judge every supplied
site exactly once. Return its site_id in one and only one group: pass_ids,
errors, uncertain, or defer_ids. A pass means the text is correct at that site.
An error needs the smallest correction. Uncertain means the answer depends on
author intent or house style. Defer means stronger or broader context is needed.
Explain only errors and uncertain decisions. Never omit a site id, invent an id,
or silently discard a plausible problem.\
"""


def parse_judgments(packet: JudgmentPacket, parsed: dict, *, judge: str
                    ) -> list[Verdict]:
    body = _Judgments.model_validate(parsed)
    returned = (list(body.pass_ids)
                + [row.site_id for row in body.errors]
                + [row.site_id for row in body.uncertain]
                + list(body.defer_ids))
    validate_verdict_coverage(packet.site_ids, returned)
    verdicts = [Verdict(site_id=site_id, decision="pass", confidence="high",
                        judge=judge)
                for site_id in body.pass_ids]
    verdicts.extend(Verdict(
        site_id=row.site_id, decision="error",
        correction=row.correction or None,
        explanation=row.explanation or None,
        confidence=_confidence(row.confidence), judge=judge)
        for row in body.errors)
    verdicts.extend(Verdict(
        site_id=row.site_id, decision="uncertain",
        correction=row.correction or None,
        explanation=row.explanation or None,
        confidence=_confidence(row.confidence), judge=judge)
        for row in body.uncertain)
    verdicts.extend(Verdict(site_id=site_id, decision="defer",
                            confidence="low", judge=judge)
                    for site_id in body.defer_ids)
    by_id = {v.site_id: v for v in verdicts}
    return [by_id[site_id] for site_id in packet.site_ids]


def judge_packet(packet: JudgmentPacket, provider: Provider, *, model: str,
                 max_tokens: int, usage: Usage | None = None,
                 max_missing_retries: int = 1) -> list[Verdict]:
    """Judge one 50–200-site packet and retry only missing site ids.

    Unknown or duplicate ids are malformed, not missing, and fail loudly.  A
    partial but otherwise valid answer is retried with just its missing subset;
    no site that already received a verdict is billed twice.
    """
    remaining = packet
    verdicts: dict[str, Verdict] = {}
    attempts = 0
    while remaining.sites:
        result = provider.complete_structured(
            model=model, system=_SYSTEM, user=remaining.prompt_payload(),
            schema=strict_json_schema(_Judgments),
            schema_name="examination_site_judgments", max_tokens=max_tokens)
        if usage is not None:
            usage.add(result.usage, model=model)
        if result.parsed is None:
            raise IncompleteVerdicts(missing=remaining.site_ids)
        try:
            rows = parse_judgments(remaining, result.parsed, judge=model)
        except IncompleteVerdicts as exc:
            if exc.duplicate or exc.unknown or not exc.missing \
                    or attempts >= max_missing_retries:
                raise
            # Parse the returned groups against their own ids so the valid rows
            # are retained, then ask only the missing subset.
            body = _Judgments.model_validate(result.parsed)
            returned = set(body.pass_ids)
            returned.update(row.site_id for row in body.errors)
            returned.update(row.site_id for row in body.uncertain)
            returned.update(body.defer_ids)
            answered = remaining.subset(returned)
            if answered.sites:
                rows = _parse_without_full_coverage(answered, body, model)
                verdicts.update((row.site_id, row) for row in rows)
            remaining = remaining.subset(exc.missing)
            attempts += 1
            continue
        verdicts.update((row.site_id, row) for row in rows)
        break
    validate_verdict_coverage(packet.site_ids, verdicts)
    return [verdicts[site_id] for site_id in packet.site_ids]


def _parse_without_full_coverage(packet: JudgmentPacket, body: _Judgments,
                                 judge: str) -> list[Verdict]:
    wanted = set(packet.site_ids)
    partial = {
        "pass_ids": [site_id for site_id in body.pass_ids if site_id in wanted],
        "errors": [row.model_dump() for row in body.errors
                   if row.site_id in wanted],
        "uncertain": [row.model_dump() for row in body.uncertain
                      if row.site_id in wanted],
        "defer_ids": [site_id for site_id in body.defer_ids
                      if site_id in wanted],
    }
    return parse_judgments(packet, partial, judge=judge)


def _confidence(value: str) -> str:
    return value if value in {"low", "medium", "high"} else "low"
