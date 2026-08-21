"""Anchoring a correction quoted from a typeset PDF into the book's own text.

The failure this covers is the one that dominated a real 400-page proof: the
anchor is quoted from a PDF rendering, the book is an IDML, and the same sentence
is not the same string in the two places. A kerning jump comes back as a space
(`. ’` for `.’`), a word broken over a line end keeps its hyphen, an em dash is
spaced in one and not the other — and the old matcher, exact then a
length-preserving punctuation fold, could bridge none of it.

Two mechanisms are tested together here because they fix one problem between
them: `textmatch`'s normalized view (what the anchor is compared against) and
`pagemap`'s page scope (how a repeated find is narrowed to the page it was
marked on).
"""
from __future__ import annotations

import pytest

from docproof.corrections.apply import all_spans, apply_to_stories
from docproof.corrections.idml import parse_story
from docproof.corrections.model import AMBIGUOUS, APPLIED, Edit, OFF_PAGE
from docproof.corrections.pagemap import PageMap, build_page_map
from docproof.corrections.textmatch import IndexCache, NormIndex, normalize


def _story(*paragraphs: str, story_id: str = "s1"):
    body = "".join(
        '<ParagraphStyleRange><CharacterStyleRange><Content>' + p
        + '</Content></CharacterStyleRange></ParagraphStyleRange>'
        for p in paragraphs)
    return parse_story(('<?xml version="1.0"?><Story>' + body
                        + '</Story>').encode("utf-8"), story_id)


# --- the normalized view ------------------------------------------------------

def test_normalize_drops_what_two_renderings_disagree_about():
    assert normalize("he said.  ” Then") == normalize("he said.”Then")
    assert normalize("twenty-five") == normalize("twenty five") == "twentyfive"
    assert normalize("said — and") == normalize("said—and") == "saidand"
    assert normalize("wait…") == normalize("wait...") == "wait..."
    assert normalize("“curly”") == normalize('"curly"') == '"curly"'


def test_norm_index_maps_a_match_back_to_the_real_characters():
    """The whole point of the index: it matches the canonical form but hands back
    a span of the real text, because the real characters are what get overwritten."""
    real = 'She stopped.” He waited.'
    idx = NormIndex(real)
    (start, end), = idx.spans('stopped. ” He')
    assert real[start:end] == 'stopped.” He'


def test_norm_index_ignores_an_anchor_of_only_dropped_characters():
    """An anchor made of whitespace and dashes is not an anchor. Saying so beats
    matching everywhere."""
    assert NormIndex("anything at all").spans("  --  ") == []


@pytest.mark.parametrize("book, anchor", [
    # a kerning jump read back as a space, before a closing quote
    ("‘WE’RE LATE.’ It was a lot like", "‘WE’RE LATE. ’ It was a lot like"),
    ("getting booked.’ That cooled them", "getting booked. ’ That cooled them"),
    ("you can plan on it.”", "plan on it. ”"),
    ("we were ridin’. That", "ridin’ . That"),
    ("‘head,’ ‘terrorist,’ and", "head, ’ ‘terrorist, ’ and"),
    # a spaced em dash against the house's unspaced one
    ("she stopped—then went on", "she stopped — then went on"),
    # a word split across PDF text chunks
    ("the unnamed street", "the un nam ed street"),
])
def test_a_pdf_artifact_anchor_still_finds_the_book_text(book, anchor):
    assert all_spans(book, anchor) != []


def test_a_word_truncated_at_a_line_break_never_overwrites_half_a_word():
    """`conces-` is "concession" broken over a line end. It may narrow a search
    (it is a real, if partial, quotation) but it must never be treated as the text
    to replace, or the edit writes over the first six letters and leaves "sion"."""
    book = "walked to the concession stand"
    assert all_spans(book, "conces-") == []                    # not a find
    assert all_spans(book, "conces-", partial_words=True) != []  # may narrow


def test_the_normalized_tier_only_runs_when_the_exact_one_fails():
    """An anchor that already matches is located exactly as before, so the wider
    net cannot move an edit that was landing correctly."""
    book = "a b-c d bc e"
    # "b-c" is present verbatim; the normalized view would also match "bc".
    assert all_spans(book, "b-c") == [(2, 5)]


