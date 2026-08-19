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
    # The reviewer types a straight apostrophe into the comment box; the book is
    # set in curly ones, and the correction is written in the book's.
    ("Add abbreviating apostrophe: callin'", "calling", "calling", "callin’"),
    ("Possessive: Shanklins'.", "the Shanklins house", "Shanklins", "Shanklins’"),
    ("Replace with drop apostrophe: ’Sides", "‘Sides, I’m hoping",
     "‘Sides", "’Sides"),
    # the note *is* the corrected text
    ("all right", "alright. ” There was", "alright", "all right"),
    ("wouldn't", "wouldnt", "wouldnt", "wouldn’t"),
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


def test_a_capitalize_clause_reaches_across_a_closing_quote_and_a_space():
    """The commonest shape of all: a comma that closes a line of dialogue, with a
    closing quotation mark AND a space between it and the named word — "though,” she".
    The adjacency test has to see across both, or the mark falls to the model, which
    on the real proof capitalized "she" and left the comma a comma."""
    got = r('Replace comma with period and capitalize "she"', "though,",
            context="“I guess. Y’all make sense, though,” she paused.")
    assert got is not None
    assert 'though.” S' in got.replace and 'though,” s' in got.find


def test_widening_to_the_line_needs_the_word_directly_after_the_mark():
    """That adjacency is the proof the line is the sentence the note is about. A
    line that merely holds a comma somewhere is not evidence of anything."""
    assert r('Replace comma with period and capitalize "he"', "paused,",
             context="He paused, then looked away.") is None


def test_the_named_word_picks_the_mark_out_of_several():
    """A line with two commas in it is not ambiguous when only one of them has the
    named word on its right. Refusing these was how "Because I\u2019m in the middle of
    preseason, besides, none of them\u2026" got its *second* comma turned into the
    period \u2014 the rule declined, the model picked, and it picked wrong."""
    got = r('Replace comma with period and capitalize "besides"', "preseason,",
            context="in the middle of preseason, besides, none of them are due")
    assert got is not None
    assert got.find == "preseason, b" and got.replace == "preseason. B"


def test_two_candidate_marks_carrying_the_named_word_still_decline():
    """The adjacency has to choose. When it does not, nothing else in the note
    does either."""
    assert r('Replace comma with period and capitalize "she"', "here,",
             context="Yes, she said, she was already gone.") is None


def test_no_other_rule_reaches_outside_the_mark():
    """Only the capitalize clause widens. Everything else stays inside the span the
    reviewer pointed at, whatever the line around it says."""
    line = "He was hungry, and tired, and a long way from home."
    assert r("Lowercase", "Hungry", context=line).find == "Hungry"
    assert r("Remove comma", "sure", context=line) is None
    assert r("Add comma after", "hungry", context=line).find == "hungry"


# --- quotation marks, and the apostrophe that is the same character -------------

def test_removing_single_quotes_keeps_the_possessive_inside_them():
    """The bug this pins shipped a wrong change without anyone seeing it. A closing
    single quote and an apostrophe are one character, so removing "the single
    quotes" around a marked span removed the possessive too — and the edit is
    mechanical, so it applied. Only the outermost pair is the reviewer's."""
    got = resolve("Remove single quotes", "‘For fuck’s sake,’")
    assert got.replace == "For fuck’s sake,"


def test_a_quotation_holding_a_contraction_converts_to_doubles():
    """Most quoted dialogue holds a contraction, so a rule that could only handle
    a span with exactly one closing mark declined nearly all of them."""
    assert resolve("Replace single quotes with doubles",
                   "‘LET’S GO’").replace == "“LET’S GO”"
    assert resolve("Replace single quotes with doubles",
                   "‘y’all.’").replace == "“y’all.”"
    # `_focus` narrows to the run that changed, so the trailing words drop off.
    got = resolve("Replace single quote with double",
                  "‘Oh, son, don’t overexert yourself.’ The latter")
    assert got.find == "‘Oh, son, don’t overexert yourself.’"
    assert got.replace == "“Oh, son, don’t overexert yourself.”"


def test_a_quotation_that_never_closes_in_the_mark_is_left_for_a_person():
    """A quotation running on to the next line is marked one line at a time, so
    the span opens and does not close. The last apostrophe in it must not pass for
    the closing mark — that would write "aren”t"."""
    assert resolve("Replace single quote with double",
                   "on his part. ‘If you aren’t going to speak to") is None


