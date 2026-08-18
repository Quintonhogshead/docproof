"""Layout requests a story can actually carry, and the ones only InDesign can settle.

There is a line running through "design changes". `Stories/*.xml` holds text and the
styles applied to it; `Spreads/*.xml` holds geometry. Quite a lot of what reads as
layout lives on the story side — where a chapter starts, whether a heading may be
stranded, whether a paragraph splits — because those are properties of the paragraph.
Those are applied here, and located and refused exactly as a word swap is.

The rest is composition: whether a line broke well, whether a page runs long. That is
InDesign's answer, so this engine neither applies it nor claims it — it hands over a
located check. Both halves are tested, and so is the seam: the verifier had to learn
that a paragraph the corrections deliberately removed is not the paragraph merge a
hand-run script produces.
"""
from __future__ import annotations

import json

import pytest

from docproof.corrections.apply import apply_to_stories
from docproof.corrections.idml import parse_story, read_stories
from docproof.corrections.instructions import resolve
from docproof.corrections.model import (APPLIED, DESIGN, Edit, NOT_FOUND,
                                        ROUTED_TO_DESIGN, UNPLACEABLE)
from docproof.corrections.parse import parse_edits
from docproof.corrections.run import apply_corrections

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"


def _story(*paragraphs: str, story_id: str = "s1", style="ParagraphStyle/Body"):
    body = "".join(
        f'<ParagraphStyleRange AppliedParagraphStyle="{style}">'
        f'<CharacterStyleRange AppliedCharacterStyle="None"><Content>{p}</Content>'
        f"<Br /></CharacterStyleRange></ParagraphStyleRange>" for p in paragraphs)
    return parse_story(('<?xml version="1.0"?><Story>' + body
                        + "</Story>").encode("utf-8"), story_id)


def _ue0():
    return next(s for s in read_stories(LAYOUT) if s.story_id == "ue0")


def ranges(story):
    """(StartParagraph, KeepWithNext, style) per paragraph range, after a round trip."""
    back = parse_story(story.serialize(), story.story_id)
    return [(e.get("StartParagraph"), e.get("KeepWithNext"),
             e.get("AppliedParagraphStyle", "").split("/")[-1])
            for e in back.root.iter("ParagraphStyleRange")]


def texts(story):
    return [p.text for p in parse_story(story.serialize(), story.story_id).paragraphs]


# --- isolating one paragraph out of a shared range ----------------------------

def test_a_paragraph_is_given_a_range_of_its_own_without_moving_a_break():
    """The fixture's fourth range holds four paragraphs. Setting a property on one
    of them means splitting that range first, and the split must leave every
    paragraph and every break exactly where it was."""
    story = _ue0()
    before = texts(story)
    assert story.isolate(2) is not None
    assert texts(story) == before
    assert len(story.paragraphs) == len(before)


def test_isolating_leaves_the_paragraph_index_meaning_the_same_thing():
    story = _ue0()
    target = story.paragraphs[3].text
    story.isolate(3)
    assert story.paragraphs[3].text == target


def test_isolating_keeps_every_applied_character_style():
    """The first paragraph runs across five character ranges, one of them italic and
    one at a different size. Splitting the paragraph range must not flatten them."""
    story = _ue0()
    before = story.serialize().decode("utf-8")
    assert "CharacterStyle/Emph" in before and 'PointSize="9"' in before
    story.isolate(1)
    after = story.serialize().decode("utf-8")
    assert "CharacterStyle/Emph" in after and 'PointSize="9"' in after


# --- the paragraph operations -------------------------------------------------

@pytest.mark.parametrize("op, attr, value", [
    ("recto", "StartParagraph", "NextOddPage"),
    ("verso", "StartParagraph", "NextEvenPage"),
    ("page-break", "StartParagraph", "NextPage"),
    ("keep-with-next", "KeepWithNext", "1"),
    ("keep-together", "KeepAllLinesTogether", "true"),
])
def test_a_paragraph_operation_lands_on_that_paragraph_alone(op, attr, value):
    story = _ue0()
    outs, changed = apply_to_stories([story], [
        Edit(id="e1", find="She opened the door", replace="She opened the door",
             paragraph=op)])
    assert outs[0].status == APPLIED and changed == {"ue0"}
    back = parse_story(story.serialize(), "ue0")
    carrying = [e for e in back.root.iter("ParagraphStyleRange")
                if e.get(attr) == value]
    assert len(carrying) == 1
    assert "She opened the door" in "".join(carrying[0].itertext())


