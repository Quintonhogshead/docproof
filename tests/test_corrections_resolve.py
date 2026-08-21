"""Resolving flagged corrections from the review screen: the queue a run leaves
behind, the click that applies one of its options, the typed answer a model
transcribes, and the report staying honest through it all.

Everything runs on tests/fixtures/layout.idml, like the rest of the corrections
suite; the model in the typed path is a FakeProvider, so nothing reaches a
vendor.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docproof.corrections.idml import read_stories
from docproof.corrections.model import Edit
from docproof.corrections.resolve import (ResolveError, adjudicate_instruction,
                                          apply_edit_to_corrected,
                                          apply_option, queue_counts,
                                          record_resolution)
from docproof.corrections.run import apply_corrections
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from .conftest import FIXTURES
from .fakes import FakeProvider

LAYOUT = FIXTURES / "layout.idml"


def _run(tmp_path, corrections):
    """A deterministic corrections run (no model passes), returning the outputs
    and the report payload it wrote."""
    out = apply_corrections(LAYOUT, corrections, tmp_path)
    payload = json.loads(out.report_json.read_text("utf-8"))
    return out, payload


def _texts(idml, story_id="ue0"):
    story = next(s for s in read_stories(idml) if s.story_id == story_id)
    return [p.text for p in story.paragraphs]


# --- the queue ----------------------------------------------------------------

def test_an_ambiguous_edit_leaves_options_in_the_queue(tmp_path):
    # "was" appears in two paragraphs and nothing chooses between them, so the
    # run flags it — and the queue offers both places, fully materialized.
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    assert out.flagged == 1
    queue = payload["queue"]
    assert len(queue) == 1
    item = queue[0]
    assert item["status"] == "ambiguous"
    assert item["resolved"] is None
    options = item["options"]
    assert len(options) >= 2
    for o in options:
        assert o["found"] == "was"
        assert o["replacement"] == "is"
        assert o["after"] == (o["before"][:o["start"]] + "is"
                              + o["before"][o["end"]:])


def test_a_clean_run_leaves_an_empty_queue(tmp_path):
    out, payload = _run(tmp_path,
                        [{"find": "Their were", "replace": "There were"}])
    assert out.flagged == 0
    assert payload["queue"] == []


def test_a_not_found_edit_gets_an_item_with_no_options(tmp_path):
    out, payload = _run(tmp_path,
                        [{"find": "nowhere in this book", "replace": "x"}])
    assert out.flagged == 1
    item = payload["queue"][0]
    assert item["status"] == "not_found"
    assert item["options"] == []


# --- clicking an option -------------------------------------------------------

def test_clicking_an_option_applies_it_to_the_corrected_file(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    corrected = out.corrected_idml
    item = payload["queue"][0]
    # The copy in "the room was empty".
    option = next(o for o in item["options"] if "room" in o["before"])
    result = apply_option(corrected, option)
    assert result["before"] == "She opened the door, the room was empty."
    assert result["after"] == "She opened the door, the room is empty."
    assert "She opened the door, the room is empty." in _texts(corrected)
    # The other copy is untouched.
    assert "It was late, we were tired and the road went on forever." \
        in _texts(corrected)


def test_a_shifted_span_is_relocated_within_its_paragraph(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    option = next(o for o in payload["queue"][0]["options"]
                  if "room" in o["before"])
    option = {**option, "start": option["start"] - 3,
              "end": option["end"] - 3}
    result = apply_option(out.corrected_idml, option)
    assert result["after"] == "She opened the door, the room is empty."


def test_an_option_whose_text_is_gone_is_refused(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    option = dict(next(o for o in payload["queue"][0]["options"]
                       if "room" in o["before"]))
    option["found"] = "no longer here"
    with pytest.raises(ResolveError):
        apply_option(out.corrected_idml, option)
    # Nothing was written.
    assert "She opened the door, the room was empty." \
        in _texts(out.corrected_idml)


def test_an_option_that_empties_the_line_removes_it(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    line = "A third paragraph with plain text for good measure."
    texts = _texts(corrected)
    index = texts.index(line)
    option = {"story_id": "ue0", "paragraph": index, "start": 0,
              "end": len(line), "found": line, "replacement": "",
              "before": line, "after": ""}
    result = apply_option(corrected, option)
    assert result["removed_line"] is True
    assert line not in _texts(corrected)
    assert len(_texts(corrected)) == len(texts) - 1


# --- the typed answer ---------------------------------------------------------

def _scripted(answer: dict) -> FakeProvider:
    return FakeProvider([ProviderResult(
        parsed=answer, usage=NormalizedUsage(input_tokens=10, output_tokens=5))])


def test_a_typed_answer_becomes_an_edit_and_applies(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    provider = _scripted({"decision": "apply",
                          "find": "the room was empty",
                          "replace": "the room stood empty",
                          "context": "", "format": "", "note": "as asked"})
    usage = Usage()
    edit, note, verdict = adjudicate_instruction(
        item, "change it to 'stood empty' in the room sentence", provider,
        model="fake-model", usage=usage,
        stories=read_stories(out.corrected_idml))
    assert isinstance(edit, Edit) and note == "as asked" and verdict == "apply"
    # The prompt carried the book's own text and the designer's words.
    user = provider.calls[0]["user"]
    assert "the room was empty" in user
    assert "stood empty" in user
    result = apply_edit_to_corrected(out.corrected_idml, edit)
    assert result["after"] == "She opened the door, the room stood empty."
    assert "She opened the door, the room stood empty." \
        in _texts(out.corrected_idml)


def test_an_accepted_change_can_adjust_line_spacing(tmp_path):
    """A designer's accepted decision about the paragraph — here, space above it —
    is carried out as a spacing op, not declined as layout."""
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    line = "A third paragraph with plain text for good measure."
    provider = _scripted({"decision": "apply", "find": line, "replace": line,
                          "context": "", "format": "",
                          "paragraph": "space-before", "paragraph_value": "6",
                          "note": "added six points above"})
    edit, note, verdict = adjudicate_instruction(
        payload["queue"][0], "add a little space above this paragraph", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert verdict == "apply" and edit.paragraph == "space-before"
    assert edit.paragraph_value == "6"
    apply_edit_to_corrected(out.corrected_idml, edit)
    from docproof.corrections.idml import read_stories as _rs
    xml = next(s for s in _rs(out.corrected_idml)
               if s.story_id == "ue0").serialize().decode("utf-8")
    assert 'SpaceBefore="6"' in xml


def test_an_accepted_change_can_cross_a_paragraph_break(tmp_path):
    """A break change the designer accepts (join this line with the next) applies
    as a paragraph op rather than being refused as a cross-paragraph edit."""
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    texts = _texts(out.corrected_idml)
    # Two consecutive body paragraphs (same style), to be joined.
    a, b = texts[2], texts[3]
    provider = _scripted({"decision": "apply", "find": a, "replace": a,
                          "context": "", "format": "", "paragraph": "merge-next",
                          "note": "joined the two lines"})
    item = {"id": "q1", "find": a, "replace": a}
    edit, note, verdict = adjudicate_instruction(
        item, "run this line on into the next", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert verdict == "apply" and edit.paragraph == "merge-next"
    apply_edit_to_corrected(out.corrected_idml, edit)
    joined = _texts(out.corrected_idml)
    assert len(joined) == len(texts) - 1
    assert any(a in t and b in t for t in joined)


def test_a_declined_answer_writes_nothing(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    provider = _scripted({"decision": "decline", "find": "", "replace": "",
                          "context": "", "format": "",
                          "note": "that is a layout request"})
    edit, why, verdict = adjudicate_instruction(
        payload["queue"][0], "move it up a line", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert edit is None and verdict == "decline"
    assert why == "that is a layout request"


def test_a_leave_verdict_settles_the_flag_as_no_change(tmp_path):
    # The model concludes the text is already correct as set — a "leave", which
    # is a real settlement, not a refusal. adjudicate reports it as such.
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    provider = _scripted({"decision": "leave", "find": "", "replace": "",
                          "context": "", "format": "",
                          "note": "the open line is deliberate — leave it"})
    edit, note, verdict = adjudicate_instruction(
        payload["queue"][0], "should this have a period?", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert edit is None and verdict == "leave"
    assert "deliberate" in note


def test_an_apply_that_changes_nothing_reads_as_leave(tmp_path):
    # The model says "apply" but its find == replace — the text is already right.
    # That is a leave, not a failure, so the flag can still be settled.
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    provider = _scripted({"decision": "apply",
                          "find": "the room was empty",
                          "replace": "the room was empty",
                          "context": "", "format": "", "note": "already fine"})
    edit, note, verdict = adjudicate_instruction(
        payload["queue"][0], "make sure it reads right", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert edit is None and verdict == "leave"


def test_an_edit_that_cannot_anchor_is_refused_not_written(tmp_path):
    out, _ = _run(tmp_path, [{"find": "was", "replace": "is"}])
    edit = Edit(id="q1-designer", find="text the book does not carry",
                replace="x")
    with pytest.raises(ResolveError):
        apply_edit_to_corrected(out.corrected_idml, edit)


# --- the report staying honest -------------------------------------------------

def test_a_resolution_moves_the_counts_and_the_notes(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    json_path = out.report_json
    before = queue_counts(payload)
    assert before["flags"] == 1
    item = payload["queue"][0]
    option = next(o for o in item["options"] if "room" in o["before"])
    result = apply_option(out.corrected_idml, option)
    updated = record_resolution(json_path, item["id"], {
        "kind": "option", "option_id": option["id"],
        "edit_id": option["edit_id"], **result})
    after = queue_counts(updated)
    assert after["flags"] == 0
    assert after["applied"] == before["applied"] + 1
    assert updated["queue"][0]["resolved"]["option_id"] == option["id"]
    # The notes were re-rendered beside the JSON and say what happened.
    md = (json_path.parent / "corrections_notes.md").read_text("utf-8")
    assert "Resolved in review" in md
    # And the equation still reconciles.
    assert "counts do not reconcile" not in md


def test_a_resolution_cannot_land_twice(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    option = next(o for o in item["options"] if "room" in o["before"])
    result = apply_option(out.corrected_idml, option)
    record_resolution(out.report_json, item["id"],
                      {"kind": "option", "option_id": option["id"],
                       "edit_id": option["edit_id"], **result})
    with pytest.raises(ResolveError):
        record_resolution(out.report_json, item["id"],
                          {"kind": "option", "option_id": option["id"],
                           "edit_id": option["edit_id"], **result})


# --- ignoring a flag (set aside), and putting it back --------------------------

def test_a_dismissal_sets_the_flag_aside_without_touching_the_file(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    before_counts = queue_counts(payload)
    updated = record_resolution(out.report_json, item["id"],
                                {"kind": "dismissed"})
    after = queue_counts(updated)
    assert after["flags"] == 0                      # off the awaiting pile
    assert after["applied"] == before_counts["applied"]   # nothing applied
    # The file was never touched — both copies still read "was".
    texts = _texts(out.corrected_idml)
    assert "It was late, we were tired and the road went on forever." in texts
    md = (out.report_json.parent / "corrections_notes.md").read_text("utf-8")
    assert "Set aside in review" in md
    assert "counts do not reconcile" not in md


def test_a_dismissal_can_be_reopened(tmp_path):
    from docproof.corrections.resolve import reopen_dismissed
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    record_resolution(out.report_json, item["id"], {"kind": "dismissed"})
    updated = reopen_dismissed(out.report_json, item["id"])
    assert queue_counts(updated)["flags"] == 1
    assert updated["queue"][0]["resolved"] is None
    md = (out.report_json.parent / "corrections_notes.md").read_text("utf-8")
    assert "Set aside in review" not in md


def test_an_applied_resolution_cannot_be_reopened(tmp_path):
    from docproof.corrections.resolve import reopen_dismissed
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    option = next(o for o in item["options"] if "room" in o["before"])
    result = apply_option(out.corrected_idml, option)
    record_resolution(out.report_json, item["id"],
                      {"kind": "option", "option_id": option["id"],
                       "edit_id": option["edit_id"], **result})
    with pytest.raises(ResolveError):
        reopen_dismissed(out.report_json, item["id"])


# --- the one-click suggestion and the adjudicator's context --------------------

def test_suggestion_instruction_wraps_the_advice_and_requires_one():
    from docproof.corrections.resolve import suggestion_instruction
    item = {"advice": "the book uses the em dash here; replace the comma"}
    text = suggestion_instruction(item)
    assert "the em dash" in text and "exactly" in text
    with pytest.raises(ResolveError):
        suggestion_instruction({"advice": ""})


def test_the_prompt_carries_the_screen_options_page_and_advice(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = dict(payload["queue"][0])
    item["advice"] = "use the copy in the room sentence"
    item["page_text"] = "She opened the door, the room was empty."
    provider = _scripted({"decision": "decline", "find": "", "replace": "",
                          "context": "", "format": "", "note": "n/a"})
    adjudicate_instruction(item, "the second one", provider,
                           model="fake-model", usage=Usage(),
                           stories=read_stories(out.corrected_idml))
    user = provider.calls[0]["user"]
    # The numbered placements the designer's "the second one" refers to.
    assert "PLACEMENTS SHOWN ON SCREEN" in user
    assert "1. [" in user and "2. [" in user
    # The page the mark was made on, and the earlier advice.
    assert "THE BOOK'S OWN TEXT FOR PAGE" in user
    assert "use the copy in the room sentence" in user
    # And the passages now bring the paragraph either side of each candidate.
    assert "Chapter One" in user or "A third paragraph" in user


# --- the manual editor: reading a line, and hand-editing it --------------------

from docproof.corrections.resolve import (apply_manual,  # noqa: E402
                                          paragraph_state, _targets_for)

ROAD = "It was late, we were tired and the road went on forever."
ROOM = "She opened the door, the room was empty."


def test_paragraph_state_reads_text_runs_and_neighbours(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    state = paragraph_state(read_stories(out.corrected_idml), "ue0", 2)
    assert state["text"] == ROOM
    # The fixture carries no bold or italics here: one plain run, covering all.
    assert state["runs"][0]["start"] == 0
    assert state["runs"][-1]["end"] == len(ROOM)
    assert all(not r["bold"] and not r["italic"] for r in state["runs"])
    assert state["prev_break"] is False and state["next_break"] is False


def test_a_hand_edit_rewrites_only_what_changed(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    new = ROAD.replace("tired", "exhausted")
    result = apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 1, "expected": ROAD, "text": new,
        "runs": []})
    assert result["after"] == new
    assert new in _texts(corrected)
    # The character style on "late," — a different run of the same paragraph —
    # survived, because only the changed span was rewritten.
    story = next(s for s in read_stories(corrected) if s.story_id == "ue0")
    styles = {(n.text or ""): (n.getparent().get("AppliedCharacterStyle") or "")
              for n in story.paragraphs[1].nodes}
    assert styles.get("late,", "").endswith("/Emph")


def test_bolding_a_selection_lands_and_reads_back(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    start = ROOM.index("opened")
    end = start + len("opened the door")
    apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
        "runs": [{"start": start, "end": end, "bold": True, "italic": False}]})
    state = paragraph_state(read_stories(corrected), "ue0", 2)
    bolded = [r for r in state["runs"] if r["bold"]]
    assert bolded == [{"start": start, "end": end, "bold": True,
                       "italic": False}]
    # And clearing it again takes the override back off.
    apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
        "runs": []})
    state = paragraph_state(read_stories(corrected), "ue0", 2)
    assert all(not r["bold"] for r in state["runs"])


def test_bold_and_italic_together_set_the_combined_face(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
        "runs": [{"start": 0, "end": 3, "bold": True, "italic": True}]})
    story = next(s for s in read_stories(corrected) if s.story_id == "ue0")
    faces = {(n.text or ""): (n.getparent().get("FontStyle") or "")
             for n in story.paragraphs[2].nodes}
    assert faces.get("She") == "Bold Italic"


def test_section_breaks_add_and_remove(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    line = "A third paragraph with plain text for good measure."
    before = _texts(corrected)
    apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 3, "expected": line, "text": line,
        "runs": [{"start": 0, "end": len(line)}],
        "insert_break_after": True})
    texts = _texts(corrected)
    assert len(texts) == len(before) + 1
    assert texts[4] == ""                    # the new blank line
    # Now the paragraph after the blank can remove it again.
    follows = texts[5]
    result = apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 5, "expected": follows,
        "text": follows, "runs": [{"start": 0, "end": len(follows)}],
        "remove_break_above": True})
    assert _texts(corrected) == before
    assert result["paragraph"] == 4          # renumbered by the removal


def test_manual_refusals_write_nothing(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    before = _texts(corrected)
    for spec in (
        # a stale editor
        {"story_id": "ue0", "paragraph": 2, "expected": "something else",
         "text": ROOM, "runs": []},
        # contradictory break asks
        {"story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
         "runs": [{"start": 0, "end": len(ROOM)}],
         "insert_break_after": True, "remove_break_below": True},
        # removing a break that is not there
        {"story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
         "runs": [{"start": 0, "end": len(ROOM)}],
         "remove_break_above": True},
        # nothing changed at all
        {"story_id": "ue0", "paragraph": 2, "expected": ROOM, "text": ROOM,
         "runs": [{"start": 0, "end": len(ROOM)}]},
    ):
        with pytest.raises(ResolveError):
            apply_manual(corrected, spec)
    assert _texts(corrected) == before


def test_clearing_the_whole_line_removes_it(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    line = "A third paragraph with plain text for good measure."
    result = apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 3, "expected": line, "text": "",
        "runs": []})
    assert result["removed_line"] is True
    assert line not in _texts(corrected)


def test_a_located_design_note_gets_an_editable_target(tmp_path):
    # A layout request is never a clickable option, but it IS located — so the
    # editor can open on the line it names.
    out, payload = _run(tmp_path, [
        {"find": "Their were", "replace": "Their were", "kind": "design",
         "instruction": "bad break here — tighten this line"}])
    item = payload["queue"][0]
    assert item["status"] == "routed_to_design"
    assert item["options"] == []
    assert len(item["targets"]) == 1
    assert "Their were" in item["targets"][0]["before"]


def test_targets_fall_back_to_the_anchor_text(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    from docproof.corrections.textmatch import IndexCache
    stories = read_stories(out.corrected_idml)
    targets = _targets_for(None, "the road went on", [], stories, None,
                           IndexCache())
    assert len(targets) == 1
    assert targets[0]["before"].startswith("It was late")


def test_the_anchor_fallback_stays_on_the_cited_page(tmp_path):
    # A short anchor occurs all over the book; the "Edit the line" fallback must not
    # open a paragraph on a page the mark has nothing to do with. When a page is
    # cited and the anchor's copy is elsewhere, no target is offered.
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    from docproof.corrections.textmatch import IndexCache
    stories = read_stories(out.corrected_idml)

    class _Scope:
        def page_of(self, story_id, index):
            return 7                       # every paragraph is on page 7

    scope = _Scope()
    on_page = _targets_for(None, "the road went on", [], stories, scope,
                           IndexCache(), page=7)
    assert len(on_page) == 1               # cited page matches — offered
    off_page = _targets_for(None, "the road went on", [], stories, scope,
                            IndexCache(), page=3)
    assert off_page == []                  # anchor is on page 7, mark cited 3


# --- relocation, touch-ups, and materialized suggestions -----------------------

from docproof.corrections.resolve import (converse,  # noqa: E402
                                          materialize_suggestion,
                                          record_agent_edit, run_agent,
                                          apply_option, record_touchup)


def _step(d: dict) -> ProviderResult:
    return ProviderResult(parsed=d, usage=NormalizedUsage(input_tokens=10,
                                                          output_tokens=5))


def test_paragraph_state_relocates_a_renumbered_line(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    line = "A third paragraph with plain text for good measure."
    # A break inserted above renumbers everything after it.
    apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 3, "expected": line, "text": line,
        "runs": [{"start": 0, "end": len(line)}], "insert_break_after": True})
    # The report still says the corrected line sits at index 4 — but the file
    # now has the blank there. Expect-based relocation finds it at 5.
    state = paragraph_state(read_stories(corrected), "ue0", 4,
                            expect="There were several mistakes here to find.")
    assert state["text"] == "There were several mistakes here to find."
    assert state["paragraph"] == 5
    # A text the story does not carry is refused, not guessed.
    with pytest.raises(ResolveError):
        paragraph_state(read_stories(corrected), "ue0", 4,
                        expect="text the book never held")


def test_a_touchup_records_without_moving_the_counts(tmp_path):
    out, payload = _run(tmp_path,
                        [{"find": "Their were", "replace": "There were"}])
    corrected = out.corrected_idml
    line = "There were several mistakes here to find."
    new = line.replace("mistakes", "errors")
    result = apply_manual(corrected, {
        "story_id": "ue0", "paragraph": 4, "expected": line, "text": new,
        "runs": []})
    before_counts = queue_counts(payload)
    updated = record_touchup(out.report_json, result)
    assert queue_counts(updated) == before_counts
    assert updated["resolutions"][0]["touchup"] is True
    assert updated["changes"][-1]["instruction"] == "edited by hand in review"
    md = (out.report_json.parent / "corrections_notes.md").read_text("utf-8")
    assert "edited the line by hand" in md
    assert "counts do not reconcile" not in md


def test_a_suggestion_materializes_as_a_clickable_placement(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    provider = _scripted({"decision": "apply",
                          "find": "the room was empty",
                          "replace": "the room is empty",
                          "context": "", "format": "", "note": "the room copy"})
    option = materialize_suggestion(item, out.corrected_idml, provider,
                                    model="fake-model", usage=Usage())
    assert option["suggested"] is True
    # The span is the minimal character diff — "wa" → "i" carries "was" → "is".
    assert option["found"] == "wa" and option["replacement"] == "i"
    assert option["before"] == "She opened the door, the room was empty."
    assert option["after"] == "She opened the door, the room is empty."
    # Nothing was written by the dry run.
    assert "She opened the door, the room was empty." \
        in _texts(out.corrected_idml)
    # And the materialized placement applies exactly like any other option.
    result = apply_option(out.corrected_idml, {**option, "id": "q1-s1"})
    assert result["after"] == "She opened the door, the room is empty."
    # With no stored advice, the model was asked to decide — the delegated
    # instruction, not a transcription of advice.
    assert "Decide this one" in provider.calls[0]["user"]


def test_a_suggestion_carries_the_folio_its_item_shows(tmp_path):
    # The suggested placement's page must read exactly like every other row's —
    # the folio the finished IDML shows, riding along from the item.
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = {**payload["queue"][0], "page": 7, "page_label": "1"}
    provider = _scripted({"decision": "apply",
                          "find": "the room was empty",
                          "replace": "the room is empty",
                          "context": "", "format": "", "note": "the room copy"})
    option = materialize_suggestion(item, out.corrected_idml, provider,
                                    model="fake-model", usage=Usage())
    assert option["page"] == 7
    assert option["page_label"] == "1"


def test_a_declined_suggestion_is_refused_with_the_reason(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    provider = _scripted({"decision": "decline", "find": "", "replace": "",
                          "context": "", "format": "",
                          "note": "nothing settles which copy"})
    with pytest.raises(ResolveError) as e:
        materialize_suggestion(payload["queue"][0], out.corrected_idml,
                               provider, model="fake-model", usage=Usage())
    assert "nothing settles which copy" in str(e.value)


# --- the per-flag conversation -------------------------------------------------

def test_a_chat_message_proposes_a_change_without_writing(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    item["chat"] = [{"role": "designer",
                     "text": "change it to 'stood empty' in the room line"}]
    provider = _scripted({"decision": "apply", "find": "the room was empty",
                          "replace": "the room stood empty", "context": "",
                          "format": "", "note": "done"})
    turn = converse(item, out.corrected_idml, provider, model="m",
                    usage=Usage())
    assert turn["reply"] == "done"
    assert turn["proposal"]["after"] == \
        "She opened the door, the room stood empty."
    assert turn["proposal"]["from_chat"] is True
    # Nothing was written — the proposal is a dry run.
    assert "She opened the door, the room was empty." \
        in _texts(out.corrected_idml)


def test_a_follow_up_turn_carries_the_whole_thread_into_the_prompt(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    # A prior turn proposed one thing; the designer now corrects it.
    item["chat"] = [
        {"role": "designer", "text": "change the room line"},
        {"role": "model", "text": "swapped it",
         "proposal": {"found": "the room was empty",
                      "replacement": "the room is empty"}},
        {"role": "designer", "text": "no, use 'stood empty' instead"},
    ]
    provider = _scripted({"decision": "apply", "find": "the room was empty",
                          "replace": "the room stood empty", "context": "",
                          "format": "", "note": "swapped"})
    converse(item, out.corrected_idml, provider, model="m", usage=Usage())
    user = provider.calls[0]["user"]
    assert "THE CONVERSATION SO FAR" in user
    assert "change the room line" in user            # an earlier designer turn
    assert "the room is empty" in user               # the prior proposal, shown
    # The latest message is the decision to carry out.
    assert "no, use 'stood empty' instead" in user
    assert "LATEST MESSAGE" in user


def test_a_chat_decline_is_a_reply_not_an_error(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    item = payload["queue"][0]
    item["chat"] = [{"role": "designer", "text": "just handle it in InDesign"}]
    provider = _scripted({"decision": "decline", "find": "", "replace": "",
                          "context": "", "format": "",
                          "note": "that's a layout call, not a text edit"})
    turn = converse(item, out.corrected_idml, provider, model="m",
                    usage=Usage())
    assert turn["proposal"] is None
    assert "layout call" in turn["reply"]


def test_changes_split_text_edits_from_layout_ops(tmp_path):
    """The 'Changes to review' section separates a change done in the file (a
    text edit — just confirm) from a paragraph layout op that reflows the page
    (needs to be finished in InDesign). The layout op carries the flag, and the
    report renders the two under their own headings."""
    out, payload = _run(tmp_path, [
        {"find": "the room was empty", "replace": "the room was bare"},  # text
        {"find": "A footnote with its own sentence, it also runs on.",
         "replace": "", "paragraph": "delete-paragraph"},          # layout op
    ])
    changes = payload["changes"]
    text = [c for c in changes if not c.get("layout")]
    layout = [c for c in changes if c.get("layout")]
    assert text and layout, changes
    assert any("the room was bare" in c["after"] for c in text)
    md = out.report_md.read_text("utf-8")
    assert "### Changed here — confirm it reads right" in md
    assert "### Needs to be done in InDesign" in md


# --- the run-level agent -------------------------------------------------------

def test_the_agent_searches_then_proposes_then_replies(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        _step({"action": "search", "query": "road"}),
        _step({"action": "propose", "find": "the road went on forever",
               "replace": "the lane went on forever",
               "why": "the designer wants 'lane'"}),
        _step({"action": "reply", "text": "Proposed one change to the road "
                                          "line."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="change 'road' to 'lane'")
    assert res["reply"].startswith("Proposed")
    assert len(provider.calls) == 3          # search, propose, reply
    assert len(res["proposals"]) == 1
    p = res["proposals"][0]
    assert p["after"] == "It was late, we were tired and the lane went on " \
                         "forever."
    assert p["from_agent"] is True and p["why"] == "the designer wants 'lane'"
    # Nothing was written — a proposal is a dry run.
    assert "It was late, we were tired and the road went on forever." \
        in _texts(out.corrected_idml)


def test_an_unplaceable_proposal_is_skipped_not_fatal(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        _step({"action": "propose", "find": "text nowhere in the book",
               "replace": "x", "why": "won't anchor"}),
        _step({"action": "reply", "text": "Couldn't place that one."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="try something")
    assert res["proposals"] == []            # the bad one was dropped
    assert res["reply"] == "Couldn't place that one."


def test_the_agent_raises_a_flag_as_advice(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        _step({"action": "flag", "note": "the tense shifts here — an author "
                                         "call", "quote": "the room was empty"}),
        _step({"action": "reply", "text": "Flagged one thing for you."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="check the tenses")
    assert res["proposals"] == []
    assert len(res["flags"]) == 1
    assert "tense shifts" in res["flags"][0]["note"]
    assert res["flags"][0]["category"] == "note"      # the default


def test_the_agent_triages_a_mixed_layout_request(tmp_path):
    """A real designer ask: reorder pages (a layout task the agent can't do in
    the text), wait for a pending blurb (a hold), and add quotes (a text edit it
    can propose). Each part is routed to the right place — nothing is silently
    dropped, and nothing risky is attempted on the file."""
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        _step({"action": "flag", "category": "task",
               "note": "Move the dedication to fall after the copyright page."}),
        _step({"action": "flag", "category": "hold",
               "note": "Wait for the Kirkus blurb before finalising the praise "
                       "section."}),
        _step({"action": "propose", "find": "the road went on forever",
               "replace": "“the road went on forever”",
               "why": "quotation marks around the blurb"}),
        _step({"action": "reply", "text": "One edit proposed, one task and one "
                                          "hold flagged."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="reorder the front matter, wait for "
                                           "Kirkus, quote the blurbs")
    assert len(res["proposals"]) == 1                  # the quote edit
    cats = sorted(f["category"] for f in res["flags"])
    assert cats == ["hold", "task"]                    # routed, not dropped
    # And the report groups them into a worklist, a blocked list, and edits.
    from docproof.corrections.report import _markdown
    report["agent"] = {"chat": [], "proposals": res["proposals"],
                       "flags": res["flags"]}
    md = _markdown(report)
    assert "## To do in InDesign" in md
    assert "Move the dedication" in md
    assert "## Waiting on" in md
    assert "Kirkus blurb" in md


def test_a_transient_step_miss_is_retried_not_a_dead_end(tmp_path):
    """A single empty/blipped model reply must not end the turn with a
    'rephrase' message — it is retried, and the request goes through. This is
    the failure the designer saw as the agent being 'picky' about phrasing."""
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        ProviderResult(parsed=None, stop_reason="error",
                       error="empty response",
                       usage=NormalizedUsage(input_tokens=5, output_tokens=0)),
        _step({"action": "propose", "find": "the road went on forever",
               "replace": "“the road went on forever”",
               "why": "quotation marks"}),
        _step({"action": "reply", "text": "Added the quotes."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="quote the blurb")
    assert res["reply"] == "Added the quotes."
    assert len(res["proposals"]) == 1       # the blip did not lose the work
    assert len(provider.calls) == 3         # error, then propose, then reply


def test_the_agent_loop_is_bounded(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    # A provider that only ever searches would loop forever without the cap.
    provider = FakeProvider([_step({"action": "search", "query": "the"})
                             for _ in range(50)])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="loop", max_steps=5)
    assert len(provider.calls) == 5          # stopped at the cap
    assert res["proposals"] == [] and "for now" in res["reply"]


def test_accepting_an_agent_proposal_writes_and_logs_it(tmp_path):
    out, _ = _run(tmp_path, [{"find": "Their were", "replace": "There were"}])
    report = json.loads(out.report_json.read_text("utf-8"))
    provider = FakeProvider([
        _step({"action": "propose", "find": "the road went on forever",
               "replace": "the lane went on forever", "why": "wants 'lane'"}),
        _step({"action": "reply", "text": "Done."}),
    ])
    res = run_agent(report, out.corrected_idml, provider, model="m",
                    usage=Usage(), message="road to lane")
    proposal = {**res["proposals"][0], "id": "agent-1"}
    result = apply_option(out.corrected_idml, proposal)
    updated = record_agent_edit(out.report_json, proposal, result)
    # The file carries the change now.
    assert "It was late, we were tired and the lane went on forever." \
        in _texts(out.corrected_idml)
    # And it is logged as an agent change, with the reason.
    agent_res = [r for r in updated["resolutions"] if r["kind"] == "agent"]
    assert len(agent_res) == 1 and agent_res[0]["why"] == "wants 'lane'"
    md = (out.report_json.parent / "corrections_notes.md").read_text("utf-8")
    assert "accepted the agent's change" in md
