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

# The two companions to the InDesign-ready deliverable, under the same base.
TRACKED_SUFFIX = " - tracked changes"
NOTES_SUFFIX = " - notes"


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


def is_output_name(name: str) -> bool:
    """Whether a filename is one the formatting stage wrote.

    The stage token — " - book 0" — is the tell: an author manuscript is
    "...- Book Original", never "...- book 0", so a file whose stem carries the
    output token is DocProof's own, marker or no marker. It catches the
    deliverable and its "- tracked changes" and "- notes" companions alike,
    because all three share the base."""
    return f" - {OUTPUT_STAGE}".lower() in Path(name).stem.lower()


__all__ = ["SOURCE_STAGE", "OUTPUT_STAGE", "TRACKED_SUFFIX", "NOTES_SUFFIX",
           "format_base", "is_output_name"]
