"""Feature-gated candidate generation, screening, and guarded application."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .candidate_generators import generate_initial_candidates
from .candidate_judge import (
    candidate_judgment_fingerprint, candidate_judgment_prompt,
    judge_screening_packet)
from .candidate_ledger import CandidateLedger
from .candidate_models import (
    Candidate, CandidateAnchor, CandidateStatus, ScreeningPacket, Verdict,
    deterministic_candidate_id)
from .candidate_coverage import coverage_summary as _coverage_summary
from .candidate_packet import build_screening_packets
from .context_service import ContextService, unsupported_recipes
from .models import DocumentModel, Finding, Usage, index_paragraphs
from .providers import cost_of_usage, estimate_cost
from .utils.tokens import estimate_tokens

log = logging.getLogger("docproof.candidate_screening")


_GHOST_SITE_TYPES = frozenset({"quote_balance"})


class CandidateScreeningCancelled(RuntimeError):
    pass

_FINDING_TYPES = {
    "dialogue_tag": "dialogue_tag_punctuation",
    "sweep_dialogue_tag": "dialogue_tag_punctuation",
    "unclosed_quote": "quote_balance",
    "introductory_comma": "introductory_comma",
    "direct_address_comma": "direct_address_comma",
    "number_style": "number_style",
    "sweep_century": "number_style",
    "sweep_compound_number": "number_style",
    "currency_style": "currency_style",
    "repeated_word": "repeated_word",
    "sweep_doubled_word": "repeated_word",
    "word_echo": "word_echo",
    "heading_sequence": "heading_sequence",
    "list_punctuation": "list_punctuation",
}


@dataclass
class CandidateScreeningRun:
    """One isolated candidate ledger and its screening/application state."""

    ledger: CandidateLedger
    doc: DocumentModel
    mode: str = "shadow"
    packets: list[ScreeningPacket] = field(default_factory=list)
    generation_omissions: Counter = field(default_factory=Counter)
    local_resolved_ids: set[str] = field(default_factory=set)
    model_judged_ids: set[str] = field(default_factory=set)
    escalated_ids: set[str] = field(default_factory=set)
    budget_omissions: set[str] = field(default_factory=set)
    packet_records: list[dict] = field(default_factory=list)
    verdict_costs: dict[tuple[str, str], float] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    production_matches: dict[str, list[dict]] = field(default_factory=dict)
    discovery_ids: set[str] = field(default_factory=set)
    production_finding_ids: set[str] = field(default_factory=set)
    generated_coverage_finding_ids: set[str] = field(default_factory=set)
    candidate_finding_ids: dict[str, str] = field(default_factory=dict)
    emitted_candidate_ids: set[str] = field(default_factory=set)
    applied_candidate_ids: set[str] = field(default_factory=set)
    unapplied_candidate_ids: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)

    @classmethod
    def prepare(cls, cfg, doc: DocumentModel, *, paragraphs, sweep_findings=(),
                finding_sources=None) -> "CandidateScreeningRun":
        from .config import resolve_candidate_mode
        # P0-01 containment: a requested ``apply`` is clamped to ``shadow`` until
        # the release gate opens, so no path (config, profile, or UI) can mutate
        # a document while the subsystem is unvalidated.
        run = cls(CandidateLedger(), doc, mode=resolve_candidate_mode(cfg.mode))
        generated = generate_initial_candidates(
            doc, paragraphs, candidate_types=cfg.candidate_types,
            sweep_findings=sweep_findings, finding_sources=finding_sources)
        for candidate in generated:
            if len(run.ledger) >= cfg.max_candidates:
                run.generation_omissions[
                    f"{candidate.generator_id}:{candidate.candidate_type}"] += 1
                continue
            candidate_id, created = run.ledger.register(candidate)
            if not created:
                continue
            candidate = run.ledger.candidate(candidate_id)
            invalid = validate_candidate_anchor(candidate, doc)
            if invalid is not None:
                run.ledger.apply_verdict(invalid, stage="anchor_validation")
                continue
            run.ledger.observe(
                candidate_id, stage="anchor_validation",
                judge_id="candidate.anchor_validator",
                reason_code="anchor_valid",
                explanation="The part, paragraph, offsets, and observed text match.",
                input_record={
                    "anchors": [anchor.model_dump(mode="json", exclude_none=True)
                                for anchor in candidate.anchors],
                    "observed_text": candidate.observed_text,
                }, output_record={"valid": True})
            run._deterministic_screen(candidate)

        unresolved = [candidate for candidate in run.ledger.candidates
                      if run.ledger.status(candidate.candidate_id)
                      == CandidateStatus.NEEDS_MODEL_JUDGMENT]
        # P3-02: a candidate whose recipe asks for context ContextService cannot
        # assemble is deferred with an explicit reason, not packaged and judged
        # on partial context as if it were complete.
        ready = []
        for candidate in unresolved:
            missing = unsupported_recipes(candidate.context_recipe)
            if missing:
                run.ledger.transition(
                    candidate.candidate_id, CandidateStatus.DEFERRED,
                    stage="context_assembly",
                    judge_id="candidate.context_service",
                    reason_code="insufficient_context",
                    explanation="Required context is not available: "
                                + ", ".join(missing) + ".",
                    input_record={"context_recipe": list(candidate.context_recipe),
                                  "unavailable": list(missing)},
                    output_record={"deferred": True})
            else:
                ready.append(candidate)
        if ready:
            run.packets = build_screening_packets(
                ready, ContextService(doc), batch_size=cfg.batch_size)
            packet_by_id = {
                candidate_id: packet.packet_id
                for packet in run.packets for candidate_id in packet.candidate_ids
            }
            for candidate in ready:
                run.ledger.transition(
                    candidate.candidate_id, CandidateStatus.PACKET_READY,
                    stage="context_assembly", judge_id="candidate.context_service",
                    reason_code="packet_ready",
                    explanation="Minimum requested context was assembled and shared.",
                    input_record={"context_recipe": list(candidate.context_recipe)},
                    output_record={"packet_id": packet_by_id[candidate.candidate_id]})
        run.ledger.assert_accounted()
        return run

    def _deterministic_screen(self, candidate: Candidate) -> None:
        hint = candidate.evidence.get("local_screening") or {}
        decision = hint.get("decision", "needs_model_judgment")
        judge = str(hint.get("judge_id") or candidate.generator_id)
        reason = str(hint.get("reason_code") or "local_rule_inconclusive")
        explanation = str(hint.get("explanation") or "Local rule was inconclusive.")
        if decision in {"pass", "error"}:
            verdict = Verdict(
                candidate_id=candidate.candidate_id, decision=decision,
                corrected_text=(candidate.candidate_correction
                                if decision == "error" else None),
                confidence="high", reason_code=reason,
                explanation=explanation if decision == "error" else None,
                evidence_used=("local_screening",), judge_id=judge,
                meaning_change_risk=candidate.meaning_change_risk)
            verdict = validate_correction(candidate, verdict, self.doc)
            self.ledger.apply_verdict(verdict, stage="deterministic_resolution")
            self.local_resolved_ids.add(candidate.candidate_id)
            return

        self.ledger.transition(
            candidate.candidate_id, CandidateStatus.NEEDS_SCREENING,
            stage="deterministic_resolution", judge_id=judge,
            reason_code=reason, explanation=explanation,
            input_record={"evidence": candidate.evidence},
            output_record={"decision": "needs_screening"})
        # The initial lightweight layer combines the independent local signals
        # but deliberately does not pretend ambiguity is negative evidence.
        self.ledger.transition(
            candidate.candidate_id, CandidateStatus.NEEDS_MODEL_JUDGMENT,
            stage="lightweight_screening",
            judge_id="candidate.lightweight_combiner",
            reason_code="ambiguous_after_local_screening",
            explanation="No strong independent local evidence settled this site.",
            input_record={"risk_prior": candidate.risk_prior,
                          "meaning_change_risk": candidate.meaning_change_risk},
            output_record={"decision": "needs_model_judgment"})

    def screen(self, root_cfg, *, provider_factory, should_cancel=None) -> Usage:
        """Run compact paid judgment and retain every verdict in the ledger."""
        cfg = root_cfg.candidate_screening
        if not cfg.judgment_enabled or not self.packets:
            return Usage()
        primary = self._judge_packets(
            self.packets, stage="batched_primary_judgment",
            model=cfg.model, effort=cfg.effort, root_cfg=root_cfg,
            provider_factory=provider_factory, should_cancel=should_cancel)

        conflicts = _overlapping_error_ids(primary, self.ledger)
        escalate: list[str] = []
        for verdict in primary:
            candidate = self.ledger.candidate(verdict.candidate_id)
            routed = (
                verdict.decision in {"uncertain", "defer"}
                or verdict.candidate_id in conflicts
                or (verdict.decision == "error"
                    and (candidate.meaning_change_risk == "high"
                         or verdict.meaning_change_risk == "high"
                         or verdict.confidence == "low")))
            if routed and cfg.escalation_model:
                self.escalated_ids.add(verdict.candidate_id)
                escalate.append(verdict.candidate_id)
                self.ledger.observe(
                    verdict.candidate_id, stage="escalation",
                    judge_id="candidate.risk_router",
                    reason_code="second_opinion_required",
                    explanation="Risk, uncertainty, or an overlapping correction requires escalation.",
                    output_record=verdict.model_dump(mode="json", exclude_none=True),
                    verdict=verdict,
                    cost_usd=self.verdict_costs.get(
                        ("batched_primary_judgment", verdict.candidate_id), 0.0))
                continue
            if routed:
                verdict = verdict.model_copy(update={
                    "decision": "uncertain",
                    "reason_code": "reviewer_query_required",
                    "explanation": (
                        "Risk, uncertainty, or an overlapping correction "
                        "requires reviewer judgment."),
                    "confidence": "low",
                })
            checked = validate_correction(candidate, verdict, self.doc)
            self.ledger.apply_verdict(
                checked, stage="correction_validation",
                cost_usd=self.verdict_costs.get(
                    ("batched_primary_judgment", verdict.candidate_id), 0.0))
            self.model_judged_ids.add(verdict.candidate_id)

        if escalate and cfg.escalation_model:
            candidates = [self.ledger.candidate(candidate_id)
                          for candidate_id in escalate]
            packets = build_screening_packets(
                candidates, ContextService(self.doc), batch_size=cfg.batch_size)
            escalated = self._judge_packets(
                packets, stage="escalation", model=cfg.escalation_model,
                effort=cfg.escalation_effort, root_cfg=root_cfg,
                provider_factory=provider_factory, should_cancel=should_cancel)
            returned = {verdict.candidate_id for verdict in escalated}
            for verdict in escalated:
                candidate = self.ledger.candidate(verdict.candidate_id)
                checked = validate_correction(candidate, verdict, self.doc)
                self.ledger.apply_verdict(
                    checked, stage="correction_validation",
                    cost_usd=self.verdict_costs.get(
                        ("escalation", verdict.candidate_id), 0.0))
                self.model_judged_ids.add(verdict.candidate_id)
            # A failed escalation remains packet_ready and explicitly pending.
            for candidate_id in set(escalate) - returned:
                self.ledger.observe(
                    candidate_id, stage="escalation",
                    judge_id="candidate.escalation",
                    reason_code="escalation_unresolved",
                    explanation="No complete escalation verdict was returned.")
        self.ledger.assert_accounted()
        return self.usage

    def _judge_packets(self, packets: list[ScreeningPacket], *, stage: str,
                       model: str, effort: str | None, root_cfg,
                       provider_factory, should_cancel=None) -> list[Verdict]:
        cfg = root_cfg.candidate_screening
        provider = None
        rows: list[Verdict] = []
        for packet in packets:
            if should_cancel and should_cancel():
                raise CandidateScreeningCancelled()
            estimate = _packet_estimate(
                packet, model, cfg.max_output_tokens, effort)
            actual = cost_of_usage(
                self.usage, fallback_model=model, batch=False) or 0.0
            record = {
                "packet_id": packet.packet_id, "stage": stage,
                "model": model, "submitted_ids": list(packet.candidate_ids),
                "returned_ids": [], "estimated_cost_usd": estimate,
            }
            if estimate is None or actual + estimate > cfg.max_cost_usd:
                self.budget_omissions.update(packet.candidate_ids)
                record["failure"] = "configured_cost_ceiling"
                self.packet_records.append(record)
                for candidate_id in packet.candidate_ids:
                    self.ledger.observe(
                        candidate_id, stage=stage,
                        judge_id="candidate.cost_guard",
                        reason_code="cost_ceiling_deferred",
                        explanation="The packet did not fit under the configured model budget.",
                        evidence={"estimated_cost_usd": estimate,
                                  "max_cost_usd": cfg.max_cost_usd})
                continue
            try:
                if provider is None:
                    model_cfg = root_cfg.model_copy(deep=True)
                    model_cfg.api.model = model
                    model_cfg.api.effort = effort
                    provider = provider_factory(model_cfg)
                for candidate_id in packet.candidate_ids:
                    self.ledger.observe(
                        candidate_id, stage=stage, judge_id=model,
                        reason_code="packet_submitted",
                        explanation="Candidate was submitted in a compact shared-context packet.",
                        input_record={"packet_id": packet.packet_id})
                focused_retries: list[dict] = []
                record["focused_retries"] = focused_retries
                verdicts = judge_screening_packet(
                    packet, provider, model=model,
                    max_tokens=cfg.max_output_tokens, usage=self.usage,
                    max_missing_retries=cfg.max_missing_retries,
                    retry_log=focused_retries)
                record["returned_ids"] = [row.candidate_id for row in verdicts]
                record["complete"] = True
                after = cost_of_usage(
                    self.usage, fallback_model=model, batch=False) or actual
                packet_cost = max(0.0, after - actual)
                record["actual_cost_usd"] = round(packet_cost, 8)
                per_candidate = packet_cost / len(verdicts) if verdicts else 0.0
                for verdict in verdicts:
                    self.verdict_costs[(stage, verdict.candidate_id)] = \
                        per_candidate
                rows.extend(verdicts)
            except Exception as exc:
                after = cost_of_usage(
                    self.usage, fallback_model=model, batch=False) or actual
                packet_cost = max(0.0, after - actual)
                record["actual_cost_usd"] = round(packet_cost, 8)
                failure = {
                    "stage": stage, "packet_id": packet.packet_id,
                    "type": type(exc).__name__, "message": str(exc),
                    "review_unchanged": True,
                }
                self.failures.append(failure)
                record["failure"] = f"{type(exc).__name__}: {exc}"
                per_candidate = (
                    packet_cost / len(packet.candidate_ids)
                    if packet.candidate_ids else 0.0)
                for candidate_id in packet.candidate_ids:
                    self.ledger.observe(
                        candidate_id, stage=stage, judge_id=model,
                        reason_code="packet_failure",
                        explanation="The screening response was incomplete or malformed.",
                        evidence={"packet_id": packet.packet_id,
                                  "failure": record["failure"]},
                        cost_usd=per_candidate)
                log.warning("Candidate screening packet %s failed: %s",
                            packet.packet_id, exc)
            self.packet_records.append(record)
        return rows

    def production_findings(self) -> list[Finding]:
        """Convert safe confirmed errors into ordinary pending Findings.

        This is the lane's only bridge into document mutation. It is absent in
        shadow mode, rejects query-preferred and virtual sites, revalidates the
        exact anchor, and expresses the correction against its source paragraph.
        The shared validator then shrinks, arbitrates overlaps, enforces the
        confidence/edit guards, and the ordinary writer applies tracked changes.
        """
        from .config import candidate_apply_released
        if self.mode != "apply" or not candidate_apply_released():
            return []
        by_id = index_paragraphs(self.doc)
        findings: list[Finding] = []
        for candidate in self.ledger.candidates:
            if (self.ledger.status(candidate.candidate_id)
                    != CandidateStatus.ERROR
                    or candidate.channel_preference == "query"):
                continue
            verdict = next((event.verdict for event in reversed(self.ledger.events)
                            if event.candidate_id == candidate.candidate_id
                            and event.verdict is not None), None)
            correction = (verdict.corrected_text if verdict is not None
                          else candidate.candidate_correction)
            if correction is None:
                continue
            anchor = candidate.anchors[0]
            if (anchor.virtual_location or anchor.paragraph_id is None
                    or anchor.start_offset is None or anchor.end_offset is None):
                continue
            para = by_id.get(anchor.paragraph_id)
            if para is None or validate_candidate_anchor(candidate, self.doc):
                continue
            original = para.text
            corrected = (original[:anchor.start_offset] + correction
                         + original[anchor.end_offset:])
            if corrected == original:
                continue
            # Defense-in-depth universal insertion guard (P1-02/P1-04): even
            # though validate_correction already rejected invariant-violating
            # verdicts before they reached ERROR, withhold any correction that
            # would still create duplicate punctuation or malformed spacing, and
            # leave the rejection visible in the ledger.
            violation = text_invariant_violation(original, corrected)
            if violation is not None:
                self.ledger.observe(
                    candidate.candidate_id, stage="insertion_guard",
                    judge_id="candidate.insertion_guard",
                    reason_code="text_invariant_violation",
                    explanation=f"Correction withheld from the document: it {violation}.",
                    input_record={"original": original, "corrected": corrected},
                    output_record={"applied": False})
                continue
            finding_id = f"cs-{candidate.candidate_id.removeprefix('C-')}"
            explanation = ((verdict.explanation if verdict is not None else "")
                           or "Candidate screening confirmed this correction.")
            confidence = (verdict.confidence if verdict is not None else "high")
            findings.append(Finding(
                finding_id=finding_id, chunk_id="candidate-screening",
                para_id=para.para_id, error_type=candidate.candidate_type,
                original_text=original, occurrence=1,
                corrected_text=corrected,
                explanation=f"{explanation} [candidate {candidate.candidate_id}]",
                confidence=confidence))
            self.candidate_finding_ids[finding_id] = candidate.candidate_id
            self.emitted_candidate_ids.add(candidate.candidate_id)
        return findings

    def production_queries(self) -> list[Finding]:
        """Candidate queries as force_query margin comments (P3-05).

        Every finding here is ``force_query``: the writer renders a comment, so a
        query can modify the document (by annotating it) but can never silently
        become an edit. Absent in shadow mode and until Apply is released.
        """
        from .config import candidate_apply_released
        if self.mode != "apply" or not candidate_apply_released():
            return []
        by_id = index_paragraphs(self.doc)
        query_states = {CandidateStatus.UNCERTAIN, CandidateStatus.DEFERRED}
        findings: list[Finding] = []
        for candidate in self.ledger.candidates:
            if self.ledger.status(candidate.candidate_id) not in query_states:
                continue
            anchor = candidate.anchors[0]
            if (anchor.virtual_location or anchor.paragraph_id is None
                    or anchor.start_offset is None):
                continue
            para = by_id.get(anchor.paragraph_id)
            if para is None or validate_candidate_anchor(candidate, self.doc):
                continue
            verdict = next((event.verdict for event in reversed(self.ledger.events)
                            if event.candidate_id == candidate.candidate_id
                            and event.verdict is not None), None)
            explanation = ((verdict.explanation if verdict is not None else "")
                           or "Candidate screening raised a query here.")
            finding_id = f"csq-{candidate.candidate_id.removeprefix('C-')}"
            findings.append(Finding(
                finding_id=finding_id, chunk_id="candidate-screening-query",
                para_id=para.para_id, error_type=candidate.candidate_type,
                original_text=para.text, occurrence=1,
                corrected_text=para.text,
                explanation=f"{explanation} [candidate {candidate.candidate_id}]",
                confidence="low", force_query=True))
            self.candidate_finding_ids[finding_id] = candidate.candidate_id
            self.emitted_candidate_ids.add(candidate.candidate_id)
        return findings

    def observe_findings(self, findings: list[Finding], *, applied_ids=()) -> None:
        """Link final validator/writer outcomes back to candidate evidence."""
        from .site_generators import site_from_finding

        applied = set(applied_ids)
        for finding in findings:
            self.production_finding_ids.add(finding.finding_id)
            direct_id = self.candidate_finding_ids.get(finding.finding_id)
            if direct_id is not None:
                was_applied = finding.finding_id in applied
                (self.applied_candidate_ids if was_applied
                 else self.unapplied_candidate_ids).add(direct_id)
                evidence = {
                    "finding_id": finding.finding_id,
                    "finding_status": finding.status,
                    "finding_error_type": finding.error_type,
                    "confidence": finding.confidence,
                    "applied_as_tracked_change": was_applied,
                }
                self.generated_coverage_finding_ids.add(finding.finding_id)
                self._record_production_match(
                    self.ledger.candidate(direct_id), evidence)
                continue
            candidate_type = _FINDING_TYPES.get(
                finding.error_type, finding.error_type)
            site = site_from_finding(finding, self.doc)
            if site is None:
                continue
            anchor = site.anchors[0]
            if anchor.paragraph_id is None:
                continue
            matched = []
            if candidate_type is not None:
                matched = [
                    candidate for candidate in self.ledger.candidates
                    if candidate.candidate_type == candidate_type
                    and candidate.anchors[0].paragraph_id == anchor.paragraph_id
                    and _overlaps(
                        candidate.anchors[0].start_offset,
                        candidate.anchors[0].end_offset,
                        anchor.start_offset, anchor.end_offset)
                ]
            evidence = {
                "finding_id": finding.finding_id,
                "finding_status": finding.status,
                "finding_error_type": finding.error_type,
                "confidence": finding.confidence,
                "applied_as_tracked_change": finding.finding_id in applied,
            }
            if matched:
                self.generated_coverage_finding_ids.add(finding.finding_id)
                for candidate in matched:
                    if candidate.candidate_id in self.emitted_candidate_ids:
                        (self.applied_candidate_ids
                         if finding.finding_id in applied
                         else self.unapplied_candidate_ids).add(
                             candidate.candidate_id)
                    self._record_production_match(candidate, evidence)
                continue

            # The existing exploratory reviewer remains in place. A material
            # finding outside the generated taxonomy enters this same ledger as
            # an open-discovery candidate, so it can become a future generator
            # pattern instead of disappearing from candidate coverage metrics.
            discovery = _candidate_from_discovery(finding, site, self.doc)
            if discovery is None:
                continue
            candidate_id, created = self.ledger.register(discovery)
            candidate = self.ledger.candidate(candidate_id)
            self.discovery_ids.add(candidate_id)
            self._record_production_match(candidate, evidence)
            if not created:
                continue
            invalid = validate_candidate_anchor(candidate, self.doc)
            if invalid is not None:
                self.ledger.apply_verdict(invalid, stage="anchor_validation")
                continue
            self.ledger.observe(
                candidate_id, stage="anchor_validation",
                judge_id="candidate.anchor_validator",
                reason_code="anchor_valid",
                explanation="The discovery finding retained a valid exact anchor.")
            verdict = _discovery_verdict(candidate, finding)
            verdict = validate_correction(candidate, verdict, self.doc)
            self.ledger.apply_verdict(verdict, stage="open_discovery")

    def _record_production_match(self, candidate: Candidate,
                                 evidence: dict) -> None:
        if evidence in self.production_matches.setdefault(
                candidate.candidate_id, []):
            return
        self.production_matches[candidate.candidate_id].append(evidence)
        self.ledger.observe(
            candidate.candidate_id, stage="finalization",
            judge_id="production_pipeline",
            reason_code="production_finding_overlap",
            explanation="The existing pipeline surfaced an overlapping finding.",
            evidence=evidence)

    def report(self, cfg, *, source: str) -> dict:
        projection = self.ledger.projection()
        total = len(self.ledger)
        terminal = len(self.ledger.terminal_ids())
        states = self.ledger.state_counts()
        by_type = Counter(candidate.candidate_type
                          for candidate in self.ledger.candidates)
        errors = states.get("error", 0)
        queries = states.get("uncertain", 0) + states.get("deferred", 0)
        matched = len(self.production_matches)
        incomplete = sum(not row.get("complete", False)
                         and row.get("failure") != "configured_cost_ceiling"
                         for row in self.packet_records)
        cost = cost_of_usage(
            self.usage, fallback_model=cfg.model, batch=False) or 0.0
        return {
            "schema_version": 1,
            "mode": self.mode,
            "source": source,
            "scope": {
                "shadow_only": self.mode == "shadow",
                "may_create_findings": self.mode == "apply",
                "may_modify_documents": self.mode == "apply",
                "candidate_types": list(cfg.candidate_types),
            },
            "accounting": {
                "generated_candidates": total,
                "terminal_candidates": terminal,
                "explicitly_pending_candidates": total - terminal,
                "terminal_percent": round(
                    terminal / total * 100 if total else 100.0, 2),
                "all_candidates_have_state": len(projection) == total,
                "events": len(self.ledger.events),
            },
            "states": states,
            "candidate_types": dict(sorted(by_type.items())),
            "coverage": _coverage_summary(set(by_type)),
            "generation_omissions": dict(sorted(self.generation_omissions.items())),
            "screening": {
                "resolved_locally": len(self.local_resolved_ids),
                "resolved_locally_percent": round(
                    len(self.local_resolved_ids) / total * 100 if total else 100.0,
                    2),
                "model_judged": len(self.model_judged_ids),
                "escalated": len(self.escalated_ids),
                "reviewer_queries": queries,
                "anchor_failures": states.get("invalid", 0),
                "packets": len(self.packet_records),
                "incomplete_packets": incomplete,
                "budget_omitted_candidates": len(self.budget_omissions),
                "estimated_cost_usd": round(cost, 6),
            },
            "comparison": {
                "candidate_errors": errors,
                "candidates_overlapping_production_findings": matched,
                "candidate_errors_without_production_match": sum(
                    self.ledger.status(candidate.candidate_id)
                    == CandidateStatus.ERROR
                    and candidate.candidate_id not in self.production_matches
                    for candidate in self.ledger.candidates),
                "production_matches": self.production_matches,
                "open_discovery_candidates": len(self.discovery_ids),
            },
            "application": {
                "emitted_findings": len(self.emitted_candidate_ids),
                "applied_tracked_changes": len(self.applied_candidate_ids),
                "not_applied": len(self.unapplied_candidate_ids),
                "finding_to_candidate": dict(sorted(
                    self.candidate_finding_ids.items())),
            },
            "metrics": {
                "candidate_generation_recall": None,
                "candidate_generation_recall_against_production": round(
                    len(self.generated_coverage_finding_ids)
                    / len(self.production_finding_ids) * 100, 2)
                    if self.production_finding_ids else 100.0,
                "screening_recall": None,
                "screening_precision": None,
                "human_baseline_required": True,
                "additional_error_candidates_per_dollar": (
                    round(errors / cost, 3) if cost else None),
            },
            "packet_records": self.packet_records,
            "usage": dataclasses.asdict(self.usage),
            "failures": self.failures,
        }

    def write(self, out_dir: Path, cfg, *, source: str
              ) -> tuple[dict, Path, Path]:
        report = self.report(cfg, source=source)
        ledger_path = out_dir / cfg.ledger_filename
        report_path = out_dir / cfg.report_filename
        if cfg.write_ledger:
            self.ledger.write_jsonl(ledger_path)
        if cfg.write_report:
            json_path = out_dir / cfg.report_json_filename
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            report_path.write_text(_markdown_report(report), encoding="utf-8")
        return report, ledger_path, report_path

    def record_failure(self, stage: str, error: Exception) -> None:
        self.failures.append({
            "stage": stage, "type": type(error).__name__,
            "message": str(error), "review_unchanged": True})

    def failure_warnings(self) -> list[str]:
        return [
            f"Candidate screening {row['stage']} failed "
            f"({row['type']}: {row['message']}); candidate results may be "
            "incomplete, other review sources unchanged"
            for row in self.failures
        ]


# Punctuation sweeps reused as candidate sources (P2-04) — ERROR sweeps only.
# The ellipsis and dash sweeps are deliberately absent: they are house-style
# normalization, and routing them through the candidate lane buried the real
# catches under hundreds of restyling edits (the Johnson canary was ~90% dash
# spacing). Style stays with the ordinary sweep/normalizer channel; this lane
# exists to catch the errors the other passes miss.
_REUSE_SWEEP_KEYS = (
    "sweep_stacked_punctuation",
    "sweep_terminal_period", "sweep_quote_punctuation", "sweep_nested_quote",
)


def _grammar_findings(cfg, doc: DocumentModel, rows) -> list[Finding]:
    """LanguageTool proposals as Findings for the grammar candidate adapter."""
    try:
        from .languagetool import propose
    except Exception:  # pragma: no cover - optional dependency
        return []
    variant = _load_variant(cfg)
    dictionary = (cfg.spellcheck.dictionary or getattr(variant, "dictionary", None)
                  or "en-US")
    cands = propose(rows, dictionary=dictionary)
    by_id = index_paragraphs(doc)
    findings: list[Finding] = []
    for index, rc in enumerate(cands):
        para = by_id.get(rc.para_id)
        if para is None:
            continue
        corrected = para.text[:rc.start] + rc.replacement + para.text[rc.end:]
        findings.append(Finding(
            # The transient Finding carries LanguageTool's own label for
            # provenance; the candidate it becomes is typed "grammar".
            finding_id=f"lt-{index:04d}", chunk_id="languagetool",
            para_id=rc.para_id, error_type="languagetool",
            original_text=para.text, occurrence=1, corrected_text=corrected,
            explanation=rc.note or "LanguageTool flagged a mechanical issue.",
            confidence="medium"))
    return findings


def _load_variant(cfg):
    from .variants import load_variant
    return load_variant(getattr(cfg, "variant", None))


def _local_analyzer_sources(cfg, doc: DocumentModel, paragraphs, *,
                            existing_sweeps=()):
    """Run the free local analyzers and return (sweep_findings, finding_sources)
    so standalone candidate mode reuses them as ledger candidates (P2-01/02)."""
    from .consistency import find_inconsistencies, to_findings
    from .sweeps import run_sweeps, unclosed_quote_findings

    rows = list(paragraphs) if paragraphs is not None else list(doc.paragraphs)
    variant = _load_variant(cfg)
    sweep_findings = list(existing_sweeps)
    if not sweep_findings:
        found, _reports = run_sweeps(
            rows, _REUSE_SWEEP_KEYS, variant,
            ellipsis_style=cfg.style.ellipsis)
        sweep_findings = found + unclosed_quote_findings(rows, variant)

    finding_sources: dict[str, list[Finding]] = {}
    report = find_inconsistencies(
        # Forced on for reuse: in standalone candidate mode the scan feeds the
        # ledger as candidates, so it runs even when the normal stage is off.
        doc.paragraphs, enabled=True,
        min_length=cfg.consistency.min_length,
        min_dominance=cfg.consistency.min_dominance,
        names=cfg.consistency.names,
        name_dominance=cfg.consistency.name_dominance,
        name_min_count=cfg.consistency.name_min_count,
        spelling_variants=cfg.consistency.spelling_variants,
        abbreviations=cfg.consistency.abbreviations,
        acronym_case=cfg.consistency.acronym_case,
        chicago_notes=cfg.consistency.chicago_notes,
        max_queries_per_kind=cfg.consistency.max_queries_per_kind,
        respell=variant.respell_map,
        dictionary=cfg.spellcheck.dictionary or variant.dictionary)
    consistency_findings = to_findings(report, doc.paragraphs)
    if consistency_findings:
        finding_sources["term_consistency"] = consistency_findings
    if cfg.candidate_screening.languagetool_floor:
        grammar = _grammar_findings(cfg, doc, rows)
        if grammar:
            finding_sources["grammar"] = grammar
    return sweep_findings, finding_sources


def prepare_candidate_screening(cfg, doc: DocumentModel, *, paragraphs,
                                sweep_findings=()
                                ) -> CandidateScreeningRun | None:
    from .config import candidate_screening_killed
    screen_cfg = cfg.candidate_screening
    if candidate_screening_killed() or screen_cfg.mode == "off":
        return None
    finding_sources = None
    # Standalone candidate mode: no ordinary detector or ensemble is running, so
    # reused analyzer output cannot double up with the normal review — route it
    # through the ledger as candidates (P2-01/02).
    standalone = (not getattr(cfg, "error_types", None)
                  and not cfg.ensemble.detectors)
    if screen_cfg.reuse_local_analyzers and standalone:
        sweep_findings, finding_sources = _local_analyzer_sources(
            cfg, doc, paragraphs, existing_sweeps=sweep_findings)
    return CandidateScreeningRun.prepare(
        screen_cfg, doc, paragraphs=paragraphs,
        sweep_findings=sweep_findings, finding_sources=finding_sources)


def validate_candidate_anchor(candidate: Candidate,
                              doc: DocumentModel) -> Verdict | None:
    by_id = index_paragraphs(doc)
    document_parts = {para.part for para in doc.paragraphs}
    for index, anchor in enumerate(candidate.anchors):
        if anchor.document_part not in document_parts:
            return _invalid(candidate, "stale_document_part",
                            "The anchored OOXML document part does not exist.")
        if anchor.virtual_location:
            if candidate.candidate_type not in _GHOST_SITE_TYPES:
                return _invalid(candidate, "unregistered_ghost_site",
                                "This candidate type cannot use virtual anchors.")
            continue
        para = by_id.get(anchor.paragraph_id or "")
        if para is None or para.part != anchor.document_part:
            return _invalid(candidate, "stale_paragraph_anchor",
                            "The anchored document part or paragraph does not exist.")
        if anchor.start_offset is None or anchor.end_offset is None:
            if anchor.object_id:
                continue
            return _invalid(candidate, "missing_offsets",
                            "A text anchor is missing character offsets.")
        if not (0 <= anchor.start_offset <= anchor.end_offset <= len(para.text)):
            return _invalid(candidate, "offset_out_of_bounds",
                            "The candidate offsets are outside the paragraph.")
        if index == 0:
            # P1-03: distinguish a replacement span from a zero-width insertion
            # and fail closed on ambiguity. A replacement must name the
            # non-empty text it replaces and match it exactly; an insertion
            # cannot claim observed text (an empty observed span validates at
            # every offset, so it carries no positional evidence — the insertion
            # guard, not the anchor, is what keeps it safe).
            zero_width = anchor.start_offset == anchor.end_offset
            actual = para.text[anchor.start_offset:anchor.end_offset]
            if not zero_width:
                if not candidate.observed_text:
                    return _invalid(
                        candidate, "missing_observed_span",
                        "A replacement anchor must record the non-empty text it replaces.")
                if actual != candidate.observed_text:
                    return _invalid(
                        candidate, "anchored_text_mismatch",
                        "The observed text no longer matches the source anchor.")
            elif candidate.observed_text:
                return _invalid(
                    candidate, "insertion_observed_mismatch",
                    "A zero-width insertion anchor cannot claim observed text.")
    return None


_DUP_GUARD_MARKS = (",", ";", ":")
_SPACE_BEFORE_PUNCT = re.compile(r"\s[,;:]")
# A comma/semicolon/colon must never sit directly against a sentence terminal,
# an ellipsis, or a dash — "No,.", "word,…", "Tannithan—," are all wrong. The
# Johnson run produced these when an opener before a full stop or an interrupted
# line was flagged as an intro/compound comma.
_COMMA_ABUTS_TERMINAL = re.compile(r"[,;:]\s*[.!?…—–]|[.!?…—–]\s*[,;:]")


def new_adjacent_duplicate_punctuation(original: str, corrected: str
                                       ) -> str | None:
    """Return the offending mark pair if ``corrected`` introduces an adjacent
    duplicate of a comma/semicolon/colon that was not present in ``original``.

    This is the universal insertion guard (P1-02): it catches the reported
    double-comma failure regardless of which generator or judge produced the
    correction. Legitimate runs already in the source (``!!``, ``--``, ``...``,
    or a pre-existing ``,,``) are not flagged because the count must strictly
    increase.
    """
    for mark in _DUP_GUARD_MARKS:
        pair = mark * 2
        if corrected.count(pair) > original.count(pair):
            return pair
    return None


def text_invariant_violation(original: str, corrected: str) -> str | None:
    """Final-document invariant for a single correction (P1-02, P1-04).

    Rejects a correction that would create adjacent duplicate punctuation, a new
    doubled space, or a new space before a comma/semicolon/colon. Lost text,
    formatting damage, and reject-all recoverability are enforced downstream by
    the shared validator and the strict reopen-and-diff audit; this guard closes
    the punctuation/spacing gap those do not cover for candidate insertions.
    """
    duplicate = new_adjacent_duplicate_punctuation(original, corrected)
    if duplicate is not None:
        return f"would create duplicate punctuation {duplicate!r}"
    if (len(_COMMA_ABUTS_TERMINAL.findall(corrected))
            > len(_COMMA_ABUTS_TERMINAL.findall(original))):
        return "would place a comma against a period, ellipsis, or dash"
    if "  " in corrected and corrected.count("  ") > original.count("  "):
        return "would create a doubled space"
    if (len(_SPACE_BEFORE_PUNCT.findall(corrected))
            > len(_SPACE_BEFORE_PUNCT.findall(original))):
        return "would create a space before punctuation"
    return None


def _correction_preview(candidate: Candidate, correction: str,
                        doc: DocumentModel) -> tuple[str, str] | None:
    """The (original, corrected) paragraph text a correction would produce, or
    None when the first anchor is not a concrete in-paragraph text span."""
    anchor = candidate.anchors[0]
    if (anchor.virtual_location or anchor.paragraph_id is None
            or anchor.start_offset is None or anchor.end_offset is None):
        return None
    para = index_paragraphs(doc).get(anchor.paragraph_id)
    if para is None:
        return None
    original = para.text
    corrected = (original[:anchor.start_offset] + correction
                 + original[anchor.end_offset:])
    return original, corrected


def validate_correction(candidate: Candidate, verdict: Verdict,
                        doc: DocumentModel) -> Verdict:
    invalid = validate_candidate_anchor(candidate, doc)
    if invalid is not None:
        return invalid
    if verdict.decision != "error":
        return verdict
    correction = verdict.corrected_text
    observed = candidate.observed_text
    if correction is None:
        return verdict.model_copy(update={
            "decision": "uncertain", "reason_code": "missing_correction",
            "explanation": "An error verdict did not include a correction.",
            "confidence": "low"})
    if correction == observed:
        return verdict.model_copy(update={
            "decision": "uncertain", "reason_code": "unchanged_correction",
            "explanation": "The proposed correction does not change the anchor.",
            "confidence": "low"})
    before = observed or ""
    if max(len(before), len(correction)) > 80 \
            and abs(len(correction) - len(before)) > 20:
        return verdict.model_copy(update={
            "decision": "uncertain", "reason_code": "excessive_rewrite",
            "explanation": "The proposed correction is larger than a minimal proofread.",
            "confidence": "low"})
    preview = _correction_preview(candidate, correction, doc)
    if preview is not None:
        violation = text_invariant_violation(*preview)
        if violation is not None:
            return verdict.model_copy(update={
                "decision": "uncertain",
                "reason_code": "text_invariant_violation",
                "explanation": f"Correction withheld: it {violation}.",
                "confidence": "low"})
    if (candidate.meaning_change_risk == "high"
            and candidate.channel_preference == "query"):
        return verdict.model_copy(update={
            "decision": "uncertain", "reason_code": "unsafe_meaning_change",
            "explanation": "The plausible correction could change meaning and should be queried.",
            "confidence": "low"})
    return verdict


def _invalid(candidate: Candidate, reason: str, explanation: str) -> Verdict:
    return Verdict(
        candidate_id=candidate.candidate_id, decision="invalid",
        confidence="high", reason_code=reason, explanation=explanation,
        judge_id="candidate.anchor_validator",
        meaning_change_risk=candidate.meaning_change_risk)


def _packet_estimate(packet: ScreeningPacket, model: str, max_tokens: int,
                     effort: str | None) -> float | None:
    input_tokens = estimate_tokens(
        candidate_judgment_prompt() + packet.prompt_payload())
    return estimate_cost(
        model, input_tokens=math.ceil(input_tokens * 1.2),
        output_tokens=max_tokens, effort=effort)


def _overlapping_error_ids(verdicts: list[Verdict], ledger: CandidateLedger
                           ) -> set[str]:
    errors = [verdict for verdict in verdicts if verdict.decision == "error"]
    conflicts = set()
    for index, first in enumerate(errors):
        a = ledger.candidate(first.candidate_id).anchors[0]
        for second in errors[index + 1:]:
            b = ledger.candidate(second.candidate_id).anchors[0]
            if a.paragraph_id == b.paragraph_id and _overlaps(
                    a.start_offset, a.end_offset, b.start_offset, b.end_offset):
                conflicts.update((first.candidate_id, second.candidate_id))
    return conflicts


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    if None in {a_start, a_end, b_start, b_end}:
        return False
    # Zero-width insertions collide when they share or touch a claimed span.
    if a_start == a_end:
        return b_start <= a_start <= b_end
    if b_start == b_end:
        return a_start <= b_start <= a_end
    return a_start < b_end and b_start < a_end


def _candidate_from_discovery(finding: Finding, site,
                              doc: DocumentModel) -> Candidate | None:
    from .validator import shrink

    by_id = index_paragraphs(doc)
    para = by_id.get(finding.para_id)
    if para is None:
        return None
    site_anchor = site.anchors[0]
    if site_anchor.start_offset is None or site_anchor.end_offset is None:
        return None
    anchor = CandidateAnchor(
        document_part=site_anchor.part,
        paragraph_id=site_anchor.paragraph_id,
        start_offset=site_anchor.start_offset,
        end_offset=site_anchor.end_offset,
        object_id=site_anchor.object_id)
    observed = para.text[site_anchor.start_offset:site_anchor.end_offset]
    correction = None
    if finding.corrected_text != finding.original_text:
        if finding.anchor is not None:
            correction = finding.anchor.insert_text
        else:
            _prefix, _deleted, correction = shrink(
                finding.original_text, finding.corrected_text)
    candidate_type = _FINDING_TYPES.get(
        finding.error_type, finding.error_type)
    candidate_id = deterministic_candidate_id(
        candidate_type, (anchor,), observed, correction)
    return Candidate(
        candidate_id=candidate_id, candidate_type=candidate_type,
        generator_id="open_discovery.existing_reviewer", anchors=(anchor,),
        observed_text=observed, candidate_correction=correction,
        evidence={
            "finding_id": finding.finding_id,
            "error_type": finding.error_type,
            "finding_status": finding.status,
            "explanation": finding.explanation,
            "confidence": finding.confidence,
        },
        context_recipe=("current sentence", "current paragraph"),
        risk_prior=0.7, meaning_change_risk="medium",
        channel_preference=(
            "query" if finding.force_query
            or finding.corrected_text == finding.original_text else "edit"))


def _discovery_verdict(candidate: Candidate, finding: Finding) -> Verdict:
    if finding.status == "validated":
        decision, reason = "error", "production_validated_error"
        explanation = finding.explanation or "The production validator admitted this finding."
    elif finding.status == "query" or finding.force_query:
        decision, reason = "uncertain", "production_reviewer_query"
        explanation = finding.explanation or "The production lane routed this to reviewer judgment."
    elif finding.status == "skipped_low_confidence":
        decision, reason = "uncertain", "production_low_confidence"
        explanation = finding.explanation or "The production lane retained this below the edit threshold."
    else:
        decision, reason = "pass", "production_rejected_finding"
        explanation = None
    return Verdict(
        candidate_id=candidate.candidate_id, decision=decision,
        corrected_text=(candidate.candidate_correction
                        if decision == "error" else None),
        confidence=(finding.confidence
                    if finding.confidence in {"low", "medium", "high"}
                    else "low"),
        reason_code=reason, explanation=explanation,
        evidence_used=(finding.finding_id,), judge_id="production_pipeline",
        meaning_change_risk=candidate.meaning_change_risk)


def _markdown_report(report: dict) -> str:
    accounting = report["accounting"]
    screening = report["screening"]
    application = report["application"]
    applying = report["mode"] == "apply"
    lines = [
        ("# Candidate screening report" if applying
         else "# Candidate screening shadow report"), "",
        ("Confirmed safe corrections entered the regular validator and Word "
         "tracked-change writer."
         if applying else
         "This lane generated and screened explicit candidates but did not "
         "create findings or modify the manuscript."), "",
        f"- Generated candidates: **{accounting['generated_candidates']:,}**",
        f"- Terminal verdicts: **{accounting['terminal_candidates']:,}** "
        f"({accounting['terminal_percent']:.2f}%)",
        f"- Explicitly pending: **{accounting['explicitly_pending_candidates']:,}**",
        f"- Resolved locally: **{screening['resolved_locally']:,}**",
        f"- Model judged: **{screening['model_judged']:,}**",
        f"- Incomplete packets: **{screening['incomplete_packets']:,}**",
        f"- Model cost: **${screening['estimated_cost_usd']:.4f}**", "",
        "## Verdict states", "", "| State | Candidates |", "|---|---:|",
    ]
    for state, count in report["states"].items():
        lines.append(f"| `{state}` | {count:,} |")
    lines += ["", "## Candidate types", "", "| Type | Candidates |",
              "|---|---:|"]
    for candidate_type, count in report["candidate_types"].items():
        lines.append(f"| `{candidate_type}` | {count:,} |")
    coverage = report.get("coverage")
    if coverage:
        counts = coverage["counts"]
        lines += [
            "", "## Generation coverage", "",
            f"Families — covered: **{counts['covered']}**, partial: "
            f"**{counts['partial']}**, gap: **{counts['gap']}**. A low-recall or "
            "incomplete run is visible here as families that produced nothing.",
            "", "| Family | Status | Produced this run |", "|---|---|---|"]
        for fam in coverage["families"]:
            produced = fam.get("produced_this_run")
            mark = ("—" if produced is None else "yes" if produced else "no")
            lines.append(f"| {fam['title']} | `{fam['status']}` | {mark} |")
    if report.get("generation_omissions"):
        lines += ["", "## Omissions (capped generation)", ""]
        for key, count in report["generation_omissions"].items():
            lines.append(f"- `{key}`: {count:,} candidate(s) dropped at the cap")
    if applying:
        lines += [
            "", "## Application", "",
            f"- Candidate findings emitted: **{application['emitted_findings']:,}**",
            f"- Applied as tracked changes: **{application['applied_tracked_changes']:,}**",
            f"- Not applied by downstream safeguards: **{application['not_applied']:,}**",
        ]
    lines += ["", "Recall and precision remain unset until these candidate "
              "verdicts are compared with human proofreading results.", ""]
    return "\n".join(lines)


__all__ = [
    "CandidateScreeningCancelled", "CandidateScreeningRun",
    "prepare_candidate_screening",
    "validate_candidate_anchor", "validate_correction",
    "candidate_judgment_fingerprint",
]
