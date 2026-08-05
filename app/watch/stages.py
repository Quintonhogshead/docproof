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
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from docproof.prep.convert import CONVERTIBLE

from .drive import DriveFile, GOOGLE_DOC_MIME

# What prep can read: Word, plus everything LibreOffice converts for it. Taken
# from prep itself so the two cannot drift — a format added there is watched
# for here on the next run.
MANUSCRIPT_SUFFIXES = (".docx",) + CONVERTIBLE

# The names DocProof gives what it writes. Properties are the real record; this
# is the belt for a file that lost them — somebody duplicated an output, or
# re-uploaded one from their Downloads folder — because preparing a prepared
# manuscript is the one mistake here that costs money.
OUTPUT_PREFIXES = ("tagged_", "tracked_", "reviewed_", "prep_notes",
                   "prep_failed")

# Written by the watcher onto the files it touches.
STATE_PROP = "docproof.state"
JOB_PROP = "docproof.job"
AT_PROP = "docproof.at"
REASON_PROP = "docproof.reason"
OUTPUT_PROP = "docproof.output"
SOURCE_PROP = "docproof.src"

FORMATTED = "formatted"
FAILED = "failed"


class Stage(Enum):
    """Where a file is in the pipeline, from the folder's point of view."""

    NEW_MANUSCRIPT = "new"      # version zero: prepare it
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
    if file.is_google_doc or _is_manuscript(file.name):
        return Stage.NEW_MANUSCRIPT
    return Stage.SKIP


def _looks_like_output(name: str) -> bool:
    return name.lower().startswith(OUTPUT_PREFIXES)


def _is_manuscript(name: str) -> bool:
    return Path(name).suffix.lower() in MANUSCRIPT_SUFFIXES
