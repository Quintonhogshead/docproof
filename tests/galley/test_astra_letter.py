"""Author-facing reports follow reconciled comments, not stale finding rows."""
import json

import docx
import pytest

from galley import astra_review as ar
from galley.casefile import CaseFile
from galley.letter import (comment_count, edit_shapes, query_rows, render_author_letter,
                           render_letter, render_style_sheet, run_evidence)
from tests.galley.test_astra_reconcile import _action, _freeze, run


FINAL_QUESTION = "Which title should the author use for this volume?"
NEW_QUESTION = "Does this arrival take place before the earlier visit?"


def _planned_run(run, *, reconcile=True):
    from galley.astra_reconcile import reconcile_run
    stale_rows = [
        {"finding_id": "stale-title", "para_id": "body-0000", "status": "query", "queried": True,
         "error_type": "spelling", "original_text": "Atlas", "explanation": "Is the obsolete surname Staleness correct?"},
        {"finding_id": "old-count", "para_id": "body-0003", "status": "validated", "applied": True,
         "format": "italic", "error_type": "serial_comma", "original_text": "wrong old quote",
         "corrected_text": "wrong old correction"},
    ]
    (run / "findings.json").write_text(json.dumps({"findings": stale_rows}))
    def configure(review, packet):
        review["comment_decisions"][0]["action"] = "drop"
        review["comment_decisions"][1]["action"] = "replace_question"
        actions = [
            _action("remove_comment", "body-0000", comment_ids=["7"]),
            _action("replace_comment", "body-0001", replacement=FINAL_QUESTION, comment_ids=["8"]),
            _action("add_author_query", "body-0003", quote="untouched paragraph", replacement=NEW_QUESTION),
        ]
        for index, action in enumerate(actions):
            action["id"] = f"repair-{index + 1}"
        review["actions"] = actions
    _freeze(run, configure)
    if reconcile:
        reconcile_run(run)
    return run_evidence(run)


def _docx_text(path):
    return "\n".join(p.text for p in docx.Document(path).paragraphs)


def test_questions_and_counts_use_only_actual_surviving_comment_bodies(run):
    evidence = _planned_run(run)
    rows = query_rows(evidence)
    assert [row["explanation"] for row in rows] == [FINAL_QUESTION, NEW_QUESTION]
    assert [row["original_text"] for row in rows] == ["Saide came home.", "untouched paragraph"]
    assert comment_count(evidence) == 2
    assert all(row["error_type"] == "author_query" for row in rows)


def test_author_letter_contains_final_questions_without_old_guarantees_or_counts(run, tmp_path):
    evidence = _planned_run(run)
    path = render_author_letter(CaseFile(book="Test Book"), tmp_path / "out", evidence=evidence)
    text = _docx_text(path)
    assert "2 margin questions" in text and "Questions for you (2)" in text
    assert FINAL_QUESTION in text and NEW_QUESTION in text
    assert "Near “Saide came home.”" in text
    assert "obsolete surname" not in text and "Is Atlas" not in text
    assert "serial comma" not in text
    assert "No sentence" not in text and "left as you wrote" not in text
    assert "tracked correction(s)" not in text and "paragraph break(s)" not in text
    assert "Accept or reject" in text


def test_style_sheet_and_internal_summary_do_not_resurrect_removed_questions(run, tmp_path):
    evidence = _planned_run(run)
    cf = CaseFile(book="Test Book")
    sheet = render_style_sheet(cf, tmp_path / "out", evidence=evidence).read_text()
    letter = render_letter(cf, tmp_path / "out", evidence=evidence).read_text()
    assert FINAL_QUESTION in sheet and "obsolete surname" not in sheet
    assert "Sites this run" not in sheet
    assert "are preserved; a clear expression is never rewritten" not in sheet
    assert FINAL_QUESTION in letter and NEW_QUESTION in letter
    assert "obsolete surname" not in letter and "No sentence was recast" not in letter
    assert "**2 margin question(s)**" in letter


def test_unreconciled_or_missing_review_cannot_fall_back_to_stale_author_questions(run, tmp_path):
    evidence = _planned_run(run, reconcile=False)
    with pytest.raises(ar.AstraReviewError):
        render_author_letter(CaseFile(), tmp_path / "out", evidence=evidence)
    assert not (tmp_path / "out" / "author-letter.docx").exists()
    (run / ar.RECEIPT_FILE).unlink()
    (run / "astra-review-required.json").write_text("{}")
    with pytest.raises(ar.AstraReviewError):
        query_rows(evidence)


def test_stale_document_does_not_overwrite_existing_author_letter(run, tmp_path):
    evidence = _planned_run(run)
    cf = CaseFile()
    path = render_author_letter(cf, tmp_path / "out", evidence=evidence)
    before = path.read_bytes()
    (run / "VOICE.md").write_text("An unreviewed subsequent instruction.")
    with pytest.raises(ar.AstraReviewError):
        render_author_letter(cf, tmp_path / "out", evidence=evidence)
    assert path.read_bytes() == before


def test_no_questions_remain_when_the_final_review_removes_all_comments(run, tmp_path):
    from galley.astra_reconcile import reconcile_run
    def configure(review, packet):
        for decision in review["comment_decisions"]:
            decision["action"] = "drop"
        review["actions"] = [
            _action("remove_comment", "body-0000", comment_ids=["7"]),
            _action("remove_comment", "body-0001", comment_ids=["8"]),
        ]
        review["actions"][1]["id"] = "repair-2"
    _freeze(run, configure)
    reconcile_run(run)
    evidence = run_evidence(run)
    assert query_rows(evidence) == [] and comment_count(evidence) == 0
    text = _docx_text(render_author_letter(CaseFile(), tmp_path / "out", evidence=evidence))
    assert "0 margin questions" in text and "No author questions remain" in text
    assert "Is Atlas" not in text and "Is this return" not in text


def test_formatting_is_not_reported_as_a_paragraph_break():
    rows = [
        {"format": "italic", "original_text": "Atlas", "corrected_text": "Atlas"},
        {"format": "roman", "original_text": "title", "corrected_text": "title"},
        {"format": True},
        {"error_type": "speaker_split", "format": True},
        {"original_text": "teh", "corrected_text": "the"},
        {"original_text": "all of the sudden", "corrected_text": "all of a sudden"},
    ]
    assert edit_shapes(rows) == {"single_word": 1, "multiword": 1,
                                 "paragraph_marks": 1, "formatting": 3}
