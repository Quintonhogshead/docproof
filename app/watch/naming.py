"""What DocProof calls the files it hands back.

The house convention is a stage series, and each stage reads the file the one
before it left:

    <surname> - Book Original    what the author sends
    <surname> - book 0           formatting hands back
    <surname> - Book 1           the developmental edit, done by people
    <surname> - Book 2           proofing (Galley) hands back

So a stage has two tokens, not one: the name it reads and the name it writes.
Formatting reads `Book Original` and writes `book 0`; proofing reads `Book 1`
and writes `Book 2`. `Book 1` is an *input* — it is not something DocProof
wrote, and treating it as an output would hide proofing's own source from it.

One place decides those names because several places have to agree on them:
`prep` and `proof` write them into the folder, `stages.classify` has to know one
when it sees one — so a formatted book is never handed back to be formatted
again — and `galley/driver.py` builds the practitioner's hand-off from the same
transform. The appProperties marker is still the real record; the name is the
belt for a file that lost it (a duplicate, or one re-uploaded out of Downloads).

Recognition is deliberately forgiving of the drift a real filename picks up on
its way through Word and Google Docs — the " - " separator autocorrected into an
en or em dash, a stray dash between "Book" and "Original", case, doubled spaces,
a co-author parenthetical on the surname — because the alternative is a real
"<surname> - Book Original" silently passed over, or a draft beside it prepared
by mistake. What it does *not* forgive is a wrong surname or a missing token: a
"Developmental Editorial Review" is not the book, however it is spaced.

A fourth stage is two more constants and one more entry in `STAGE_TOKENS`, not a
rule spread across the watcher.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# The stage tokens, read side and write side. Compared case-insensitively, and
# tolerant of every dash and spacing variant (see `_fold` and `_token_re`).
SOURCE_STAGE = "Book Original"           # formatting reads
OUTPUT_STAGE = "book 0"                  # formatting writes
PROOF_SOURCE_STAGE = "Book 1"            # proofing reads (the dev-edited book)
PROOF_STAGE = "Book 2"                   # proofing writes

# Every token DocProof *writes*. `is_output_name` is the one reader: a stem
# carrying any of these is something DocProof produced, never a file to work on
# again. `Book 1` is deliberately absent — it is proofing's input, and promo's —
# and listing it here would hide proofing's source from proofing.
OUTPUT_STAGES = (OUTPUT_STAGE, PROOF_STAGE)

# Every token in the series, read side and write side alike. Used only to find
# where an author's name ends and the stage begins, so every `*_base` transform
# is idempotent and a name at one stage converts to any other.
STAGE_TOKENS = (SOURCE_STAGE, OUTPUT_STAGE, PROOF_SOURCE_STAGE, PROOF_STAGE)

# The companions to the primary deliverable, under the same base.
TRACKED_SUFFIX = " - tracked changes"
NOTES_SUFFIX = " - notes"
# When the book-styled reading copy is the deliverable, the InDesign-ready
# file (if also asked for) sits beside it under this suffix.
INDESIGN_SUFFIX = " - indesign"
# Proofing's companions, under the "<surname> - Book 2" base: the editorial
# letter, the style sheet, the decision log, and the machine-readable verdict
# the watcher reads to decide whether to move HubSpot on. See
# `galley/outcome.py` and `galley/journal.py`.
LETTER_SUFFIX = " - letter"
STYLE_SHEET_SUFFIX = " - style-sheet"
DECISION_LOG_SUFFIX = " - decision-log"
OUTCOME_SUFFIX = " - outcome"

# The dashes a " - " separator turns up as in the wild: a plain hyphen-minus,
# the hyphen and non-breaking hyphen, the figure/en/em dashes, the horizontal
# bar, and the maths minus. Folded to a hyphen before any name is compared, so
# "Johnson — Book Original" (em dash, what an autocorrect made of it) is the same
# intake file as "Johnson - Book Original".
_DASH_CHARS = "-‐‑‒–—―−"
_DASH_RE = re.compile(f"[{_DASH_CHARS}]")

# What separates the words of a stage token from each other. A space is the
# house spelling, but "Book - Original" (a stray dash a typist or an autocorrect
# put between them) is the same token, and so is "Book-1", so any dash variant
# or whitespace run counts.
_STAGE_SEP = rf"\s*[{_DASH_CHARS}\s]\s*"

# A trailing co-author parenthetical on a surname — "Lichtenstein (and Dolores
# DelBello)" — dropped before two surnames are compared, so a filename that
# carries only the first author still matches the record that carries both. The
# same forgiveness `hubspot.name_matches` gives the CRM-side key.
_COAUTHOR_RE = re.compile(r"\s*\(.*\)\s*$")


def _words(stage: str) -> str:
    """A stage token as a pattern: its words in order, with any dash or
    whitespace run between them. Built from the constant, so the recognizers
    below cannot drift from the name DocProof actually writes."""
    return _STAGE_SEP.join(map(re.escape, stage.casefold().split()))


def _strict_re(stage: str) -> re.Pattern:
    """The whole stem is "<surname> - <stage>", nothing after: the strict intake
    recognizer, run against an already-folded stem. A trailing draft/notes word
    is deliberately *not* a match — that is a different file. The digit guard
    keeps "Book 1" from claiming "Book 12"."""
    return re.compile(rf"^(?P<surname>.+?)\s*-\s*{_words(stage)}(?![0-9])\s*$")


def _token_re(stage: str) -> re.Pattern:
    """The " - <stage>" token anywhere in a stem, any dash, any case: what the
    base transforms slice on and `is_output_name` searches for, so an em-dashed
    or oddly-spaced name still resolves to the right base."""
    return re.compile(rf"[{_DASH_CHARS}]\s*{_words(stage)}(?![0-9])",
                      re.IGNORECASE)


_STRICT = {stage: _strict_re(stage) for stage in STAGE_TOKENS}
_TOKEN = {stage: _token_re(stage) for stage in STAGE_TOKENS}


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


def _stage_surname(name: str, stage: str) -> str | None:
    """The surname a "<surname> - <stage>" filename carries, folded, or `None`
    if the name is not that stage's intake file at all. The single reader every
    `has_*_label` / `is_*_name` pair is built on, so "what counts as the file
    this stage reads" has one answer per stage."""
    match = _STRICT[stage].match(_fold(Path(name).stem))
    return match.group("surname").strip() if match else None


