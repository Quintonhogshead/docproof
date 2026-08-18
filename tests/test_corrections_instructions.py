"""Reviewer notes resolved against the span they were marked on, with no model.

Most of what a copy editor writes on a proof has one right answer given the marked
text: "Lowercase" over a word, "Replace comma with period" over a clause, a bare
"wouldn't" over "wouldnt". Those were being handed to a model that had to invent a
`find` for them from a rendering of a book it had never seen, and it produced bare
commas with seven thousand homes in the novel.

The other half of these tests is the declining. A rule that fires on a note it has
not really understood is worse than no rule at all, because the edit it proposes is
plausible — so each rule's guard is tested as deliberately as its behaviour.
"""
from __future__ import annotations

import pytest

from docproof.corrections.from_pdf import PdfComment
from docproof.corrections.instructions import (edits_from_comments, resolve)
from docproof.corrections.model import JUDGMENT, MECHANICAL


def r(note, anchor, *, context="", highlighted=True):
    return resolve(note, anchor, context=context, highlighted=highlighted)


# --- the notes that have one right answer -------------------------------------

@pytest.mark.parametrize("note, anchor, find, replace", [
    # punctuation named in words
    ("Replace comma with period", "“Okay,", "“Okay,", "“Okay."),
    ("Replace period with comma", "tonight.", "tonight.", "tonight,"),
    ("Replace comma with ellipsis", "saying,", "saying,", "saying…"),
    ("Replace period with question mark", "eyes.", "eyes.", "eyes?"),
    ("Remove comma", "sure,", "sure,", "sure"),
    ("Add comma after", "damn", "damn", "damn,"),
    ("Add period after", "Dr", "Dr", "Dr."),
    # case, where the mark is on the word
    ("Lowercase", "Hungry?", "Hungry", "hungry"),
    ("Capitalize", "solo", "solo", "Solo"),
    ("Capital T", "a tournament", "a tournament", "a Tournament"),
    # the note states the answer
    ("Add abbreviating apostrophe: callin'", "calling", "calling", "callin'"),
    ("Possessive: Shanklins'.", "the Shanklins house", "Shanklins", "Shanklins'"),
    ("Replace with drop apostrophe: ’Sides", "‘Sides, I’m hoping",
     "‘Sides", "’Sides"),
    # the note *is* the corrected text
    ("all right", "alright. ” There was", "alright", "all right"),
    ("wouldn't", "wouldnt", "wouldnt", "wouldn't"),
    ("switched", "switch", "switch", "switched"),
    ("’til", "till", "till", "’til"),
    # other shapes
    ("Hyphenate", "twenty five", "twenty five", "twenty-five"),
    ("Enclose in quotes", "across the", "across the", "“across the”"),
    ("Replace single quotes with doubles", "‘Baba’", "‘Baba’", "“Baba”"),
])
def test_a_note_resolves_against_its_span(note, anchor, find, replace):
    got = r(note, anchor)
    assert got is not None, f"{note!r} on {anchor!r} did not resolve"
    assert (got.find, got.replace) == (find, replace)
    assert got.kind == MECHANICAL


def test_a_punctuation_swap_can_capitalize_the_next_word():
    got = r('Replace comma with period and capitalize "she"', "quietly, she left")
    assert got is not None
    assert got.replace.startswith("quietly. She") or got.replace == ". She"
    assert "She" in got.replace


def test_the_find_is_always_part_of_the_marked_span():
    """The one invariant that makes these rules safe: a rule may only ever propose
    changing text the reviewer actually pointed at."""
    for note, anchor in [("Lowercase", "Hungry?"), ("all right", "alright, then"),
                         ("Replace comma with period", "“Okay,"),
                         ("Add comma after", "damn")]:
        got = r(note, anchor)
        assert got is not None and got.find in anchor


def test_a_find_long_enough_to_locate_is_kept():
    """A minimal span is right to write and wrong to locate: the smallest edit for
    "replace this comma with a period" is "," → ".", and a novel holds thousands of
    commas. The span widens back out until it can serve as an address."""
    got = r("Replace comma with period", "he said, and then he left")
    assert got is not None
    assert len(got.find) >= 12
    assert "," in got.find and "." in got.replace


