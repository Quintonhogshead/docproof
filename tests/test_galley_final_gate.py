"""The second Astra reading is the only needs_human gate, and code owns its rule.

More than the ceiling of core mechanical corrections still found, or any
verified publication blocker, sends the book to a person; otherwise the
proofread is complete. A reader's own window verdict, a skipped window, a
title italic or a walk-through usage note never decides it.
"""
from __future__ import annotations

import json

import pytest

from galley.fixed_workflow import (ASTRA, CORE_MECHANICAL_CATEGORIES, FINAL_REVIEW_ERROR_CEILING,
                                   FINAL_REVIEW_STAGE, FixedWorkflow, final_review_verdict)

from .test_galley_fixed_workflow import Readers, finding, local_scans, make_book  # noqa: F401


def _edit(i, category="grammar", **over):
    row = {"id": f"c-{i}", "para_id": f"body-{i:04d}", "action": "edit", "category": category,
           "before": "walk", "replacement": "walks"}
    row.update(over)
    return row


def test_the_rule_counts_core_mechanical_edits_against_the_ceiling():
    at_ceiling = [_edit(i) for i in range(FINAL_REVIEW_ERROR_CEILING)]
    ready = final_review_verdict(at_ceiling, [{"verdict": "ready"}])
    assert ready["verdict"] == "ready" and ready["core_mechanical_errors"] == FINAL_REVIEW_ERROR_CEILING
    assert "proofread complete" in ready["reason"]
    over = final_review_verdict(at_ceiling + [_edit(99)], [{"verdict": "ready"}])
    assert over["verdict"] == "needs_human" and over["core_mechanical_errors"] == FINAL_REVIEW_ERROR_CEILING + 1
    assert "human proofreader" in over["reason"]
    assert over["stage"] == FINAL_REVIEW_STAGE and over["ceiling"] == FINAL_REVIEW_ERROR_CEILING


def test_only_core_mechanical_edits_count():
    padding = ([_edit(i, "usage") for i in range(30)] + [_edit(50, "typesetting")]
               + [_edit(51, "continuity")] + [_edit(52, "format", format="italic", replacement="walk")]
               + [_edit(53, action="query", category="author_question")])
    review = final_review_verdict(padding, [])
    assert review["verdict"] == "ready" and review["core_mechanical_errors"] == 0
    assert set(CORE_MECHANICAL_CATEGORIES) == {"spelling", "grammar", "punctuation", "number_style",
                                                "currency_style", "broken_sentence"}
    for category in CORE_MECHANICAL_CATEGORIES:
        assert final_review_verdict([_edit(1, category)], [])["core_mechanical_errors"] == 1


def test_a_verified_blocker_alone_is_needs_human_and_an_unverified_one_is_not():
    blocker = {"para_id": "body-0001", "quote": "lorem ipsum", "problem": "Placeholder text", "reason": "Unfinished."}
    review = final_review_verdict([], [{"verdict": "ready", "publication_blockers": [blocker]}])
    assert review["verdict"] == "needs_human" and review["publication_blockers"] == [blocker]
    assert "1 publication blocker" in review["reason"]
    diagnostic = final_review_verdict([], [{"verdict": "needs_human", "unverified_blockers": [blocker]}])
    assert diagnostic["verdict"] == "ready" and diagnostic["unverified_blockers"] == [blocker]
    assert diagnostic["window_verdicts"] == {"needs_human": 1}


def test_a_skipped_window_is_reported_but_never_a_verdict():
    review = final_review_verdict([], [{"status": "skipped", "verdict": "ready"}, {"verdict": "ready"}])
    assert review["verdict"] == "ready" and review["skipped_windows"] == 1
    assert "1 reading window(s) were unavailable" in review["reason"]


def _reading(rows, findings=(), blockers=(), verdict="ready", comments=()):
    return {"reviewed_ids": [x["id"] for x in rows], "findings": list(findings),
            "comment_decisions": list(comments), "editorial_verdict": verdict,
            "publication_blockers": list(blockers)}


def test_first_astra_window_verdict_no_longer_decides(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "astra":
            return _reading(payload["paragraphs"], verdict="needs_human")
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("She walked home."), tmp_path / "run", calls=readers).run()
    assert result["editorial_verdict"] == "ready"
    assert result["final_review"]["verdict"] == "ready" and result["final_review"]["core_mechanical_errors"] == 0
    assert [r["stage"] for r in result["stages"]][-3:] == ["astra", FINAL_REVIEW_STAGE, "astra_gate"]
    final = next(r for r in readers.events if r["stage"] == FINAL_REVIEW_STAGE)
    assert final["model"] == ASTRA and "publication_blockers" in final["schema"]["properties"]


def test_more_than_the_ceiling_of_remaining_errors_is_needs_human_and_they_are_fixed(make_book, tmp_path):
    count = FINAL_REVIEW_ERROR_CEILING + 1
    book = make_book(*[f"He walk to house number {i}." for i in range(count)])

    def handler(stage, model, payload, kwargs):
        if stage == FINAL_REVIEW_STAGE:
            rows = payload["paragraphs"]
            return _reading(rows, findings=[finding(row["id"], "He walk", "He walks") for row in rows])
    readers = Readers(handler=handler)
    result = FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    assert all(text.startswith("He walks") for text in result["accepted"].values())
    review = result["final_review"]
    assert result["editorial_verdict"] == "needs_human" and review["verdict"] == "needs_human"
    assert review["core_mechanical_errors"] == count and review["publication_blockers"] == []
    assert len(review["core_mechanical_edits"]) == count
    saved = json.loads((tmp_path / "run" / "stages" / f"{FINAL_REVIEW_STAGE}.json").read_text())
    assert saved["evidence"]["final_review"] == review


