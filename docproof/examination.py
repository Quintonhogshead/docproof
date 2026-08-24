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
from .site_models import LedgerState, TERMINAL_STATES, Verdict

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
    failures: list[dict] = field(default_factory=list)
    # Phase 2's production-lane receipts. Expected and observed are keyed by
    # the paid call/checkpoint id, then by the broad paragraph/category site.
    # They stay separate until every response has folded so repeated passes and
    # ensembles cannot make the ledger result depend on arrival order.
    production_expected: dict[str, set[str]] = field(default_factory=dict)
    production_observed: dict[str, dict[str, bool]] = field(default_factory=dict)
    production_contract_issues: list[dict] = field(default_factory=list)
    production_finalized: bool = False

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
        obligation_findings: dict[str, list[Finding]] = {}
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
                obligation_findings.setdefault(obligation_id, []).append(finding)
        for obligation_id, matched in obligation_findings.items():
            self._observe_obligation(obligation_id, matched, applied)
        self.ledger.assert_accounted()

    def write(self, out_dir: Path, cfg, *, source: str) -> tuple[dict, Path, Path]:
        from .config import examination_production_verdicts_enabled
        phase_two = examination_production_verdicts_enabled(cfg)
        if phase_two:
            self.finalize_production_verdicts()
        judgment = self.judgment_report(cfg.judgment)
        report = build_coverage_report(
            self.ledger, self.graph, mode=self.mode,
            omitted=dict(self.omitted), source=source, judgment=judgment,
            production_verdicts=self.production_verdicts_report(
                phase_two))
        report["failures"] = list(self.failures)
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

    def record_failure(self, stage: str, error: Exception) -> dict:
        failure = {
            "stage": stage,
            "type": type(error).__name__,
            "message": str(error),
            "review_unchanged": True,
        }
        self.failures.append(failure)
        return failure

    def failure_warnings(self) -> list[str]:
        warnings = [
            f"Examination graph {failure['stage']} failed "
            f"({failure['type']}: {failure['message']}); normal review unchanged"
            for failure in self.failures
        ]
        production = self.production_verdicts_report(True)
        incomplete = (production["expected_responses"]
                      - production["complete_responses"])
        if production["expected_responses"] and incomplete:
            warnings.append(
                f"Phase 2 examination receipts were incomplete for "
                f"{incomplete} production response(s); unacknowledged sites "
                f"remain pending and the normal review is unchanged")
        return warnings

    def diagnostic_report(self, cfg, *, source: str) -> dict:
        """Machine-readable fallback when the full artifact write fails."""
        from .config import examination_production_verdicts_enabled
        return {
            "schema_version": 1,
            "mode": self.mode,
            "source": source,
            "failures": list(self.failures),
            "judgment": self.judgment_report(cfg.judgment),
            "production_verdicts": self.production_verdicts_report(
                examination_production_verdicts_enabled(cfg)),
            "scope": {
                "shadow_only": True,
                "may_create_edits": False,
            },
        }

    def expect_production_response(self, response_id: str, error_types,
                                   paragraphs) -> None:
        """Register the broad sites one paid production call must account for."""
        expected: set[str] = set()
        for para in paragraphs:
            for error_type in error_types:
                site_id = self.model_obligations.get(
                    (para.para_id, str(error_type)))
                if site_id:
                    expected.add(site_id)
        if expected:
            self.production_expected.setdefault(response_id, set()).update(
                expected)

    def observe_production_response(self, response_id: str, error_types,
                                    paragraphs, reviewed_paragraph_ids,
                                    findings: list[Finding]) -> None:
        """Fold one explicit production receipt without changing manuscript work.

        Returned paragraph ids are coverage receipts. A valid receipt plus no
        finding is pass evidence; an actual finding is error evidence even when
        the receipt list itself is malformed. Missing receipts remain missing —
        silence is never converted into a pass.
        """
        self.expect_production_response(response_id, error_types, paragraphs)
        paragraph_ids = tuple(p.para_id for p in paragraphs)
        expected_paragraphs = set(paragraph_ids)
        returned = tuple(str(x) for x in reviewed_paragraph_ids)
        counts = Counter(returned)
        missing = expected_paragraphs - set(returned)
        unknown = set(returned) - expected_paragraphs
        duplicate = {pid for pid, count in counts.items() if count != 1}
        if missing or unknown or duplicate:
            issue = {
                "response_id": response_id,
                "missing_paragraph_ids": sorted(missing),
                "unknown_paragraph_ids": sorted(unknown),
                "duplicate_paragraph_ids": sorted(duplicate),
            }
            if issue not in self.production_contract_issues:
                self.production_contract_issues.append(issue)

        finding_paragraphs = {
            f.para_id for f in findings if f.para_id in expected_paragraphs
        }
        accounted = ((set(returned) & expected_paragraphs)
                     | finding_paragraphs)
        outcomes = self.production_observed.setdefault(response_id, {})
        for para_id in accounted:
            is_error = para_id in finding_paragraphs
            for error_type in error_types:
                site_id = self.model_obligations.get(
                    (para_id, str(error_type)))
                if site_id:
                    outcomes[site_id] = outcomes.get(site_id, False) or is_error

    def checkpoint_metadata(self, response_id: str) -> dict:
        """The receipt portion of a paid call that a resume must replay."""
        outcomes = self.production_observed.get(response_id)
        issues = [row for row in self.production_contract_issues
                  if row.get("response_id") == response_id]
        if outcomes is None and not issues:
            return {}
        return {"examination_production_verdicts": {
            "outcomes": dict(outcomes or {}),
            "contract_issues": issues,
        }}

    def restore_production_response(self, response_id: str,
                                    metadata: dict | None) -> None:
        """Replay a checkpointed receipt after its expected sites are rebuilt."""
        payload = (metadata or {}).get("examination_production_verdicts") or {}
        expected = self.production_expected.get(response_id, set())
        outcomes = self.production_observed.setdefault(response_id, {})
        for site_id, is_error in (payload.get("outcomes") or {}).items():
            if site_id in expected and self.ledger.has(site_id):
                outcomes[site_id] = outcomes.get(site_id, False) or bool(is_error)
        for issue in payload.get("contract_issues") or []:
            if isinstance(issue, dict) and issue not in self.production_contract_issues:
                self.production_contract_issues.append(dict(issue))

    def finalize_production_verdicts(self) -> None:
        """Project all production receipts once, strongest evidence first."""
        if self.production_finalized:
            return
        expected_by_site: dict[str, set[str]] = {}
        observed_by_site: dict[str, dict[str, bool]] = {}
        for response_id, site_ids in self.production_expected.items():
            for site_id in site_ids:
                expected_by_site.setdefault(site_id, set()).add(response_id)
        for response_id, outcomes in self.production_observed.items():
            for site_id, is_error in outcomes.items():
                observed_by_site.setdefault(site_id, {})[response_id] = is_error

        for site_id, expected_responses in expected_by_site.items():
            if self.ledger.state(site_id) != LedgerState.NEEDS_JUDGMENT:
                continue
            observed = observed_by_site.get(site_id, {})
            evidence = {
                "expected_responses": sorted(expected_responses),
                "observed_responses": sorted(observed),
            }
            if any(observed.values()):
                self.ledger.transition(
                    site_id, LedgerState.MODEL_CONFIRMED,
                    actor="production detector",
                    reason="explicit Phase 2 response reported a finding",
                    evidence=evidence)
            elif expected_responses and set(observed) == expected_responses:
                self.ledger.transition(
                    site_id, LedgerState.MODEL_PASSED,
                    actor="production detector",
                    reason="every expected Phase 2 response explicitly reviewed "
                           "this paragraph/category and reported no finding",
                    evidence=evidence)
        self.production_finalized = True

    def production_verdicts_report(self, enabled: bool) -> dict:
        expected_by_site: dict[str, set[str]] = {}
        observed_by_site: dict[str, dict[str, bool]] = {}
        for response_id, site_ids in self.production_expected.items():
            for site_id in site_ids:
                expected_by_site.setdefault(site_id, set()).add(response_id)
        for response_id, outcomes in self.production_observed.items():
            for site_id, is_error in outcomes.items():
                observed_by_site.setdefault(site_id, {})[response_id] = is_error
        explicit_errors = 0
        explicit_passes = 0
        for site_id, expected_responses in expected_by_site.items():
            observed = observed_by_site.get(site_id, {})
            if any(observed.values()):
                explicit_errors += 1
            elif expected_responses and set(observed) == expected_responses:
                explicit_passes += 1
        expected_sites = len(expected_by_site)
        explicit_sites = explicit_errors + explicit_passes
        complete_responses = sum(
            bool(site_ids) and site_ids <= set(
                self.production_observed.get(response_id, {}))
            for response_id, site_ids in self.production_expected.items())
        return {
            "enabled": bool(enabled),
            "shadow_only": True,
            "may_create_findings": False,
            "expected_sites": expected_sites,
            "explicit_sites": explicit_sites,
            "explicit_passes": explicit_passes,
            "explicit_errors": explicit_errors,
            "pending_sites": max(0, expected_sites - explicit_sites),
            "coverage_percent": round(
                explicit_sites / expected_sites * 100 if expected_sites else 100.0,
                2),
            "expected_responses": len(self.production_expected),
            "complete_responses": complete_responses,
            "contract_issues": list(self.production_contract_issues),
        }

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

    def _observe_obligation(self, site_id: str, findings: list[Finding],
                            applied: set[str]) -> None:
        """Project many precise outcomes onto one broad category obligation.

        A paragraph/category obligation is intentionally coarser than a
        finding, so several findings can match it. Applying their transitions
        one at a time makes the result order-dependent and can try to reopen a
        terminal or uncertain ledger state. The precise sites already preserve
        every individual outcome; this row records each as observation evidence
        and takes the strongest aggregate outcome exactly once.
        """
        if site_id in self.primary_judgment_verdicts:
            for finding in findings:
                self._observe_target(site_id, finding, applied)
            return

        # A Phase 2 explicit pass is terminal evidence from the production
        # detector. A later auxiliary pass may still surface a matching finding,
        # but that is independent evidence, not permission to rewrite history.
        if self.ledger.state(site_id) in TERMINAL_STATES:
            for finding in findings:
                self.ledger.record_observation(
                    site_id, actor="production reviewer",
                    reason="later finding observed after an explicit terminal "
                           "production verdict",
                    evidence={"finding_id": finding.finding_id,
                              "status": finding.status,
                              "applied": finding.finding_id in applied})
            return

        for finding in findings:
            self.ledger.record_observation(
                site_id, actor="production reviewer",
                reason="production finding matched this broad obligation",
                evidence={
                    "finding_id": finding.finding_id,
                    "status": finding.status,
                    "confidence": finding.confidence,
                    "applied": finding.finding_id in applied,
                })

        def strength(finding: Finding) -> int:
            if finding.status == "validated":
                return 5 if finding.finding_id in applied else 4
            if finding.status == "query":
                return 3
            if finding.status == "skipped_low_confidence":
                return 2
            if finding.status.startswith("rejected"):
                return 1
            return 0

        representative = max(findings, key=strength)
        self._confirm_site(site_id, representative)
        self._record_outcome(site_id, representative, applied)

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
        if state not in {LedgerState.GENERATED, LedgerState.NEEDS_JUDGMENT}:
            return
        local = finding.chunk_id in {
            "sweep", "consistency", "residual", "calendar"}
        # The local/model split only picks the confirmation tier a *generated*
        # site enters for the first time. A site already routed to
        # `needs_judgment` sits downstream of `locally_confirmed`; the ledger has
        # no edge back to it, by design (reopening a decision starts a new site,
        # it does not rewrite history). A legacy finding arriving on such a site
        # is the production review confirming the awaited error, so it resolves
        # forward to the model-confirmed tier regardless of which detector lane
        # produced it — the sanctioned `needs_judgment -> model_confirmed -> edit
        # -> applied` path. Provenance stays honest via the actor and reason.
        if state == LedgerState.NEEDS_JUDGMENT:
            target = LedgerState.MODEL_CONFIRMED
        else:
            target = (LedgerState.LOCALLY_CONFIRMED if local
                      else LedgerState.MODEL_CONFIRMED)
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
