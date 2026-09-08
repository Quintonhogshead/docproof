"""The headless corrections intake: an author's submission — a marked-up PDF, a
Word redline or list, or free text — read into the three strings a corrections
Job takes, without the FastAPI routes. Every model call goes through the fake
provider; the PDF and Word inputs are built here, the same way the extract and
route tests build theirs.
"""
from __future__ import annotations

import json

import pytest

from docproof.corrections.intake import (
    IntakeError, IntakeResult, edits_to_corrections_json, read_docx_submission,
    read_pdf_submission, read_submission, read_text)
from docproof.corrections.parse import parse_edits
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from .conftest import FIXTURES
from .fakes import FakeProvider
from .test_corrections_extract import (_proof, make_commented_pdf,
                                       make_tracked_docx, story_text)

LAYOUT = FIXTURES / "layout.idml"


def _canned(*edit_lists: list[dict]) -> FakeProvider:
    """One scripted structured result per model call, in order."""
    return FakeProvider([
        ProviderResult(parsed={"edits": edits},
                       usage=NormalizedUsage(input_tokens=200, output_tokens=40))
        for edits in edit_lists])


def _row(find: str, replace: str, **extra) -> dict:
    return {"find": find, "replace": replace, "instruction": "",
            "kind": "mechanical", "occurrence": 0, **extra}


def _reparsed(result: IntakeResult):
    """The Job's own validator agrees with the count the intake reported."""
    parsed = parse_edits(result.corrections)
    assert len(parsed.edits) == result.edits
    assert isinstance(json.loads(result.corrections), list)
    return parsed


# --- free text ---------------------------------------------------------------------

def test_free_text_is_read_by_the_model():
    provider = _canned([_row("Their were", "There were")])
    usage = Usage()
    result = read_submission("change 'Their were' to 'There were'",
                             idml_path=None, provider=provider, model="m",
                             usage=usage)
    assert result.source_kind == "text"
    assert result.edits == 1 and not result.issues
    assert result.comments == "" and result.pages == ""
    assert result.comments_total == 0 and result.page_texts_from == ""
    parsed = _reparsed(result)
    assert parsed.edits[0].find == "Their were"
    # The text itself went to the model, and its tokens were counted.
    assert provider.calls[0]["user"].startswith("change 'Their were'")
    assert usage.output_tokens == 40


