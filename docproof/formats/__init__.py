"""Format dispatch, by file extension."""
from __future__ import annotations

from pathlib import Path

from ..ingest import IngestError
from .base import DocumentFormat
from . import docx as _docx
from .idml import ingest as _idml_ingest
from .idml import reassembler as _idml_reassembler

DOCX = DocumentFormat(
    suffix=".docx",
    name="Word",
    kind="Word document",
    app="Word",
    review_instructions=(
        "Open the reviewed .docx in Word → **Review** tab → walk the changes "
        "with Accept/Reject."),
    where_to_look=("In Word, open the Review tab and step through the changes "
                   "with Accept and Reject."),
    comment_noun="margin comment",
    preflight=_docx.preflight,
    build_document_model=_docx.build_document_model,
    apply_tracked_changes=_docx.apply_tracked_changes,
    normalize=_docx.normalize,
    snapshot=_docx.snapshot,
    annotate_excluded_words=_docx.annotate_excluded_words,
)

IDML = DocumentFormat(
    suffix=".idml",
    name="InDesign",
    kind="InDesign layout",
    app="InDesign",
    review_instructions=(
        "Open the reviewed .idml in InDesign → **Window → Editorial → Track "
        "Changes** (or open the Story Editor) → walk the changes with "
        "Accept/Reject."),
    where_to_look=("In InDesign, open Window → Editorial → Track Changes, or "
                   "the Story Editor, to step through the changes."),
    comment_noun="margin note",
    preflight=_idml_ingest.preflight,
    build_document_model=_idml_ingest.build_document_model,
    apply_tracked_changes=_idml_reassembler.apply_tracked_changes,
)

FORMATS: dict[str, DocumentFormat] = {DOCX.suffix: DOCX, IDML.suffix: IDML}
SUFFIXES: tuple[str, ...] = tuple(FORMATS)


def get_format(path: str | Path) -> DocumentFormat:
    """The format for a path, or an IngestError naming what docproof does read.

    Dispatching on the extension rather than sniffing the bytes is deliberate:
    both formats are zips, so the extension is the only honest signal of what
    the user meant, and each format's preflight verifies the guess immediately.
    """
    fmt = FORMATS.get(Path(path).suffix.lower())
    if fmt is None:
        readable = " or ".join(f"{f.suffix} ({f.kind})" for f in FORMATS.values())
        raise IngestError(
            f"{Path(path).name} is not a kind of file docproof can review. "
            f"It reads {readable}.")
    return fmt


def describe() -> list[dict]:
    return [f.to_api() for f in FORMATS.values()]
