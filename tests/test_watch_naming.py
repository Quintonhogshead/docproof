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
    ("Grest — Book Original", "Grest - book 0"),         # em dash separator
    ("Grest – Book Original", "Grest - book 0"),         # en dash separator
    ("Grest - Book - Original", "Grest - book 0"),       # stray dash in the token
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


# --- is_source_name -----------------------------------------------------------

@pytest.mark.parametrize("name,last,want", [
    ("Grest - Book Original.docx", "Grest", True),        # the intake file
    ("grest  -  book original.docx", "Grest", True),      # case & spacing drift
    ("Grest - Book Original.docx", "GREST", True),        # surname case drift
    ("St Denis - Book Original.docx", "St Denis", True),  # a spaced surname
    ("Grest — Book Original.docx", "Grest", True),        # em dash separator
    ("Grest – Book Original.docx", "Grest", True),        # en dash separator
    ("Grest - Book - Original.docx", "Grest", True),      # stray dash in the token
    # A co-author parenthetical on the record's surname is set aside, the same
    # forgiveness the HubSpot key match gives — the file carries only the first.
    ("Lichtenstein - Book Original.docx",
     "Lichtenstein (and Dolores DelBello)", True),
    ("Grest - book 0.docx", "Grest", False),              # the deliverable, not it
    ("Grest - Draft.docx", "Grest", False),              # a draft, not it
    ("Grest - Book Original.docx", "Smith", False),       # wrong surname
    # A fuller name than the surname is not a silent match: the token is there
    # but "Ada Grest" is not "Grest", so it is refused rather than guessed.
    ("Ada Grest - Book Original.docx", "Grest", False),
    ("Book Original.docx", "Grest", False),               # no surname in the name
    ("Grest - Book Original.docx", "", False),            # no surname to match
    ("Grest - Book Original.docx", "   ", False),         # blank surname
])
def test_is_source_name_matches_only_the_authors_intake_file(name, last, want):
    assert naming.is_source_name(name, last) is want


# --- has_source_label ---------------------------------------------------------

@pytest.mark.parametrize("name,want", [
    ("Grest - Book Original.docx", True),                 # the intake file
    ("Grest — Book Original", True),                       # em dash, no extension
    ("Grest - Book - Original.docx", True),               # stray dash in the token
    ("grest  -  book original.docx", True),               # case & spacing drift
    ("Developmental Editorial Review 1 Johnson", False),  # a review, not the book
    ("Grest - book 0.docx", False),                       # the deliverable
    ("Grest - Draft.docx", False),                        # a draft
    ("Wolves.docx", False),                               # no token at all
    ("Book Original.docx", False),                        # no surname before it
])
def test_has_source_label_knows_an_intake_file_without_a_surname(name, want):
    assert naming.has_source_label(name) is want
