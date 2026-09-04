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


# --- the proofing half of the series ------------------------------------------
#
# Proofing reads "<surname> - Book 1" (the developmental edit, done by people)
# and writes "<surname> - Book 2". `Book 1` is an INPUT: it must never be an
# output name, or proofing would be hidden from its own source.

@pytest.mark.parametrize("stem,base", [
    ("Grest - Book 1", "Grest - Book 2"),
    ("Grest - book 1", "Grest - Book 2"),               # case-insensitive token
    ("Grest — Book 1", "Grest - Book 2"),               # em dash separator
    ("Grest - Book-1", "Grest - Book 2"),               # dash inside the token
    ("Grest  -  Book  1", "Grest - Book 2"),            # doubled spaces
    ("St Denis - Book 1", "St Denis - Book 2"),
    # Any stage in the series converts, so a run driven at an odd file never
    # produces "Grest - Book Original - Book 2".
    ("Grest - Book Original", "Grest - Book 2"),
    ("Grest - book 0", "Grest - Book 2"),
    ("Grest - Book 2", "Grest - Book 2"),               # idempotent
    ("Wolves", "Wolves - Book 2"),                      # no token: append
])
def test_proof_base_moves_the_manuscript_to_the_proofing_stage(stem, base):
    assert naming.proof_base(stem) == base


@pytest.mark.parametrize("name,want", [
    ("Grest - Book 2.docx", True),                      # the deliverable
    ("Grest - Book 2 - letter.md", True),
    ("Grest - Book 2 - style-sheet.md", True),
    ("Grest - Book 2 - decision-log.md", True),
    ("Grest - Book 2 - outcome.json", True),
    ("Grest — Book 2.docx", True),                      # em dash, from a Mac
    ("grest - book 2 - letter.md", True),               # case drift
    ("Grest - Book-2.docx", True),                      # dash inside the token
    # The one that matters: Book 1 is proofing's INPUT, not an output.
    ("Grest - Book 1.docx", False),
    ("Grest — book 1.docx", False),
    ("Grest - Book 20.docx", False),                    # a different number
    ("Grest - Book 21 - letter.md", False),
])
def test_is_output_name_claims_book_2_and_never_book_1(name, want):
    assert naming.is_output_name(name) is want


@pytest.mark.parametrize("name,last,want", [
    ("Grest - Book 1.docx", "Grest", True),
    ("grest  -  book  1.docx", "Grest", True),          # case & spacing drift
    ("Grest — Book 1.docx", "Grest", True),             # em dash separator
    ("Grest - Book-1.docx", "Grest", True),             # dash inside the token
    ("St Denis - Book 1.docx", "St Denis", True),       # a spaced surname
    ("Lichtenstein - Book 1.docx",
     "Lichtenstein (and Dolores DelBello)", True),      # co-author parenthetical
    ("Grest - Book 2.docx", "Grest", False),            # the deliverable
    ("Grest - Book Original.docx", "Grest", False),     # the author's own file
    ("Grest - Book 1.docx", "Smith", False),            # wrong surname
    ("Ada Grest - Book 1.docx", "Grest", False),        # a fuller name is not it
    ("Book 1.docx", "Grest", False),                    # no surname in the name
    ("Grest - Book 1.docx", "", False),                 # no surname to match
])
def test_is_proof_source_name_matches_only_that_authors_dev_edit(name, last,
                                                                 want):
    assert naming.is_proof_source_name(name, last) is want


@pytest.mark.parametrize("name,want", [
    ("Grest - Book 1.docx", True),
    ("Grest — Book 1", True),                           # em dash, no extension
    ("grest  -  book 1.docx", True),                    # case & spacing drift
    ("Grest - Book Original.docx", False),              # a different stage
    ("Grest - Book 2.docx", False),
    ("Grest - Book 12.docx", False),                    # a different number
    ("Developmental Editorial Review 1 Johnson", False),
    ("Book 1.docx", False),                             # no surname before it
])
def test_has_proof_source_label_knows_the_dev_edit_without_a_surname(name, want):
    assert naming.has_proof_source_label(name) is want


def test_the_hand_off_names_all_hang_off_one_base():
    """One transform, so the five names DocWatch looks for and the five
    `galley/driver.py` writes cannot drift apart."""
    base = naming.proof_base("Johnson - Book 1")
    assert base == "Johnson - Book 2"
    assert naming.proof_outcome_name("Johnson - Book 1.docx") == \
        f"{base}{naming.OUTCOME_SUFFIX}.json"
    for suffix in (naming.LETTER_SUFFIX, naming.STYLE_SHEET_SUFFIX,
                   naming.DECISION_LOG_SUFFIX, naming.OUTCOME_SUFFIX):
        assert naming.is_output_name(f"{base}{suffix}.md")
