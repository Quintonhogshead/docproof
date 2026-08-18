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

from docproof.corrections.apply import (all_occurrences, apply_edits,
                                        apply_to_stories, indefinite_article)
from docproof.corrections.idml import parse_story, read_stories
from docproof.corrections.model import (AMBIGUOUS, APPLIED, APPLIED_EXACTLY,
                                        CROSSES_PARAGRAPH, DESIGN, DEVIATES,
                                        DISP_APPLIED, DISP_NOT_EXTRACTED, Edit,
                                        JUDGMENT, MECHANICAL, MISSING, NOT_FOUND,
                                        OVERLAPS, ReviewChange, ROUTED_TO_DESIGN,
                                        WITHHELD)
from docproof.corrections.parse import ParseIssue, parse_edits
from docproof.corrections.run import (apply_corrections, corrected_name,
                                      verify_corrections)
from docproof.corrections.verify import verify

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"


def _one_para_story(text: str, story_id: str = "s1"):
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>'
           '<CharacterStyleRange><Content>' + text
           + '</Content></CharacterStyleRange></ParagraphStyleRange></Story>')
    return parse_story(xml.encode("utf-8"), story_id)


# --- grammar guards -----------------------------------------------------------

@pytest.mark.parametrize("word, article", [
    ("immense", "an"), ("enormous", "an"), ("old", "an"), ("apple", "an"),
    ("hour", "an"), ("honest", "an"), ("house", "a"), ("dragon", "a"),
    ("one", "a"), ("once", "a"), ("onion", "an"),
    ("university", None), ("umbrella", None), ("euphoric", None),
])
def test_indefinite_article_decides_or_declines(word, article):
    assert indefinite_article(word) == article


def test_a_word_swap_fixes_the_indefinite_article():
    """Changing the word after "a"/"an" fixes the article to agree with the new
    word's sound — the "a immense" a blind find/replace would leave behind."""
    s = _one_para_story("She saw a huge banner and an ancient gate.")
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="huge", replace="immense"),
        Edit(id="e2", find="ancient", replace="rusted")])
    assert [o.status for o in outs] == [APPLIED, APPLIED]
    assert s.text == "She saw an immense banner and a rusted gate."


def test_a_sentence_initial_article_keeps_its_capital():
    s = _one_para_story("A huge mistake was made.")
    apply_to_stories([s], [Edit(id="e1", find="huge", replace="obvious")])
    assert s.text == "An obvious mistake was made."


def test_an_ambiguous_following_word_leaves_the_article_alone():
    """"a university" must not become "an university" — a "u" word is undecidable
    from spelling, so the article is left as the reviewer had it."""
    s = _one_para_story("It was a good university.")
    apply_to_stories([s], [Edit(id="e1", find="good", replace="")])
    assert s.text == "It was a  university."       # article untouched


def test_two_edits_on_overlapping_spans_flag_the_second():
    """A second correction whose span collides with one already applied to the
    paragraph is flagged, not applied blindly on top of it."""
    s = _one_para_story("abcabc def")
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="abc", occurrence=1, replace="Z"),
        Edit(id="e2", find="Zab", replace="Q")])
    assert [o.status for o in outs] == [APPLIED, OVERLAPS]
    assert s.text == "Zabc def"                    # the second never landed
    assert any(o.needs_human for o in outs)


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


def test_a_context_anchor_pins_a_repeated_find_to_one_place(tmp_path):
    """"runs on" is in the footnote and in the table. A context anchor — the
    line the correction sat on — lands it on the footnote and leaves the table
    alone, without needing to count occurrences."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="runs on", replace="rambles on",
             context="it also runs on")])
    assert report.applied == 1
    assert "rambles on" in story_text(out)[5]                 # footnote changed
    assert story_text(out, "uff")[0].endswith("it runs on.")  # table untouched


def test_a_context_that_does_not_contain_the_find_is_flagged(tmp_path):
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="zebra", replace="x", context="it also runs on")])
    assert report.applied == 0
    assert report.outcomes[0].status == NOT_FOUND
    assert "not inside" in report.outcomes[0].detail


def test_a_repeated_find_whose_context_is_missing_is_ambiguous_not_lost(tmp_path):
    """The context that would have chosen between the two "runs on" is not in the
    book, so the engine cannot pick — but the find itself is there, so this is a
    choice for a human (ambiguous), not a correction silently lost to "the context
    was not found". Recovering these was the point of making context a soft anchor."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="runs on", replace="x",
             context="nowhere near this book at all")])
    assert report.applied == 0
    assert report.outcomes[0].status == AMBIGUOUS
    assert report.outcomes[0].occurrences == 2
    assert report.outcomes[0].needs_human


