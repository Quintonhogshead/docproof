"""Ingest: which paragraphs the model reviews, which the sweeps reach, and
which nobody touches — decided by paragraph style.

The distinction that matters for recall: a chapter heading is set text the
model has no business rewriting, but it still carries mechanical fixes a sweep
can make ("Chapter Twenty Four" needs a hyphen). So a heading is swept, not
modelled. A table of contents, which regenerates from the headings, is left
alone entirely.
"""
from __future__ import annotations

import docx

import pytest

from docproof.config import load_config
from docproof.ingest import IngestError, build_document_model, preflight
from docproof.sweeps import run_sweeps
from docproof.utils.xml_helpers import DocxPackage

CONFIG = "config/default.yaml"
BODY = "A body paragraph long enough for the model to actually review it."


def _model(tmp_path, *paras):
    """paras: (text, style-name) pairs -> a built DocumentModel."""
    d = docx.Document()
    for text, style in paras:
        d.add_paragraph(text, style=style)
    path = tmp_path / "doc.docx"
    d.save(path)
    return build_document_model(DocxPackage(path), load_config(CONFIG))


def test_a_heading_is_swept_but_not_modelled(tmp_path):
    doc = _model(tmp_path, ("Chapter Twenty Four", "Heading 1"), (BODY, "Normal"))
    by_text = {p.text: p for p in doc.paragraphs}
    # Present (so the sweeps see it) but not reviewable (so the model does not).
    assert by_text["Chapter Twenty Four"].reviewable is False
    assert by_text[BODY].reviewable is True


def test_a_title_is_skipped_entirely(tmp_path):
    doc = _model(tmp_path, ("The Grand Title", "Title"), (BODY, "Normal"))
    assert "The Grand Title" not in {p.text for p in doc.paragraphs}
    assert any(reason == "style:Title" for _, reason in doc.skipped)


def test_a_heading_now_gets_its_sweep_fix(tmp_path):
    """The whole point of the change: the compound-number fix a chapter heading
    carries is now made. Before, the heading was skipped and it was unreachable."""
    doc = _model(tmp_path, ("Chapter Thirty One", "Heading 1"))
    sweep_only = [p for p in doc.paragraphs if not p.reviewable]
    findings, _ = run_sweeps(sweep_only, ["sweep_compound_number"])
    assert [f.corrected_text for f in findings] == ["Chapter Thirty-One"]


def test_skip_config_classifies_styles():
    skip = load_config(CONFIG).skip
    assert skip.is_sweep_only("Heading1") and skip.is_sweep_only("Heading2")
    assert skip.fully_skipped("Title") and skip.fully_skipped("TOC1")
    # Heading moved out of the full-skip list into sweep-only.
    assert not skip.fully_skipped("Heading1")
    assert not skip.is_sweep_only("Normal")


# --- accepting tracked changes the file arrived with ---------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REV = {f"{{{_W}}}id": "1", f"{{{_W}}}author": "A",
        f"{{{_W}}}date": "2026-01-01T00:00:00Z"}


def _mark(p, kind):
    """Give paragraph `p` a tracked paragraph mark of `kind` (del/ins)."""
    rpr = p._p.get_or_add_pPr().makeelement(f"{{{_W}}}rPr", {})
    rpr.append(rpr.makeelement(f"{{{_W}}}{kind}", dict(_REV)))
    p._p.get_or_add_pPr().append(rpr)


def _tracked_doc(tmp_path, cell_revision=False):
    d = docx.Document()
    d.add_paragraph("Chapter One", style="Heading 1")
    first = d.add_paragraph("The first half ")
    _mark(first, "del")
    second = d.add_paragraph("and the second half.", style="Quote")
    added = d.add_paragraph()
    run = added.add_run("A new paragraph.")
    ins = run._r.makeelement(f"{{{_W}}}ins", dict(_REV))
    run._r.addprevious(ins)
    ins.append(run._r)
    _mark(added, "ins")
    d.add_paragraph("The end.")
    if cell_revision:
        cell = d.add_table(rows=1, cols=1).cell(0, 0)
        tcpr = cell._tc.get_or_add_tcPr()
        tcpr.append(tcpr.makeelement(f"{{{_W}}}cellIns", dict(_REV)))
    path = tmp_path / "tracked.docx"
    d.save(path)
    return path


def test_accept_all_first_joins_a_deleted_paragraph_mark(tmp_path):
    """Accepting a deleted paragraph mark is what Word does with it: the
    paragraph joins the one after it, which keeps its own style."""
    pkg = preflight(_tracked_doc(tmp_path), "accept_all_first")
    doc = build_document_model(pkg, load_config(CONFIG))
    texts = [p.text for p in doc.paragraphs]
    assert "The first half and the second half." in texts
    assert "The first half " not in texts
    assert "A new paragraph." in texts
    joined = next(p for p in doc.paragraphs
                  if p.text == "The first half and the second half.")
    assert joined.style == "Quote"
    assert pkg.tree("word/document.xml").find(f".//{{{_W}}}ins") is None
    assert pkg.tree("word/document.xml").find(f".//{{{_W}}}del") is None


def test_accept_all_first_still_refuses_a_cell_revision(tmp_path):
    with pytest.raises(IngestError, match="cellIns"):
        preflight(_tracked_doc(tmp_path, cell_revision=True), "accept_all_first")


def test_abort_policy_names_the_part(tmp_path):
    with pytest.raises(IngestError, match="word/document.xml"):
        preflight(_tracked_doc(tmp_path), "abort")
