"""The fixed lane's redline is always "<surname> - Book Two - Pre-Proofread.docx".

Unlike the legacy `Book 2` series, which mirrors the spelling the source
carried, the pre-proofread spells the stage out whatever came in — the press
asked for one name (2026-09-16). Recognition of the digit spelling is
unchanged, so an older hand-off is still an output and never a manuscript to
work on again.
"""
from __future__ import annotations

import pytest

from app.watch import naming


@pytest.mark.parametrize("stem,expected", [
    ("Grest - Book 1", "Grest - Book Two - Pre-Proofread.docx"),
    ("Grest - Book One", "Grest - Book Two - Pre-Proofread.docx"),
    ("Grest — book 1", "Grest - Book Two - Pre-Proofread.docx"),          # em dash, lower case
    ("Grest - Book-1", "Grest - Book Two - Pre-Proofread.docx"),          # dash inside the token
    ("Dalton 2 - Book 1", "Dalton 2 - Book Two - Pre-Proofread.docx"),    # the series number stays
    ("St Denis - Book One", "St Denis - Book Two - Pre-Proofread.docx"),
    ("Grest - Book Original", "Grest - Book Two - Pre-Proofread.docx"),   # any token in the series
    ("Grest - Book Two - Pre-Proofread", "Grest - Book Two - Pre-Proofread.docx"),  # idempotent
    ("Wolves", "Wolves - Book Two - Pre-Proofread.docx"),                 # no token: append
])
def test_the_pre_proofread_is_always_spelled_book_two(stem, expected):
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
