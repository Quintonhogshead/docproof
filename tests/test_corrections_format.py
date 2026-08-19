"""Italics as a correction, not a note to a designer.

"Italicize movie title" was reaching `routed_to_design` on every proof and being
applied by nobody. It is not a layout request: the italics of a film title are part
of the text as much as its spelling, and an IDML carries them as `FontStyle` on the
`CharacterStyleRange` around the words. So the mark is appliable, and these tests
say so against the real InDesign fixture — where one character range holds
`Content Br Content Br Content Br Content`, four paragraphs' worth, so splitting it
has to leave every break exactly where it was.
"""
from __future__ import annotations

import zipfile

import lxml.etree as ET
import pytest

from docproof.corrections.apply import apply_edits, apply_to_stories
from docproof.corrections.idml import parse_story, read_stories
from docproof.corrections.model import (APPLIED, Edit, OVERLAPS,
                                        ROUTED_TO_DESIGN, UNSTYLEABLE)

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"


def _story(*runs: tuple[str, str], story_id: str = "s1"):
    """A one-paragraph story from (text, character-style) runs."""
    body = "".join(
        f'<CharacterStyleRange AppliedCharacterStyle="{style}">'
        f"<Content>{text}</Content></CharacterStyleRange>"
        for text, style in runs)
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>' + body
           + "</ParagraphStyleRange></Story>")
    return parse_story(xml.encode("utf-8"), story_id)


def styled_runs(story) -> list[tuple[str, str | None]]:
    """Every (text, FontStyle) run of the story, in order — what a reader of the
    file would see as italic and what they would not."""
    out = []
    for csr in story.root.iter("CharacterStyleRange"):
        style = csr.get("FontStyle")
        for content in csr:
            if content.tag == "Content":
                out.append((content.text or "", style))
    return out


# --- the surgery --------------------------------------------------------------

def test_a_whole_run_is_styled_without_splitting_anything():
    s = _story(("It was ", "None"), ("late", "None"), (" and dark.", "None"))
    assert s.paragraphs[0].restyle(7, 11, {"FontStyle": "Italic"})
    assert styled_runs(s) == [("It was ", None), ("late", "Italic"),
                              (" and dark.", None)]
    assert s.paragraphs[0].text == "It was late and dark."


def test_part_of_a_run_is_split_out_and_styled():
    s = _story(("She read Fiddler on the Roof twice.", "None"))
    para = s.paragraphs[0]
    start = para.text.index("Fiddler on the Roof")
    assert para.restyle(start, start + len("Fiddler on the Roof"),
                        {"FontStyle": "Italic"})
    assert styled_runs(s) == [("She read ", None),
                              ("Fiddler on the Roof", "Italic"),
                              (" twice.", None)]
    assert para.text == "She read Fiddler on the Roof twice."


def test_styling_carries_the_range_style_into_every_piece():
    """A split range's pieces keep what the original had applied — otherwise the
    words either side of the italics silently lose their character style."""
    s = _story(("a quiet word here", "CharacterStyle/Emph"))
    para = s.paragraphs[0]
    at = para.text.index("word")
    assert para.restyle(at, at + 4, {"FontStyle": "Italic"})
    styles = [csr.get("AppliedCharacterStyle")
              for csr in s.root.iter("CharacterStyleRange")]
    assert styles == ["CharacterStyle/Emph"] * 3


def test_styling_copies_the_range_properties_into_every_piece():
    """`Properties` holds the applied font. A piece without it renders in the wrong
    face, which is the kind of damage a corrections pass must not do."""
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>'
           '<CharacterStyleRange AppliedCharacterStyle="None">'
           '<Properties><AppliedFont type="string">Minion Pro</AppliedFont>'
           "</Properties>"
           "<Content>the quick brown fox</Content>"
           "</CharacterStyleRange></ParagraphStyleRange></Story>")
    s = parse_story(xml.encode("utf-8"), "s1")
    assert s.paragraphs[0].restyle(4, 9, {"FontStyle": "Italic"})
    fonts = [csr.findtext("Properties/AppliedFont")
             for csr in s.root.iter("CharacterStyleRange")]
    assert fonts == ["Minion Pro"] * 3


def test_de_italicizing_clears_the_override_rather_than_adding_one():
    s = _story(("a ", "None"), ("borrowed", "None"), (" phrase", "None"))
    for csr in s.root.iter("CharacterStyleRange"):
        csr.set("FontStyle", "Italic")
    assert s.paragraphs[0].restyle(2, 10, {"FontStyle": None})
    assert styled_runs(s) == [("a ", "Italic"), ("borrowed", None),
                              (" phrase", "Italic")]


def test_a_span_crossing_two_ranges_styles_both():
    s = _story(("Come Away ", "None"), ("With Me", "CharacterStyle/Emph"))
    assert s.paragraphs[0].restyle(0, 17, {"FontStyle": "Italic"})
    assert styled_runs(s) == [("Come Away ", "Italic"), ("With Me", "Italic")]


# --- the real fixture, where one range spans four paragraphs -------------------

