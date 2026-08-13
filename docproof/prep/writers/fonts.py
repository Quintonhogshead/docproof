"""Embedding the design's fonts into the book file.

A .docx names fonts; it does not normally carry them. The book output is a
reading copy sent to people whose machines have never heard of the design's
faces, so the faces travel inside the file: each TTF is written as an
obfuscated `word/fonts/*.odttf` part and registered in the font table, which
is how Word expects an embedded font to arrive (ECMA-376 §17.8).

The obfuscation is the spec's, not ours: the first 32 bytes of the font are
XORed with the bytes of a GUID in reverse order, and the same GUID rides along
as `w:fontKey` so a consumer can undo it. It is a wrapper, not protection —
every face shipped here is licensed for exactly this (see fonts/README.md).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from ...utils.xml_helpers import CT_NS, DR_NS, PR_NS, W_NS, qn

log = logging.getLogger("docproof.prep.writers.fonts")

FONT_TABLE = "word/fontTable.xml"
_FONT_TABLE_RELS = "word/_rels/fontTable.xml.rels"
_CT_ODTTF = "application/vnd.openxmlformats-officedocument.obfuscatedFont"
_CT_FONT_TABLE = ("application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.fontTable+xml")
_REL_FONT = (f"{DR_NS}/font")
_REL_FONT_TABLE = (f"{DR_NS}/fontTable")

# Which w:embed* element a file fills, read from the file name. The bundled
# fonts follow the -Italic / -Bold / -BoldItalic naming convention, and the
# design file controls the file list, so this is a convention the repo keeps
# with itself rather than font-file introspection.
_SLOTS = (("bolditalic", "w:embedBoldItalic"), ("bold", "w:embedBold"),
          ("italic", "w:embedItalic"))


@dataclass(frozen=True)
class EmbeddedFont:
    family: str
    file: Path


def _slot(path: Path) -> str:
    suffix = path.stem.rsplit("-", 1)[-1].lower()
    for marker, slot in _SLOTS:
        if suffix == marker:
            return slot
    return "w:embedRegular"


def _obfuscate(data: bytes, guid_hex: str) -> bytes:
    key = bytes.fromhex(guid_hex)[::-1]
    head = bytes(b ^ key[i % len(key)] for i, b in enumerate(data[:32]))
    return head + data[32:]


def embed_fonts(pkg, fonts: list[EmbeddedFont]) -> int:
    """Put every font into the package and point the font table at them.
    Returns how many files were embedded."""
    if not fonts:
        return 0
    table = _font_table(pkg)
    rels = _font_table_rels(pkg)
    _default_content_type(pkg, "odttf", _CT_ODTTF)

    by_family: dict[str, list[Path]] = {}
    for f in fonts:
        by_family.setdefault(f.family, []).append(f.file)

    count = 0
    for family, files in by_family.items():
        font_el = _font_element(table, family)
        for file in files:
            count += 1
            part = f"word/fonts/font{count}.odttf"
            guid = uuid.uuid4()
            pkg.add_binary_part(part, _obfuscate(file.read_bytes(),
                                                 guid.hex))
            rel_id = _add_relationship(rels, _REL_FONT,
                                       f"fonts/font{count}.odttf")
            slot = _slot(file)
            for existing in font_el.findall(qn(slot)):
                font_el.remove(existing)
            etree.SubElement(font_el, qn(slot), {
                f"{{{DR_NS}}}id": rel_id,
                qn("w:fontKey"): "{" + str(guid).upper() + "}"})
    log.info("Embedded %d font file(s) across %d famil(ies).",
             count, len(by_family))
    return count


# --- parts and registration ---------------------------------------------------

def _font_table(pkg) -> etree._Element:
    if pkg.has(FONT_TABLE):
        pkg.mark_modified(FONT_TABLE)
        return pkg.tree(FONT_TABLE)
    root = etree.Element(qn("w:fonts"), nsmap={"w": W_NS, "r": DR_NS})
    pkg.add_part(FONT_TABLE, root)
    _override_content_type(pkg, FONT_TABLE, _CT_FONT_TABLE)
    _add_document_relationship(pkg, _REL_FONT_TABLE, "fontTable.xml")
    return root


def _font_element(table: etree._Element, family: str) -> etree._Element:
    for el in table.findall(qn("w:font")):
        if el.get(qn("w:name")) == family:
            return el
    el = etree.SubElement(table, qn("w:font"), {qn("w:name"): family})
    etree.SubElement(el, qn("w:charset"), {qn("w:val"): "00"})
    etree.SubElement(el, qn("w:family"), {qn("w:val"): "auto"})
    etree.SubElement(el, qn("w:pitch"), {qn("w:val"): "variable"})
    return el


def _font_table_rels(pkg) -> etree._Element:
    if pkg.has(_FONT_TABLE_RELS):
        pkg.mark_modified(_FONT_TABLE_RELS)
        return pkg.tree(_FONT_TABLE_RELS)
    rels = etree.Element(f"{{{PR_NS}}}Relationships", nsmap={None: PR_NS})
    pkg.add_part(_FONT_TABLE_RELS, rels)
    return rels


def _add_relationship(rels: etree._Element, rel_type: str, target: str) -> str:
    used = {r.get("Id") for r in rels}
    n = 1
    while f"rId{n}" in used:
        n += 1
    etree.SubElement(rels, f"{{{PR_NS}}}Relationship",
                     {"Id": f"rId{n}", "Type": rel_type, "Target": target})
    return f"rId{n}"


def _add_document_relationship(pkg, rel_type: str, target: str) -> None:
    rels_name = "word/_rels/document.xml.rels"
    if pkg.has(rels_name):
        rels = pkg.tree(rels_name)
        pkg.mark_modified(rels_name)
    else:
        rels = etree.Element(f"{{{PR_NS}}}Relationships", nsmap={None: PR_NS})
        pkg.add_part(rels_name, rels)
    for r in rels:
        if r.get("Type") == rel_type:
            return
    _add_relationship(rels, rel_type, target)


def _default_content_type(pkg, extension: str, content_type: str) -> None:
    ct = pkg.tree("[Content_Types].xml")
    for el in ct.findall(f"{{{CT_NS}}}Default"):
        if el.get("Extension") == extension:
            return
    pkg.mark_modified("[Content_Types].xml")
    etree.SubElement(ct, f"{{{CT_NS}}}Default",
                     {"Extension": extension, "ContentType": content_type})


def _override_content_type(pkg, part: str, content_type: str) -> None:
    ct = pkg.tree("[Content_Types].xml")
    for el in ct.findall(f"{{{CT_NS}}}Override"):
        if el.get("PartName") == f"/{part}":
            return
    pkg.mark_modified("[Content_Types].xml")
    etree.SubElement(ct, f"{{{CT_NS}}}Override",
                     {"PartName": f"/{part}", "ContentType": content_type})
