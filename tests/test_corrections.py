"""The corrections engine's deterministic core: anchor an edit list to an IDML,
apply it, and verify the result against the list.

Everything here runs on tests/fixtures/layout.idml — a real InDesign-exported
IDML with a multi-run paragraph, Br-delimited paragraphs, a table and a
footnote — so the surgery is tested on the shapes InDesign actually writes, not
a tidied-up subset.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import lxml.etree as ET
import pytest

from docproof.corrections.apply import all_occurrences, apply_edits
from docproof.corrections.idml import parse_story, read_stories
from docproof.corrections.model import (AMBIGUOUS, APPLIED, APPLIED_EXACTLY,
                                        CROSSES_PARAGRAPH, DESIGN, DEVIATES,
                                        Edit, MISSING, NOT_FOUND,
                                        ROUTED_TO_DESIGN)
from docproof.corrections.verify import verify

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"


def story_text(idml: Path, story_id: str = "ue0") -> list[str]:
    with zipfile.ZipFile(idml) as z:
        s = parse_story(z.read(f"Stories/Story_{story_id}.xml"), story_id)
    return [p.text for p in s.paragraphs]


# --- reading ------------------------------------------------------------------

def test_the_story_is_read_into_clean_paragraphs():
    paras = story_text(LAYOUT)
    assert paras[1] == "It was late, we were tired and the road went on forever."
    assert paras[4] == "Their were several mistakes here to find."
    # The footnote is its own paragraph, read once — not folded into the body,
    # not counted twice.
    assert paras[5] == "A footnote with its own sentence, it also runs on."


def test_a_table_is_read_cell_by_cell():
    assert story_text(LAYOUT, "uff")[0] == "First cell has a sentence in it, it runs on."


# --- applying -----------------------------------------------------------------

def test_a_simple_edit_lands(tmp_path):
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="Their were", replace="There were")])
    assert report.applied == 1
    assert report.outcomes[0].status == APPLIED
    assert story_text(out)[4] == "There were several mistakes here to find."
    # Every other paragraph is untouched.
    assert story_text(out)[1] == story_text(LAYOUT)[1]


def test_an_edit_that_spans_several_runs_lands(tmp_path):
    """"was late, we were tired" is five Content nodes (a character style on
    'late,' and a point size on 'tired'); the replacement collapses onto the
    first and the paragraph still reads right."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="was late, we were tired",
             replace="was exhausted")])
    assert report.applied == 1
    assert story_text(out)[1] == "It was exhausted and the road went on forever."


def test_a_deletion_removes_the_span(tmp_path):
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, [Edit(id="e1", find=" for good measure", replace="")])
    assert story_text(out)[3] == "A third paragraph with plain text."


def test_a_find_that_is_not_there_is_flagged_not_guessed(tmp_path):
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="the harbor lights", replace="the harbour lights")])
    assert report.applied == 0
    assert report.outcomes[0].status == NOT_FOUND
    assert report.outcomes[0].needs_human


def test_an_ambiguous_find_is_flagged_when_no_occurrence_is_given(tmp_path):
    """"runs on" is in the table's first cell and in the footnote — two places.
    With no occurrence chosen, the engine refuses rather than pick one."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="runs on", replace="rambles on")])
    assert report.outcomes[0].status == AMBIGUOUS
    assert report.outcomes[0].occurrences == 2


def test_an_occurrence_number_picks_one_of_several(tmp_path):
    out = tmp_path / "out.idml"
    # Stories sort by id: ue0 (the body, holding the footnote) before uff (the
    # table), so occurrence 1 is the footnote's "runs on".
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="runs on", replace="rambles on", occurrence=1)])
    assert report.applied == 1
    assert "rambles on" in story_text(out)[5]              # footnote changed
    assert story_text(out, "uff")[0].endswith("it runs on.")  # table left alone


def test_a_span_across_a_paragraph_break_is_refused(tmp_path):
    """"forever. She" reaches from one paragraph into the next. Allowing it
    would let a replacement swallow the paragraph mark — the exact merge failure
    the hand-written scripts produced. It is refused."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="forever. She opened", replace="forever, and she opened")])
    assert report.outcomes[0].status == CROSSES_PARAGRAPH
    assert report.applied == 0