def test_styling_a_paragraph_inside_a_shared_range_moves_no_break():
    """The fixture's fourth character range holds four paragraphs' text with `Br`
    elements between them. Splitting it around a span in the middle paragraph must
    leave every paragraph exactly where it was."""
    before = read_stories(LAYOUT)
    story = next(s for s in before if s.story_id == "ue0")
    texts_before = [p.text for p in story.paragraphs]
    para = story.paragraphs[2]                 # "She opened the door, …"
    at = para.text.index("door")
    assert para.restyle(at, at + 4, {"FontStyle": "Italic"})

    reparsed = parse_story(story.serialize(), "ue0")
    assert [p.text for p in reparsed.paragraphs] == texts_before
    assert ("door", "Italic") in styled_runs(reparsed)


def test_only_the_named_words_come_back_italic():
    story = next(s for s in read_stories(LAYOUT) if s.story_id == "ue0")
    para = story.paragraphs[2]
    at = para.text.index("door")
    para.restyle(at, at + 4, {"FontStyle": "Italic"})
    italic = [t for t, style in styled_runs(parse_story(story.serialize(), "ue0"))
              if style == "Italic"]
    assert italic == ["door"]


# --- as an edit ---------------------------------------------------------------

def test_an_italic_edit_applies_and_changes_no_text():
    s = _story(("He hummed American Pie all evening.", "None"))
    outs, changed = apply_to_stories([s], [
        Edit(id="e1", find="American Pie", replace="American Pie",
             format="italic", instruction="Italicize album title")])
    assert outs[0].status == APPLIED
    assert changed == {"s1"}
    assert s.paragraphs[0].text == "He hummed American Pie all evening."
    assert ("American Pie", "Italic") in styled_runs(s)


def test_an_italic_edit_is_not_swallowed_as_a_no_op():
    """`find == replace` on a formatting edit is the normal case, not nothing to do
    — the old shortcut would have filed it under "no change needed" where no one
    would ever see it."""
    s = _story(("He hummed American Pie.", "None"))
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="American Pie", replace="American Pie",
             format="italic")])
    assert outs[0].status == APPLIED


def test_an_italic_edit_is_located_and_refused_like_any_other():
    """It goes through the same anchoring, so an anchor that is not in the book is
    flagged rather than guessed — and a repeated one narrows by page."""
    s = _story(("A said it, then B said it, then C said it.", "None"))
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="Fiddler on the Roof", replace="Fiddler on the Roof",
             format="italic"),
        Edit(id="e2", find="said it", replace="said it", format="italic")])
    assert outs[0].status == "not_found"
    assert outs[1].status == "ambiguous" and outs[1].occurrences == 3


def test_a_span_no_character_range_holds_is_flagged_not_forced():
    """Text inside a nested container the splitter cannot take apart is refused with
    a reason, rather than styled by rewriting more of the story than a correction
    should."""
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>'
           '<CharacterStyleRange AppliedCharacterStyle="None">'
           "<HyperlinkTextSource><Content>a linked phrase</Content>"
           "</HyperlinkTextSource></CharacterStyleRange>"
           "</ParagraphStyleRange></Story>")
    s = parse_story(xml.encode("utf-8"), "s1")
    outs, changed = apply_to_stories([s], [
        Edit(id="e1", find="linked", replace="linked", format="italic")])
    assert outs[0].status == UNSTYLEABLE
    assert changed == set()
    assert "character range" in outs[0].detail


def test_a_real_design_request_is_still_routed_to_a_designer():
    """Formatting became appliable; moving a chapter did not."""
    s = _story(("Chapter One", "None"))
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="Chapter One", replace="", kind="design",
             instruction="Start this chapter on a recto")])
    assert outs[0].status == ROUTED_TO_DESIGN


def test_an_italicised_idml_still_opens_as_a_package(tmp_path):
    """The written file is a whole IDML with the story swapped in, and reparsing it
    gives back the same text with the italics in place."""
    out = tmp_path / "italic.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="several", replace="several", format="italic",
             instruction="Italicize")])
    assert report.applied == 1
    with zipfile.ZipFile(out) as z:
        assert "mimetype" in z.namelist()
        story = parse_story(z.read("Stories/Story_ue0.xml"), "ue0")
    assert story.paragraphs[4].text == "Their were several mistakes here to find."
    assert ("several", "Italic") in styled_runs(story)


def test_a_format_edit_styles_words_another_correction_already_changed():
    """The overlap guard exists to stop one write garbling another's words. A
    formatting edit writes no words, so it is exempt: a reviewer who both fixes a
    quotation mark in a song title and asks for the title to be de-italicized has
    asked for both, and refusing the second because the first landed was losing
    real work over a collision that cannot happen."""
    s = _story(('He played “Come Away With Me" twice.', "None"))
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find='Away With Me"', replace="Away With Me”",
             instruction="Close the quote"),
        Edit(id="e2", find="Come Away With Me", replace="Come Away With Me",
             format="roman", instruction="De-italicize")])
    assert [o.status for o in outs] == [APPLIED, APPLIED]
    # The styling landed on the corrected text, not the text as it was quoted.
    assert s.paragraphs[0].text == "He played “Come Away With Me” twice."


def test_a_text_edit_over_a_restyled_span_is_still_refused():
    """The exemption runs one way only. A rewrite over words just restyled would
    take the styling out with it, so that collision is still flagged."""
    s = _story(("He hummed American Pie all evening.", "None"))
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="American Pie", replace="American Pie",
             format="italic"),
        Edit(id="e2", find="American Pie", replace="American Pie (1971)")])
    assert outs[0].status == APPLIED
    assert outs[1].status == OVERLAPS
    assert outs[1].collides_with == ("e1",)
