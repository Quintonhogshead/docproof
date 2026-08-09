"""The house stage-series names, and knowing one when we see it.

`format_base` turns an author's manuscript name into the formatting
deliverable's base; `is_output_name` is the belt `classify` leans on so a file
DocProof wrote is never handed back to be written again."""
from __future__ import annotations

import pytest

from app.watch import naming


# --- format_base --------------------------------------------------------------

@pytest.mark.parametrize("stem,base", [
    ("Grest - Book Original", "Grest - book 0"),
    ("St Denis - Book Original", "St Denis - book 0"),
    ("Grest - book original", "Grest - book 0"),        # case-insensitive token
    ("Wolves", "Wolves - book 0"),                       # no token: append
    ("Draft 3-4", "Draft 3-4 - book 0"),
])
def test_format_base_moves_the_manuscript_to_the_formatting_stage(stem, base):
    assert naming.format_base(stem) == base


def test_only_the_stage_token_is_replaced_not_a_surname_that_echoes_it():
    """A surname is kept whole; only the trailing " - Book Original" goes."""
    assert naming.format_base("Book - Book Original") == "Book - book 0"


# --- is_output_name -----------------------------------------------------------

@pytest.mark.parametrize("name,want", [
    ("Grest - book 0.docx", True),                       # the deliverable
    ("Grest - book 0 - tracked changes.docx", True),     # the redline
    ("Grest - book 0 - notes.md", True),                 # the notes
    ("Grest - Book Original.docx", False),               # the author's file
    ("Wolves.docx", False),
    ("A Book of Hours.docx", False),                     # "book" alone is not it
])
def test_is_output_name_knows_the_stage_token(name, want):
    assert naming.is_output_name(name) is want