def test_a_design_request_is_routed_not_applied(tmp_path):
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="Chapter One", replace="", kind=DESIGN,
             instruction="move this chapter to a right-hand page")])
    assert report.outcomes[0].status == ROUTED_TO_DESIGN
    assert story_text(out)[0] == "Chapter One"        # untouched


def test_two_edits_in_one_paragraph_both_land(tmp_path):
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="Their", replace="There"),
        Edit(id="e2", find="mistakes here", replace="errors here")])
    assert report.applied == 2
    assert story_text(out)[4] == "There were several errors here to find."


def test_the_source_file_is_never_touched(tmp_path):
    before = story_text(LAYOUT)
    apply_edits(LAYOUT, tmp_path / "out.idml", [
        Edit(id="e1", find="Their were", replace="There were")])
    assert story_text(LAYOUT) == before


# --- folding ------------------------------------------------------------------

def test_folding_matches_straight_quotes_against_curly():
    curly = "she said “yes,” and left"
    assert all_occurrences(curly, '"yes,"') == [9]          # straight find, curly text
    assert all_occurrences("a—b—c", "a-b") == [0]           # em dash vs hyphen


# --- verifying ----------------------------------------------------------------

def test_a_clean_apply_verifies_clean(tmp_path):
    edits = [Edit(id="e1", find="Their were", replace="There were")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    report = verify(LAYOUT, out, edits)
    assert report.clean
    assert not report.discrepancies
    assert all(r.status == APPLIED_EXACTLY for r in report.reconciliations)


def test_verification_catches_an_unrequested_change(tmp_path):
    """The after file has a change nobody asked for — a stray edit a hand-run
    script might make. The verifier surfaces it even though it is not in the
    edit list."""
    edits = [Edit(id="e1", find="Their were", replace="There were")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    # Sneak in an extra change the edit list does not account for.
    tampered = tmp_path / "tampered.idml"
    apply_edits(out, tampered, [
        Edit(id="x", find="third paragraph", replace="THIRD paragraph")])

    report = verify(LAYOUT, tampered, edits)
    assert not report.clean
    assert any("THIRD paragraph" in d.after for d in report.discrepancies)


def test_verification_reports_a_missing_edit(tmp_path):
    """An edit that never anchored is MISSING — it was asked for and is not in
    the document."""
    edits = [Edit(id="e1", find="Their were", replace="There were"),
             Edit(id="e2", find="nowhere to be found", replace="x")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    report = verify(LAYOUT, out, edits)
    by_id = {r.edit.id: r for r in report.reconciliations}
    assert by_id["e1"].status == APPLIED_EXACTLY
    assert by_id["e2"].status == MISSING


def test_verification_flags_a_paragraph_that_was_merged(tmp_path):
    """A paragraph removed between before and after is a structure change — the
    deleted-line / merged-paragraph alarm."""
    edits = [Edit(id="e1", find="Their were", replace="There were")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    # Delete a paragraph's Br in the after file so two paragraphs merge.
    merged = tmp_path / "merged.idml"
    with zipfile.ZipFile(out) as z:
        data = {n: z.read(n) for n in z.namelist()}
    root = ET.fromstring(data["Stories/Story_ue0.xml"])
    # Remove a Br *within* the Normal range (the one after "forever.") so two
    # of its paragraphs merge — the first Br sits between two different ranges,
    # whose boundary would still split them.
    inner_br = list(root.iter("Br"))[1]
    inner_br.getparent().remove(inner_br)
    data["Stories/Story_ue0.xml"] = ET.tostring(root, xml_declaration=True,
                                                encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(merged, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in data.items():
            if n == "mimetype":
                z.writestr(n, d, compress_type=zipfile.ZIP_STORED)
            else:
                z.writestr(n, d)

    report = verify(LAYOUT, merged, edits)
    assert report.structure_changed
    assert not report.clean


# --- the package stays valid --------------------------------------------------

def test_the_written_idml_is_a_valid_package(tmp_path):
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, [Edit(id="e1", find="Their were", replace="There were")])
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        for n in names:
            if n.endswith(".xml"):
                ET.fromstring(z.read(n))              # every part still parses
