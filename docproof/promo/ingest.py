"""Reading a finished manuscript for promo: the whole book as plain text.

Promo only ever reads. Prep refuses a document with unresolved tracked changes
because it restyles every paragraph and will not do that on top of edits nobody
has decided about; promo has no such stake — it takes the text as it stands and
asks a model to write about it — so it opens files prep would turn away. It
reuses prep's paragraph walker for the extraction (same tested code that reads
every paragraph, blanks and headings included) and wraps it in a tolerant
preflight that checks the file is a real .docx and nothing more.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..ingest import IngestError, OLE_MAGIC, ZIP_MAGIC
from ..prep import ingest as prep_ingest
from ..prep.ingest import BODY_PART
from ..utils.xml_helpers import DocxPackage

log = logging.getLogger("docproof.promo.ingest")


@dataclass(frozen=True)
class Manuscript:
    source_path: str
    # The press names the file, e.g. "Rowan - book 1"; that stem is the reliable
    # identifier and what the output documents are named after.
    title: str
    # The whole book, one paragraph per line, blank lines dropped.
    text: str
    word_count: int


def read_manuscript(path: str | Path) -> Manuscript:
    """Open a manuscript, or refuse it with a sentence someone can act on."""
    pkg = _preflight(path)
    structure = prep_ingest.build_structure(pkg)
    lines = [p.text.strip() for p in structure.paragraphs if p.text.strip()]
    text = "\n".join(lines)
    title = Path(path).stem or "manuscript"
    log.info("Read %d words for promo from %s.", structure.word_count,
             Path(path).name)
    return Manuscript(source_path=str(path), title=title, text=text,
                      word_count=structure.word_count)


def _preflight(path: str | Path) -> DocxPackage:
    """The same shape as prep's preflight, minus the tracked-changes refusal:
    promo reads, it does not rewrite, so revisions in the file are no obstacle."""
    path = Path(path)
    if not path.exists():
        raise IngestError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix != ".docx":
        raise IngestError(
            f"Promo reads Word .docx files; {path.name} is a "
            f"{suffix or 'file'} with no extension. Convert it first.")

    with path.open("rb") as fh:
        head = fh.read(4)
    if head == OLE_MAGIC:
        raise IngestError(
            f"{path.name} is a legacy .doc file or a password-protected .docx. "
            f"Open it in Word, remove any password, and Save As .docx.")
    if head != ZIP_MAGIC:
        raise IngestError(
            f"{path.name} is not a .docx file (bad file signature).")

    try:
        pkg = DocxPackage(path)
    except Exception as e:                    # noqa: BLE001 - zipfile variants
        raise IngestError(f"{path.name} is corrupt: {e}") from e

    for required in ("[Content_Types].xml", BODY_PART):
        if not pkg.has(required):
            raise IngestError(
                f"{path.name} is missing {required}; not a valid .docx.")
    return pkg
