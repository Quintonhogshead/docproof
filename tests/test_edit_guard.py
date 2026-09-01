"""The overreach guard: a proofreading fix is minimal, so the validator refuses
an edit that re-types a large span or adds a lot of new text.

The two failures it exists for both showed up in a real manuscript comparison: a
model pass that turned a sentence's end punctuation into a fabricated dialogue
attribution ("…at a time.'" → "…at a time,' the captain said."), and one that
re-typed a whole run-on paragraph wholesale instead of making minimal tracked
edits. Neither is a proofreading correction; both are rejected before they can
become a tracked change, and counted so the rejection stays visible.
"""
from __future__ import annotations

import pytest

from docproof.config import EditGuardConfig, load_config
from docproof.models import DocumentModel, Finding, ParagraphRef
from docproof.validator import validate_findings

GUARD = EditGuardConfig()   # shipped defaults: 64-char span, 16-char growth


def _doc(text: str) -> DocumentModel:
    para = ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal")
    return DocumentModel(source_path="x.docx", paragraphs=(para,))


def _finding(original: str, corrected: str) -> Finding:
    return Finding(finding_id="f-0001", chunk_id="chunk-000", para_id="body-0000",
                   error_type="comma_splice", original_text=original, occurrence=1,
                   corrected_text=corrected, explanation="x", confidence="high")


def _status(original: str, corrected: str, guard=GUARD) -> str:
    doc = _doc(original)
    out = validate_findings([_finding(original, corrected)], doc, "medium",
                            edit_guard=guard)
    return out[0].status


# --- what the guard refuses --------------------------------------------------

def test_fabricated_dialogue_attribution_is_rejected():
    """The model invents 'the captain said,' adding a clause that is not a
    correction of anything. Net growth is past the cap, so it is refused."""
    assert _status("She left at noon.'",
                   "She left at noon,' the captain said.") == "rejected_oversized"


def test_wholesale_retype_of_a_run_on_is_rejected():
    """A run-on re-typed end to end — capitalisation and punctuation scattered
    throughout — leaves almost nothing shared to trim, so the minimal diff is
    the whole paragraph: a re-type, not a proofreading edit."""
    original = ("i had some of that i dont know how much and found myself on "
                "the dance floor beside the bonfire")
    corrected = ("I had some of that; I don't know how much, and found myself "
                 "on the dance floor, beside the bonfire.")
    assert _status(original, corrected) == "rejected_oversized"


def test_an_oversized_finding_keeps_its_anchor_for_the_comment_channel():
    """Rejected as a tracked change, but not thrown away: the anchor is retained
    so the reassembler can surface it as a margin comment for a human to make by
    hand (the round trip is in tests/test_queries.py). Dropping the anchor would
    put the catch back in the silent-rejection bucket this fix exists to empty."""
    doc = _doc("She left at noon.'")
    out = validate_findings(
        [_finding("She left at noon.'", "She left at noon,' the captain said.")],
        doc, "medium", edit_guard=GUARD)
    assert out[0].status == "rejected_oversized"
    assert out[0].anchor is not None


# --- what the guard lets through ---------------------------------------------

def test_a_missing_small_word_passes():
    assert _status("He holds out hand.", "He holds out a hand.") == "validated"


def test_a_spelled_out_number_passes():
    """House style spells out numbers to one hundred; 'one hundred' for '100'
    adds new characters but well under the growth cap — a real correction, not
    fabrication."""
    assert _status("She counted 100 sheep.",
                   "She counted one hundred sheep.") == "validated"


def test_a_one_character_fix_passes():
    assert _status("It was, late.", "It was late.") == "validated"


def test_a_pure_deletion_is_never_capped_by_growth():
    """Removing text is not fabrication, so even a large deletion clears the
    growth cap. (It could still trip the span cap; this one does not.)"""
    assert _status("He was very, very, very tired that day.",
                   "He was very tired that day.") == "validated"


# --- the guard is opt-in and configurable ------------------------------------

def test_disabling_the_guard_lets_an_over_large_edit_through():
    off = EditGuardConfig(enabled=False)
    assert _status("She left at noon.'",
                   "She left at noon,' the captain said.", off) == "validated"


def test_no_guard_argument_leaves_findings_unguarded():
    doc = _doc("She left at noon.'")
    out = validate_findings(
        [_finding("She left at noon.'", "She left at noon,' the captain said.")],
        doc, "medium")            # no edit_guard passed
    assert out[0].status == "validated"


def test_thresholds_are_honoured():
    """A tighter growth cap catches a shorter addition; a looser one lets the
    fabrication through."""
    tight = EditGuardConfig(max_added_chars=1)
    loose = EditGuardConfig(max_added_chars=100, max_edit_chars=100)
    assert _status("He holds out hand.", "He holds out a hand.", tight) \
        == "rejected_oversized"
    assert _status("She left at noon.'",
                   "She left at noon,' the captain said.", loose) == "validated"