def stage_base(stem: str, stage: str) -> str:
    """One stage's deliverable base name for a manuscript.

    "Smith - Book Original" -> "Smith - book 0" (formatting); "Smith - Book 1"
    -> "Smith - Book 2" (proofing). The author's name is whatever precedes the
    LAST stage token in the stem — any token in the series, so every transform
    is idempotent and a name at one stage converts to any other. A name carrying
    no token at all keeps its whole stem and has the stage appended, so every
    file still lands under one predictable base."""
    cut = max((m.start() for token in STAGE_TOKENS
               for m in _TOKEN[token].finditer(stem)), default=None)
    author = stem[:cut].rstrip() if cut is not None else stem
    return f"{author} - {stage}"


def format_base(stem: str) -> str:
    """The formatting deliverable's base name — "Smith - book 0"."""
    return stage_base(stem, OUTPUT_STAGE)


def proof_base(stem: str) -> str:
    """The proofing deliverable's base name — "Smith - Book 2".

    The one place the proofread hand-off names are built from, so the five files
    DocWatch expects (`<base>.docx`, `<base> - letter.md`,
    `<base> - style-sheet.md`, `<base> - decision-log.md`,
    `<base> - outcome.json`) cannot drift from what it looks for.
    `galley/driver.py` builds the practitioner side of the hand-off from this
    same function."""
    return stage_base(stem, PROOF_STAGE)