def test_an_artifact_anchor_applies_to_the_story():
    """End to end: the space the PDF reader invented before the closing quote no
    longer costs the edit."""
    s = _story('“Alright.” There was nothing else to say.')
    outs, changed = apply_to_stories([s], [
        Edit(id="e1", find="Alright", replace="All right",
             context='alright. ” There was')])
    assert outs[0].status == APPLIED
    assert s.paragraphs[0].text.startswith('“All right.”')


# --- the page scope -----------------------------------------------------------

# One comma on page 1, two on page 2, one on page 3 — four in the "book", so the
# page is the only thing that can choose between them.
BOOK_PAGES = [
    "She waited by the door, listening. The house was quiet.",
    "He said nothing, then he laughed, and the room warmed up.",
    "Later she would remember it differently, and smile about it.",
]


def _three_page_story():
    return _story(*BOOK_PAGES)


def test_a_repeated_find_lands_on_the_page_it_was_marked_on():
    """A comma is everywhere in a book. The page a mark sat on is what chooses,
    and it is the narrowing an IDML could not do before."""
    s = _three_page_story()
    scope = build_page_map([s], BOOK_PAGES)
    edit = Edit(id="e1", find=",", replace=".", page=3)
    outs, _ = apply_to_stories([s], [edit], scope=scope)
    assert outs[0].status == APPLIED
    assert s.paragraphs[2].text.startswith(
        "Later she would remember it differently. and smile")


def test_without_a_page_the_same_edit_is_flagged_not_guessed():
    s = _three_page_story()
    outs, _ = apply_to_stories([s], [Edit(id="e1", find=",", replace=".")])
    assert outs[0].status == AMBIGUOUS
    assert outs[0].occurrences == 4


def test_a_page_that_still_holds_two_copies_is_flagged_with_the_real_count():
    """The scope narrows; it does not guess. Two commas on the marked page is
    still a choice for a human — but a choice between two, not four."""
    s = _three_page_story()
    scope = build_page_map([s], BOOK_PAGES)
    outs, _ = apply_to_stories([s], [Edit(id="e1", find=",", replace=".",
                                          page=2)], scope=scope)
    assert outs[0].status == AMBIGUOUS
    assert outs[0].occurrences == 2
    assert "page 2" in outs[0].detail


def test_a_page_scope_that_cannot_place_the_page_falls_back_to_the_book():
    """A page map is a narrower, never a gate: a page it could not align must not
    cost an edit that would otherwise land."""
    s = _three_page_story()
    scope = build_page_map([s], BOOK_PAGES)
    outs, _ = apply_to_stories([s], [Edit(id="e1", find="listening",
                                          replace="waiting", page=99)],
                               scope=scope)
    assert outs[0].status == APPLIED


def test_a_lone_match_on_the_wrong_placed_page_is_refused():
    """The p157→181 bug: a mark cites a page that WAS placed, but the only copy of
    its text is on another page. Applying it would land on the wrong copy, so the
    failed page anchor is refused rather than discarded just because one candidate
    remains. (Contrast the page-99 case above, where the page could not be placed
    and the edit rightly falls back to the book.)"""
    s = _three_page_story()
    scope = build_page_map([s], BOOK_PAGES)
    outs, _ = apply_to_stories([s], [Edit(id="e1", find="listening",
                                          replace="waiting", page=3)],
                               scope=scope)
    assert outs[0].status == OFF_PAGE and outs[0].needs_human
    assert s.paragraphs[0].text == BOOK_PAGES[0]     # page 1 untouched


def test_the_proof_exonerates_a_lone_match_the_map_mis_placed():
    """The counterpart to the refusal above: the map placed the cited page but its
    alignment did not cover the marked line, and the line's one copy in the book sits
    outside it — so on the alignment alone this reads as the wrong copy. But the
    proof's own cited page prints the line, so the map simply missed it, and refusing
    would let a bad alignment cost the only correction. It applies."""
    s = _three_page_story()
    # Page 3 is placed, but its proof text also prints "listening" — a line the
    # alignment (which pins page 3 to its own paragraph) does not cover, and whose
    # single book copy lives back on page 1.
    proof = list(BOOK_PAGES)
    proof[2] = BOOK_PAGES[2] + " She was still listening."
    scope = build_page_map([s], proof)
    assert scope.knows(3)
    outs, _ = apply_to_stories([s], [Edit(id="e1", find="listening",
                                          replace="waiting", page=3)],
                               scope=scope)
    assert outs[0].status == APPLIED
    assert s.paragraphs[0].text == "She waited by the door, waiting. The house was quiet."