def test_a_unique_find_lands_when_its_context_does_not_match(tmp_path):
    """The headline fix: a context anchor only *chooses* among repeated copies of a
    find. When the marked line does not match the book verbatim — an extraction
    artifact in the longer context run — a find that is unique still lands, rather
    than being dropped with "the context was not found" as it was before."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="Their were", replace="There were",
             context="a line that is not in the book verbatim")])
    assert report.applied == 1
    assert story_text(out)[4] == "There were several mistakes here to find."


def test_a_missing_context_still_honours_an_occurrence_number(tmp_path):
    """The context failing does not throw away a perfectly good disambiguator: if
    the correction also named which occurrence, that number still picks among the
    copies of a repeated find."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="runs on", replace="rambles on", occurrence=1,
             context="a context that will not be found")])
    assert report.applied == 1
    assert "rambles on" in story_text(out)[5]                 # occurrence 1 = footnote
    assert story_text(out, "uff")[0].endswith("it runs on.")  # table untouched


def test_a_unique_find_lands_though_its_context_ran_across_a_break(tmp_path):
    """The captured context "forever. She opened" straddles a paragraph break, so
    it anchors nothing — but "forever" is unique and its own span sits well inside
    one paragraph, so the correction still lands. Only a span that *itself* crosses
    a break is refused (see test_a_span_across_a_paragraph_break_is_refused)."""
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, [
        Edit(id="e1", find="forever", replace="x",
             context="forever. She opened")])
    assert report.applied == 1
    assert story_text(out)[1] == "It was late, we were tired and the road went on x."


