"""Reading a manuscript for prep: every paragraph, nothing filtered.

The review pipeline's ingest throws away exactly what prep needs — blank lines
(the author's scene-break signal), short lines ("Chapter One" is eleven
characters), and anything already styled as a heading. So prep walks the same
package with the same walker, for id parity, and keeps all of it.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from lxml import etree

from ..ingest import IngestError, OLE_MAGIC, REVISION_TAGS, ZIP_MAGIC
from ..utils.xml_helpers import (DocxPackage, RPR_TAG, iter_text_elements,
                                 paragraph_text, qn, walk_package)
from .model import Structure, StructureParagraph

log = logging.getLogger("docproof.prep.ingest")

BODY_PART = "word/document.xml"

# Whitespace the author typed at the start of a line. Not \s — a non-breaking
# space is a character the author chose, and stripping it would change the text.
WS = " \t　"

_URL = re.compile(r"(https?://|www\.)\S", re.IGNORECASE)
_HYPERLINK = qn("w:hyperlink")
_ITALIC = qn("w:i")


def preflight(path: str | Path) -> DocxPackage:
    """Open a manuscript, or refuse it with a sentence the author can act on.

    Unlike review, prep has no tracked-changes policy: a document with
    revisions in it is refused, full stop. Prep rewrites paragraph styles and
    removes blank lines wholesale, and doing that around someone else's
    unresolved edits would either bury their changes or nest revisions inside
    revisions. Accepting or rejecting them first is a decision for the person
    who made them, not for us."""
    path = Path(path)
    if not path.exists():
        raise IngestError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".idml":
        raise IngestError(
            f"{path.name} is an InDesign layout. Prep is the step that gets a "
            f"manuscript INTO InDesign, so it takes a Word document.")
    if suffix != ".docx":
        raise IngestError(
            f"Prep reads Word .docx files; {path.name} is a {suffix or 'file'} "
            f"with no extension. Convert it first — DocProof will do that for "
            f"you if LibreOffice is installed.")

    with path.open("rb") as fh:
        head = fh.read(4)
    if head == OLE_MAGIC:
        raise IngestError(
            f"{path.name} is a legacy .doc file or a password-protected .docx. "
            f"Open it in Word, remove any password, and Save As .docx.")
    if head != ZIP_MAGIC:
        raise IngestError(f"{path.name} is not a .docx file (bad file signature).")

    try:
        pkg = DocxPackage(path)
    except Exception as e:                    # noqa: BLE001 - zipfile variants
        raise IngestError(f"{path.name} is corrupt: {e}") from e

    for required in ("[Content_Types].xml", BODY_PART):
        if not pkg.has(required):
            raise IngestError(
                f"{path.name} is missing {required}; not a valid .docx.")

    tracked = _tracked_parts(pkg)
    if tracked:
        raise IngestError(
            f"{path.name} still has tracked changes in it ({', '.join(tracked)}). "
            f"Accept or reject them in Word first — prep restyles every "
            f"paragraph and removes blank lines, and it will not do that on top "
            f"of edits nobody has decided about yet.")
    return pkg


def _tracked_parts(pkg: DocxPackage) -> list[str]:
    parts = list(dict.fromkeys(wp.part for wp in walk_package(pkg)))
    return sorted(part for part in parts
                  if any(el.tag in REVISION_TAGS for el in pkg.tree(part).iter()))


def build_structure(pkg: DocxPackage) -> Structure:
    """Every paragraph in the body, in document order, blanks included."""
    paragraphs: list[StructureParagraph] = []
    untouched: list[tuple[str, str]] = []
    style_path = f"{qn('w:pPr')}/{qn('w:pStyle')}"

    for wp in walk_package(pkg):
        if wp.part != BODY_PART:
            # Headers, footers, footnotes and endnotes are not manuscript body.
            # A house template supplies its own running heads, so prep reports
            # them rather than tagging them into a style that doesn't fit.
            untouched.append((wp.para_id, wp.location))
            continue

        text = paragraph_text(wp.element)
        style_el = wp.element.find(style_path)
        style = style_el.get(qn("w:val")) if style_el is not None else "Normal"
        stripped = text.strip(WS)
        paragraphs.append(StructureParagraph(
            para_id=wp.para_id, part=wp.part, index=len(paragraphs),
            location=wp.location, text=text, style=style,
            is_blank=not text.strip(),
            leading_ws=len(text) - len(text.lstrip(WS)),
            trailing_ws=len(text) - len(text.rstrip(WS)) if stripped else 0,
            has_italics=_has_italics(wp.element),
            has_link=_has_link(wp.element, text),
            is_list=wp.element.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None,
        ))

    blanks = sum(1 for p in paragraphs if p.is_blank)
    log.info("Read %d paragraphs (%d blank) from %s; %d left untouched "
             "outside the body.", len(paragraphs), blanks, pkg.path.name,
             len(untouched))
    return Structure(source_path=str(pkg.path), paragraphs=tuple(paragraphs),
                     untouched=tuple(untouched))


def _has_italics(p: etree._Element) -> bool:
    for t in iter_text_elements(p):
        if not (t.text or "").strip():
            continue
        rpr = t.getparent().find(RPR_TAG)
        if rpr is not None and rpr.find(_ITALIC) is not None:
            return True
    return False


def _has_link(p: etree._Element, text: str) -> bool:
    return p.find(f".//{_HYPERLINK}") is not None or bool(_URL.search(text))
