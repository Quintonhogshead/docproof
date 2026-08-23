"""Regression coverage for the candidate-screening double-comma failure.

See ``tests/fixtures/candidate_double_comma/README.md`` for the incident.
These tests assert the post-fix behaviour: correctly punctuated introductory
clauses pass, a genuinely missing intro-clause comma fails closed to a query,
and no candidate — nor the guarded apply path — can ever splice a second comma.
"""
from pathlib import Path

import pytest

from docproof.candidate_generators import _introductory_candidates
from docproof.config import Config
from docproof.models import DocumentModel, ParagraphRef

FIXTURE = Path("tests/fixtures/candidate_double_comma/source.docx")


def _para(text, style="Normal"):
    return ParagraphRef(
        "body-0000", "word/document.xml", "body", text, style, True)


def _splice(text, candidate):
    # Model apply: only an edit-channel candidate carrying a correction is ever
    # spliced into the document. A pass/query (correction is None) is a no-op.
    anchor = candidate.anchors[0]
    if (anchor.start_offset is None
            or candidate.candidate_correction is None
            or candidate.channel_preference == "query"):
        return text
    correction = candidate.candidate_correction
    return text[:anchor.start_offset] + correction + text[anchor.end_offset:]


CORRECT_CLAUSES = [
    "After the war, the men returned home.",
    "Although she was tired, she kept walking.",
    "Because it rained, the match was cancelled.",
    "Meanwhile, the kettle boiled.",
]


@pytest.mark.parametrize("text", CORRECT_CLAUSES)
def test_correct_introductory_clause_never_inserts_a_comma(text):
    for candidate in _introductory_candidates(_para(text)):
        # No candidate on a correctly punctuated clause may carry an edit
        # correction, and simulating its application must not change the text.
        assert candidate.candidate_correction is None or candidate.channel_preference == "query"
        assert _splice(text, candidate) == text
        assert ",," not in _splice(text, candidate)


def test_missing_intro_clause_comma_fails_closed_to_a_query():
    text = "When the sun rose the birds sang."
    candidates = _introductory_candidates(_para(text))
    assert candidates, "a missing intro-clause comma should still be surfaced"
    for candidate in candidates:
        # It must never become a wrong-location edit like 'When, the sun rose'.
        assert candidate.channel_preference != "edit" or candidate.candidate_correction is None
        assert _splice(text, candidate) != "When, the sun rose the birds sang."


def test_strong_single_word_intro_still_inserts_at_the_boundary():
    text = "However he never arrived."
    edits = [c for c in _introductory_candidates(_para(text))
             if c.candidate_correction and c.channel_preference == "edit"]
    assert edits, "a strong intro missing its comma is a safe insertion"
    for candidate in edits:
        assert _splice(text, candidate) == "However, he never arrived."


def test_no_generator_splice_produces_adjacent_duplicate_punctuation():
    from docproof.candidate_generators import generate_initial_candidates
    from docproof.models import index_paragraphs

    paras = tuple(
        ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t, "Normal", True)
        for i, t in enumerate([
            "After the war, the men returned home.",
            "Although she was tired, she kept walking.",
            "Because it rained, the match was cancelled.",
            "When the sun rose the birds sang.",
            'He said "Hello," and left.',
            "I saw 3 dogs.",
        ]))
    doc = DocumentModel("book.docx", paras)
    by_id = index_paragraphs(doc)
    for candidate in generate_initial_candidates(
            doc, paras, candidate_types=Config().candidate_screening.candidate_types):
        anchor = candidate.anchors[0]
        if anchor.paragraph_id is None or anchor.start_offset is None:
            continue
        para = by_id[anchor.paragraph_id]
        result = _splice(para.text, candidate)
        for pair in (",,", "..", ";;", "::", ",;", ";,"):
            assert pair not in result or pair in para.text, (
                f"{candidate.candidate_type} splice created {pair!r}: {result!r}")


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not built")
def test_apply_path_over_fixture_never_writes_a_double_comma(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCPROOF_CANDIDATE_APPLY", "1")
    from docproof.__main__ import _run_mock
    from docproof.pipeline import finish, prepare
    from docproof.profiles import CANDIDATE_ONLY, apply_profile
    from docproof.config import load_config
    from .test_error_types import ERROR_DIR

    cfg = apply_profile(load_config("config/default.yaml"), CANDIDATE_ONLY)
    prepared = prepare(cfg, FIXTURE, ERROR_DIR)
    findings, usage = _run_mock(cfg, prepared, [])
    for finding in findings:
        assert ",," not in finding.corrected_text
        assert ",," not in (finding.original_text or "")
    # finish() runs the strict reject-all audit before it writes, so a document
    # only exists if the mutation was safe. Reopen it and confirm the mutated
    # XML carries no double comma.
    outputs = finish(prepared, findings, usage, cfg,
                     out_dir=tmp_path / "out", source_path=FIXTURE)
    from zipfile import ZipFile
    with ZipFile(outputs.reviewed_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert ",," not in document_xml