def test_a_forced_break_can_be_cleared_as_well_as_set():
    story = _ue0()
    apply_to_stories([story], [Edit(id="e1", find="She opened", replace="She opened",
                                    paragraph="recto")])
    assert any(sp == "NextOddPage" for sp, _, _ in ranges(story))
    apply_to_stories([story], [Edit(id="e2", find="She opened", replace="She opened",
                                    paragraph="no-page-break")])
    assert all(sp is None for sp, _, _ in ranges(story))


def test_a_paragraph_style_can_be_reassigned():
    story = _ue0()
    outs, _ = apply_to_stories([story], [
        Edit(id="e1", find="A third paragraph", replace="A third paragraph",
             paragraph_style="ParagraphStyle/Block Quote")])
    assert outs[0].status == APPLIED
    styles = [s for _, _, s in ranges(story)]
    assert styles.count("Block Quote") == 1


def test_a_layout_request_is_located_and_refused_like_any_other_edit():
    story = _ue0()
    outs, _ = apply_to_stories([story], [
        Edit(id="e1", find="a paragraph that is not in this book",
             replace="x", paragraph="recto")])
    assert outs[0].status == NOT_FOUND


def test_a_paragraph_no_range_can_hold_alone_is_flagged_not_forced():
    """Flow text sitting directly under a paragraph range cannot be split off, so
    the request would apply to its neighbours. Refused with a reason."""
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>'
           "<Content>bare text with no character range</Content>"
           "</ParagraphStyleRange></Story>")
    story = parse_story(xml.encode("utf-8"), "s1")
    outs, changed = apply_to_stories([story], [
        Edit(id="e1", find="bare text", replace="bare text", paragraph="recto")])
    assert outs[0].status == UNPLACEABLE and changed == set()


# --- structural: paragraphs added and removed ---------------------------------

def test_a_paragraph_can_be_removed_whole():
    story = _story("First line.", "A stray line.", "Third line.")
    outs, _ = apply_to_stories([story], [
        Edit(id="e1", find="A stray line.", replace="", paragraph="delete-paragraph")])
    assert outs[0].status == APPLIED
    assert texts(story) == ["First line.", "Third line."]


def test_a_paragraph_can_be_put_back():
    """The Shams run lost an Acknowledgments page. Restoring copy is paragraphs and
    a break, not layout."""
    story = _story("Chapter One", "It began badly.")
    outs, _ = apply_to_stories([story], [
        Edit(id="e1", find="It began badly.", replace="For my mother.",
             paragraph="insert-before",
             paragraph_style="ParagraphStyle/Dedication")])
    assert outs[0].status == APPLIED
    assert texts(story) == ["Chapter One", "For my mother.", "It began badly."]
    assert [s for _, _, s in ranges(story)][1] == "Dedication"


def test_structural_edits_run_last_so_they_cannot_shift_another_edit():
    """Inserting a paragraph renumbers every one after it. A word swap listed after
    the insertion must still land on the word it named, not on whatever moved into
    that index."""
    story = _story("First line.", "Their were mistakes.", "Third line.")
    outs, _ = apply_to_stories([story], [
        Edit(id="e1", find="First line.", replace="A new opening.",
             paragraph="insert-before"),
        Edit(id="e2", find="Their were", replace="There were")])
    assert [o.status for o in outs] == [APPLIED, APPLIED]
    assert texts(story) == ["A new opening.", "First line.",
                            "There were mistakes.", "Third line."]


# --- the verifier had to learn the difference ---------------------------------

def test_an_intended_deletion_is_not_read_as_a_paragraph_merge(tmp_path):
    """A count change the corrections asked for is in the expectation too, so the
    two agree. The alarm still exists — it just means what it says now."""
    out = apply_corrections(LAYOUT, json.dumps([
        {"find": "A third paragraph with plain text for good measure.",
         "replace": "", "paragraph": "delete-paragraph",
         "instruction": "Cut this line"}]), tmp_path)
    assert out.apply.applied == 1
    assert out.verify.structure_intended is True
    assert out.verify.structure_changed is False
    assert out.verify.paragraphs_after == out.verify.paragraphs_before - 1
    assert out.verify.clean