def test_a_text_file_path_is_read_as_text(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("p. 4: 'Their were' should be 'There were'")
    provider = _canned([_row("Their were", "There were")])
    result = read_submission(notes, idml_path=None, provider=provider,
                             model="m", usage=Usage())
    assert result.source_kind == "text" and result.edits == 1
    assert "Their were" in provider.calls[0]["user"]


def test_free_text_without_a_model_raises():
    with pytest.raises(IntakeError, match="needs a model to read a typed list"):
        read_submission("change 'teh' to 'the'", idml_path=None, provider=None,
                        model="", usage=Usage())


def test_empty_text_raises():
    with pytest.raises(IntakeError):
        read_text("   ", provider=_canned([]), model="m", usage=Usage())


def test_a_model_failure_is_an_intake_error():
    provider = FakeProvider([ProviderResult(parsed=None, stop_reason="refusal")])
    with pytest.raises(IntakeError, match="could not read"):
        read_text("notes", provider=provider, model="m", usage=Usage())


def test_a_bad_entry_from_the_model_is_an_issue_not_a_drop():
    provider = _canned([_row("Their were", "There were"), _row("", "orphan")])
    result = read_text("notes", provider=provider, model="m", usage=Usage())
    assert result.edits == 1
    assert len(result.issues) == 1 and "no find text" in result.issues[0]
    _reparsed(result)


# --- Word files ---------------------------------------------------------------------

def test_a_tracked_docx_needs_no_model(tmp_path):
    doc = make_tracked_docx(tmp_path / "corr.docx", [
        [("", "Their "), ("del", "were"), ("ins", "was"),
         ("", " several mistakes here to find.")]])
    result = read_submission(doc, idml_path=None, provider=None, model="",
                             usage=Usage())
    assert result.source_kind == "docx-tracked"
    assert result.edits == 1 and not result.issues
    assert result.comments == "" and result.pages == ""
    parsed = _reparsed(result)
    assert "were" in parsed.edits[0].find and "was" in parsed.edits[0].replace


def test_a_wholly_inserted_paragraph_is_an_issue(tmp_path):
    doc = make_tracked_docx(tmp_path / "ins.docx", [
        [("", "Their "), ("del", "were"), ("ins", "was"), ("", " mistakes.")],
        [("ins", "A whole new paragraph with nothing to anchor to.")]])
    result = read_docx_submission(doc, provider=None)
    assert result.source_kind == "docx-tracked"
    assert result.edits == 1
    assert len(result.issues) == 1 and "anchor" in result.issues[0]


def test_a_typed_list_docx_falls_back_to_the_model(tmp_path):
    doc = make_tracked_docx(tmp_path / "list.docx", [
        [("", "p. 12: change 'teh' to 'the'")],
        [("", "p. 40: 'harbor' should be 'harbour'")]])
    provider = _canned([_row("teh", "the"), _row("harbor", "harbour")])
    result = read_submission(doc, idml_path=None, provider=provider, model="m",
                             usage=Usage())
    assert result.source_kind == "docx-list"
    assert result.edits == 2 and not result.issues
    _reparsed(result)
    # The Word file's text, one paragraph per line, is what the model read.
    user = provider.calls[0]["user"]
    assert "change 'teh' to 'the'" in user and "harbour" in user


def test_a_typed_list_docx_without_a_model_raises(tmp_path):
    doc = make_tracked_docx(tmp_path / "list.docx", [
        [("", "p. 12: change 'teh' to 'the'")]])
    with pytest.raises(IntakeError, match="needs a model to read a typed list"):
        read_submission(doc, idml_path=None, provider=None, model="",
                        usage=Usage())


def test_a_blank_docx_raises(tmp_path):
    doc = make_tracked_docx(tmp_path / "blank.docx", [[("", "   ")]])
    with pytest.raises(IntakeError, match="no tracked changes and no text"):
        read_docx_submission(doc, provider=_canned([]), model="m")


def test_a_separate_proof_pdf_supplies_the_pages_for_a_docx(tmp_path):
    doc = make_tracked_docx(tmp_path / "corr.docx", [
        [("", "were slick with "), ("del", "fish oil"),
         ("ins", "petroleum jelly"), ("", ".")]])
    result = read_docx_submission(doc, provider=None,
                                  proof_pdf=_proof(tmp_path / "proof.pdf"))
    assert result.source_kind == "docx-tracked" and result.edits == 1
    pages = json.loads(result.pages)
    assert len(pages) == 1 and "fish oil" in pages[0]
    assert result.page_texts_from == "pdf"
    assert result.comments == ""            # a redline has no reviewer comments


# --- marked-up PDF proofs -----------------------------------------------------------

def test_a_marked_pdf_is_read_in_batches_with_its_comments_and_pages(tmp_path):
    """The two-comment proof, one comment per batch: the edits accumulate across
    two model calls, the comments and page texts ride along for the Job, and the
    caller is told about every batch."""
    provider = _canned([_row("fish oil", "petroleum jelly")],
                       [_row("tobacco", "candlestick")])
    ticks: list[tuple[str, int, int]] = []
    usage = Usage()
    result = read_submission(_proof(tmp_path / "proof.pdf"), idml_path=None,
                             provider=provider, model="m", usage=usage,
                             batch_size=1, progress=lambda *a: ticks.append(a))
    assert result.source_kind == "pdf"
    assert result.edits == 2 and not result.issues
    assert result.comments_total == 2
    assert result.page_texts_from == "proof"
    _reparsed(result)
    assert len([c for c in provider.calls if not c.get("batch")]) == 2
    assert usage.api_calls == 2

    comments = json.loads(result.comments)
    assert [c["id"] for c in comments] == ["p1-1", "p1-2"]
    assert set(comments[0]) == {"id", "page", "kind", "instruction", "anchor",
                                "offset", "replies"}
    assert comments[0]["kind"] == "highlight" and comments[1]["kind"] == "note"
    assert comments[1]["instruction"] == "Replace tobacco with candlestick"

    pages = json.loads(result.pages)
    assert len(pages) == 1 and "fish oil" in pages[0]

    assert ticks == [("read", 1, 1), ("extract", 1, 2), ("extract", 2, 2)]


def test_rule_resolved_marks_lead_the_list_and_cost_nothing(tmp_path):
    """A "Lowercase" mark on a highlighted word is resolved by the rules; the
    prose note goes to the model. The resolved row leads, the model's follows,
    and each cites its comment."""
    pdf = make_commented_pdf(
        tmp_path / "marks.pdf",
        lines=[(72, 700, "were slick with Fish oil."),
               (72, 680, "he carried a pouch of tobacco here")],
        annots=[{"subtype": "/Highlight",
                 # over "Fish" — Helvetica 12pt puts it at x 150-176
                 "rect": [150, 698, 176, 712],
                 "quad": [150, 712, 176, 712, 150, 698, 176, 698],
                 "contents": "Lowercase"},
                {"subtype": "/Text", "rect": [72, 677, 90, 695],
                 "contents": "Replace tobacco with candlestick"}])
    provider = _canned([_row("tobacco", "candlestick", source="p1-2")])
    result = read_pdf_submission(pdf, provider=provider, model="m",
                                 usage=Usage())
    assert result.edits == 2 and result.comments_total == 2
    parsed = _reparsed(result)
    assert (parsed.edits[0].find, parsed.edits[0].replace) == ("Fish", "fish")
    assert parsed.edits[0].source == "p1-1"
    assert parsed.edits[1].find == "tobacco" and parsed.edits[1].source == "p1-2"
    # One model call, for the one comment the rules could not settle.
    assert len(provider.calls) == 1
    assert "tobacco" in provider.calls[0]["user"]
    assert "Lowercase" not in provider.calls[0]["user"]


def test_a_pdf_without_a_model_keeps_the_resolved_edits_and_all_comments(tmp_path):
    """No provider: the rules' edits still come back, and the comments the model
    would have read ride in `comments` so the report accounts for them."""
    pdf = make_commented_pdf(
        tmp_path / "marks.pdf",
        lines=[(72, 700, "were slick with Fish oil."),
               (72, 680, "he carried a pouch of tobacco here")],
        annots=[{"subtype": "/Highlight", "rect": [150, 698, 176, 712],
                 "quad": [150, 712, 176, 712, 150, 698, 176, 698],
                 "contents": "Lowercase"},
                {"subtype": "/Text", "rect": [72, 677, 90, 695],
                 "contents": "Replace tobacco with candlestick"}])
    result = read_submission(pdf, idml_path=None, provider=None, model="",
                             usage=Usage())
    assert result.edits == 1 and result.comments_total == 2
    assert len(json.loads(result.comments)) == 2
    assert result.issues and "need a model" in result.issues[0]
    _reparsed(result)


def test_a_pdf_with_no_comments_raises(tmp_path):
    pdf = make_commented_pdf(tmp_path / "flat.pdf",
                             lines=[(72, 700, "nothing marked here")], annots=[])
    with pytest.raises(IntakeError, match="No comments found in flat.pdf"):
        read_submission(pdf, idml_path=None, provider=_canned([]), model="m",
                        usage=Usage())


def test_the_idml_shows_the_model_the_books_own_text(tmp_path):
    """Given the IDML the corrections will be applied to, the batch the model
    reads carries the book's text for the marked page — the anchors are copied,
    not recalled."""
    book = story_text(LAYOUT, "ue0")
    page = " ".join(book[:3])
    pdf = make_commented_pdf(
        tmp_path / "aligned.pdf",
        lines=[(72, 700 - 14 * i, chunk) for i, chunk in enumerate(
            [page[i:i + 90] for i in range(0, min(len(page), 540), 90)])],
        annots=[{"subtype": "/Text", "rect": [72, 697, 90, 712],
                 "contents": "Make this read better somehow, your call"}])
    provider = _canned([_row("several", "many", kind="judgment", source="p1-1")])
    result = read_submission(pdf, idml_path=LAYOUT, provider=provider, model="m",
                             usage=Usage())
    assert result.edits == 1 and result.comments_total == 1
    user = provider.calls[0]["user"]
    assert "THE BOOK'S OWN TEXT" in user and "[book text, page 1]" in user
    _reparsed(result)


def test_an_unreadable_idml_is_a_nicety_not_a_failure(tmp_path):
    provider = _canned([_row("fish oil", "petroleum jelly")],
                       [_row("tobacco", "candlestick")])
    result = read_submission(_proof(tmp_path / "proof.pdf"),
                             idml_path=tmp_path / "missing.idml",
                             provider=provider, model="m", usage=Usage(),
                             batch_size=1)
    assert result.edits == 2
    assert "THE BOOK'S OWN TEXT" not in provider.calls[0]["user"]


def test_a_non_pdf_extension_is_refused(tmp_path):
    odd = tmp_path / "proof.xlsx"
    odd.write_bytes(b"not a corrections source")
    with pytest.raises(IntakeError, match="not a corrections source"):
        read_submission(odd, idml_path=None, provider=None, model="",
                        usage=Usage())


# --- the shared serializer ----------------------------------------------------------

def test_the_routes_serialize_through_the_same_function():
    """The panel's JSON and the headless intake's JSON come from one function, so
    a Job built either way holds the same string for the same edits."""
    from app.routes import jobs
    assert jobs._edits_to_corrections_json is edits_to_corrections_json


def test_the_serializer_round_trips_through_parse():
    parsed = parse_edits(json.dumps([
        {"find": "Fish", "replace": "fish", "source": "p1-1", "context": "with Fish oil",
         "instruction": "Lowercase", "occurrence": 2, "format": "italic"}]))
    again = parse_edits(edits_to_corrections_json(parsed.edits))
    assert len(again.edits) == 1
    a, b = parsed.edits[0], again.edits[0]
    assert (a.find, a.replace, a.source, a.context, a.instruction, a.occurrence,
            a.format) == (b.find, b.replace, b.source, b.context, b.instruction,
                          b.occurrence, b.format)
