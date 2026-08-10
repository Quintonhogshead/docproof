"""The eval harness foundation: the corpus loads, the leakage guard bites, and
a built .docx numbers its paragraphs so each maps back to its case exactly."""
from __future__ import annotations

from pathlib import Path

import pytest

from docproof.config import load_config
from docproof.eval.corpus import (CorpusError, load_corpus, check_no_leakage,
                                  load_case_file)
from docproof.eval.docbuilder import DocBuildError, build_docx
from docproof.ingest import build_document_model
from docproof.formats.docx import preflight

ROOT = Path(__file__).parent.parent
CASES = ROOT / "eval" / "cases"
ERROR_DIR = ROOT / "config" / "error_types"


def test_corpus_loads_and_splits_into_errors_and_traps():
    cases = load_corpus(CASES)
    assert cases, "no cases loaded"
    assert any(not c.is_clean for c in cases), "no seeded errors"
    assert any(c.is_clean for c in cases), "no traps"
    # Every seeded error's labeled span is a verbatim substring of its text.
    for c in cases:
        if not c.is_clean:
            assert c.span and c.span in c.text


def test_case_ids_are_unique_across_the_corpus():
    cases = load_corpus(CASES)
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


def test_shipped_corpus_does_not_leak_calibration_examples():
    # The real guard against scoring on paragraphs the model was shown.
    check_no_leakage(load_corpus(CASES), ERROR_DIR)


def test_leakage_check_bites(tmp_path):
    # A case whose text IS a calibration example must be rejected.
    import yaml
    ex = yaml.safe_load((ERROR_DIR / "spelling.yaml").read_text())["examples"]
    seeded = next(e["text"] for e in ex if e.get("finding"))
    f = tmp_path / "spelling.yaml"
    f.write_text(yaml.safe_dump({
        "error_type": "spelling",
        "cases": [{"id": "leak-1", "text": seeded,
                   "expect": {"span": seeded.split()[0]}}],
    }))
    with pytest.raises(CorpusError, match="leaks the test set"):
        check_no_leakage(load_case_file(f), ERROR_DIR)


def test_missing_span_in_text_is_rejected(tmp_path):
    import yaml
    f = tmp_path / "tense_shift.yaml"
    f.write_text(yaml.safe_dump({
        "error_type": "tense_shift",
        "cases": [{"id": "x-1", "text": "He walked home.",
                   "expect": {"span": "sprinted"}}],
    }))
    with pytest.raises(CorpusError, match="does not occur"):
        load_case_file(f)


def test_build_docx_maps_every_paragraph_to_its_case(tmp_path):
    cases = load_corpus(CASES)
    built = build_docx(cases, tmp_path / "corpus.docx")
    # The whole point: para_id -> case, exact, via the real ingest walker.
    cfg = load_config(str(ROOT / "config" / "default.yaml"))
    pkg = preflight(str(built.docx_path), cfg.tracked_changes_policy)
    doc = build_document_model(pkg, cfg)
    assert len(doc.paragraphs) == len(cases)
    for p in doc.paragraphs:
        case = built.case_for(p.para_id)
        assert case is not None, f"{p.para_id} maps to no case"
        # Text matches modulo the quote-normalizer that runs before ingest.
        assert _quote_fold(case.text) == _quote_fold(p.text)


def test_build_docx_refuses_too_short_cases(tmp_path):
    from docproof.eval.corpus import Case
    tiny = [Case(id="t-1", error_type="spelling", text="Too short.",
                 is_clean=True)]
    with pytest.raises(DocBuildError, match="min_paragraph_chars"):
        build_docx(tiny, tmp_path / "x.docx")


def _quote_fold(s: str) -> str:
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))


# --- runner + scorer (mock; no API) ------------------------------------------

def _para_for(built, case_id: str) -> str:
    return next(pid for pid, cid in built.para_to_case.items() if cid == case_id)


def _run_mock(tmp_path, canned_by_case):
    """Run the corpus through the pipeline with canned findings keyed by case id
    (resolved to para_ids here so tests don't hardcode body-NNNN)."""
    from docproof.eval.docbuilder import build_docx
    from docproof.eval.runner import run_eval
    cases = load_corpus(CASES)
    built = build_docx(cases, tmp_path / "map.docx")   # to learn para ids
    canned = []
    for case_id, patch in canned_by_case.items():
        canned.append({"para_id": _para_for(built, case_id), **patch})
    cfg = load_config(str(ROOT / "config" / "default.yaml"))
    return run_eval(cfg, cases, ERROR_DIR, tmp_path / "run",
                    mock_findings=canned)


def test_runner_validates_a_correct_finding(tmp_path):
    run = _run_mock(tmp_path, {
        "sp-001": {"error_type": "spelling",
                   "original_text": "The committee met in secret and reached no concensus before midnight.",
                   "corrected_text": "The committee met in secret and reached no consensus before midnight.",
                   "confidence": "high", "explanation": "x"},
    })
    # The deterministic sweeps run over the whole corpus and legitimately fire
    # on unrelated cases (doubled words, em dashes, dialogue tags) now that the
    # corpus spans every type; they carry chunk_id "sweep". Count only the
    # model's own findings, which is what this test is about.
    hit = [f for f in run.findings
           if f.status == "validated" and f.chunk_id != "sweep"]
    assert len(hit) == 1 and hit[0].error_type == "spelling"


def test_scorer_counts_catch_miss_and_trap_fp(tmp_path):
    from docproof.eval.scorer import score
    run = _run_mock(tmp_path, {
        # a real catch
        "cs-001": {"error_type": "comma_splice",
                   "original_text": "The lamps were lit along the quay, the last of the boats had not come in.",
                   "corrected_text": "The lamps were lit along the quay; the last of the boats had not come in.",
                   "confidence": "high", "explanation": "x"},
        # a false positive on a trap, low confidence
        "cs-101": {"error_type": "comma_splice",
                   "original_text": "It had rained all week, so the river was running high under the bridge.",
                   "corrected_text": "It had rained all week; the river was running high under the bridge.",
                   "confidence": "low", "explanation": "bogus"},
    })
    low = score(run, "low")
    cs = low.types["comma_splice"]
    assert cs.caught == 1                       # cs-001 found
    assert cs.seeded == 4 and cs.recall == 0.25
    assert cs.trap_cases_flagged == 1           # the trap got flagged at low
    # Raising the gate filters the low-confidence trap FP away.
    assert score(run, "medium").types["comma_splice"].trap_cases_flagged == 0


def test_threshold_sweep_is_monotone_on_this_fixture(tmp_path):
    from docproof.eval.scorer import score_curve
    run = _run_mock(tmp_path, {
        "ts-001": {"error_type": "tense_shift",
                   "original_text": "He shut the ledger and reaches for the lamp on the corner of the desk.",
                   "corrected_text": "He shut the ledger and reached for the lamp on the corner of the desk.",
                   "confidence": "medium", "explanation": "x"},
    })
    curve = score_curve(run)
    # A medium-confidence catch survives medium, disappears at high.
    assert curve["medium"].types["tense_shift"].caught == 1
    assert curve["high"].types["tense_shift"].caught == 0


def test_scorecard_writes_both_files(tmp_path):
    from docproof.eval.scorecard import write_scorecard
    from docproof.eval.scorer import score_curve
    run = _run_mock(tmp_path, {})
    j, m = write_scorecard(score_curve(run), tmp_path / "sc")
    assert j.exists() and m.exists()
    assert "Scorecard" in m.read_text()