def test_shipped_config_enables_the_guard():
    cfg = load_config("config/default.yaml")
    assert cfg.edit_guard.enabled
    assert cfg.edit_guard.max_edit_chars == 64
    assert cfg.edit_guard.max_added_chars == 16


# --- the compound-merge guard --------------------------------------------------
#
# The Purpura head proofreader reversed exactly one kind of "fix": the space in
# "blood work" deleted to make "bloodwork". Whether a compound is open or
# closed is the author's call — the consistency scan asks about it — so an
# edit whose whole effect is deleting one inter-word space is demoted to a
# margin query, never applied.

def test_a_space_deletion_merge_is_demoted_to_a_query():
    original = "Standard procedures include questions, blood work, and imaging."
    corrected = "Standard procedures include questions, bloodwork, and imaging."
    doc = _doc(original)
    out = validate_findings([_finding(original, corrected)], doc, "medium",
                            edit_guard=GUARD)
    assert out[0].status == "query"
    assert out[0].force_query


def test_a_space_deletion_beside_punctuation_is_not_a_merge():
    """Deleting a doubled space, or a space before punctuation, is an ordinary
    typographic fix — only a deletion that JOINS two words is a compound call."""
    original = "She left ,and the door closed."
    corrected = "She left,and the door closed."
    doc = _doc(original)
    out = validate_findings([_finding(original, corrected)], doc, "medium",
                            edit_guard=GUARD)
    assert out[0].status == "validated"


# --- number labels are not prose numbers (Redding Book 1, 2026-09-01) --------
#
# The typed number_style pass spelled out the numeral in "Mindset Number 23",
# renaming a section of the book. A numeral that is part of a LABEL — a
# labelling noun and its number, a list marker, a heading — is refused outright.

def _number_status(text: str, original: str, corrected: str,
                   style: str = "Normal") -> str:
    para = ParagraphRef("body-0000", "word/document.xml", "body", text, style)
    doc = DocumentModel(source_path="x.docx", paragraphs=(para,))
    f = Finding(finding_id="f-0001", chunk_id="chunk-000", para_id="body-0000",
                error_type="number_style", original_text=original, occurrence=1,
                corrected_text=corrected, explanation="x", confidence="high")
    return validate_findings([f], doc, "medium", edit_guard=GUARD)[0].status


def test_a_labelled_number_is_refused_by_policy():
    assert _number_status(
        "Mindset Number 23 is the one that finally landed.",
        "Mindset Number 23 is the one that finally landed.",
        "Mindset Number twenty-three is the one that finally landed."
    ) == "rejected_policy"


@pytest.mark.parametrize("label", [
    "Chapter 4", "Part 2", "Step 7", "Day 3", "Week 12", "Lesson 9",
    "Figure 5", "Table 1", "Section 6",
])
def test_every_labelling_noun_is_refused(label):
    noun, number = label.split()
    text = f"{label} was the hardest one to write."
    corrected = text.replace(number, "four" if number == "4" else "seven")
    assert _number_status(text, text, corrected) == "rejected_policy"


def test_a_list_marker_is_refused():
    assert _number_status("3) Keep the receipts in one place.",
                          "3) Keep the receipts in one place.",
                          "Three) Keep the receipts in one place."
                          ) == "rejected_policy"


def test_a_heading_is_refused():
    assert _number_status("The 3 Rules of Rest", "The 3 Rules of Rest",
                          "The Three Rules of Rest", style="Heading1"
                          ) == "rejected_policy"


def test_an_ordinary_prose_number_still_gets_styled():
    assert _number_status("She counted 3 dogs on the porch that morning.",
                          "She counted 3 dogs on the porch that morning.",
                          "She counted three dogs on the porch that morning."
                          ) == "validated"


def test_the_policy_gate_only_applies_to_number_style():
    """A typo fix inside a labelled line is still a typo fix."""
    para = ParagraphRef("body-0000", "word/document.xml", "body",
                        "Mindset Number 23 is teh one that landed.", "Normal")
    doc = DocumentModel(source_path="x.docx", paragraphs=(para,))
    f = Finding(finding_id="f-0001", chunk_id="chunk-000", para_id="body-0000",
                error_type="spelling",
                original_text="Mindset Number 23 is teh one that landed.",
                occurrence=1,
                corrected_text="Mindset Number 23 is the one that landed.",
                explanation="x", confidence="high")
    assert validate_findings([f], doc, "medium",
                             edit_guard=GUARD)[0].status == "validated"