def test_the_page_map_survives_a_pdf_rendering_of_the_page():
    """The page text comes from the PDF reader, so it carries the same artifacts
    the anchors do. The map has to align through them."""
    s = _three_page_story()
    as_read = [
        "She waited by the door, listening. The house was qui- et.",
        "He said nothing, then he laughed , and the room warmed up.",
        "Later she would remember it dif- ferently , and smile about it.",
    ]
    scope = build_page_map([s], as_read)
    assert scope.knows(1) and scope.knows(2) and scope.knows(3)
    outs, _ = apply_to_stories([s], [Edit(id="e1", find=",", replace=".",
                                          page=1)], scope=scope)
    assert outs[0].status == APPLIED
    assert s.paragraphs[0].text.startswith("She waited by the door. listening")


def test_an_empty_page_map_is_no_scope_at_all():
    s = _three_page_story()
    scope = build_page_map([s], [])
    assert isinstance(scope, PageMap)
    assert not scope.knows(1)


def test_the_index_cache_tracks_a_paragraph_through_an_edit():
    """The cache is keyed on the text, not the paragraph, so a paragraph that an
    earlier edit rewrote is never matched against its stale index."""
    cache = IndexCache()
    first = cache.get("the quick fox")
    assert cache.get("the quick fox") is first
    assert cache.get("the slow fox") is not first


def test_a_page_covers_part_of_the_paragraphs_it_straddles():
    """A real page starts and ends mid-paragraph — a book is a flow, and the page
    breaks fall wherever the measure runs out. The map has to attribute part of a
    paragraph to a page, or an edit on the last line of page 7 would look like it
    belongs to page 8."""
    s = _story(
        "The first paragraph runs on for a while and then keeps going past where "
        "the page will break, carrying a comma, into the next page entirely.",
        "A second paragraph, shorter, that finishes the spread off.")
    # Page 1 takes the head of paragraph 1; page 2 takes its tail plus paragraph 2.
    page1 = "The first paragraph runs on for a while and then keeps going past where"
    page2 = ("the page will break, carrying a comma, into the next page entirely. "
             "A second paragraph, shorter, that finishes the spread off.")
    scope = build_page_map([s], [page1, page2])
    assert scope.knows(1) and scope.knows(2)

    # The comma inside "carrying a comma," sits on page 2, not page 1.
    para = s.paragraphs[0].text
    at = para.index("carrying a comma") + len("carrying a comma")
    assert para[at] == ","
    assert scope.contains(2, "s1", 0, at, at + 1)
    assert not scope.contains(1, "s1", 0, at, at + 1)


def test_an_edit_on_a_straddled_paragraph_lands_on_the_marked_page():
    s = _story(
        "He paused, then went on, and the crowd waited for him to say it, "
        "and the room stayed quiet, and nobody moved at all.",
        "She watched from the door, unmoving.")
    page1 = "He paused, then went on, and the crowd waited for him to say it,"
    page2 = ("and the room stayed quiet, and nobody moved at all. "
             "She watched from the door, unmoving.")
    scope = build_page_map([s], [page1, page2])
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="quiet,", replace="quiet.", page=2)], scope=scope)
    assert outs[0].status == APPLIED
    assert "stayed quiet. and nobody" in s.paragraphs[0].text


# --- case, which a reviewer cannot see -----------------------------------------

def test_a_title_set_in_capitals_is_found_by_the_title_as_written():
    """Full capitals in a heading are a design, not spelling. The story holds
    "TO THE PILOT…"; the reviewer, reading the page, writes the title the way
    anyone writes a title. Refusing the correction over that is refusing it over
    the typesetting."""
    story = _story("TO THE PILOT OF THE BICYCLE CARRIAGE OUTSIDE BARBARELLA’S")
    edit = Edit("e1", "To the Pilot of the Bicycle Carriage Outside Barbarella’s",
                "To the Pilot of the Bicycle Carriage")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    # And the design survives: the book's capitals, the reviewer's edit.
    assert [p.text for p in story.paragraphs] == [
        "TO THE PILOT OF THE BICYCLE CARRIAGE"]


