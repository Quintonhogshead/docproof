"""What DocProof calls the files it hands back.

The house convention is a stage series: an author's manuscript arrives as
"<surname> - Book Original", and the formatting stage returns "<surname> - book
0", with the tracked-changes copy and the notes beside it under the same base.

One place decides those names because two places have to agree on them: `prep`
writes them into the folder, and `stages.classify` has to know one when it sees
one — so a formatted book is never handed back to be formatted again. The
appProperties marker is still the real record; the name is the belt for a file
that lost it (a duplicate, or one re-uploaded out of Downloads).

Proofing will add "book 1" and so on; the seam is `OUTPUT_STAGE` and the small
`format_base` transform, not a rule spread across the watcher.
"""
from __future__ import annotations

from pathlib import Path

# The stage token an author's manuscript arrives carrying, and the one the
# formatting stage stamps on what it hands back. Compared case-insensitively.
SOURCE_STAGE = "Book Original"
OUTPUT_STAGE = "book 0"

# The companions to the primary deliverable, under the same base.
TRACKED_SUFFIX = " - tracked changes"
NOTES_SUFFIX = " - notes"
# When the book-styled reading copy is the deliverable, the InDesign-ready
# file (if also asked for) sits beside it under this suffix.
INDESIGN_SUFFIX = " - indesign"


def format_base(stem: str) -> str:
    """The formatting deliverable's base name for a manuscript.

    "Smith - Book Original" -> "Smith - book 0". A name that does not carry the
    source stage token keeps its whole stem and has the output stage appended,
    so every file still lands under one predictable base."""
    author = stem
    marker = f" - {SOURCE_STAGE}".lower()
    idx = stem.lower().rfind(marker)
    if idx != -1:
        author = stem[:idx]
    return f"{author} - {OUTPUT_STAGE}"


def is_source_name(name: str, last: str) -> bool:
    """Whether a filename is the intake manuscript for a given surname:
    "<surname> - Book Original".

    The mirror of `is_output_name` for the other end of the series. Used to hold
    the watcher to the house convention — only "<surname> - Book Original" is the
    book to prepare, so a draft or a developmental copy dropped in the same
    folder is left alone. Compared case- and whitespace-insensitively, so
    "johnson  -  book original" and "Johnson - Book Original" are one name; a
    blank surname matches nothing, because it would otherwise match every stem
    that merely ends in the stage token."""
    if not last.strip():
        return False
    stem = " ".join(Path(name).stem.split()).casefold()
    wanted = " ".join(f"{last} - {SOURCE_STAGE}".split()).casefold()
    return stem == wanted


def is_output_name(name: str) -> bool:
    """Whether a filename is one the formatting stage wrote.

    The stage token — " - book 0" — is the tell: an author manuscript is
    "...- Book Original", never "...- book 0", so a file whose stem carries the
    output token is DocProof's own, marker or no marker. It catches the
    deliverable and its "- tracked changes" and "- notes" companions alike,
    because all three share the base."""
    return f" - {OUTPUT_STAGE}".lower() in Path(name).stem.lower()


__all__ = ["SOURCE_STAGE", "OUTPUT_STAGE", "TRACKED_SUFFIX", "NOTES_SUFFIX",
           "format_base", "is_output_name", "is_source_name"]
