"""Shadow-mode orchestration around DocProof's existing review pipeline."""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .coverage_report import (build_coverage_report, write_coverage_json,
                              write_coverage_markdown)
from .examination_graph import ExaminationGraph
from .models import DocumentModel, Finding
from .providers import cost_of_usage
from .context_service import ContextService
from .site_generators import (site_from_candidate, site_from_finding,
                              sites_from_model_obligations,
                              sites_from_spell_scan,
                              sites_from_sweep_obligations)
from .site_ledger import ExaminationLedger, LedgerInvariantError
from .site_models import LedgerState, Verdict

log = logging.getLogger("docproof.examination")


@dataclass
class ShadowExamination:
    ledger: ExaminationLedger
    graph: ExaminationGraph
    doc: DocumentModel
    max_sites: int
    mode: str = "shadow"
    omitted: Counter = field(default_factory=Counter)
    model_obligations: dict[tuple[str, str], str] = field(default_factory=dict)
    primary_judgment_verdicts: dict[str, Verdict] = field(default_factory=dict)
    judgment_verdicts: dict[str, Verdict] = field(default_factory=dict)
    judgment_failures: dict[str, dict] = field(default_factory=dict)
    judgment_budget_omissions: set[str] = field(default_factory=set)
    judgment_selection: dict = field(default_factory=dict)
    judgment_execution: dict = field(default_factory=dict)
    judgment_usage: dict = field(default_factory=dict)
    legacy_observations: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def prepare(cls, cfg, doc: DocumentModel, *, paragraphs,
                sweep_findings, consistency_findings, spell,
                adjudicate_candidates, sweep_keys,
                error_groups) -> "ShadowExamination":
        run = cls(ExaminationLedger(), ExaminationGraph.from_document(doc), doc,
                  max_sites=cfg.max_sites, mode=cfg.mode)

        # Negative evidence first: every configured sweep records a local pass
        # or local confirmation for every paragraph it actually scanned.
        for site in sites_from_sweep_obligations(
                paragraphs, sweep_keys, sweep_findings):
            run._register(site)

        # The old model contract returns only findings. These paragraph/type
        # obligations make that missing pass accounting visible in shadow mode.
        if cfg.model_obligations:
            for site in sites_from_model_obligations(
                    paragraphs, error_groups):
                if run._register(site):
                    para_id = site.anchors[0].paragraph_id or ""
                    for evidence_type in site.evidence.get("error_types", []):
                        run.model_obligations[(para_id, str(evidence_type))] = \
                            site.site_id

        for finding in tuple(sweep_findings) + tuple(consistency_findings):
            site = site_from_finding(finding, doc)
            if site is None:
                continue
            if finding.force_query or finding.corrected_text == finding.original_text:
                site = site.model_copy(update={"status": LedgerState.QUERY})
            else:
                site = site.model_copy(update={"status": LedgerState.LOCALLY_CONFIRMED})
            run._register(site)

        # Adjudication candidates have exact offsets and should win registration
        # over the spell scan's broader evidence when both describe one site.
        for candidate in adjudicate_candidates:
            site = site_from_candidate(candidate, doc)
            if site is not None:
                run._register(site)
        if cfg.spell_sites:
            for site in sites_from_spell_scan(spell, doc):
                run._register(site)

        log.info("Examination graph shadow: generated %d site(s), %d event(s)",
                 len(run.ledger), len(run.ledger.events))
        return run

    def observe_findings(self, findings: list[Finding], doc: DocumentModel,
                         *, applied_ids=()) -> None:
        applied = set(applied_ids)
        for finding in findings:
            site = site_from_finding(finding, doc)
            if site is None:
                continue
            existing = self.ledger.has(site.site_id)
            if not existing:
                if not self._register(site):
                    continue
            self._observe_target(site.site_id, finding, applied)

            obligation_id = self.model_obligations.get(
                (finding.para_id, finding.error_type))
            if obligation_id and obligation_id != site.site_id:
                self._observe_target(obligation_id, finding, applied)
        self.ledger.assert_accounted()

    def write(self, out_dir: Path, cfg, *, source: str) -> tuple[dict, Path, Path]:
        judgment = self.judgment_report(cfg.judgment)
        report = build_coverage_report(
            self.ledger, self.graph, mode=self.mode,
            omitted=dict(self.omitted), source=source, judgment=judgment)
        ledger_path = out_dir / cfg.ledger_filename
        report_path = out_dir / cfg.report_filename
        if cfg.write_ledger:
            self.ledger.write_jsonl(ledger_path)
        if cfg.write_report:
            write_coverage_json(out_dir / cfg.report_json_filename, report)
            write_coverage_markdown(report_path, report)
            queue, key = self.evaluation_queue()
            if queue:
                write_coverage_json(out_dir / cfg.evaluation_filename, {
                    "schema_version": 1,
                    "instructions": "Rate each row without opening the answer key.",
                    "ratings": ["candidate_a", "candidate_b", "both", "neither",
                                "unclear"],
                    "rows": queue,
                })
                write_coverage_json(out_dir / cfg.evaluation_key_filename, {
                    "schema_version": 1, "rows": key})
        return report, ledger_path, report_path

    def _observe_target(self, site_id: str, finding: Finding,
                        applied: set[str]) -> None:
        if site_id in self.primary_judgment_verdicts:
            observation = {
                "finding_id": finding.finding_id,
                "status": finding.status,
                "confidence": finding.confidence,
                "error_type": finding.error_type,
                "original_text": finding.original_text,
                "corrected_text": finding.corrected_text,
                "explanation": finding.explanation,
                "applied": finding.finding_id in applied,
            }
            self.legacy_observations.setdefault(site_id, []).append(observation)
            self.ledger.record_observation(
                site_id, actor="production reviewer",
                reason="existing review lane observed this judged site",
                evidence=observation)
            return
        self._confirm_site(site_id, finding)
        self._record_outcome(site_id, finding, applied)

    def comparison(self) -> dict:
        counts = Counter()
        for site_id, primary in self.primary_judgment_verdicts.items():
            verdict = self.judgment_verdicts.get(site_id, primary)
            legacy_found = bool(self.legacy_observations.get(site_id))
            if verdict.decision == "error" and legacy_found:
                counts["both_found_error"] += 1
            elif verdict.decision == "error":
                counts["examination_only_error"] += 1
            elif legacy_found:
                counts["production_only_error"] += 1
            elif verdict.decision == "pass":
                counts["examination_pass_production_silent"] += 1
            else:
                counts["unresolved"] += 1
        return {
            "judged_sites": len(self.primary_judgment_verdicts),
            **{key: counts.get(key, 0) for key in (
                "both_found_error", "examination_only_error",
                "production_only_error", "examination_pass_production_silent",
                "unresolved")},
            "failed_sites": len(self.judgment_failures),
            "budget_omitted_sites": len(self.judgment_budget_omissions),
        }

    def judgment_report(self, cfg) -> dict:
        usage = self.judgment_usage or {}
        cost = (cost_of_usage(usage, fallback_model=cfg.primary_model,
                              batch=False) or 0.0) if usage else 0.0
        return {
            "enabled": bool(cfg.enabled),
            "shadow_only": True,
            "may_create_findings": False,
            "selection": self.judgment_selection,
            "execution": self.judgment_execution,
            "usage": usage,
            "estimated_cost_usd": round(cost, 6),
            "comparison": self.comparison(),
        }

    def evaluation_queue(self) -> tuple[list[dict], list[dict]]:
        """Blinded disagreements plus a separate answer key."""
        service = ContextService(self.doc)
        queue, key = [], []
        for site_id, primary in sorted(self.primary_judgment_verdicts.items()):
            verdict = self.judgment_verdicts.get(site_id, primary)
            legacy = self.legacy_observations.get(site_id, [])
            legacy_found = bool(legacy)
            if verdict.decision == "pass" and not legacy_found:
                continue
            if verdict.decision == "error" and legacy_found:
                continue
            site = self.ledger.site(site_id)
            judge_candidate = {
                "decision": verdict.decision,
                "correction": verdict.correction,
                "explanation": verdict.explanation,
                "confidence": verdict.confidence,
            }
            legacy_candidate = ({
                "decision": "error",
                "correction": legacy[0].get("corrected_text"),
                "explanation": legacy[0].get("explanation"),
                "confidence": legacy[0].get("confidence"),
            } if legacy else {
                "decision": "no issue surfaced", "correction": None,
                "explanation": None, "confidence": None,
            })
            judge_is_a = int(hashlib.sha256(site_id.encode()).hexdigest(), 16) % 2 == 0
            evaluation_id = f"EVAL-{len(queue) + 1:05d}"
            queue.append({
                "evaluation_id": evaluation_id,
                "site_id": site_id,
                "site_type": site.site_type,
                "context": service.for_site(site).text,
                "candidate_a": judge_candidate if judge_is_a else legacy_candidate,
                "candidate_b": legacy_candidate if judge_is_a else judge_candidate,
                "human_rating": None,
                "human_note": "",
            })
            key.append({
                "evaluation_id": evaluation_id,
                "candidate_a_source": ("examination" if judge_is_a
                                       else "production"),
                "candidate_b_source": ("production" if judge_is_a
                                       else "examination"),
            })
        return queue, key

    def _register(self, site) -> bool:
        if self.ledger.has(site.site_id):
            return False
        if len(self.ledger) >= self.max_sites:
            self.omitted[f"{site.generator}:{site.site_type}"] += 1
            return False
        self.ledger.register(site, initial_state=site.status,
                             reason="phase-one shadow generator decision")
        return True

    def _confirm_site(self, site_id: str, finding: Finding) -> None:
        state = self.ledger.state(site_id)
        local = finding.chunk_id in {
            "sweep", "consistency", "residual", "calendar"}
        target = (LedgerState.LOCALLY_CONFIRMED if local
                  else LedgerState.MODEL_CONFIRMED)
        if state in {LedgerState.GENERATED, LedgerState.NEEDS_JUDGMENT}:
            self.ledger.transition(
                site_id, target, actor=finding.chunk_id,
                reason=f"legacy finding {finding.finding_id} confirmed this site",
                evidence={"finding_id": finding.finding_id,
                          "confidence": finding.confidence})

    def _record_outcome(self, site_id: str, finding: Finding,
                        applied: set[str]) -> None:
        state = self.ledger.state(site_id)
        if state in {LedgerState.QUERY, LedgerState.REJECTED,
                     LedgerState.APPLIED, LedgerState.LOCALLY_PASSED,
                     LedgerState.MODEL_PASSED}:
            return
        if finding.status == "validated":
            if state != LedgerState.EDIT:
                self.ledger.transition(
                    site_id, LedgerState.EDIT, actor="validator",
                    reason="legacy validator admitted the finding")
            if finding.finding_id in applied:
                self.ledger.transition(
                    site_id, LedgerState.APPLIED, actor="tracked-change writer",
                    reason="tracked change was written",
                    evidence={"finding_id": finding.finding_id})
        elif finding.status == "query":
            self.ledger.transition(
                site_id, LedgerState.QUERY, actor="validator",
                reason="routed to the reviewer query channel")
        elif finding.status == "skipped_low_confidence":
            self.ledger.transition(
                site_id, LedgerState.UNCERTAIN, actor="confidence gate",
                reason="below the edit confidence threshold")
        elif finding.status.startswith("rejected"):
            self.ledger.transition(
                site_id, LedgerState.REJECTED, actor="validator",
                reason=finding.status,
                evidence={"legacy_status": finding.status})
        else:
            # A state unknown to this shadow adapter is never converted to pass.
            # It stays explicitly pending and is visible in the report.
            log.warning("Examination site %s kept pending: unknown legacy status %s",
                        site_id, finding.status)


def prepare_shadow(cfg, doc: DocumentModel, *, paragraphs, sweep_findings,
                   consistency_findings, spell, adjudicate_candidates
                   ) -> ShadowExamination | None:
    from .config import examination_graph_killed
    excfg = cfg.examination_graph
    if examination_graph_killed():
        log.warning("Examination graph disabled by DOCPROOF_EXAMINATION_GRAPH")
        return None
    if not excfg.enabled:
        return None
    if excfg.mode != "shadow":
        raise ValueError("phase one supports examination_graph.mode: shadow only")
    return ShadowExamination.prepare(
        excfg, doc, paragraphs=paragraphs, sweep_findings=sweep_findings,
        consistency_findings=consistency_findings, spell=spell,
        adjudicate_candidates=adjudicate_candidates,
        sweep_keys=cfg.sweeps, error_groups=cfg.error_type_groups)
