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
# An image can arrive three ways: a modern DrawingML picture, a legacy VML
# shape (`w:pict`), or an embedded OLE object. A paragraph holding only one of
# these has no text, and prep must not mistake it for a blank line.
_IMAGE_TAGS = (qn("w:drawing"), qn("w:pict"), qn("w:object"))

# The styles Word and LibreOffice give the *entries* of a generated contents
# list. "TOCHeading"/"Contents Heading" is the word "Contents" itself, which is
# a front-matter title and must not match: hence the required digit.
_TOC_STYLE = re.compile(r"^(toc|contents|inhaltsverzeichnis)\s*\d+$",
                        re.IGNORECASE)
# Field instructions inside one. The whole contents list is usually a single
# TOC field spanning many paragraphs, but each entry carries its own page
# reference, so per-paragraph detection has something to find either way.
_TOC_FIELD = re.compile(r"\bTOC\b|PAGEREF\s+_Toc", re.IGNORECASE)
_FLD_SIMPLE = qn("w:fldSimple")
_INSTR_TEXT = qn("w:instrText")
_INSTR_ATTR = qn("w:instr")
_ANCHOR = qn("w:anchor")


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
        # A picture on its own line has no text but is content, not spacing:
        # calling it blank would route it to the blank-line drop and delete the
        # image from the formatted document.
        has_image = _has_image(wp.element)
        paragraphs.append(StructureParagraph(
            para_id=wp.para_id, part=wp.part, index=len(paragraphs),
            location=wp.location, text=text, style=style,
            is_blank=(not text.strip()) and not has_image,
            leading_ws=len(text) - len(text.lstrip(WS)),
            trailing_ws=len(text) - len(text.rstrip(WS)) if stripped else 0,
            has_italics=_has_italics(wp.element),
            has_link=_has_link(wp.element, text),
            has_image=has_image,
            is_list=wp.element.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None,
            is_toc=_is_toc(wp.element, style),
        ))

    blanks = sum(1 for p in paragraphs if p.is_blank)
    log.info("Read %d paragraphs (%d blank) from %s; %d left untouched "
             "outside the body.", len(paragraphs), blanks, pkg.path.name,
             len(untouched))
    return Structure(source_path=str(pkg.path), paragraphs=tuple(paragraphs),
                     untouched=tuple(untouched))


def _is_toc(p: etree._Element, style: str) -> bool:
    """Is this paragraph a line of the document's own table of contents?

    Asked of the file, never of the words: a contents entry reading "Chapter
    One" is character-for-character the chapter heading it points at, so the
    text cannot tell them apart and neither can anything reading only the text.
    Three signals, any one of which is enough — the entry's own style, a TOC or
    PAGEREF field instruction, or a link to a _Toc bookmark."""
    if _TOC_STYLE.match(style or ""):
        return True
    for field in p.iter(_FLD_SIMPLE):
        if _TOC_FIELD.search(field.get(_INSTR_ATTR) or ""):
            return True
    for instr in p.iter(_INSTR_TEXT):
        if _TOC_FIELD.search(instr.text or ""):
            return True
    for link in p.iter(_HYPERLINK):
        if (link.get(_ANCHOR) or "").startswith("_Toc"):
            return True
    return False


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


def _has_image(p: etree._Element) -> bool:
    """Does a picture, VML shape, or embedded object live anywhere in this
    paragraph? A drawing sits inside a run, so the search is over descendants,
    not direct children."""
    return any(p.find(f".//{tag}") is not None for tag in _IMAGE_TAGS)
