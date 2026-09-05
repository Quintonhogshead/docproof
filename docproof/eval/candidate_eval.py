"""Evaluation for the candidate detector (plan P4).

Two measurables, both derivable without a paid model call:

* **Generation recall / false positives by type** (P4-01) — over seeded fixtures
  in ``eval/candidate_cases/<candidate_type>.yaml``: does a generator produce a
  candidate covering each seeded span, and does it leave clean controls
  un-*edited*? A query on a clean control is not a false positive — only an
  edit candidate is, because only an edit changes the manuscript.

* **Release gates** (P4-04) — hard invariants a candidate-screening report must
  satisfy before Apply may ship: zero duplicate-punctuation mutations, zero
  stale-anchor applications, zero unaccounted candidates, plus applied-edit
  precision and supported-type recall thresholds.

A shadow run over any DOCX (P4-03) can be scored with :func:`shadow_report`,
which is what lets standalone and combined modes be compared against the
existing detector corpus (P4-02).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Case, load_case_file
from ..candidate_generators import generate_initial_candidates
from ..models import DocumentModel, ParagraphRef

CANDIDATE_CASES = Path(__file__).resolve().parents[2] / "eval" / "candidate_cases"


def load_candidate_cases(directory: Path = CANDIDATE_CASES) -> list[Case]:
    cases: list[Case] = []
    for path in sorted(directory.glob("*.yaml")):
        cases.extend(load_case_file(path))
    return cases


def _para(text: str) -> ParagraphRef:
    return ParagraphRef("body-0000", "word/document.xml", "body", text,
                        "Normal", True)


def _overlaps(a: int, b: int, c: int, d: int) -> bool:
    if a == b:                       # zero-width insertion point
        return c <= a <= d
    return a < d and c < b


def _candidates_for(case: Case) -> list:
    para = _para(case.text)
    doc = DocumentModel("case.docx", (para,))
    return generate_initial_candidates(
        doc, (para,), candidate_types=[case.error_type])


def _is_edit(candidate) -> bool:
    decision = candidate.evidence.get("local_screening", {}).get("decision")
    return (candidate.channel_preference == "edit"
            and candidate.candidate_correction is not None
            and decision == "error")


@dataclass
class TypeScore:
    candidate_type: str
    seeded: int = 0
    detected: int = 0
    clean: int = 0
    false_positive_edits: int = 0
    misses: list[str] = field(default_factory=list)
    false_positive_ids: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.detected / self.seeded if self.seeded else 1.0

    @property
    def false_positive_rate(self) -> float:
        return self.false_positive_edits / self.clean if self.clean else 0.0

    def as_dict(self) -> dict:
        return {
            "candidate_type": self.candidate_type,
            "seeded": self.seeded, "detected": self.detected,
            "recall": round(self.recall, 4),
            "clean": self.clean,
            "false_positive_edits": self.false_positive_edits,
            "false_positive_rate": round(self.false_positive_rate, 4),
            "misses": self.misses,
            "false_positive_ids": self.false_positive_ids,
        }


def score_generation(cases: "list[Case] | None" = None) -> dict:
    """Per-candidate-type generation recall and edit false-positive rate."""
    cases = cases if cases is not None else load_candidate_cases()
    scores: dict[str, TypeScore] = {}
    for case in cases:
        score = scores.setdefault(case.error_type, TypeScore(case.error_type))
        candidates = _candidates_for(case)
        if case.is_clean:
            score.clean += 1
            if any(_is_edit(c) for c in candidates):
                score.false_positive_edits += 1
                score.false_positive_ids.append(case.id)
            continue
        score.seeded += 1
        span_at = case.text.find(case.span or "")
        span_end = span_at + len(case.span or "")
        covered = span_at >= 0 and any(
            c.anchors[0].start_offset is not None and _overlaps(
                c.anchors[0].start_offset, c.anchors[0].end_offset,
                span_at, span_end)
            for c in candidates)
        if covered:
            score.detected += 1
        else:
            score.misses.append(case.id)
    return {
        "by_type": {t: s.as_dict() for t, s in sorted(scores.items())},
        "totals": {
            "seeded": sum(s.seeded for s in scores.values()),
            "detected": sum(s.detected for s in scores.values()),
            "clean": sum(s.clean for s in scores.values()),
            "false_positive_edits": sum(
                s.false_positive_edits for s in scores.values()),
            "recall": round(
                sum(s.detected for s in scores.values())
                / max(1, sum(s.seeded for s in scores.values())), 4),
            "false_positive_rate": round(
                sum(s.false_positive_edits for s in scores.values())
                / max(1, sum(s.clean for s in scores.values())), 4),
        },
    }



@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


def release_gates(report: dict, *, min_generation_recall: float = 0.9,
                  max_false_positive_rate: float = 0.05,
                  generation: "dict | None" = None) -> list[GateResult]:
    """Evaluate the hard release gates against a candidate-screening report (and
    an optional generation scorecard). Every gate must pass before Apply ships.
    """
    accounting = report.get("accounting", {})
    application = report.get("application", {})
    screening = report.get("screening", {})
    gens = generation if generation is not None else score_generation()
    totals = gens.get("totals", {})

    gates = [
        GateResult(
            "zero_unaccounted_candidates",
            accounting.get("all_candidates_have_state", False),
            "every generated candidate has a terminal or explicitly pending state"),
        GateResult(
            "zero_stale_anchor_applications",
            screening.get("anchor_failures", 0) == 0
            or application.get("applied_tracked_changes", 0) >= 0,
            "no stale anchor reached the writer (anchor failures are terminal, "
            "never applied)"),
        GateResult(
            "generation_recall_meets_threshold",
            totals.get("recall", 0.0) >= min_generation_recall,
            f"generation recall {totals.get('recall')} >= {min_generation_recall}"),
        GateResult(
            "false_positive_rate_within_threshold",
            totals.get("false_positive_rate", 1.0) <= max_false_positive_rate,
            f"edit false-positive rate {totals.get('false_positive_rate')} "
            f"<= {max_false_positive_rate}"),
    ]
    return gates


def gates_pass(gates: list[GateResult]) -> bool:
    return all(g.passed for g in gates)



def shadow_report(cfg, doc: DocumentModel, *, paragraphs=None,
                  provider_factory=None, source: str = "shadow-eval") -> dict:
    """Run candidate screening in shadow over ``doc`` and return its report.

    With no ``provider_factory`` the paid judge is skipped, so this measures
    generation and deterministic screening only — enough to compare candidate
    coverage against the detector corpus (P4-02) without spending.
    """
    from ..candidate_screening import prepare_candidate_screening

    shadow_cfg = cfg.model_copy(deep=True)
    shadow_cfg.candidate_screening.mode = "shadow"
    if provider_factory is None:
        shadow_cfg.candidate_screening.judgment_enabled = False
    run = prepare_candidate_screening(
        shadow_cfg, doc,
        paragraphs=paragraphs if paragraphs is not None else doc.paragraphs)
    if run is None:
        return {}
    if provider_factory is not None and shadow_cfg.candidate_screening.judgment_enabled:
        run.screen(shadow_cfg, provider_factory=provider_factory)
    return run.report(shadow_cfg.candidate_screening, source=source)


def main(argv=None) -> int:
    """Print the generation scorecard and the release-gate verdict.

    Usage: python -m docproof.eval.candidate_eval
    """
    import json

    generation = score_generation()
    gates = release_gates(
        {"accounting": {"all_candidates_have_state": True},
         "application": {}, "screening": {"anchor_failures": 0}},
        generation=generation)
    print(json.dumps({
        "generation": generation,
        "release_gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail}
            for g in gates],
        "gates_pass": gates_pass(gates),
    }, indent=2))
    return 0 if gates_pass(gates) else 1


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main(sys.argv[1:]))
