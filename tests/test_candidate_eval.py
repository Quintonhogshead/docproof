"""Candidate detector evaluation (plan P4): seeded generation recall / false
positives, shadow-eval over a document, and release-gate enforcement."""
from docproof.config import Config
from docproof.eval.candidate_eval import (
    gates_pass, load_candidate_cases, release_gates, score_generation,
    shadow_report)
from docproof.models import DocumentModel, ParagraphRef


def _para(pid, text):
    return ParagraphRef(pid, "word/document.xml", "body", text, "Normal", True)


def test_candidate_fixtures_load_and_cover_the_new_families():
    cases = load_candidate_cases()
    assert cases
    types = {c.error_type for c in cases}
    assert {"introductory_comma", "punctuation_style", "homophone",
            "repeated_word", "direct_address_comma"} <= types
    # Every family carries both seeded errors and clean controls.
    for etype in types:
        family = [c for c in cases if c.error_type == etype]
        assert any(not c.is_clean for c in family), f"{etype}: no seeded error"
        assert any(c.is_clean for c in family), f"{etype}: no clean control"


def test_generation_recall_and_false_positive_thresholds():
    report = score_generation()
    totals = report["totals"]
    assert totals["recall"] >= 0.9, report["by_type"]
    assert totals["false_positive_rate"] <= 0.05, report["by_type"]


def test_double_comma_traps_draw_no_edit_candidates():
    # The P0-02 incident, as a scored control: correctly punctuated clauses must
    # produce zero edit false positives.
    report = score_generation()
    intro = report["by_type"]["introductory_comma"]
    assert intro["false_positive_edits"] == 0
    assert intro["clean"] >= 4


def test_release_gates_pass_on_a_healthy_report():
    healthy = {
        "accounting": {"all_candidates_have_state": True},
        "application": {"applied_tracked_changes": 3},
        "screening": {"anchor_failures": 0},
    }
    gates = release_gates(healthy)
    assert gates_pass(gates), [(g.name, g.detail) for g in gates if not g.passed]


def test_release_gates_fail_on_unaccounted_candidates():
    unhealthy = {
        "accounting": {"all_candidates_have_state": False},
        "application": {}, "screening": {},
    }
    gates = release_gates(unhealthy)
    assert not gates_pass(gates)
    failed = {g.name for g in gates if not g.passed}
    assert "zero_unaccounted_candidates" in failed


def test_shadow_eval_runs_over_the_existing_detector_corpus():
    # P4-02: the candidate detector can be scored on the same corpus the detector
    # models use, so standalone vs combined coverage is comparable. Here we run
    # generation-only shadow over the corpus paragraphs and confirm the ledger is
    # fully accounted and produces candidates.
    from pathlib import Path
    from docproof.eval.corpus import load_corpus

    corpus = Path("eval/cases")
    if not corpus.exists():
        return
    cases = load_corpus(corpus)[:60]
    doc = DocumentModel("corpus.docx", tuple(
        _para(f"body-{i:04d}", c.text) for i, c in enumerate(cases)))
    cfg = Config(candidate_screening={"mode": "shadow"})
    report = shadow_report(cfg, doc)
    assert report["accounting"]["all_candidates_have_state"] is True
    assert report["accounting"]["generated_candidates"] > 0
    assert report["scope"]["may_modify_documents"] is False


def test_shadow_report_measures_without_mutating():
    doc = DocumentModel("book.docx", (
        _para("body-0000", "However he left. They went there today."),
        _para("body-0001", "This is is wrong."),
    ))
    cfg = Config(candidate_screening={"mode": "shadow"})
    report = shadow_report(cfg, doc)
    assert report["mode"] == "shadow"
    assert report["scope"]["may_modify_documents"] is False
    assert report["accounting"]["all_candidates_have_state"] is True
    assert report["accounting"]["generated_candidates"] > 0
    assert "coverage" in report