def test_a_publication_blocker_must_anchor_to_the_current_paragraph(make_book, tmp_path):
    book = make_book("Chapter Two", "TK TK insert the ending here.")

    def handler(stage, model, payload, kwargs):
        if stage == FINAL_REVIEW_STAGE:
            rows = payload["paragraphs"]
            return _reading(rows, blockers=[
                {"para_id": rows[1]["id"], "quote": "TK TK insert the ending here.",
                 "problem": "Placeholder text where the ending should be", "reason": "The book is unfinished."},
                {"para_id": rows[0]["id"], "quote": "text that is not in the paragraph",
                 "problem": "Invented", "reason": "Cannot be anchored."}])
    result = FixedWorkflow(book, tmp_path / "run", calls=Readers(handler=handler)).run()
    review = result["final_review"]
    assert result["editorial_verdict"] == "needs_human"
    assert [b["problem"] for b in review["publication_blockers"]] == ["Placeholder text where the ending should be"]
    assert [b["problem"] for b in review["unverified_blockers"]] == ["Invented"]
    assert review["core_mechanical_errors"] == 0


def test_a_clean_second_reading_is_proofread_complete_with_the_reason_recorded(make_book, tmp_path):
    result = FixedWorkflow(make_book("She walked home."), tmp_path / "run", calls=Readers()).run()
    review = result["final_review"]
    assert result["editorial_verdict"] == "ready"
    assert review["reason"].startswith("The second Astra reading found 0 core mechanical errors")
    assert review["reason"].endswith("proofread complete.")


# --- a placeholder the interior designer fills --------------------------------

def _blocker(para_id, kind="placeholder", quote="Cover design by XXX", resolution="none"):
    return {"para_id": para_id, "quote": quote, "kind": kind, "resolution": resolution,
            "problem": "Unresolved cover-credit placeholder.",
            "reason": "The final credit requires the designer's name."}


def test_a_placeholder_is_never_a_blocker_wherever_it_sits():
    """Gunn - Book One (2026-09-18) went to a human proofreader over "Cover
    design by XXX" on its copyright page, and Jimenez - Book 1 the same day
    over "Copyright Page Placeholder" in a poetry book with no chapter
    headings to place it outside of. A blocker never triggers on something
    that could be a query or an easy fix (Quinton, 2026-09-18): the designer
    or the author fills a placeholder, so it is waived wherever it sits."""
    coverage = [{"verdict": "ready", "publication_blockers": [
        _blocker("body-0024", quote="Copyright Page Placeholder"),
        _blocker("body-0400", quote="TK TK")]}]
    review = final_review_verdict([], coverage)
    assert review["verdict"] == "ready"
    assert review["publication_blockers"] == []
    # Waived, not dropped: somebody still has to fill it.
    assert [b["para_id"] for b in review["waived_blockers"]] == ["body-0024", "body-0400"]
    assert "designer" in review["waived_blockers"][0]["waived"]
    assert "did not count" in review["reason"]


@pytest.mark.parametrize("resolution, word", [("query", "question"), ("edit", "correction")])
def test_a_blocker_a_query_or_an_edit_resolves_is_waived(resolution, word):
    """What could be asked or fixed is asked or fixed, never a blocker."""
    blocker = _blocker("body-0400", kind="text_defect", quote="garbled", resolution=resolution)
    review = final_review_verdict([], [{"verdict": "ready", "publication_blockers": [blocker]}])
    assert review["verdict"] == "ready" and review["publication_blockers"] == []
    assert word in review["waived_blockers"][0]["waived"]


@pytest.mark.parametrize("kind", ["text_defect", "structure", "other"])
def test_only_what_neither_a_query_nor_an_edit_resolves_blocks(kind):
    """A garbled passage nobody can reconstruct or ask about still blocks."""
    blocker = _blocker("body-0400", kind=kind, quote="garbled", resolution="none")
    review = final_review_verdict([], [{"verdict": "ready", "publication_blockers": [blocker]}])
    assert review["verdict"] == "needs_human" and review["waived_blockers"] == []
    assert [b["para_id"] for b in review["publication_blockers"]] == ["body-0400"]


def test_a_blocker_with_no_resolution_recorded_still_counts():
    """The stricter reading when the reader did not say."""
    blocker = {k: v for k, v in _blocker("body-0400", kind="other").items() if k != "resolution"}
    review = final_review_verdict([], [{"verdict": "ready", "publication_blockers": [blocker]}])
    assert review["verdict"] == "needs_human" and review["waived_blockers"] == []


def test_the_ceiling_still_decides_when_a_placeholder_is_waived():
    """Waiving the blocker does not waive the count."""
    over = [_edit(i) for i in range(FINAL_REVIEW_ERROR_CEILING + 1)]
    review = final_review_verdict(over, [{"verdict": "ready", "publication_blockers": [_blocker("body-0058")]}])
    assert review["verdict"] == "needs_human"
    assert review["publication_blockers"] == [] and len(review["waived_blockers"]) == 1
