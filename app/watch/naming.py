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

The press writes the numbered stages both ways, so both are read: `Book 1` and
`Book One` are the same file, and so are `Book 2` and `Book Two`. What is
written back MIRRORS what came in — a `Book One` is proofread into a `Book Two`,
a `Book 1` into a `Book 2` — so a folder keeps one house style per book rather
than DocProof imposing its own halfway through the series. With nothing to
mirror, the digit is the default. See `SPELLINGS` and `stage_base`.

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

A fourth stage is two more constants and one more entry in `STAGE_TOKENS` (plus
one in `SPELLINGS` if it is numbered), not a rule spread across the watcher.
"""
from __future__ import annotations

import re
from pathlib import Path

from .names import flatten_accents

# The stage tokens, read side and write side. Compared case-insensitively, and
# tolerant of every dash and spacing variant (see `_fold` and `_token_re`).
SOURCE_STAGE = "Book Original"           # formatting reads
OUTPUT_STAGE = "book 0"                  # formatting writes
PROOF_SOURCE_STAGE = "Book 1"            # proofing reads (the dev-edited book)
PROOF_STAGE = "Book 2"                   # proofing writes

# The numbered stages come in two spellings, because the press writes both:
# "Johnson - Book 1" and "Johnson - Book One" are the same book. Both are
# recognised; the FIRST of each pair is what DocProof writes when it has no
# reason to prefer the other.
#
# The pairs are parallel on purpose. `stage_base` remembers which spelling the
# source carried and hands back the same index on the way out, so a `Book One`
# comes back as `Book Two` and a `Book 1` as `Book 2` — the folder keeps one
# house style per book instead of DocProof imposing its own halfway through.
# Anything with no spelling to mirror (a `Book Original`, a bare title) falls to
# index 0, which is why digits are the default.
SPELLINGS = {
    PROOF_SOURCE_STAGE: (PROOF_SOURCE_STAGE, "Book One"),
    PROOF_STAGE: (PROOF_STAGE, "Book Two"),
}

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
VERIFICATION_SUFFIX = " - verification"
OUTCOME_SUFFIX = " - outcome"
# The reading copy of the proofread: the tracked-changes file with every
# change accepted and every comment removed, derived at hand-off from the
# certified file (see `docproof/cleancopy.py`). Proofing's primary stays the
# redline — the record the author accepts or rejects — so this is the
# companion, the mirror of prep's `- tracked changes` beside a clean primary.
CLEAN_SUFFIX = " - clean"

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


def spellings_of(stage: str) -> tuple[str, ...]:
    """Every spelling of one stage token, the house one first. A stage with only
    one spelling is its own single-entry tuple, so callers never branch."""
    return SPELLINGS.get(stage, (stage,))


def _words(spelling: str) -> str:
    """One spelling as a pattern: its words in order, with any dash or
    whitespace run between them. Built from the constant, so the recognizers
    below cannot drift from the name DocProof actually writes."""
    return _STAGE_SEP.join(map(re.escape, spelling.casefold().split()))


def _tail_guard(spelling: str) -> str:
    """What may not follow a stage token, so one stage cannot claim another's
    name: a digit after "Book 1" (which would swallow "Book 12"), a letter after
    "Book Two" (which would swallow "Book Twosome"). Read off the spelling's own
    last character rather than hardcoded, so a stage added later is guarded by
    the same rule without anyone remembering to."""
    return r"(?![0-9])" if spelling[-1].isdigit() else r"(?![0-9a-z])"


def _strict_re(spelling: str) -> re.Pattern:
    """The whole stem is "<surname> - <spelling>", nothing after: the strict
    intake recognizer, run against an already-folded stem. A trailing draft or
    notes word is deliberately *not* a match — that is a different file."""
    return re.compile(
        rf"^(?P<surname>.+?)\s*-\s*{_words(spelling)}{_tail_guard(spelling)}\s*$")


def _token_re(spelling: str) -> re.Pattern:
    """The " - <spelling>" token anywhere in a stem, any dash, any case: what the
    base transforms slice on and `is_output_name` searches for, so an em-dashed
    or oddly-spaced name still resolves to the right base."""
    return re.compile(
        rf"[{_DASH_CHARS}]\s*{_words(spelling)}{_tail_guard(spelling)}",
        re.IGNORECASE)


# One regex per SPELLING, not per stage, because which spelling matched is
# itself the answer `stage_base` needs to mirror it on the way out.
_STRICT = {stage: tuple(_strict_re(sp) for sp in spellings_of(stage))
           for stage in STAGE_TOKENS}
_TOKEN = {stage: tuple(_token_re(sp) for sp in spellings_of(stage))
          for stage in STAGE_TOKENS}


def _fold(text: str) -> str:
    """A name reduced to what a house-convention comparison should care about:
    Accents removed, every dash variant made a hyphen, whitespace runs
    collapsed to one space, and case folded."""
    text = flatten_accents(text)
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
    this stage reads" has one answer per stage — and every spelling of that
    stage is that same answer, so "Johnson - Book One" is Johnson's Book 1."""
    stem = _fold(Path(name).stem)
    for pattern in _STRICT[stage]:
        match = pattern.match(stem)
        if match:
            return match.group("surname").strip()
    return None