def test_two_quoted_runs_in_one_mark_are_left_for_a_person():
    assert resolve("Replace single quotes with doubles",
                   "‘towel head,’ ‘terrorist,’") is None


def test_a_rule_writes_the_book_s_apostrophe_not_the_reviewer_s():
    """A note is typed into a comment box, where "didn't" gets a straight quote,
    and the book is set in curly ones. Correcting the word and introducing a
    typographic inconsistency in the same stroke is not a correction."""
    assert resolve("didn't", "doesn’t").replace == "didn’t"
    assert resolve("Add abbreviating apostrophe: callin'", "callin").replace \
        == "callin’"
    assert resolve("Possessive: Shanklins'.", "Shanklins").replace == "Shanklins’"


def test_a_leading_straight_quote_is_left_as_typed():
    """An elision and an opening quotation are indistinguishable there, so the
    direction is not guessed."""
    assert resolve("Add drop apostrophe: 'bout", "How bout a drive").replace \
        == "'bout"


# --- two marks on the same words -----------------------------------------------

def _c(cid, page, anchor, instruction, offset, kind="highlight", context=""):
    return PdfComment(id=cid, page=page, anchor=anchor, instruction=instruction,
                      offset=offset, kind=kind, context=context)


def test_one_request_marked_twice_becomes_one_edit_citing_both_comments():
    """A reviewer converting a quotation puts a note on the opening mark and
    another on the closing one. That is one request recorded twice: two edits
    would mean the second looking for text the first had already changed, and
    coming back as a correction that could not be found."""
    rows, unresolved = edits_from_comments([
        _c("p386-550", 386, "saying ‘KY Kingdom or BUST’ were",
           "Replace single quote with double", 184),
        _c("p386-551", 386, "saying ‘KY Kingdom or BUST’ were",
           "Replace single quote with double", 184)])
    assert not unresolved and len(rows) == 1
    # Both comments cite the one edit, so the change log still answers for each.
    assert rows[0]["source"] == "p386-550 p386-551"
    assert rows[0]["replace"] == "“KY Kingdom or BUST”"


def test_the_same_words_marked_in_two_places_stay_two_edits():
    """Two copies of "‘Baba’" in one paragraph, both marked, are two requests —
    and collapsing them would leave one of them uncorrected. The marks sit at
    different positions, which is what tells this from the case above."""
    page = "I switch between ‘Baba’ and ‘Dad’ effortlessly. I used ‘Dad’ to " \
           "make a point and ‘Baba’ to be playful."
    rows, _ = edits_from_comments(
        [_c("p111-128", 111, "‘Baba’", "Replace single quotes with doubles", 17),
         _c("p111-131", 111, "‘Baba’", "Replace single quotes with doubles", 84)],
        pages={111: page})
    assert len(rows) == 2
    assert [r["occurrence"] for r in rows] == [1, 2]


def test_no_ordinal_is_carried_for_text_that_occurs_once():
    """An edit with no ordinal is the one that insists its anchor be unique, which
    is a stronger check than any number — so the number is only added where the
    page really does hold several copies."""
    rows, _ = edits_from_comments(
        [_c("p9-1", 9, "harbor", "harbour", 10)],
        pages={9: "we sailed into the harbor at dawn"})
    assert "occurrence" not in rows[0]


def test_a_repeated_word_reads_its_offset_in_the_proof_not_the_book():
    """The mark's offset is measured against the PDF's rendering of the page —
    running head and folio included — while the copies are counted over the book's
    own text. Those are different coordinate systems: dropping the offset straight
    into the book text put the mark between the book's copies and chose none of
    them, so the edit came back ambiguous. Positioned in the proof text, where the
    offset is exact, it lands on the copy the reviewer marked."""
    book = "the harbor at dawn, and the harbor at dusk"      # two copies
    proof = "48  THE CROSSING\n" + book                      # + a running head
    off = proof.index("harbor", proof.index("harbor") + 1)   # the 2nd copy's mark
    rows, _ = edits_from_comments(
        [_c("p48-2", 48, "harbor", "harbour", off)],
        pages={48: book}, pdf_pages={48: proof})
    assert rows[0]["occurrence"] == 2


