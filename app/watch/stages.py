"""What each file in the watched folder is, and therefore what to do with it.

One pure function over one file. Everything the watcher decides about a
manuscript is decided here, so the question "why did it not touch that?" has a
single answer that can be read and tested without a network.

This is also the seam. Today the folder holds version-zero manuscripts and the
files DocProof wrote beside them, so the interesting answer is
`NEW_MANUSCRIPT`. Copy editing arrives with one more: a manuscript that has
been through developmental edits, recognised by the subfolder an editor drops
it into, and later by asking HubSpot what stage the book is at. Both are a
branch in `classify`, not a change anywhere else.

The `is_*_candidate` functions beside `classify` are the other shape a stage
takes. `classify` answers "what is this file" once, for the formatting pass that
owns `STATE_PROP`; promo, the marketing plan and proofing each run over the same
book on their own marker, so each asks its own question and none of them may
read another's "done".
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from docproof.formats.base import DocumentFormat
from docproof.prep.convert import CONVERTIBLE

from .drive import DriveFile, GOOGLE_DOC_MIME
from .naming import (has_proof_source_label, has_source_label, is_output_name)

# What prep can read: Word, plus everything LibreOffice converts for it. Taken
# from prep itself so the two cannot drift — a format added there is watched
# for here on the next run.
MANUSCRIPT_SUFFIXES = (".docx",) + CONVERTIBLE

# The names DocProof gives what it writes. Properties are the real record; this
# is the belt for a file that lost them — somebody duplicated an output, or
# re-uploaded one from their Downloads folder — because preparing a prepared
# manuscript is the one mistake here that costs money.
OUTPUT_PREFIXES = ("book_", "tagged_", "tracked_", "reviewed_", "prep_notes",
                   "prep_failed")

# Review outputs are named by suffix rather than prefix, because that is what
# the press hands to an author: "<book> - Atmosphere Press Proofreader.docx"
# sorts next to the book. Taken from the format so the two cannot drift.
#
# The legacy " - Pre-Proofread" names are kept here too: books proofread before
# the rename already sit in Drive under the old suffix, and the watcher must
# still recognise them as finished output — otherwise a rename would make every
# one of them look like a fresh manuscript and reprocess it, which is exactly
# the costly mistake this recognition exists to prevent.
_LEGACY_STEM_SUFFIXES = (" - pre-proofread", " - pre-proofread change log")
OUTPUT_STEM_SUFFIXES = tuple(
    s.lower() for s in (DocumentFormat.REVIEWED_SUFFIX,
                        DocumentFormat.CHANGE_LOG_SUFFIX)) + _LEGACY_STEM_SUFFIXES

# Written by the watcher onto the files it touches.
STATE_PROP = "docproof.state"
JOB_PROP = "docproof.job"
AT_PROP = "docproof.at"
REASON_PROP = "docproof.reason"
OUTPUT_PROP = "docproof.output"
SOURCE_PROP = "docproof.src"
# Promo's own marker, deliberately separate from STATE_PROP: a book can be
# formatted and not yet promo'd, or the other way round, so the two lifecycles
# must not share one "done" flag. "pending" is written when a hold-mode run has
# generated copy that is waiting for a person; "done" once it is delivered.
PROMO_PROP = "docproof.promo"
# The marketing plan's own marker, separate again for the same reason: a book
# can be formatted, promo'd, and planned in any combination, so each lifecycle
# owns its own "done". "pending" waits for panel approval in hold mode; "done"
# once the plan is in the folder and HubSpot has moved on.
PLAN_PROP = "docproof.plan"
# Proofing's own marker, separate again for the same reason: formatting and
# proofing are two passes over one manuscript, gated on two values of the same
# dropdown, so neither may read the other's "done". Unlike the others this one
# has a non-terminal value — "awaiting", written when an external practitioner
# has the book and the watcher is waiting for the hand-off files to appear.
PROOF_PROP = "docproof.proof"

FORMATTED = "formatted"
FAILED = "failed"
PROMO_PENDING = "pending"
PROMO_DONE = "done"
PROMO_FAILED = "failed"
PLAN_PENDING = "pending"
PLAN_DONE = "done"
PLAN_FAILED = "failed"
# Proofing's marker values. "awaiting" is deliberately NOT terminal: a book an
# external practitioner is holding must stay a candidate, or the tick that
# finds its outcome.json would never look at it again.
PROOF_AWAITING = "awaiting"
PROOF_DONE = "done"
# The book needs a human proofreader. Terminal for DocProof — it will not be
# re-run and re-charged — and deliberately NOT a HubSpot write: the record stays
# at "Ready for Proofing", which is what tells a person to pick it up.
PROOF_HUMAN = "human"
PROOF_FAILED = "failed"
PROOF_TERMINAL = (PROOF_DONE, PROOF_HUMAN, PROOF_FAILED)


class Stage(Enum):
    """Where a file is in the pipeline, from the folder's point of view."""

    NEW_MANUSCRIPT = "new"      # version zero: prepare it
    PROOF_MANUSCRIPT = "proof"  # the dev-edited "Book 1": proofing's input
    OUTPUT = "output"           # DocProof wrote this; never an input
    DONE = "done"               # already prepared
    FAILED = "failed"           # tried, and needs a person before trying again
    SKIP = "skip"               # not a manuscript at all