def _split_stage(stem: str) -> tuple[str, int]:
    """Where the author's name ends, and which spelling the stage token used.

    The LAST stage token in the stem wins — any token in the series, so every
    `*_base` transform is idempotent and a name at one stage converts to any
    other. The index is the spelling's position in its stage's `SPELLINGS`
    tuple, which is what lets the transform answer in the same style it was
    asked in. A stem carrying no token at all is all author, index 0."""
    best: tuple[int, int] | None = None
    for token in STAGE_TOKENS:
        for index, pattern in enumerate(_TOKEN[token]):
            for match in pattern.finditer(stem):
                if best is None or match.start() > best[0]:
                    best = (match.start(), index)
    if best is None:
        return stem, 0
    return stem[:best[0]].rstrip(), best[1]


def stage_base(stem: str, stage: str) -> str:
    """One stage's deliverable base name for a manuscript.

    "Smith - Book Original" -> "Smith - book 0" (formatting); "Smith - Book 1"
    -> "Smith - Book 2" and "Smith - Book One" -> "Smith - Book Two"
    (proofing). The output mirrors the spelling the input carried, so a folder
    keeps one house style per book; a stem with no spelling to mirror falls to
    the stage's first spelling, which is why digits are the default."""
    author, index = _split_stage(stem)
    variants = spellings_of(stage)
    return f"{author} - {variants[index] if index < len(variants) else variants[0]}"


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
    an external practitioner's tooling) rather than written by DocProof.

    EITHER spelling of the output stage counts, whichever the source used. The
    mirroring in `stage_base` is what DocProof writes; this is what it will
    accept, and the two differ on purpose — a practitioner who typed
    "Book 2 - outcome.json" for a "Book One" source has still answered, and a
    book must not sit unread because of a house-style disagreement."""
    author, _index = _split_stage(Path(source_stem).stem)
    folded = _fold(name)
    return any(folded == _fold(f"{author} - {spelling}{OUTCOME_SUFFIX}.json")
               for spelling in spellings_of(PROOF_STAGE))


def has_source_label(name: str) -> bool:
    """Whether a filename carries the house intake token "- Book Original" at
    all, with no reference to any surname.

    What the flat path gates on when `require_source_label` is set: a file that
    is not a "<something> - Book Original" is not the book to prepare, so a
    developmental review or a questionnaire dropped in the folder is left alone
    rather than formatted. Dash-, case- and spacing-tolerant, so an em-dashed
    "Johnson — Book Original" still counts."""
    return _stage_surname(name, SOURCE_STAGE) is not None


def source_surname(name: str) -> str:
    """The author part of a "<surname> - Book Original" filename as it was
    typed — "Johnson", or "Lichtenstein (and Dolores DelBello)" — or "" when
    the name is not an intake file at all. For the by-name intake, where the
    surname comes from the file rather than from a HubSpot record."""
    if not has_source_label(name):
        return ""
    return _split_stage(Path(name).stem)[0].strip()


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


# --- The designer's IDML series -------------------------------------------------
#
# Interior corrections read a different series from the manuscripts above: the
# designer exports "<surname> - Book 3.idml", and each round of the author's
# corrections goes to the highest-numbered export in the "Interior Design"
# folder. DocProof hands back the half-step — "Book 3.5" — and the designer's
# next export is "Book 4". So an integer version is an input and a fractional
# one is DocProof's own output, which is the whole recogniser.
IDML_SUFFIX = ".idml"
CORRECTIONS_STEP = 0.5
_IDML_VERSION_RE = re.compile(
    rf"^(?P<author>.+?){_STAGE_SEP}(?P<word>book)(?P<gap>\s*)"
    rf"(?P<version>\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def idml_version(name: str) -> tuple[str, float] | None:
    """`(author, version)` for a "<surname> - Book N.idml" export, or None.

    Only `.idml` files count; any dash and either spacing of the token ("Book 3"
    or "Book3") are forgiven, as everywhere else here. The version is numeric so
    "Book 10" sorts after "Book 9" and "Book 3.5" sits between 3 and 4."""
    path = Path(name)
    if path.suffix.lower() != IDML_SUFFIX:
        return None
    m = _IDML_VERSION_RE.match(_fold(path.stem))
    if not m:
        return None
    try:
        return m.group("author").strip(), float(m.group("version"))
    except ValueError:
        return None


def is_idml_source_name(name: str, last: str) -> bool:
    """Whether this is one of the author's designer exports — an integer
    "<surname> - Book N.idml" whose surname is the record's. A fractional
    version is DocProof's own hand-off and never an input."""
    parsed = idml_version(name)
    if parsed is None:
        return False
    author, version = parsed
    return (version == int(version)
            and _surname_key(author) == _surname_key(last))