def test_case_folding_is_the_last_resort_not_a_wider_net():
    """An anchor that matches exactly somewhere must never be pulled onto a
    differently-cased twin. The fold only runs when every case-exact reading has
    already come up empty."""
    story = _story("the harbor at dawn.", "THE HARBOR AT DUSK.")
    outcomes, _ = apply_to_stories(
        [story], [Edit("e1", "the harbor at dawn.", "the harbour at dawn.")])
    assert outcomes[0].status == APPLIED
    assert [p.text for p in story.paragraphs] == ["the harbour at dawn.",
                                                  "THE HARBOR AT DUSK."]


def test_a_fold_that_finds_two_candidates_is_flagged_not_guessed():
    story = _story("THE HARBOR AT DAWN.", "The Harbor At Dawn.")
    outcomes, _ = apply_to_stories(
        [story], [Edit("e1", "the harbor at dawn.", "the harbour at dawn.")])
    assert outcomes[0].status == AMBIGUOUS
    assert outcomes[0].occurrences == 2


def test_a_reviewer_correcting_case_on_purpose_still_gets_their_case():
    """The capitals are kept only where the book's own run is entirely capital and
    the match was found by folding. An ordinary case fix is untouched by any of
    this — it matched exactly."""
    story = _story("we sailed to the harbor.")
    outcomes, _ = apply_to_stories(
        [story], [Edit("e1", "the harbor", "the Harbor")])
    assert outcomes[0].status == APPLIED
    assert [p.text for p in story.paragraphs] == ["we sailed to the Harbor."]


def test_a_mixed_case_run_is_not_recased():
    """Only an all-capital run is restored, because only that pattern is
    unambiguously a design. A heading in title case takes the edit as written."""
    story = _story("The Harbor At Dawn And Dusk")
    outcomes, _ = apply_to_stories(
        [story], [Edit("e1", "the harbor at dawn and dusk", "the harbour at dawn")])
    assert outcomes[0].status == APPLIED
    assert [p.text for p in story.paragraphs] == ["the harbour at dawn"]


def test_a_narrowing_that_leaves_one_candidate_beats_the_ordinal():
    """An ordinal counts copies on the proof page; the page map and the marked
    line run before it and have already chosen among those. Having chosen, "the
    second copy" indexes a set of one and overshoots it — which turned twenty-five
    corrections that had landed into "asked for #2 of 1" when ordinals were first
    carried. The narrowing is the better evidence and wins."""
    story = _story("She said the word once here.", "And the word again there.")
    edit = Edit("e1", "the word", "the phrase", occurrence=2,
                context="And the word again there.")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert [p.text for p in story.paragraphs] == ["She said the word once here.",
                                                  "And the phrase again there."]


def test_an_ordinal_counted_on_a_page_that_cannot_be_placed_is_refused():
    """"The second copy on page 111" is not "the second copy in the novel", and
    reading it as one would correct whichever copy happened to come second."""
    story = _story("the word here.", "the word there.")
    edit = Edit("e1", "the word", "the phrase", occurrence=2, page=111)
    outcomes, _ = apply_to_stories([story], [edit])          # no page map
    assert outcomes[0].status == AMBIGUOUS
    assert "could not be placed" in outcomes[0].detail
    assert [p.text for p in story.paragraphs] == ["the word here.",
                                                  "the word there."]


# --- what makes locating an edit fast ------------------------------------------

def test_a_paragraph_reports_the_text_it_has_after_an_edit():
    """`Paragraph.text` is cached — the corrections engine reads it once per
    paragraph per edit — so the thing to hold it to is that an edited paragraph
    reports what it now says, not what it said when the cache was filled."""
    s = _story("The cat sat on the mat.")
    para = s.paragraphs[0]
    assert para.text == "The cat sat on the mat."       # fills the cache
    para.replace(4, 7, "dog")
    assert para.text == "The dog sat on the mat."
    para.replace(0, 3, "A")
    assert para.text == "A dog sat on the mat."
    assert s.text == "A dog sat on the mat."