def test_no_ordinal_when_the_renderings_disagree_on_the_count():
    """A running head that itself holds the word is a copy the proof has and the
    book does not, so a rank in the one is not a rank in the other. The ordinal is
    refused rather than miscounted — the anchor's uniqueness check still guards the
    apply."""
    book = "the harbor at dawn, and the harbor at dusk"      # two copies
    proof = "48  the harbor chapter\n" + book                # running head: three
    off = proof.index("harbor", proof.index("harbor") + 1)
    rows, _ = edits_from_comments(
        [_c("p48-2", 48, "harbor", "harbour", off)],
        pages={48: book}, pdf_pages={48: proof})
    assert "occurrence" not in rows[0]


def test_a_model_edit_takes_its_ordinal_from_the_mark_it_cites():
    """A model-read edit carries no reliable ordinal — the schema asks the model to
    count copies off a page dumped to text, and that is exactly where it miscounts.
    The citing comment's own offset settles it deterministically, the same way and
    in the same coordinate the rule path does, so a repeated word the model left at
    0 lands on the copy the mark sits on instead of flagging."""
    from docproof.corrections.instructions import fill_edit_occurrences
    from docproof.corrections.model import Edit
    book = "she can wait, or she can leave"
    proof = "90  THE OFFER\n" + book
    off = proof.index("can", proof.index("can") + 1)          # the 2nd copy
    edit = Edit("e1", "can", "could", source="p90-3", page=90)   # occurrence 0
    filled = fill_edit_occurrences(
        [edit], [_c("p90-3", 90, "can", "could", off)],
        book_pages={90: book}, pdf_pages={90: proof})
    assert filled[0].occurrence == 2


def test_filling_clears_an_ordinal_the_model_invented_for_a_unique_word():
    """A find that occurs once needs no ordinal, and insisting the anchor be unique
    is the stronger check — so a stray number a model attached is cleared, rather
    than left to turn a clean apply into 'asked for #2 of 1'."""
    from docproof.corrections.instructions import fill_edit_occurrences
    from docproof.corrections.model import Edit
    book = "she can wait by the door"                         # one copy
    edit = Edit("e1", "can", "could", occurrence=2, source="p90-3", page=90)
    filled = fill_edit_occurrences(
        [edit], [_c("p90-3", 90, "can", "could", 4)],
        book_pages={90: book}, pdf_pages={90: book})
    assert filled[0].occurrence == 0


def test_filling_leaves_a_repeat_it_cannot_locate_as_extracted():
    """A sticky note carries no highlight and so no position; a repeated word it
    marks cannot be pinned to a copy deterministically, so the fill leaves the edit
    exactly as extracted rather than guessing."""
    from docproof.corrections.instructions import fill_edit_occurrences
    from docproof.corrections.model import Edit
    book = "she can wait, or she can leave"                   # two copies
    edit = Edit("e1", "can", "could", occurrence=1, source="p90-3", page=90)
    filled = fill_edit_occurrences(
        [edit], [_c("p90-3", 90, "can", "could", -1)],        # -1: no position
        book_pages={90: book}, pdf_pages={90: book})
    assert filled[0].occurrence == 1


def test_the_ledger_reaches_both_comments_of_a_merged_edit():
    """The point of merging into one edit rather than dropping a comment: every
    mark still gets told what became of it."""
    from docproof.corrections.model import DISP_APPLIED, Edit
    from docproof.corrections.run import _reconcile_comments
    edit = Edit("e1", "‘x’", "“x”", source="p1-1 p1-2")
    dispositions = _reconcile_comments(
        [_c("p1-1", 1, "‘x’", "Replace single quotes with doubles", 0),
         _c("p1-2", 1, "‘x’", "Replace single quotes with doubles", 0)],
        [edit], lambda _id: (DISP_APPLIED, ""))
    assert [d.disposition for d in dispositions] == [DISP_APPLIED, DISP_APPLIED]
    assert all(d.edit_ids == ("e1",) for d in dispositions)


# --- the typography a note cannot carry ---------------------------------------

def test_an_inserted_dash_is_closed_up():
    """The house sets both dashes with no space around them. A comma swapped for
    an em dash leaves the space that followed the comma standing, and "Exactly—
    what else" is then the one shape in the book that is not house style — four of
    them on the last proof, each one a correctly applied edit."""
    got = r("Replace with em dash", "Exactly, what else would you be doing")
    assert got is not None
    assert got.find == "Exactly, wha" and got.replace == "Exactly—wha"