# --- the notes a rule must refuse ---------------------------------------------

def test_a_query_is_never_turned_into_a_mechanical_edit():
    assert r("Should this be a single period or an ellipsis?", "wait.") is None
    assert r("Unsure if there's a typo here", "dude. Brent") is None
    assert r("Confusing -- should this be deleted?", "locked the door") is None


def test_a_question_naming_its_own_answer_becomes_a_judgment_edit():
    """`Should this be "not at all"?` still needs a person, but the change it
    proposes is right there in quotes — and showing an editor the proposal beats
    telling them the model proposed nothing."""
    got = r('Should this be "not at all"?', "not all, besides you")
    assert got is not None
    assert got.kind == JUDGMENT
    assert got.replace == "not at all"


def test_a_case_note_declines_on_anything_longer_than_the_word():
    """"Capitalize" names no word, so over a clause the first letter is a guess —
    and on a hyphenation fragment ("hibition" for "exhibition") it is nonsense."""
    assert r("Capitalize", "hibition, but even") is None
    assert r("Capitalize", "took a sip,") is None
    assert r("Lowercase", "man, and in") is None


def test_add_after_declines_when_the_span_runs_past_the_target():
    """"Add comma after" means after the marked words. A span that overshoots into
    the next sentence would put the comma in the wrong place entirely — a vocative
    comma for "Sure" landing after "Sure.” I was"."""
    assert r("Add comma after", 'Sure. ” I was') is None
    assert r("Add comma after", "Maryam. We’ll") is None
    assert r("Add comma after", "motherfuckers let’s circle up. Cyrus") is None


def test_a_punctuation_swap_declines_when_the_span_holds_two_candidates():
    """The note does not say which comma, so the rule does not pick one. Picking is
    the failure this module exists to remove."""
    assert r("Replace comma with period", "he said, quietly, and left") is None
    assert r("Remove comma", "one, two, three") is None


def test_a_punctuation_swap_declines_when_the_span_holds_none():
    assert r("Replace comma with period", "I paused") is None
    assert r("Remove comma", "sure") is None


def test_a_bare_word_declines_when_it_matches_nothing_in_the_span():
    """The commonest case on a real proof: the mark was read back off the wrong
    words, so there is nothing for the note to be a correction *of*."""
    assert r("wouldn't", "make it go away.") is None
    assert r("she'd", "match. Like") is None
    assert r("meant", "“Areh.") is None


def test_a_bare_word_declines_when_it_would_rewrite_the_wrong_word():
    """"backup" against a span truncated to "up the" must not rewrite "up" — that
    yields "back backup the". A correction of a word keeps the word's first letter."""
    assert r("backup", "up the") is None


def test_a_bare_word_replaces_only_the_word_it_corrects():
    """Not the whole marked span: replacing "alright. ” There was" with "all right"
    would delete the rest of the line."""
    got = r("all right", 'alright. ” There was')
    assert got is not None and got.find == "alright"


def test_a_sticky_note_names_no_target_so_targeted_rules_decline():
    """A note's anchor is the whole line it sits on, which points at nothing in
    particular. Rules that need a marked word must not guess inside it."""
    line = "he carried a pouch of tobacco here"
    assert r("Lowercase", line, highlighted=False) is None
    assert r("Add comma after", line, highlighted=False) is None
    assert r("tobacco pouch", line, highlighted=False) is None


def test_an_italic_request_becomes_a_formatting_edit():
    """Formatting used to be routed to a designer and applied by nobody. The mark
    names its own target — the highlighted words are exactly what gets styled — and
    the text is left alone."""
    got = r("Italicize movie title", "Remember the Titans")
    assert got is not None
    assert (got.find, got.replace) == ("Remember the Titans",
                                       "Remember the Titans")
    assert got.format == "italic"

    roman = r("De-italicize", "of Queen’s")
    assert roman is not None and roman.format == "roman"


