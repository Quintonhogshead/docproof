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
from docproof.corrections.model import AMBIGUOUS, APPLIED, Edit
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