def test_a_dash_is_closed_up_on_both_sides():
    got = r("Replace comma with em dash", "the pitch , when things got tough")
    assert got is not None
    assert got.find == "the pitch , " and got.replace == "the pitch—"


def test_an_en_dash_takes_the_score_not_the_hyphenated_compound():
    """"an easy tap-in to go up 1-0" holds two hyphens and the note names neither.
    One of them is between digits, which is the only thing an en dash is ever asked
    for here — and picking the other one printed "tap–in" and left the score."""
    got = r("Replace with en dash", "an easy tap-in to go up 1-0")
    assert got is not None
    assert got.find == " to go up 1-" and got.replace == " to go up 1–"


def test_a_note_may_show_the_character_it_names():
    """`Replace with en dash (–).` is the same instruction. Reading it as neither
    sent a score to the model, which put an em dash in instead."""
    got = r("Replace with en dash (–).", "We won 3-1, and I played")
    assert got is not None
    assert got.find == "We won 3-1, " and got.replace == "We won 3–1, "


def test_enclosing_a_single_quoted_span_converts_the_marks():
    """Wrapping it instead nests one pair inside the other and prints ‘“like
    this”’ — which is what the find string that omitted the singles produced."""
    got = r("Enclose in quotation marks", "‘What the fuck just happened?’")
    assert got is not None
    assert got.replace == "“What the fuck just happened?”"


def test_enclosing_declines_a_span_whose_quotes_do_not_pair():
    assert r("Enclose in quotation marks", "‘it isn’t over") is None


def test_house_typography_over_edits_a_model_read():
    from docproof.corrections.instructions import house_typography
    from docproof.corrections.model import Edit
    got = {e.id: e.replace for e in house_typography([
        Edit("a", "Id just had", "I'd just had"),
        Edit("b", "appétit”", "appétit”,"),
        Edit("c", "off the pot’”", "off the pot’”."),
    ])}
    assert got["a"] == "I’d just had"
    assert got["b"] == "appétit,”"
    assert got["c"] == "off the pot.’”"


def test_house_typography_leaves_the_book_alone():
    """Only what the edit itself introduced. A `find` that already carries the
    shape is book text the reviewer did not mark."""
    from docproof.corrections.instructions import house_typography
    from docproof.corrections.model import Edit
    kept = house_typography([Edit("a", "the pot’”.", "the pot’”. He said")])
    assert kept[0].replace == "the pot’”. He said"


def test_a_long_mark_still_answers_a_note_that_names_its_target():
    """A mark over a whole line points at nothing in particular, so the rules that
    have to pick inside it decline. Naming is not picking: only one hyphen on this
    line stands between two digits, and it is the one an en dash is asked for."""
    got = r("Replace hyphen with en dash (–)",
            "ing left winger, Danny. It was an easy tap-in to go up 1-0. As")
    assert got is not None
    assert got.find == " to go up 1-" and got.replace == " to go up 1–"


def test_a_long_mark_declines_the_bare_count():
    """One comma in a line the reviewer highlighted whole is not evidence of
    anything — they marked the line, not the comma."""
    assert r("Replace comma with period",
             "ing left winger, Danny. It was an easy tap in to go up one nil") is None


def test_an_addition_keeps_the_marked_opening_quote():
    """"Add drop apostrophe: ’bout" against ‘bout — the ‘ opens a nested quotation
    whose ’ is further down the line, and writing the literal over the word
    converts the opener and leaves the closer without a partner."""
    got = r("Add drop apostrophe: ’bout", "‘bout")
    assert got is not None and got.replace == "‘’bout"


def test_a_replacement_after_a_colon_still_replaces():
    """Only an *addition* keeps what the mark carried. "Replace with" is a swap."""
    assert r("Replace with: ’bout", "‘bout").replace == "’bout"


def test_a_note_that_asks_for_nothing_is_not_a_change_request():
    from docproof.corrections.instructions import asks_for_a_change
    assert asks_for_a_change("Song title should be enclosed in quotes, "
                             "not italicized")
    assert asks_for_a_change("Replace comma with period")
    assert not asks_for_a_change("already fine")
    assert not asks_for_a_change("stet")
    assert not asks_for_a_change("Leave as set — the repetition is deliberate")