def test_a_layout_request_is_a_paragraph_edit_not_a_character_one():
    """Italics are a property of a run of text; where a chapter starts is a property
    of the paragraph. Both are appliable, and they are not the same edit."""
    got = r("Move this chapter to start on a recto", "Chapter Twelve")
    assert got is not None
    assert got.paragraph == "recto" and not got.format
    assert got.find == got.replace          # the words are untouched


def test_a_destructive_operation_is_never_read_out_of_a_question():
    """"Should we cut this paragraph?" names the operation and asks for it in the
    same breath. Reading that as an instruction is worst here of all."""
    assert r("Should we cut this paragraph?", "a line of prose") is None
    assert r("Delete this paragraph?", "a line of prose") is None
    assert r("Should this start on a recto?", "Chapter Twelve") is None


def test_a_note_that_changes_nothing_is_not_an_edit():
    assert r("Lowercase", "hungry") is None          # already lower case
    assert r("all right", "all right") is None       # the note quotes the text


# --- splitting a proof between the rules and the model ------------------------

def _comment(instruction, anchor, *, kind="highlight", cid="p1-1", page=1):
    return PdfComment(page=page, instruction=instruction, anchor=anchor,
                      context=anchor, kind=kind, id=cid)


def test_only_the_notes_no_rule_understands_reach_the_model():
    comments = [
        _comment("All right", "Alright, kiddo", cid="p98-1", page=98),
        _comment("Replace comma with period", "“Okay,", cid="p99-1", page=99),
        _comment("Should we cut this paragraph?", "a line of prose",
                 kind="note", cid="p100-1", page=100),
    ]
    rows, unresolved = edits_from_comments(comments)
    assert len(rows) == 2
    assert [c.id for c in unresolved] == ["p100-1"]


def test_a_resolved_row_carries_its_comment_id_so_the_page_comes_free():
    """The row cites the comment it came from; the id names the page; `parse_edits`
    reads the page off it. Nothing has to retype a page number."""
    from docproof.corrections.parse import parse_edits
    rows, _ = edits_from_comments([
        _comment("Replace comma with period", "“Okay,", cid="p261-3", page=261)])
    assert rows[0]["source"] == "p261-3"
    edit = parse_edits(rows).edits[0]
    assert edit.page == 261
    assert edit.instruction == "Replace comma with period"


def test_a_resolved_row_keeps_the_line_as_its_context():
    rows, _ = edits_from_comments([
        PdfComment(page=5, instruction="Lowercase", anchor="Hungry",
                   context="“Hungry?” she asked.", kind="highlight", id="p5-1")])
    assert rows[0]["context"] == "“Hungry?” she asked."


# --- the one note that reaches past its own mark ------------------------------

def test_a_capitalize_clause_may_work_over_the_marked_line():
    """A reviewer highlights the comma, not the word after it, so
    `Replace comma with period and capitalize "we"` cannot be carried out inside the
    mark. It is the commonest instruction on a proof after a bare word swap, and
    declining it was most of what "flagged for a human" meant."""
    got = r('Replace comma with period and capitalize "we"', "late,",
            context="It was late, we were tired and the road went on")
    assert got is not None
    assert "late. W" in got.replace
    assert "late, w" in got.find


def test_widening_to_the_line_needs_the_word_directly_after_the_mark():
    """That adjacency is the proof the line is the sentence the note is about. A
    line that merely holds a comma somewhere is not evidence of anything."""
    assert r('Replace comma with period and capitalize "he"', "paused,",
             context="He paused, then looked away.") is None


def test_widening_to_the_line_needs_the_mark_to_be_unique_in_it():
    assert r('Replace comma with period and capitalize "she"', "here,",
             context="Everyone else here, she said, was already gone.") is None


def test_no_other_rule_reaches_outside_the_mark():
    """Only the capitalize clause widens. Everything else stays inside the span the
    reviewer pointed at, whatever the line around it says."""
    line = "He was hungry, and tired, and a long way from home."
    assert r("Lowercase", "Hungry", context=line).find == "Hungry"
    assert r("Remove comma", "sure", context=line) is None
    assert r("Add comma after", "hungry", context=line).find == "hungry"
