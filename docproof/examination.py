"""Shadow-mode orchestration around DocProof's existing review pipeline."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .coverage_report import (build_coverage_report, write_coverage_json,
                              write_coverage_markdown)
from .examination_graph import ExaminationGraph
from .models import DocumentModel, Finding
from .site_generators import (site_from_candidate, site_from_finding,
                              sites_from_model_obligations,
                              sites_from_spell_scan,
                              sites_from_sweep_obligations)
from .site_ledger import ExaminationLedger, LedgerInvariantError
from .site_models import LedgerState

log = logging.getLogger("docproof.examination")


@dataclass
class ShadowExamination:
    ledger: ExaminationLedger
    graph: ExaminationGraph
    max_sites: int
    mode: str = "shadow"
    omitted: Counter = field(default_factory=Counter)
    model_obligations: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def prepare(cls, cfg, doc: DocumentModel, *, paragraphs,
                sweep_findings, consistency_findings, spell,
                adjudicate_candidates, sweep_keys,
                error_groups) -> "ShadowExamination":
        run = cls(ExaminationLedger(), ExaminationGraph.from_document(doc),
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
            self._confirm_site(site.site_id, finding)
            self._record_outcome(site.site_id, finding, applied)

            obligation_id = self.model_obligations.get(
                (finding.para_id, finding.error_type))
            if obligation_id and obligation_id != site.site_id:
                self._confirm_site(obligation_id, finding)
                self._record_outcome(obligation_id, finding, applied)
        self.ledger.assert_accounted()

    def write(self, out_dir: Path, cfg, *, source: str) -> tuple[dict, Path, Path]:
        report = build_coverage_report(
            self.ledger, self.graph, mode=self.mode,
            omitted=dict(self.omitted), source=source)
        ledger_path = out_dir / cfg.ledger_filename
        report_path = out_dir / cfg.report_filename
        if cfg.write_ledger:
            self.ledger.write_jsonl(ledger_path)
        if cfg.write_report:
            write_coverage_json(out_dir / cfg.report_json_filename, report)
            write_coverage_markdown(report_path, report)
        return report, ledger_path, report_path

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
