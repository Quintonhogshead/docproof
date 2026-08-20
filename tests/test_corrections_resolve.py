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
    edit, note = adjudicate_instruction(
        item, "change it to 'stood empty' in the room sentence", provider,
        model="fake-model", usage=usage,
        stories=read_stories(out.corrected_idml))
    assert isinstance(edit, Edit) and note == "as asked"
    # The prompt carried the book's own text and the designer's words.
    user = provider.calls[0]["user"]
    assert "the room was empty" in user
    assert "stood empty" in user
    result = apply_edit_to_corrected(out.corrected_idml, edit)
    assert result["after"] == "She opened the door, the room stood empty."
    assert "She opened the door, the room stood empty." \
        in _texts(out.corrected_idml)


def test_a_declined_answer_writes_nothing(tmp_path):
    out, payload = _run(tmp_path, [{"find": "was", "replace": "is"}])
    provider = _scripted({"decision": "decline", "find": "", "replace": "",
                          "context": "", "format": "",
                          "note": "that is a layout request"})
    edit, why = adjudicate_instruction(
        payload["queue"][0], "move it up a line", provider,
        model="fake-model", usage=Usage(),
        stories=read_stories(out.corrected_idml))
    assert edit is None
    assert why == "that is a layout request"


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
