"""Paid Phase-1B judgment over precise sites, isolated from manuscript edits."""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
from typing import Iterable

from .checkpoint import add_usage, snapshot, usage_delta
from .context_service import ContextService
from .models import Usage
from .providers import cost_of_usage, estimate_cost
from .site_judge import (judge_packet, judgment_prompt_fingerprint,
                         judgment_system_prompt)
from .site_models import ExaminationSite, LedgerState, Verdict
from .site_packet import JudgmentPacket, build_packets
from .site_router import route
from .utils.tokens import estimate_tokens

log = logging.getLogger("docproof.examination.judgment")


class JudgmentCancelled(RuntimeError):
    pass


def _rank(site: ExaminationSite) -> str:
    return hashlib.sha256(site.site_id.encode("utf-8")).hexdigest()


def eligible_sites(run, cfg) -> tuple[ExaminationSite, ...]:
    """The exact, unresolved candidates Phase 1B is allowed to bill for."""
    prefixes = tuple(cfg.eligible_generator_prefixes)
    rows = []
    for site in run.ledger.sites:
        if run.ledger.state(site.site_id) != LedgerState.NEEDS_JUDGMENT:
            continue
        if site.generator == "legacy.model_obligation" \
                and not cfg.allow_legacy_obligations:
            continue
        if prefixes and not any(site.generator.startswith(p) for p in prefixes):
            continue
        # An exact candidate can be judged and later evaluated at one location.
        # Broad scan/category rows stay coverage obligations, not paid prompts.
        anchor = site.anchors[0]
        if anchor.start_offset is None or anchor.end_offset is None:
            continue
        if anchor.object_id and anchor.object_id.startswith(("scan:", "obligation:")):
            continue
        rows.append(site)
    return tuple(sorted(rows, key=_rank))


def _sample(sites: tuple[ExaminationSite, ...], rate: float,
            maximum: int) -> tuple[tuple[ExaminationSite, ...], int, int]:
    if not sites:
        return (), 0, 0
    sampled_n = min(len(sites), max(1, math.ceil(len(sites) * rate)))
    sampled = sites[:sampled_n]
    selected = sampled[:maximum]
    return selected, len(sites) - sampled_n, len(sampled) - len(selected)


def _packet_estimate(packet: JudgmentPacket, model: str, max_tokens: int,
                     effort: str | None) -> float | None:
    input_tokens = estimate_tokens(
        judgment_system_prompt() + "\n" + packet.prompt_payload())
    # Reserve the provider's entire configured output allowance. Most replies
    # are far shorter, but a budget cap is a guardrail, not a median estimate;
    # admitting a packet on an 80-token-per-site guess could let a legal long
    # reply exceed the ceiling. Pad the local input estimate for tokenizer drift.
    return estimate_cost(model, input_tokens=math.ceil(input_tokens * 1.2),
                         output_tokens=max_tokens, effort=effort)


def _checkpoint_key(stage: str, packet: JudgmentPacket, model: str) -> str:
    return (f"examination:{stage}:{model}:"
            f"{judgment_prompt_fingerprint()[:16]}:{packet.packet_id}")


def _verdicts_from_entry(entry) -> list[Verdict]:
    return [Verdict.model_validate(row) for row in entry.items]


def _record_failure(run, packet: JudgmentPacket, stage: str, reason: str) -> None:
    for site_id in packet.site_ids:
        run.judgment_failures[site_id] = {"stage": stage, "reason": reason}
        run.ledger.record_observation(
            site_id, actor=f"examination.{stage}",
            reason="judgment packet did not complete",
            evidence={"packet_id": packet.packet_id, "failure": reason})


def _query(run, site_id: str, reason: str) -> None:
    if run.ledger.state(site_id) != LedgerState.QUERY:
        run.ledger.transition(site_id, LedgerState.QUERY,
                              actor="examination.router", reason=reason)


def _apply_primary(run, verdicts: Iterable[Verdict], *, can_escalate: bool
                   ) -> list[str]:
    escalation = []
    for verdict in verdicts:
        run.primary_judgment_verdicts[verdict.site_id] = verdict
        run.ledger.apply_verdict(verdict)
        site = run.ledger.site(verdict.site_id)
        action = route(verdict, site,
                       stronger_judge_available=can_escalate)
        if action == "escalate":
            if run.ledger.state(verdict.site_id) != LedgerState.ESCALATED:
                run.ledger.transition(
                    verdict.site_id, LedgerState.ESCALATED,
                    actor="examination.router",
                    reason="uncertain or high-risk verdict needs a stronger judge")
            escalation.append(verdict.site_id)
        elif action == "query":
            _query(run, verdict.site_id,
                   "primary verdict remains unresolved in shadow mode")
            run.judgment_verdicts[verdict.site_id] = verdict
        else:
            run.judgment_verdicts[verdict.site_id] = verdict
    return escalation


def _apply_escalation(run, verdicts: Iterable[Verdict]) -> None:
    for verdict in verdicts:
        # A second defer would be escalated -> escalated, which is evidence, not
        # a state transition. Record it and end at a reviewer query.
        if verdict.decision == "defer":
            run.ledger.record_observation(
                verdict.site_id, actor=verdict.judge,
                reason=verdict.explanation or "strong judge deferred",
                evidence={"decision": "defer"}, verdict=verdict)
            _query(run, verdict.site_id,
                   "strong judge deferred; reviewer judgment required")
            run.judgment_verdicts[verdict.site_id] = verdict
            continue
        run.ledger.apply_verdict(verdict)
        action = route(verdict, run.ledger.site(verdict.site_id),
                       stronger_judge_available=False)
        if action == "query":
            _query(run, verdict.site_id,
                   "strong verdict remains uncertain or high risk")
        run.judgment_verdicts[verdict.site_id] = verdict


