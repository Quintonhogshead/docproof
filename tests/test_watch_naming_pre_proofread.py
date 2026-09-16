"""The fixed lane's redline is always "<surname> - Book One - Pre-Proofread.docx".

Unlike the legacy `Book 2` series, which mirrors the spelling the source
carried, the pre-proofread spells one name whatever came in — the press asked
for that (2026-09-16), and named it after the book it redlines rather than the
next stage. That base is proofing's own INPUT token, so the tail is what makes
these files recognisable as output; a bare "<surname> - Book One" must stay the
manuscript proofing reads.
"""
from __future__ import annotations

import pytest

from app.watch import naming


@pytest.mark.parametrize("stem,expected", [
    ("Grest - Book 1", "Grest - Book One - Pre-Proofread.docx"),
    ("Grest - Book One", "Grest - Book One - Pre-Proofread.docx"),
    ("Grest — book 1", "Grest - Book One - Pre-Proofread.docx"),          # em dash, lower case
    ("Grest - Book-1", "Grest - Book One - Pre-Proofread.docx"),          # dash inside the token
    ("Dalton 2 - Book 1", "Dalton 2 - Book One - Pre-Proofread.docx"),    # the series number stays
    ("St Denis - Book One", "St Denis - Book One - Pre-Proofread.docx"),
    ("Grest - Book Original", "Grest - Book One - Pre-Proofread.docx"),   # any token in the series
    ("Grest - Book One - Pre-Proofread", "Grest - Book One - Pre-Proofread.docx"),  # idempotent
    ("Wolves", "Wolves - Book One - Pre-Proofread.docx"),                 # no token: append
])
def test_the_pre_proofread_is_always_spelled_book_one(stem, expected):
    assert naming.pre_proofread_name(stem + ".docx") == expected
    assert naming.pre_proofread_base(stem) == expected[: -len(" - Pre-Proofread.docx")]


def test_the_pre_proofread_and_its_companions_are_outputs():
    base = naming.pre_proofread_base("Grest - Book 1")
    for name in (naming.pre_proofread_name("Grest - Book 1.docx"),
                 f"{base}{naming.OUTCOME_SUFFIX}.json", f"{base}{naming.CLEAN_SUFFIX}.docx",
                 f"{base} - proofreading report.md"):
        assert naming.is_output_name(name), name
    # The archived verdict is this book's outcome whichever spelling was used.
    assert naming.is_proof_outcome_name(f"{base}{naming.OUTCOME_SUFFIX}.json", "Grest - Book 1")
    assert naming.is_proof_outcome_name("Grest - Book 2 - outcome.json", "Grest - Book 1")


def test_the_legacy_book_two_series_still_mirrors_the_source():
    assert naming.proof_base("Grest - Book 1") == "Grest - Book 2"
    assert naming.proof_base("Grest - Book One") == "Grest - Book Two"
    # An older hand-off, made before the rename, is still an output.
    assert naming.is_output_name("Grest - Book Two - Pre-Proofread.docx")


def test_the_dev_edited_book_is_still_proofings_input():
    """The rename puts the hand-off under proofing's own input token, so the
    bare name must not be swept up with it — otherwise no book is ever read."""
    assert not naming.is_output_name("Grest - Book One.docx")
    assert not naming.is_output_name("Grest - Book 1.docx")
    assert naming.has_proof_source_label("Grest - Book One.docx")
    assert naming.is_proof_source_name("Grest - Book One.docx", "Grest")
    # ...and the redline is not mistaken for a fresh manuscript to proofread.
    assert not naming.has_proof_source_label(
        naming.pre_proofread_name("Grest - Book One.docx"))