def is_idml_output_name(name: str) -> bool:
    """Whether this is a corrections hand-off — a fractional "Book N.5.idml",
    or one of its companions under the same base."""
    stem = Path(name).stem
    for suffix in (CORRECTIONS_SHEET_SUFFIX, NOTES_SUFFIX, CHECKS_SUFFIX):
        if stem.lower().endswith(suffix.lower()):
            stem = stem[:-len(suffix)]
            break
    parsed = idml_version(stem + IDML_SUFFIX)
    return parsed is not None and parsed[1] != int(parsed[1])


def corrections_base(source_name: str) -> str:
    """The hand-off base for a designer export: "Johnson - Book 3.idml" ->
    "Johnson - Book 3.5". The spacing of the token mirrors the source ("Book3"
    stays "Book3.5") so the pair sorts together in the folder."""
    stem = Path(source_name).stem
    m = _IDML_VERSION_RE.match(stem)
    if not m:
        raise ValueError(f"{source_name!r} is not a '<surname> - Book N.idml'")
    version = float(m.group("version"))
    if version != int(version):
        raise ValueError(f"{source_name!r} is already a corrections hand-off")
    # Everything up to the version, verbatim, then the half-step.
    head = stem[:m.start("version")]
    return f"{head}{int(version)}.5"


# The companions to the corrected IDML, under the "<surname> - Book N.5" base.
CORRECTIONS_SHEET_SUFFIX = " - corrections"
CHECKS_SUFFIX = " - checks"


def corrections_hand_off_names(source_name: str) -> dict[str, str]:
    """What the corrections stage puts in the folder, by role."""
    base = corrections_base(source_name)
    return {
        "idml": f"{base}{IDML_SUFFIX}",
        "sheet": f"{base}{CORRECTIONS_SHEET_SUFFIX}.xlsx",
        "notes": f"{base}{NOTES_SUFFIX}.md",
        "checks": f"{base}{CHECKS_SUFFIX}.jsx",
    }


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
    return any(pattern.search(stem) for stage in OUTPUT_STAGES
               for pattern in _TOKEN[stage])


__all__ = ["CHECKS_SUFFIX", "CLEAN_SUFFIX", "CORRECTIONS_SHEET_SUFFIX",
           "CORRECTIONS_STEP", "IDML_SUFFIX",
           "corrections_base", "corrections_hand_off_names", "idml_version",
           "is_idml_output_name", "is_idml_source_name",
           "DECISION_LOG_SUFFIX", "INDESIGN_SUFFIX",
           "LETTER_SUFFIX",
           "NOTES_SUFFIX", "OUTCOME_SUFFIX", "OUTPUT_STAGE", "OUTPUT_STAGES",
           "PROOF_SOURCE_STAGE", "PROOF_STAGE", "SOURCE_STAGE", "SPELLINGS",
           "STAGE_TOKENS", "STYLE_SHEET_SUFFIX", "TRACKED_SUFFIX",
           "VERIFICATION_SUFFIX",
           "spellings_of",
           "format_base", "has_proof_source_label", "has_source_label",
           "source_surname",
           "is_output_name", "is_proof_outcome_name", "is_proof_source_name",
           "is_source_name", "proof_base", "proof_outcome_name", "stage_base"]
