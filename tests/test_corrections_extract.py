"""Extracting a corrections edit list from the two sources buildable without a
sample PDF: an author's redlined Word file (deterministic) and a free-form list
read by a model (with a fake provider — no vendor is touched).

Both feed the same deterministic core, so each test ends where the engine does:
an anchored edit list that applies to the real InDesign fixture.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import docx
import pytest

from docproof.corrections.apply import apply_edits
from docproof.corrections.extract import ExtractionError, extract_edits
from docproof.corrections.from_word import edits_from_docx
from docproof.corrections.idml import parse_story
from docproof.corrections.model import DESIGN, JUDGMENT, MECHANICAL
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from .conftest import FIXTURES
from .fakes import FakeProvider

LAYOUT = FIXTURES / "layout.idml"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def make_tracked_docx(path: Path, paragraphs: list[list[tuple[str, str]]]) -> Path:
    """Write a .docx whose paragraphs carry tracked changes. Each paragraph is a
    list of (mode, text) segments: mode "" is plain text, "ins" a tracked
    insertion, "del" a tracked deletion."""
    d = docx.Document()
    for segs in paragraphs:
        p = d.add_paragraph()
        for mode, text in segs:
            run = p.add_run(text)
            if mode in ("ins", "del"):
                wrap = run._r.makeelement(_w(mode), {
                    _w("id"): "1", _w("author"): "Author",
                    _w("date"): "2026-01-01T00:00:00Z"})
                run._r.addprevious(wrap)
                if mode == "del":
                    t = run._r.find(_w("t"))
                    if t is not None:
                        t.tag = _w("delText")
                wrap.append(run._r)
    d.save(str(path))
    return path


def story_text(idml: Path, story_id: str = "ue0") -> list[str]:
    with zipfile.ZipFile(idml) as z:
        s = parse_story(z.read(f"Stories/Story_{story_id}.xml"), story_id)
    return [p.text for p in s.paragraphs]


# --- reading a redlined Word file ---------------------------------------------

def test_a_replacement_becomes_one_anchored_edit(tmp_path):
    doc = make_tracked_docx(tmp_path / "r.docx", [
        [("del", "Their"), ("ins", "There"), ("", " were three ships.")]])
    result = edits_from_docx(doc)
    assert result.ok and len(result.edits) == 1
    e = result.edits[0]
    # The anchor is widened past the bare "ir"/"re" so it locates uniquely.
    assert "Their" in e.find and "There" in e.replace
    assert e.kind == MECHANICAL


def test_a_deletion_removes_the_phrase(tmp_path):
    """A mid-paragraph deletion anchors with context on both sides, so it is a
    replacement whose net effect drops the phrase — the removed words are in the
    find and gone from the replace."""
    doc = make_tracked_docx(tmp_path / "d.docx", [
        [("", "A third paragraph with plain text"),
         ("del", " for good measure"), ("", ".")]])
    result = edits_from_docx(doc)
    assert result.ok and len(result.edits) == 1
    e = result.edits[0]
    assert "for good measure" in e.find and "for good measure" not in e.replace


def test_unchanged_paragraphs_produce_no_edits(tmp_path):
    doc = make_tracked_docx(tmp_path / "u.docx", [
        [("", "Nothing was touched here.")],
        [("del", "Their"), ("ins", "There"), ("", " it is.")]])
    result = edits_from_docx(doc)
    assert len(result.edits) == 1        # only the changed paragraph


def test_a_wholly_inserted_paragraph_is_flagged_not_guessed(tmp_path):
    doc = make_tracked_docx(tmp_path / "i.docx", [
        [("ins", "A brand new paragraph the author added.")]])
    result = edits_from_docx(doc)
    assert not result.edits
    assert result.issues and "anchor" in result.issues[0].reason


def test_a_redline_applies_to_the_real_indesign_file(tmp_path):
    """End to end: a Word redline of the book's text extracts to an edit that
    anchors and applies to the InDesign fixture, and verifies clean."""
    # layout.idml paragraph 4 reads "Their were several mistakes here to find."
    doc = make_tracked_docx(tmp_path / "book.docx", [
        [("del", "Their"), ("ins", "There"),
         ("", " were several mistakes here to find.")]])
    result = edits_from_docx(doc)
    assert result.ok and len(result.edits) == 1

    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, list(result.edits))
    assert report.applied == 1
    assert story_text(out)[4] == "There were several mistakes here to find."


# --- reading a free-form list with a model ------------------------------------

def _canned(edits: list[dict]) -> FakeProvider:
    return FakeProvider([ProviderResult(
        parsed={"edits": edits},
        usage=NormalizedUsage(input_tokens=200, output_tokens=40))])


def test_a_prose_list_is_extracted_into_edits():
    provider = _canned([
        {"find": "Their were", "replace": "There were", "instruction": "",
         "kind": "mechanical", "occurrence": 0},
        {"find": "Chapter One", "replace": "", "instruction": "move to a recto",
         "kind": "design", "occurrence": 0}])
    usage = Usage()
    result = extract_edits("...an author's notes...", provider,
                           model="claude-haiku-4-5", usage=usage)
    assert result.ok and len(result.edits) == 2
    assert result.edits[0].find == "Their were"
    assert result.edits[1].kind == DESIGN
    # The source text and the corrections schema went to the model, and its
    # tokens were counted.
    assert provider.calls[0]["user"].startswith("...an author")
    assert usage.output_tokens == 40


def test_the_extractor_reuses_the_parse_contract_for_bad_entries():
    """A model that returns an unanchorable entry has it flagged, not dropped —
    the same 'flag, never guess' contract a typed list gets."""
    provider = _canned([
        {"find": "Their were", "replace": "There were", "instruction": "",
         "kind": "mechanical", "occurrence": 0},
        {"find": "", "replace": "orphan", "instruction": "", "kind": "mechanical",
         "occurrence": 0}])
    result = extract_edits("notes", provider, model="m", usage=Usage())
    assert len(result.edits) == 1
    assert result.issues and "no find text" in result.issues[0].reason


def test_a_refusal_or_truncation_raises():
    provider = FakeProvider([ProviderResult(parsed=None, stop_reason="refusal")])
    with pytest.raises(ExtractionError):
        extract_edits("notes", provider, model="m", usage=Usage())


def test_an_empty_source_makes_no_model_call():
    provider = FakeProvider([])          # would raise if a call popped nothing
    result = extract_edits("   ", provider, model="m", usage=Usage())
    assert result.ok and not result.edits
    assert not provider.calls


def test_extracted_edits_apply_to_the_real_indesign_file(tmp_path):
    provider = _canned([
        {"find": "Their were", "replace": "There were", "instruction": "",
         "kind": "mechanical", "occurrence": 0}])
    result = extract_edits("change 'Their were' to 'There were'", provider,
                           model="m", usage=Usage())
    out = tmp_path / "out.idml"
    report = apply_edits(LAYOUT, out, list(result.edits))
    assert report.applied == 1
    assert story_text(out)[4] == "There were several mistakes here to find."