def test_a_paragraph_reports_its_text_after_a_run_is_split():
    """Styling part of a paragraph splits the Content nodes under it. The text is
    the same either side of that, and the cache has to say so."""
    s = _story("Read Dune tonight.")
    para = s.paragraphs[0]
    assert para.text == "Read Dune tonight."
    assert para.restyle(5, 9, {"FontStyle": "Italic"})
    assert para.text == "Read Dune tonight."
    assert len(para.nodes) > 1                          # it really did split
    para.replace(5, 9, "Emma")
    assert para.text == "Read Emma tonight."


def test_the_widest_view_gates_the_search_without_narrowing_it():
    """Locating an edit asks every paragraph in the book whether it carries the
    anchor, and the case-folded normalized view is asked first because it is the
    widest of the four readings. Anything the exact, folded or normalized tiers
    would have found has to survive that gate — these are the four, one each."""
    cache = IndexCache()
    for text, needle in (
            ("the cat sat down", "cat sat"),             # exact
            ("she said “no” twice", 'said "no"'),        # punctuation fold
            ("a conces- sion was made", "concession"),   # normalized
            ("TO THE PILOT of the carriage", "To the Pilot"),   # case fold
    ):
        assert all_spans(text, needle, cache=cache), (text, needle)
        # and the same answer whether or not a cache is in play
        assert bool(all_spans(text, needle)) is True


# --- what the apply can see and the note could not say -------------------------

def test_an_inserted_dash_closes_up_the_space_the_comma_left():
    """The house sets both dashes closed up. A note asking for an em dash where a
    comma stands does not quote the space that followed the comma, so the write
    leaves it: "Exactly— what else". Four shipped on one proof, each a correctly
    applied edit."""
    story = _story("“Exactly, what else would you be doing otherwise?”")
    edit = Edit("e1", "“Exactly, ", "“Exactly—")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].applied
    assert story.paragraphs[0].text == "“Exactly—what else would you be doing otherwise?”"


def test_a_dash_the_edit_quoted_keeps_its_own_spacing():
    story = _story("the traffic – folks on their commutes")
    edit = Edit("e1", "traffic – ", "traffic — ")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].applied
    assert story.paragraphs[0].text == "the traffic — folks on their commutes"


def test_the_sentence_comma_moves_inside_a_quotation_mark_just_added():
    """"Enclose in quotes" quotes only what it was given, so the sentence's own
    comma is left outside: `for “bon appétit”, and it was`. US practice puts it
    inside, and the mark is one character past the span, so nothing but the apply
    can see it."""
    story = _story("It was Persian for bon appétit, and it was his way.")
    edit = Edit("e1", "bon appétit", "“bon appétit”")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].applied
    assert "for “bon appétit,” and it was" in story.paragraphs[0].text


def test_doubles_replace_the_singles_they_would_otherwise_nest_inside():
    """A find that quoted the words without the marks around them printed
    ‘“What the fuck just happened?”’."""
    story = _story("‘What the fuck just happened?’ I said to myself.")
    edit = Edit("e1", "What the fuck just happened?", "“What the fuck just happened?”")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].applied
    assert story.paragraphs[0].text == "“What the fuck just happened?” I said to myself."


def test_a_rewritten_negation_does_not_leave_the_old_one_standing():
    """"it's" → "it wasn't" against "if it's not the racism" rewrites the whole
    negation; the "not" is a word further on, outside anything the note quoted."""
    story = _story("So, if it’s not the racism I faced, it was something else.")
    edit = Edit("e1", "it’s", "it wasn’t")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].applied
    assert story.paragraphs[0].text.startswith("So, if it wasn’t the racism")


def test_an_ordinal_names_the_copy_the_page_held_when_it_was_marked():
    """An ordinal counts copies on the page the reviewer read, and is read back
    against a document the corrections before it have already edited. An earlier
    "is" → "was" adds a copy the reviewer never counted, and every ordinal after it
    on the page then names the copy in front of the one it means."""
    story = _story("Anyone getting to regionals is going to be quick.",
                   "It was perfect. If there was a light drizzle, better still.")
    edits = [Edit("e1", "regionals is", "regionals was"),
             Edit("e2", "was", "were", occurrence=3)]
    outcomes, _ = apply_to_stories([story], edits)
    assert all(o.applied for o in outcomes)
    assert story.paragraphs[1].text == (
        "It was perfect. If there were a light drizzle, better still.")