def proof_outcome_name(stem: str) -> str:
    """What the proofread outcome file is called for a manuscript —
    "Smith - Book 1.docx" -> "Smith - Book 2 - outcome.json"."""
    return f"{proof_base(stem)}{OUTCOME_SUFFIX}.json"


def is_proof_outcome_name(name: str, source_stem: str) -> bool:
    """Whether a filename is the proofread outcome for a given manuscript.

    Folded the same way every other comparison here is — dash variants, case
    and doubled spaces forgiven — because this name may be typed (or dropped by
    an external practitioner's tooling) rather than written by DocProof."""
    return _fold(name) == _fold(proof_outcome_name(source_stem))


def has_source_label(name: str) -> bool:
    """Whether a filename carries the house intake token "- Book Original" at
    all, with no reference to any surname.

    What the flat path gates on when `require_source_label` is set: a file that
    is not a "<something> - Book Original" is not the book to prepare, so a
    developmental review or a questionnaire dropped in the folder is left alone
    rather than formatted. Dash-, case- and spacing-tolerant, so an em-dashed
    "Johnson — Book Original" still counts."""
    return _stage_surname(name, SOURCE_STAGE) is not None


def has_proof_source_label(name: str) -> bool:
    """The proofing twin: whether a filename carries the "- Book 1" token — the
    developmental-edited manuscript, which is what a proofread reads.

    This is what makes proofing's input a *name* rather than a guess, and it is
    also what keeps a Book 1 out of the formatting stage: `stages.classify`
    answers `PROOF_MANUSCRIPT` for one, never `NEW_MANUSCRIPT`."""
    return _stage_surname(name, PROOF_SOURCE_STAGE) is not None


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
    return _is_stage_name(name, last, SOURCE_STAGE)


def is_proof_source_name(name: str, last: str) -> bool:
    """The proofing twin: whether a filename is "<surname> - Book 1", the
    dev-edited manuscript for a given surname. Same forgiveness, and the same
    refusal of a wrong or blank surname."""
    return _is_stage_name(name, last, PROOF_SOURCE_STAGE)


def _is_stage_name(name: str, last: str, stage: str) -> bool:
    key = _surname_key(last)
    if not key:
        return False
    surname = _stage_surname(name, stage)
    return surname is not None and _surname_key(surname) == key


def is_output_name(name: str) -> bool:
    """Whether a filename is one a DocProof stage wrote.

    The written stage token — " - book 0" for formatting, " - Book 2" for
    proofing — is the tell: a file whose stem carries either is DocProof's own,
    marker or no marker. It catches each deliverable and its companions alike
    ("- tracked changes", "- notes", "- letter", "- style-sheet",
    "- decision-log", "- outcome"), because they all share the base.

    Matched with the same folded, dash- and case-tolerant recognizer the intake
    names use rather than an exact token: the proofing hand-off may be written
    by the practitioner loop on somebody's Mac rather than by DocProof itself,
    and a file that came back em-dashed must still be recognised as an output
    instead of being read as a fresh manuscript and worked on again.

    " - Book 1" is deliberately NOT one of these. It is the dev-edited book —
    proofing's input, and promo's — so calling it an output would hide it from
    the stage whose whole job is to read it."""
    stem = _fold(Path(name).stem)
    return any(_TOKEN[stage].search(stem) for stage in OUTPUT_STAGES)


__all__ = ["DECISION_LOG_SUFFIX", "INDESIGN_SUFFIX", "LETTER_SUFFIX",
           "NOTES_SUFFIX", "OUTCOME_SUFFIX", "OUTPUT_STAGE", "OUTPUT_STAGES",
           "PROOF_SOURCE_STAGE", "PROOF_STAGE", "SOURCE_STAGE", "STAGE_TOKENS",
           "STYLE_SHEET_SUFFIX", "TRACKED_SUFFIX",
           "format_base", "has_proof_source_label", "has_source_label",
           "is_output_name", "is_proof_outcome_name", "is_proof_source_name",
           "is_source_name", "proof_base", "proof_outcome_name", "stage_base"]