def classify(file: DriveFile) -> Stage:
    """What this file is. Order matters: a marker beats a name, and a name
    beats a guess from the extension."""
    props = file.app_properties
    if props.get(OUTPUT_PROP):
        return Stage.OUTPUT
    state = props.get(STATE_PROP, "")
    if state == FORMATTED:
        return Stage.DONE
    if state == FAILED:
        return Stage.FAILED
    if file.is_folder:
        return Stage.SKIP
    if _looks_like_output(file.name):
        return Stage.OUTPUT
    # The dev-edited book is a manuscript, but not the formatting stage's. It
    # gets its own answer rather than `NEW_MANUSCRIPT` so `run_prep` — which
    # prepares every NEW_MANUSCRIPT in its listing — can never format one, and
    # rather than `SKIP`, which would tell a person reading the pass report that
    # a real book is "not a manuscript". Proofing asks `is_proof_candidate`,
    # which is where its own markers are read.
    if has_proof_source_label(file.name):
        return Stage.PROOF_MANUSCRIPT
    if file.is_google_doc or _is_manuscript(file.name):
        return Stage.NEW_MANUSCRIPT
    return Stage.SKIP


def is_promo_candidate(file: DriveFile) -> bool:
    """Whether promo should consider this file — a manuscript it has not already
    written copy for.

    Deliberately blind to the formatting marker (`STATE_PROP`): a book that has
    been formatted is still a book promo can write about, and the two stages
    gate on different HubSpot values anyway. What it does exclude is anything
    promo already touched (`PROMO_PROP`, whatever its value), anything DocProof
    wrote, and anything that is not a manuscript. The HubSpot gate is the real
    control over which of these actually runs; this only keeps outputs and
    finished books out of the running."""
    if file.is_folder:
        return False
    props = file.app_properties
    if props.get(OUTPUT_PROP) or props.get(PROMO_PROP):
        return False
    if _looks_like_output(file.name):
        return False
    return file.is_google_doc or _is_manuscript(file.name)


def is_plan_candidate(file: DriveFile) -> bool:
    """Whether the marketing-plan stage should consider this file — a manuscript
    it has not already written a plan for.

    The promo twin of `is_promo_candidate`, and blind to the formatting and promo
    markers for the same reason: a book that has been formatted or promo'd is
    still a book a marketing plan can be written from, and the three stages gate
    on different HubSpot values. It excludes only what the plan stage itself
    already touched (`PLAN_PROP`, whatever its value), anything DocProof wrote,
    and anything that is not a manuscript. The HubSpot gate is the real control
    over which of these actually runs."""
    if file.is_folder:
        return False
    props = file.app_properties
    if props.get(OUTPUT_PROP) or props.get(PLAN_PROP):
        return False
    if _looks_like_output(file.name):
        return False
    return file.is_google_doc or _is_manuscript(file.name)


def is_proof_candidate(file: DriveFile) -> bool:
    """Whether the proofing stage should consider this file — the dev-edited
    "<surname> - Book 1" it has not already finished with.

    Proofing's input is a *name*, not a guess: the book it reads is the one the
    developmental editors handed back, and the house calls that `Book 1`. So a
    draft, a questionnaire, or the author's original beside it in the same
    folder is left alone rather than read at a novel's price.

    Blind to the formatting marker (`STATE_PROP`) on purpose, exactly as promo
    and the plan are: the two stages gate on different values of the same
    HubSpot dropdown, which is the real control over which of them runs.

    What it does exclude is anything DocProof wrote (the marker, or a "- book 0"
    / "- Book 2" name) and anything proofing has finished with — `PROOF_PROP` at
    a *terminal* value. "awaiting" is not terminal: an external practitioner is
    holding that book, and the tick that finds its outcome.json has to be able
    to see the manuscript to apply it."""
    if file.is_folder:
        return False
    props = file.app_properties
    if props.get(OUTPUT_PROP):
        return False
    if props.get(PROOF_PROP) in PROOF_TERMINAL:
        return False
    if _looks_like_output(file.name):
        return False
    return has_proof_source_label(file.name)


def _looks_like_output(name: str) -> bool:
    lowered = name.lower()
    return (lowered.startswith(OUTPUT_PREFIXES)
            or Path(lowered).stem.endswith(OUTPUT_STEM_SUFFIXES)
            or is_output_name(name))


def _is_manuscript(name: str) -> bool:
    if Path(name).suffix.lower() in MANUSCRIPT_SUFFIXES:
        return True
    # A file that arrives with no extension at all — a Word doc or a Google Doc
    # someone renamed to "<surname> - Book Original" and dropped the ".docx" —
    # is still the intake manuscript when it carries the house label. The token
    # is specific enough to trust on its own: an output is "- book 0" or
    # "- Book 2", never "- Book Original", and `classify` has already ruled
    # outputs — and the dev-edited "- Book 1" — out before it
    # asks this. Without it such a file was silently skipped until a person
    # re-added the extension by hand.
    return not Path(name).suffix and has_source_label(name)