def _run_packets(run, packets: Iterable[JudgmentPacket], *, stage: str,
                 model: str, effort: str | None, cfg, root_cfg,
                 provider_factory, checkpoint, usage: Usage,
                 should_cancel=None) -> tuple[list[Verdict], int, int]:
    rows: list[Verdict] = []
    completed = failed = 0
    provider = None
    provider_error = ""
    for packet in packets:
        if should_cancel and should_cancel():
            raise JudgmentCancelled()
        key = _checkpoint_key(stage, packet, model)
        cached = checkpoint.get(key) if checkpoint else None
        if cached is not None:
            add_usage(usage, cached.usage)
            verdicts = _verdicts_from_entry(cached)
            rows.extend(verdicts)
            completed += 1
            continue

        # Preserve every failed attempt's spend on a resume. Snapshot first so
        # a new checkpoint entry becomes cumulative (the same policy as the
        # detector checkpoint): if this retry also fails, the next retry still
        # sees all tokens burned so far and the cost ceiling counts them.
        before = snapshot(usage)
        burned = checkpoint.burned(key) if checkpoint else None
        if burned:
            add_usage(usage, burned)
        estimate = _packet_estimate(packet, model, cfg.max_output_tokens, effort)
        actual = cost_of_usage(usage, fallback_model=model, batch=False) or 0.0
        if estimate is None or actual + estimate > cfg.max_cost_usd:
            run.judgment_budget_omissions.update(packet.site_ids)
            for site_id in packet.site_ids:
                run.ledger.record_observation(
                    site_id, actor="examination.budget",
                    reason="site judgment skipped at the configured cost ceiling",
                    evidence={"estimated_packet_cost": estimate,
                              "max_cost_usd": cfg.max_cost_usd})
            continue

        try:
            if provider_error:
                raise RuntimeError(provider_error)
            if provider is None:
                pcfg = root_cfg.model_copy(deep=True)
                pcfg.api.model = model
                pcfg.api.effort = effort
                try:
                    provider = provider_factory(pcfg)
                except Exception as exc:  # provider setup is shadow-only
                    provider_error = str(exc)
                    raise
            verdicts = judge_packet(
                packet, provider, model=model,
                max_tokens=cfg.max_output_tokens, usage=usage,
                max_missing_retries=cfg.max_missing_retries)
            if checkpoint:
                checkpoint.put(
                    key, items=[v.model_dump(mode="json") for v in verdicts],
                    usage=usage_delta(before, usage), ok=True)
            rows.extend(verdicts)
            completed += 1
        except Exception as exc:  # experimental lane never blocks manuscript
            reason = f"{type(exc).__name__}: {exc}"
            if checkpoint:
                checkpoint.put(key, items=[], usage=usage_delta(before, usage),
                               ok=False)
            _record_failure(run, packet, stage, reason)
            failed += 1
            log.warning("Examination %s packet %s failed: %s",
                        stage, packet.packet_id, reason)
    return rows, completed, failed


def run_shadow_judgment(run, root_cfg, *, provider_factory, checkpoint=None,
                        should_cancel=None) -> Usage:
    """Judge a deterministic sample and mutate only the shadow ledger."""
    cfg = root_cfg.examination_graph.judgment
    candidates = eligible_sites(run, cfg)
    selected, sample_omitted, cap_omitted = _sample(
        candidates, cfg.sample_rate, cfg.max_sites)
    run.judgment_selection = {
        "eligible_sites": len(candidates),
        "selected_sites": len(selected),
        "sample_omitted_sites": sample_omitted,
        "site_cap_omitted_sites": cap_omitted,
    }
    if not selected:
        return Usage()

    service = ContextService(run.doc)
    packets = build_packets(selected, service, batch_size=cfg.batch_size)
    usage = Usage()
    primary, primary_done, primary_failed = _run_packets(
        run, packets, stage="primary", model=cfg.primary_model,
        effort=cfg.primary_effort, cfg=cfg, root_cfg=root_cfg,
        provider_factory=provider_factory, checkpoint=checkpoint, usage=usage,
        should_cancel=should_cancel)
    escalation_ids = _apply_primary(
        run, primary, can_escalate=bool(cfg.escalation_model))

    escalation_done = escalation_failed = 0
    if escalation_ids and cfg.escalation_model:
        sites = [run.ledger.site(site_id) for site_id in escalation_ids]
        escalation_packets = build_packets(
            sites, service, batch_size=cfg.batch_size)
        escalated, escalation_done, escalation_failed = _run_packets(
            run, escalation_packets, stage="escalation",
            model=cfg.escalation_model, effort=cfg.escalation_effort,
            cfg=cfg, root_cfg=root_cfg, provider_factory=provider_factory,
            checkpoint=checkpoint, usage=usage,
            should_cancel=should_cancel)
        _apply_escalation(run, escalated)

    # Escalations that could not be completed stay explicitly escalated. They
    # are not turned into a pass or silently dropped.
    run.judgment_execution = {
        "primary_model": cfg.primary_model,
        "escalation_model": cfg.escalation_model,
        "primary_packets": len(packets),
        "primary_packets_completed": primary_done,
        "primary_packets_failed": primary_failed,
        "escalated_sites": len(escalation_ids),
        "escalation_packets_completed": escalation_done,
        "escalation_packets_failed": escalation_failed,
        "prompt_fingerprint": judgment_prompt_fingerprint(),
        "max_cost_usd": cfg.max_cost_usd,
    }
    run.judgment_usage = dataclasses.asdict(usage)
    return usage
