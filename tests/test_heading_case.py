"""The heading pass (DP-006): chapter titles and section headings — the names
of the book's paragraphs — set in Chicago title case, folded into DocProof's
job as word-level tracked changes.
"""
from __future__ import annotations

import pytest

from docproof.config import Config
from docproof.models import ParagraphRef
from docproof.sweeps import apply_hits, heading_case_findings, title_case_hits


def cased(text: str) -> str:
    return apply_hits(text, title_case_hits(text))


# --- the caser itself ---------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    # The report's own evidence.
    ("the shape of things to come", "The Shape of Things to Come"),
    ("did we get here", "Did We Get Here"),
    # Minor words stay down mid-title, up at the edges.
    ("a river in the dark", "A River in the Dark"),
    ("what we talk about", "What We Talk About"),
    # A stray capitalized minor word is drift, and comes down.
    ("The Shape Of Things", "The Shape of Things"),
    # A colon opens a subtitle; the word after it takes a capital.
    ("the fall: a beginning", "The Fall: A Beginning"),
    # Small roman numerals are set as numerals, wherever they stand.
    ("part ii", "Part II"),
    # Hyphenated compounds capitalize their parts.
    ("the well-made bed", "The Well-Made Bed"),
])
def test_title_case(before, after):
    assert cased(before) == after


@pytest.mark.parametrize("text", [
    "CHAPTER ONE",                    # all-caps is a styling choice
    "Chapter 12",                     # already right; digits carry no case
    "McCoy and EVTOL Rising",         # internal capitals are the author's
    "The Shape of Things to Come",    # already correct: zero hits
])
def test_headings_left_alone(text):
    assert title_case_hits(text) == []


def test_soft_broken_headings_case_each_line():
    assert cased("the storm\nafter the storm") == \
        "The Storm\nAfter the Storm"


def test_the_caser_is_idempotent():
    once = cased("the shape of things to come: part ii")
    assert cased(once) == once


# --- the pass over the document ------------------------------------------------

def _para(pid, text, style):
    return ParagraphRef(pid, "word/document.xml", "body", text, style,
                        reviewable=style == "Normal")


def test_only_heading_styled_paragraphs_are_touched():
    ps = [_para("body-0000", "the shape of things to come", "Heading1"),
          _para("body-0001", "the road went on and nobody spoke.", "Normal")]
    findings, report = heading_case_findings(ps, Config().skip)
    assert findings, "the heading produced no findings"
    assert {f.para_id for f in findings} == {"body-0000"}
    assert all(f.error_type == "heading_case" for f in findings)
    assert report.key == "heading_case"
    assert report.flagged == len(findings)
    assert report.remaining == 0


def test_word_level_findings_apply_through_the_validator():
    from docproof.models import DocumentModel
    from docproof.validator import validate_findings

    ps = (_para("body-0000", "the shape of things to come", "Heading1"),)
    doc = DocumentModel(source_path="x.docx", paragraphs=ps)
    findings, _report = heading_case_findings(list(ps), Config().skip)
    validated = validate_findings(findings, doc, "medium")
    assert all(f.status == "validated" for f in validated)
    # Each edit is one word's capital — never a wholesale retype of the line.
    assert all(len(f.anchor.delete_text) <= 5 for f in validated)
