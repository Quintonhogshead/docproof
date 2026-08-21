"""What DocProof calls the files it hands back.

The house convention is a stage series: an author's manuscript arrives as
"<surname> - Book Original", and the formatting stage returns "<surname> - book
0", with the tracked-changes copy and the notes beside it under the same base.

One place decides those names because two places have to agree on them: `prep`
writes them into the folder, and `stages.classify` has to know one when it sees
one — so a formatted book is never handed back to be formatted again. The
appProperties marker is still the real record; the name is the belt for a file
that lost it (a duplicate, or one re-uploaded out of Downloads).

Recognition is deliberately forgiving of the drift a real filename picks up on
its way through Word and Google Docs — the " - " separator autocorrected into an
en or em dash, a stray dash between "Book" and "Original", case, doubled spaces,
a co-author parenthetical on the surname —
because the alternative is a real "<surname> - Book Original" silently passed
over, or a draft beside it prepared by mistake. What it does *not* forgive is a
wrong surname or a missing token: a "Developmental Editorial Review" is not the
book, however it is spaced.

Proofing will add "book 1" and so on; the seam is `OUTPUT_STAGE` and the small
`format_base` transform, not a rule spread across the watcher.
"""
from __future__ import annotations

import re
import unicodedata
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

# The dashes a " - " separator turns up as in the wild: a plain hyphen-minus,
# the hyphen and non-breaking hyphen, the figure/en/em dashes, the horizontal
# bar, and the maths minus. Folded to a hyphen before any name is compared, so
# "Johnson — Book Original" (em dash, what an autocorrect made of it) is the same
# intake file as "Johnson - Book Original".
_DASH_CHARS = "-‐‑‒–—―−"
_DASH_RE = re.compile(f"[{_DASH_CHARS}]")

# What separates the source-stage words from each other. A space is the house
# spelling, but "Book - Original" (a stray dash a typist or an autocorrect put
# between them) is the same token, so any dash variant or whitespace run counts.
# `_SOURCE_RE` runs on a folded stem (dashes already hyphens) and
# `_SOURCE_TOKEN_RE` on a raw one (any dash), and this class covers both.
_STAGE_SEP = rf"\s*[{_DASH_CHARS}\s]\s*"

# The source stage words, spelled once so the two regexes below cannot drift
# from the constant. Flexible inner separator, because "Book  Original" (doubled
# space) and "Book - Original" (stray dash) are the same token; matched
# case-insensitively by the callers.
_SOURCE_WORDS = _STAGE_SEP.join(map(re.escape, SOURCE_STAGE.casefold().split()))

# The whole stem is "<surname> - Book Original", nothing after: the strict intake
# recognizer, run against an already-folded stem. A trailing draft/notes word is
# deliberately *not* a match — that is a different file.
_SOURCE_RE = re.compile(rf"^(?P<surname>.+?)\s*-\s*{_SOURCE_WORDS}\s*$")
# The " - Book Original" token anywhere in a raw stem, any dash: what
# `format_base` slices on, tolerant of case and dash so an em-dashed intake name
# still yields the right deliverable base.
_SOURCE_TOKEN_RE = re.compile(rf"[{_DASH_CHARS}]\s*{_SOURCE_WORDS}", re.IGNORECASE)
# A trailing co-author parenthetical on a surname — "Lichtenstein (and Dolores
# DelBello)" — dropped before two surnames are compared, so a filename that
# carries only the first author still matches the record that carries both. The
# same forgiveness `hubspot.name_matches` gives the CRM-side key.
_COAUTHOR_RE = re.compile(r"\s*\(.*\)\s*$")


def _fold(text: str) -> str:
    """A name reduced to what a house-convention comparison should care about:
    Unicode-normalised, every dash variant made a hyphen, whitespace runs
    collapsed to one space, and case folded."""
    text = unicodedata.normalize("NFC", text)
    text = _DASH_RE.sub("-", text)
    return " ".join(text.split()).casefold()


def _surname_key(text: str) -> str:
    """A surname reduced to what an author-identity comparison cares about:
    folded, with any trailing co-author parenthetical set aside."""
    return _COAUTHOR_RE.sub("", _fold(text))


def _source_surname(name: str) -> str | None:
    """The surname a "<surname> - Book Original" filename carries, folded, or
    `None` if the name is not an intake file at all. The single reader both
    `has_source_label` and `is_source_name` are built on, so "what counts as the
    Book Original" has one answer."""
    match = _SOURCE_RE.match(_fold(Path(name).stem))
    return match.group("surname").strip() if match else None


def format_base(stem: str) -> str:
    """The formatting deliverable's base name for a manuscript.

    "Smith - Book Original" -> "Smith - book 0". A name that does not carry the
    source stage token keeps its whole stem and has the output stage appended,
    so every file still lands under one predictable base. The last token wins
    (so a surname that echoes it is kept whole), and the dash may be any of the
    variants an autocorrect leaves behind."""
    matches = list(_SOURCE_TOKEN_RE.finditer(stem))
    author = stem[:matches[-1].start()].rstrip() if matches else stem
    return f"{author} - {OUTPUT_STAGE}"


def has_source_label(name: str) -> bool:
    """Whether a filename carries the house intake token "- Book Original" at
    all, with no reference to any surname.

    What the flat path gates on when `require_source_label` is set: a file that
    is not a "<something> - Book Original" is not the book to prepare, so a
    developmental review or a questionnaire dropped in the folder is left alone
    rather than formatted. Dash-, case- and spacing-tolerant, so an em-dashed
    "Johnson — Book Original" still counts."""
    return _source_surname(name) is not None


def is_source_name(name: str, last: str) -> bool:
    """Whether a filename is the intake manuscript for a given surname:
    "<surname> - Book Original".

    The mirror of `is_output_name` for the other end of the series. Used to hold
    the watcher to the house convention — only "<surname> - Book Original" is the
    book to prepare, so a draft or a developmental copy dropped in the same
    folder is left alone. Case-, spacing- and dash-insensitive, and a co-author
    parenthetical on the record's surname is set aside, so "Lichtenstein - Book
    Original" is the book for a record stored "Lichtenstein (and Dolores
    DelBello)". A blank surname matches nothing, because it would otherwise match
    every stem that merely ends in the stage token; a wrong surname is refused."""
    key = _surname_key(last)
    if not key:
        return False
    surname = _source_surname(name)
    return surname is not None and _surname_key(surname) == key


def is_output_name(name: str) -> bool:
    """Whether a filename is one the formatting stage wrote.

    The stage token — " - book 0" — is the tell: an author manuscript is
    "...- Book Original", never "...- book 0", so a file whose stem carries the
    output token is DocProof's own, marker or no marker. It catches the
    deliverable and its "- tracked changes" and "- notes" companions alike,
    because all three share the base. Left an exact-token check on purpose:
    DocProof writes these names itself, always with a plain hyphen, so loosening
    it would only risk misreading a real manuscript as an output and skipping
    it."""
    return f" - {OUTPUT_STAGE}".lower() in Path(name).stem.lower()


__all__ = ["SOURCE_STAGE", "OUTPUT_STAGE", "TRACKED_SUFFIX", "NOTES_SUFFIX",
           "INDESIGN_SUFFIX", "format_base", "has_source_label",
           "is_output_name", "is_source_name"]