def test_a_clean_apply_with_a_context_verifies_clean(tmp_path):
    edits = [Edit(id="e1", find="runs on", replace="rambles on",
                  context="it also runs on")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    assert verify(LAYOUT, out, edits).clean


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


# --- changes to review (the designer's before/after) --------------------------

def test_verify_lists_each_applied_change_in_context(tmp_path):
    """The designer's quick-check data: an applied edit comes back as the whole
    line before and after — so it can be read in context, not as a bare
    find→replace divorced from its sentence."""
    edits = [Edit(id="e1", find="Their were", replace="There were")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    report = verify(LAYOUT, out, edits)
    assert len(report.changes) == 1
    c = report.changes[0]
    assert isinstance(c, ReviewChange)
    assert c.before == "Their were several mistakes here to find."
    assert c.after == "There were several mistakes here to find."
    assert c.edit_ids == ("e1",)


def test_two_edits_in_one_paragraph_are_one_review_change(tmp_path):
    """A line two corrections touched is shown once, fully corrected, carrying both
    edit ids — not the same sentence listed twice."""
    edits = [Edit(id="e1", find="Their", replace="There"),
             Edit(id="e2", find="mistakes here", replace="errors here")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    report = verify(LAYOUT, out, edits)
    assert len(report.changes) == 1
    c = report.changes[0]
    assert c.before == "Their were several mistakes here to find."
    assert c.after == "There were several errors here to find."
    assert set(c.edit_ids) == {"e1", "e2"}


def test_a_review_change_carries_the_reviewer_note(tmp_path):
    """The reviewer's own words ride along, so the person confirming the change
    sees why it was asked for, not just what it became."""
    edits = [Edit(id="e1", find="Their were", replace="There were",
                  instruction="subject-verb agreement")]
    out = tmp_path / "out.idml"
    apply_edits(LAYOUT, out, edits)
    report = verify(LAYOUT, out, edits)
    assert report.changes[0].instruction == "subject-verb agreement"


# --- parsing a structured list ------------------------------------------------

def test_a_list_of_dicts_becomes_edits():
    result = parse_edits([
        {"find": "Their were", "replace": "There were"},
        {"find": "teh", "replace": "the", "occurrence": 2},
    ])
    assert result.ok
    assert not result.issues
    e0, e1 = result.edits
    assert (e0.find, e0.replace, e0.occurrence) == ("Their were", "There were", 0)
    assert (e1.find, e1.replace, e1.occurrence) == ("teh", "the", 2)
    # An id is generated for every entry that carries none.
    assert [e.id for e in result.edits] == ["c1", "c2"]


def test_field_aliases_are_understood():
    """A corrections note says 'original'/'corrected', a model says 'from'/'to';
    both mean find/replace."""
    result = parse_edits([
        {"original": "colour", "corrected": "color"},
        {"from": "grey", "to": "gray", "note": "US spelling"},
    ])
    assert result.ok
    assert (result.edits[0].find, result.edits[0].replace) == ("colour", "color")
    assert (result.edits[1].find, result.edits[1].replace) == ("grey", "gray")
    assert result.edits[1].instruction == "US spelling"


def test_a_context_anchor_is_parsed_and_aliased():
    result = parse_edits([
        {"find": "harbor", "replace": "harbour", "context": "the harbor lights"},
        {"find": "grey", "replace": "gray", "near": "the grey sky"}])
    assert result.ok
    assert result.edits[0].context == "the harbor lights"
    assert result.edits[1].context == "the grey sky"      # alias "near"


def test_a_json_string_is_parsed():
    result = parse_edits('[{"find": "a", "replace": "b"}]')
    assert result.ok
    assert result.edits[0].find == "a"


def test_json_bytes_are_parsed():
    result = parse_edits(b'[{"find": "a", "replace": "b"}]')
    assert result.ok and result.edits[0].replace == "b"


def test_a_json_file_is_parsed(tmp_path):
    p = tmp_path / "corrections.json"
    p.write_text('[{"find": "a", "replace": "b"}]', encoding="utf-8")
    result = parse_edits(p)
    assert result.ok and result.edits[0].find == "a"


def test_a_wrapper_object_with_an_edits_key_is_unwrapped():
    result = parse_edits({"edits": [{"find": "a", "replace": "b"}]})
    assert result.ok and len(result.edits) == 1


def test_a_lone_dict_is_one_entry():
    result = parse_edits({"find": "a", "replace": "b"})
    assert result.ok and len(result.edits) == 1


def test_a_terse_pair_becomes_an_edit():
    result = parse_edits([["Their were", "There were"],
                          ["teh", "the", "typo"]])
    assert result.ok
    assert (result.edits[0].find, result.edits[0].replace) == ("Their were",
                                                               "There were")
    assert result.edits[1].instruction == "typo"


def test_an_explicit_id_is_kept_and_generated_ids_avoid_it():
    result = parse_edits([
        {"find": "a", "replace": "b"},
        {"id": "c2", "find": "c", "replace": "d"},   # collides with the default
        {"find": "e", "replace": "f"},
    ])
    ids = [e.id for e in result.edits]
    assert ids[1] == "c2"                 # the explicit one is honoured
    assert len(set(ids)) == 3             # nothing generated collides with it


def test_a_duplicate_explicit_id_is_flagged():
    result = parse_edits([
        {"id": "x", "find": "a", "replace": "b"},
        {"id": "x", "find": "c", "replace": "d"},
    ])
    assert len(result.edits) == 1
    assert result.issues and "duplicate id" in result.issues[0].reason


def test_kind_is_normalized_and_an_unknown_kind_is_flagged():
    result = parse_edits([
        {"find": "Chapter One", "replace": "", "kind": "Design",
         "instruction": "move to a right-hand page"},
        {"find": "maybe", "replace": "perhaps", "type": "JUDGMENT"},
        {"find": "x", "replace": "y", "kind": "guesswork"},
    ])
    assert result.edits[0].kind == DESIGN
    assert result.edits[1].kind == JUDGMENT
    assert len(result.issues) == 1
    assert "unknown kind" in result.issues[0].reason
    assert result.issues[0].index == 2


def test_occurrence_is_coerced_and_bad_values_flagged():
    ok = parse_edits([{"find": "a", "replace": "b", "occurrence": "3"}])
    assert ok.edits[0].occurrence == 3
    bad = parse_edits([{"find": "a", "replace": "b", "occurrence": -1},
                       {"find": "c", "replace": "d", "occurrence": "later"}])
    assert not bad.edits
    assert len(bad.issues) == 2


def test_a_missing_find_on_a_text_edit_is_flagged():
    result = parse_edits([{"replace": "the"}])           # nothing to anchor to
    assert not result.edits
    assert "no find text" in result.issues[0].reason


def test_a_design_entry_may_carry_no_find_but_needs_an_instruction():
    ok = parse_edits([{"kind": "design",
                       "instruction": "start Part Two on a recto"}])
    assert ok.ok and ok.edits[0].kind == DESIGN and ok.edits[0].find == ""
    empty = parse_edits([{"kind": "design"}])
    assert not empty.edits and empty.issues


def test_a_pure_deletion_is_kept():
    result = parse_edits([{"find": " for good measure", "replace": ""}])
    assert result.ok and result.edits[0].is_deletion


def test_a_non_object_entry_is_flagged_not_dropped():
    result = parse_edits([{"find": "a", "replace": "b"}, "just a string", 42])
    assert len(result.edits) == 1
    assert len(result.issues) == 2
    assert all(isinstance(i, ParseIssue) for i in result.issues)
    assert {i.index for i in result.issues} == {1, 2}


def test_non_text_find_or_replace_is_flagged():
    result = parse_edits([{"find": 7, "replace": "x"},
                          {"find": "a", "replace": ["b"]}])
    assert not result.edits
    assert len(result.issues) == 2


def test_an_empty_list_parses_to_nothing_cleanly():
    result = parse_edits([])
    assert result.ok and not result.edits


def test_malformed_json_raises():
    with pytest.raises(ValueError):
        parse_edits("{not json")


def test_a_non_list_source_raises():
    with pytest.raises(ValueError):
        parse_edits("42")                # valid JSON, but not a list of edits


def test_parsed_edits_apply_and_verify_clean(tmp_path):
    """The whole chain end to end: a structured list parses to edits, applies to
    the real IDML, and verifies clean — the parser feeds the deterministic core
    with no impedance mismatch."""
    result = parse_edits([
        {"find": "Their were", "replace": "There were"},
        {"find": "mistakes here", "replace": "errors here"},
    ])
    assert result.ok
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, list(result.edits))
    assert report.applied == 2
    assert story_text(out)[4] == "There were several errors here to find."
    assert verify(LAYOUT, out, list(result.edits)).clean


# --- the run end to end -------------------------------------------------------

import json as _json


def test_apply_corrections_writes_a_file_and_a_report(tmp_path):
    out = tmp_path / "job"
    result = apply_corrections(LAYOUT, [
        {"find": "Their were", "replace": "There were"},
    ], out)
    assert result.corrected_idml.exists()
    assert result.corrected_idml.name == corrected_name(LAYOUT)
    assert result.report_md.exists() and result.report_json.exists()
    assert result.applied == 1
    assert result.clean
    # The corrected file really carries the change.
    assert story_text(result.corrected_idml)[4] == \
        "There were several mistakes here to find."
    # The source is untouched.
    assert story_text(LAYOUT)[4] == "Their were several mistakes here to find."


def test_the_json_report_is_machine_readable(tmp_path):
    out = tmp_path / "job"
    result = apply_corrections(LAYOUT, [
        {"find": "Their were", "replace": "There were"},
        {"find": "not in the book at all", "replace": "x"},
    ], out)
    payload = _json.loads(result.report_json.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["deterministic"] is True
    assert payload["apply"]["applied"] == 1
    flagged = payload["apply"]["flagged"]
    assert len(flagged) == 1 and flagged[0]["status"] == NOT_FOUND
    # The unanchored edit is not clean, and the flag count reflects it.
    assert result.flagged == 1
    assert not result.clean


def test_the_report_carries_the_changes_to_review(tmp_path):
    """The applied changes reach both report artifacts: corrections.json (for the
    results screen's redline) and the notes .md (for a reader), each in context."""
    out = tmp_path / "job"
    result = apply_corrections(LAYOUT, [
        {"find": "Their were", "replace": "There were"},
    ], out)
    payload = _json.loads(result.report_json.read_text(encoding="utf-8"))
    assert len(payload["changes"]) == 1
    ch = payload["changes"][0]
    assert ch["before"] == "Their were several mistakes here to find."
    assert ch["after"] == "There were several mistakes here to find."
    md = result.report_md.read_text(encoding="utf-8")
    assert "Changes to review" in md
    assert "There were several mistakes here to find." in md


def test_a_flagged_proof_comment_is_not_listed_twice(tmp_path):
    """A flagged edit read from a proof is the same problem as its reviewer
    comment. The report shows it once — in the comment ledger — not again as a
    separate 'For a human' edits section, which read as two duplicate lists."""
    out = tmp_path / "job"
    result = apply_corrections(
        LAYOUT,
        [{"find": "not in the book at all", "replace": "x", "source": "p1-1"}],
        out,
        comments=[{"id": "p1-1", "page": 1, "kind": "note",
                   "instruction": "fix this", "anchor": "somewhere"}])
    md = result.report_md.read_text(encoding="utf-8")
    assert "Reviewer comments needing a human" in md
    assert "## For a human" not in md               # the edit is not listed again
    assert "Other edits needing a human" not in md  # nothing uncovered remains
    payload = _json.loads(result.report_json.read_text(encoding="utf-8"))
    c = payload["comments"]["items"][0]
    assert c["disposition"] == "flagged"
    assert c["edit_ids"]                             # the link that dedups them


def test_flagged_edits_without_comments_keep_the_plain_heading(tmp_path):
    """With no reviewer comments there is nothing to double up with, so a flagged
    edit still gets the plain 'For a human' heading."""
    out = tmp_path / "job"
    result = apply_corrections(
        LAYOUT, [{"find": "nowhere to be found", "replace": "x"}], out)
    md = result.report_md.read_text(encoding="utf-8")
    assert "## For a human" in md


def test_a_parse_issue_is_carried_into_the_run(tmp_path):
    out = tmp_path / "job"
    result = apply_corrections(LAYOUT, [
        {"find": "Their were", "replace": "There were"},
        {"replace": "orphan with no find"},          # unparseable
    ], out)
    assert result.applied == 1
    assert not result.parse.ok
    assert not result.clean                          # a dropped entry is not clean
    payload = _json.loads(result.report_json.read_text(encoding="utf-8"))
    assert len(payload["parse"]["issues"]) == 1
    assert "no find text" in result.report_md.read_text(encoding="utf-8")


def test_verify_corrections_audits_a_foreign_after_file(tmp_path):
    """The audit case: the book was corrected by another tool. Given before,
    after and the list, the run reports the change nobody asked for."""
    edits = [{"find": "Their were", "replace": "There were"}]
    # Produce an "after" file the way a hand-run script might — with an extra,
    # unrequested change sneaked in.
    clean = tmp_path / "clean.idml"
    apply_edits(LAYOUT, clean, list(parse_edits(edits).edits))
    tampered = tmp_path / "tampered.idml"
    apply_edits(clean, tampered, [
        Edit(id="x", find="third paragraph", replace="THIRD paragraph")])

    out = tmp_path / "audit"
    result = verify_corrections(LAYOUT, tampered, edits, out)
    assert result.corrected_idml is None          # we wrote no file
    assert result.apply is None
    assert result.discrepancies >= 1
    assert not result.clean
    md = result.report_md.read_text(encoding="utf-8")
    assert "Unaccounted changes" in md
    assert "THIRD paragraph" in md


def test_a_clean_run_reports_clean(tmp_path):
    out = tmp_path / "job"
    result = apply_corrections(LAYOUT, [
        {"find": "Their were", "replace": "There were"},
        {"find": "mistakes here", "replace": "errors here"},
    ], out)
    assert result.clean
    assert "Clean." in result.report_md.read_text(encoding="utf-8")


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


# --- the reviewer-comment ledger ----------------------------------------------

def test_source_round_trips_through_parse():
    """An edit that cites the comment it came from keeps that link through parse,
    so the run can reconcile it back to the comment."""
    parsed = parse_edits([{"find": "a", "replace": "b", "source": "p3-2"}])
    assert parsed.edits[0].source == "p3-2"


def test_every_comment_gets_a_disposition_and_none_is_swallowed(tmp_path):
    """A comment whose edit lands is `applied`; a comment no edit was made for is
    `not_extracted` and carries its original note — never dropped silently."""
    edits = [{"find": "Their were", "replace": "There were", "source": "p1-1"}]
    comments = [
        {"id": "p1-1", "page": 1, "kind": "highlight",
         "instruction": "subject-verb", "anchor": "Their were"},
        {"id": "p9-9", "page": 9, "kind": "note",
         "instruction": "cut this scene?", "anchor": "the room was empty"},
    ]
    result = apply_corrections(LAYOUT, edits, tmp_path, comments=comments)
    assert result.total_comments == 2 and result.unresolved == 1
    by_id = {c.id: c for c in result.comments}
    assert by_id["p1-1"].disposition == DISP_APPLIED
    assert by_id["p9-9"].disposition == DISP_NOT_EXTRACTED
    # The un-appliable comment reaches the report with the reviewer's own words.
    import json
    payload = json.loads((tmp_path / "corrections.json").read_text("utf-8"))
    dropped = [c for c in payload["comments"]["items"]
               if c["disposition"] == "not_extracted"]
    assert dropped and dropped[0]["instruction"] == "cut this scene?"
    assert payload["comments"]["unresolved"] == 1


def test_a_flagged_edits_comment_is_unresolved_not_applied(tmp_path):
    """A comment whose only edit could not be anchored is `flagged` — a human
    still owns it, so it counts as unresolved."""
    edits = [{"find": "nowhere in the book", "replace": "x", "source": "p2-1"}]
    comments = [{"id": "p2-1", "page": 2, "kind": "note",
                 "instruction": "fix this", "anchor": "nowhere in the book"}]
    result = apply_corrections(LAYOUT, edits, tmp_path, comments=comments)
    assert result.unresolved == 1
    assert result.comments[0].disposition == "flagged"
    assert not result.clean


def test_the_sanity_gate_withholds_a_doubtful_edit(tmp_path):
    """With the opt-in gate on, an edit the model judges an over-grab is held back
    as WITHHELD (a human owns it) and never written — the deterministic default,
    with no gate, still applies it."""
    from docproof.providers import NormalizedUsage, ProviderResult
    from .fakes import FakeProvider

    edits = [{"find": "Their were", "replace": "There were", "source": "p1-1"}]
    # Deterministic default: the edit lands.
    free = apply_corrections(LAYOUT, edits, tmp_path / "free")
    assert free.applied == 1

    # Gate on, and the model calls this one an over-grab: it is withheld.
    provider = FakeProvider([ProviderResult(
        parsed={"verdicts": [{"id": "c1", "verdict": "over_grab",
                              "reason": "drops a word"}]},
        usage=NormalizedUsage(input_tokens=50, output_tokens=10))])
    gated = apply_corrections(LAYOUT, edits, tmp_path / "gated",
                              sanity=(provider, "m"))
    assert gated.applied == 0
    assert gated.apply.outcomes[0].status == WITHHELD
    assert story_text(gated.corrected_idml)[4] == \
        "Their were several mistakes here to find."   # unchanged, held for a human