def test_a_find_an_earlier_correction_changed_is_still_placed():
    """Every mark was written against the same page, but the corrections are
    applied one after another: the second mark on a line arrives to find the words
    it quoted already gone. The run this edit changes came through untouched, so
    only that run is rewritten and both corrections stand."""
    story = _story("“No, not all, besides you haven’t been in a while.”")
    edits = [Edit("e1", "not all", "not at all"),
             Edit("e2", "“No, not all, besides you", "“No, not all. Besides, you")]
    outcomes, _ = apply_to_stories([story], edits)
    assert all(o.applied for o in outcomes), [o.status for o in outcomes]
    assert story.paragraphs[0].text == (
        "“No, not at all. Besides, you haven’t been in a while.”")


def test_a_stale_find_whose_replacement_already_carries_the_earlier_edit():
    """A reviewer marking one line twice writes the second note against the line as
    it will read, so `‘the’ → “the”` arrives inside the note about the possessive.
    The replacement is written whole."""
    story = _story("folks always dropped the ‘the’ and the possessive ‘s’)")
    edits = [Edit("e1", "‘the’", "“the”"),
             Edit("e2", "‘the’ and the possessive ‘s’)",
                  "“the” and the possessive ’s)")]
    outcomes, _ = apply_to_stories([story], edits)
    assert all(o.applied for o in outcomes), [o.status for o in outcomes]
    assert story.paragraphs[0].text == (
        "folks always dropped the “the” and the possessive ’s)")


def test_a_stale_find_the_two_corrections_disagree_about_is_still_flagged():
    """Neither safe reading holds: the earlier write is inside the run this edit
    changes, and its result is nowhere in the replacement. That is two corrections
    contradicting each other, which is a person's to settle."""
    story = _story("She was hungry and tired that evening.")
    edits = [Edit("e1", "hungry", "starving"),
             Edit("e2", "was hungry and tired", "was famished and tired")]
    outcomes, _ = apply_to_stories([story], edits)
    assert outcomes[0].applied and not outcomes[1].applied
    assert story.paragraphs[0].text == "She was starving and tired that evening."


def test_a_stale_find_is_rewritten_only_where_it_actually_changes():
    """The earlier write is in the words around the run this edit changes, so
    neither correction undoes the other."""
    story = _story("She was hungry and tired that evening.")
    edits = [Edit("e1", "hungry", "starving"),
             Edit("e2", "was hungry and tired", "was hungry and cold")]
    outcomes, _ = apply_to_stories([story], edits)
    assert all(o.applied for o in outcomes)
    assert story.paragraphs[0].text == "She was starving and cold that evening."


def test_a_pdf_artifact_hyphen_or_space_still_matches():
    """An anchor quoted from the proof can carry a mark the book does not set — a
    soft or non-breaking hyphen at a line end, a narrow or zero-width space around
    a comma, a horizontal-bar dash for an em dash. The normalized view drops every
    one of them, so a mark whose text occurs once still lands instead of being
    reported "not found"."""
    from docproof.corrections.textmatch import normalize
    assert normalize("t‑shirt") == normalize("t-shirt")         # NB hyphen
    assert normalize("tour­nament") == normalize("tournament")  # soft hyphen
    assert normalize("rumors―it") == normalize("rumors—it")  # horiz bar
    assert normalize("Maryam, and") == normalize("Maryam, and")   # narrow nbsp
    assert normalize("wo​rd") == normalize("word")              # zero-width


def test_a_soft_hyphen_anchor_lands_on_the_book_word():
    """End to end: a find carrying a soft hyphen the proof put at a line break is
    located in a book that sets the word whole, and the edit applies."""
    story = _story("In my hand was the plaque for the Tournament.")
    edits = [Edit("e1", "Tour­nament", "tournament")]
    outcomes, _ = apply_to_stories([story], edits)
    assert all(o.applied for o in outcomes)
    assert story.paragraphs[0].text == \
        "In my hand was the plaque for the tournament."
