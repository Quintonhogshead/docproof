from __future__ import annotations

import fnmatch
import logging
import zipfile
from pathlib import Path

from .config import Config
from .models import DocumentModel, ParagraphRef
from .utils.xml_helpers import DocxPackage, paragraph_text, qn, walk_package

log = logging.getLogger("docproof.ingest")

OLE_MAGIC = b"\xd0\xcf\x11\xe0"
ZIP_MAGIC = b"PK\x03\x04"

REVISION_TAGS = {qn(t) for t in (
    "w:ins", "w:del",
    "w:moveFrom", "w:moveTo",
    "w:moveFromRangeStart", "w:moveFromRangeEnd",
    "w:moveToRangeStart", "w:moveToRangeEnd",
    "w:rPrChange", "w:pPrChange", "w:sectPrChange",
    "w:tblPrChange", "w:tblGridChange", "w:tcPrChange", "w:trPrChange",
    "w:cellIns", "w:cellDel", "w:cellMerge", "w:numberingChange",
)}

# accept_all_first knows how to resolve these; anything else aborts.
_UNWRAP = {qn("w:ins"), qn("w:moveTo")}                 # keep the content
_REMOVE = {qn("w:del"), qn("w:moveFrom"),               # drop content / markers
           qn("w:moveFromRangeStart"), qn("w:moveFromRangeEnd"),
           qn("w:moveToRangeStart"), qn("w:moveToRangeEnd"),
           qn("w:rPrChange"), qn("w:pPrChange"), qn("w:sectPrChange"),
           qn("w:tblPrChange"), qn("w:tblGridChange"),
           qn("w:tcPrChange"), qn("w:trPrChange"), qn("w:numberingChange")}


class IngestError(Exception):
    """Raised for problems the user must fix. Message is user-facing."""


def preflight(path: str | Path, policy: str) -> DocxPackage:
    path = Path(path)
    if not path.exists():
        raise IngestError(f"File not found: {path}")

    with path.open("rb") as fh:
        head = fh.read(4)
    if head == OLE_MAGIC:
        raise IngestError(
            f"{path.name} is a legacy .doc file or a password-protected .docx. "
            "Open it in Word, remove any password, and Save As .docx, then rerun.")
    if head != ZIP_MAGIC:
        raise IngestError(f"{path.name} is not a .docx file (bad file signature).")

    try:
        pkg = DocxPackage(path)
    except zipfile.BadZipFile as e:
        raise IngestError(f"{path.name} is corrupt: {e}") from e

    for required in ("[Content_Types].xml", "word/document.xml"):
        if not pkg.has(required):
            raise IngestError(f"{path.name} is missing {required}; not a valid .docx.")

    parts = list(dict.fromkeys(wp.part for wp in walk_package(pkg)))
    found: dict[str, set] = {}
    for part in parts:
        tags = {el.tag for el in pkg.tree(part).iter() if el.tag in REVISION_TAGS}
        if tags:
            found[part] = tags

    if found:
        pretty = ", ".join(sorted(found))
        if policy == "abort":
            raise IngestError(
                f"Document already contains tracked changes ({pretty}). "
                "Accept or reject them in Word first, or rerun with "
                "tracked_changes_policy: accept_all_first | ignore.")
        if policy == "ignore":
            log.warning(
                "Existing tracked changes present in %s — proceeding per policy. "
                "Canonical text uses the accepted view; new edits near existing "
                "revisions may nest. Review the output carefully.", pretty)
        else:  # accept_all_first
            _accept_all(pkg, found)
            log.info("Accepted all existing tracked changes in: %s", pretty)

    return pkg


def _accept_all(pkg: DocxPackage, found: dict[str, set]) -> None:
    unsupported = {t for tags in found.values() for t in tags} - _UNWRAP - _REMOVE
    if unsupported:
        names = sorted(t.split('}')[1] for t in unsupported)
        raise IngestError(
            f"accept_all_first can't resolve revision types: {names}. "
            "Accept the changes in Word instead, then rerun.")
    for part in found:
        tree = pkg.tree(part)
        # Materialize before mutating; process removals/unwraps repeatedly
        # until fixed point (wrappers can nest).
        changed = True
        while changed:
            changed = False
            for el in list(tree.iter()):
                if el.tag in _UNWRAP:
                    parent, i = el.getparent(), el.getparent().index(el)
                    for child in list(el):
                        parent.insert(i, child)
                        i += 1
                    parent.remove(el)
                    changed = True
                elif el.tag in _REMOVE:
                    el.getparent().remove(el)
                    changed = True
        pkg.mark_modified(part)


def build_document_model(pkg: DocxPackage, cfg: Config) -> DocumentModel:
    paragraphs: list[ParagraphRef] = []
    skipped: list[tuple[str, str]] = []
    style_path = f"{qn('w:pPr')}/{qn('w:pStyle')}"

    for wp in walk_package(pkg):
        text = paragraph_text(wp.element)
        style_el = wp.element.find(style_path)
        style = style_el.get(qn("w:val")) if style_el is not None else "Normal"

        if not text.strip():
            skipped.append((wp.para_id, "empty"))
        elif any(fnmatch.fnmatchcase(style, pat) for pat in cfg.skip.styles):
            skipped.append((wp.para_id, f"style:{style}"))
        elif len(text) < cfg.chunking.min_paragraph_chars:
            skipped.append((wp.para_id, f"short:{len(text)}"))
        else:
            paragraphs.append(ParagraphRef(
                para_id=wp.para_id, part=wp.part, location=wp.location,
                text=text, style=style))
            log.debug("para %s [%s/%s] %d chars", wp.para_id, wp.location, style, len(text))

    log.info("Ingested %d reviewable paragraphs (%d skipped) from %s",
             len(paragraphs), len(skipped), pkg.path.name)
    return DocumentModel(source_path=str(pkg.path),
                         paragraphs=tuple(paragraphs), skipped=tuple(skipped))