def test_an_unasked_for_structure_change_is_still_an_alarm():
    from docproof.corrections.verify import verify
    report = verify(LAYOUT, LAYOUT, [
        Edit(id="e1", find="A third paragraph with plain text for good measure.",
             replace="", paragraph="delete-paragraph")])
    # The "after" file still has the paragraph the list said to remove.
    assert report.structure_changed is True
    assert not report.clean


# --- tier 2: what only InDesign can settle ------------------------------------

@pytest.mark.parametrize("note", [
    "Designer: bad break here",
    "Widow on this page",
    "Loose rag through here",
    "This page runs long",
    "Too much space above this",
])
def test_a_composition_note_becomes_a_check_not_an_edit(note):
    got = resolve(note, "the line it was marked on")
    assert got is not None
    assert got.kind == DESIGN
    assert got.find == got.replace          # nothing is changed


def test_a_composition_note_that_can_be_applied_is_applied_instead():
    """"hyphenate after Lime" is the typesetter's own fix and it is text, so it does
    not wait for a person. The check list is for what genuinely cannot be done."""
    got = resolve('Designer: bad break -- hyphenate after "Lime"',
                  "his Limewire downloads")
    assert got is not None
    assert got.kind != DESIGN
    assert got.replace == "Lime­wire"   # a discretionary hyphen


def test_a_design_note_is_located_so_the_check_can_be_found(tmp_path):
    out = apply_corrections(LAYOUT, json.dumps([
        {"find": "She opened the door", "replace": "She opened the door",
         "kind": "design", "instruction": "Widow on this page", "page": 12}]),
        tmp_path)
    assert out.apply.applied == 0
    check = out.checks[0]
    assert check.page == 12
    assert check.story_id == "ue0" and check.paragraph == 2
    assert "Widow" in check.why


def test_an_applied_layout_op_says_what_to_confirm(tmp_path):
    """The file now says "start on a right-hand page". Whether that left the page
    before it half empty is a question about the composed book, so it is handed over
    rather than claimed."""
    out = apply_corrections(LAYOUT, json.dumps([
        {"find": "She opened the door", "replace": "She opened the door",
         "paragraph": "recto", "instruction": "Start this on a recto",
         "source": "p31-1"}]), tmp_path)
    assert out.apply.applied == 1
    assert out.to_check == 1
    check = out.checks[0]
    assert check.page == 31                 # off the comment id
    assert "right-hand page" in check.what
    assert "run short" in check.what


def test_a_plain_word_swap_does_not_fill_the_check_list(tmp_path):
    """The list is only worth reading if everything on it needs InDesign open."""
    out = apply_corrections(LAYOUT, json.dumps([
        {"find": "Their were", "replace": "There were"}]), tmp_path)
    assert out.apply.applied == 1
    assert out.to_check == 0


def test_the_checks_reach_the_report(tmp_path):
    out = apply_corrections(LAYOUT, json.dumps([
        {"find": "She opened the door", "replace": "She opened the door",
         "paragraph": "recto", "instruction": "Start this on a recto"}]), tmp_path)
    payload = json.loads(out.report_json.read_text(encoding="utf-8"))
    assert len(payload["checks"]) == 1
    assert "To check in InDesign" in out.report_md.read_text(encoding="utf-8")


# --- parsing ------------------------------------------------------------------

def test_an_unknown_paragraph_operation_is_refused_by_name():
    result = parse_edits([{"find": "something here", "replace": "something here",
                           "paragraph": "make-it-pretty"}])
    assert not result.edits
    assert "unknown paragraph operation" in result.issues[0].reason


def test_a_layout_edit_survives_the_review_round_trip():
    rows = [{"find": "Chapter Nine", "replace": "Chapter Nine",
             "paragraph": "recto", "paragraph_style": "ParagraphStyle/Chapter Head"}]
    edit = parse_edits(rows).edits[0]
    assert edit.paragraph == "recto"
    assert edit.paragraph_style == "ParagraphStyle/Chapter Head"
    assert edit.is_layout and not edit.is_structural
