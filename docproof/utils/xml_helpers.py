"""OOXML namespace helpers, the shared paragraph walker, and the zip-level
package wrapper. Every stage that reads or writes document XML goes through
this module — that single-source-of-truth is what makes text anchors safe.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator, NamedTuple

from lxml import etree

# --- Namespaces -------------------------------------------------------------

W_NS   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS  = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_NS = "http://www.w3.org/XML/1998/namespace"
CT_NS  = "http://schemas.openxmlformats.org/package/2006/content-types"
PR_NS  = "http://schemas.openxmlformats.org/package/2006/relationships"      # package rels files
DR_NS  = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"  # rel *types*

NSMAP = {"w": W_NS}

_PREFIXES = {"w": W_NS, "mc": MC_NS, "xml": XML_NS}


def qn(tag: str) -> str:
    """'w:p' -> '{http://...main}p' (lxml's Clark notation)."""
    prefix, local = tag.split(":")
    return f"{{{_PREFIXES[prefix]}}}{local}"


# Tags used constantly — resolve once.
P_TAG        = qn("w:p")
R_TAG        = qn("w:r")
RPR_TAG      = qn("w:rPr")
T_TAG        = qn("w:t")
DELTEXT_TAG  = qn("w:delText")
TBL_TAG      = qn("w:tbl")
TR_TAG       = qn("w:tr")
TC_TAG       = qn("w:tc")
SDT_TAG      = qn("w:sdt")
SDT_CONTENT  = qn("w:sdtContent")
INS_TAG      = qn("w:ins")
DEL_TAG      = qn("w:del")
XML_SPACE    = f"{{{XML_NS}}}space"

_TEXT_SKIP_ANCESTORS = {qn("w:txbxContent"), f"{{{MC_NS}}}Fallback"}


# --- The canonical text contract --------------------------------------------

def iter_text_elements(p: etree._Element) -> Iterator[etree._Element]:
    """All w:t descendants of a paragraph, in document order, excluding
    textbox content and mc:Fallback duplicates. THE anchoring contract:
    ingest, validator, and reassembler all derive paragraph text from
    exactly this function."""
    for t in p.iter(T_TAG):
        node = t.getparent()
        skip = False
        while node is not None and node is not p:
            if node.tag in _TEXT_SKIP_ANCESTORS:
                skip = True
                break
            node = node.getparent()
        if not skip:
            yield t


def paragraph_text(p: etree._Element) -> str:
    return "".join(t.text or "" for t in iter_text_elements(p))


def set_text(t: etree._Element, s: str) -> None:
    """Set a w:t/w:delText's text, adding xml:space='preserve' when the
    content has leading/trailing spaces (else Word trims them)."""
    t.text = s
    if s != s.strip(" "):
        t.set(XML_SPACE, "preserve")


# --- The shared paragraph walker --------------------------------------------

class WalkedParagraph(NamedTuple):
    part: str        # e.g. "word/document.xml"
    para_id: str     # e.g. "body-0042", "table-0-r2-c1-p0", "footnote-2-p0"
    location: str    # "body" | "table" | "header" | "footer" | "footnote" | "endnote"
    element: etree._Element


def _walk_container(container, part, location, p_prefix, tbl_prefix,
                    pad_p=False, counters=None) -> Iterator[WalkedParagraph]:
    """Walk direct block children of a container: paragraphs, tables
    (recursing into cells), and content controls (transparent). Deliberately
    NOT a .//w:p search — that would catch textbox paragraphs and wreck
    determinism."""
    if counters is None:
        counters = {"p": 0, "tbl": 0}
    for child in container:
        tag = child.tag
        if tag == P_TAG:
            i = counters["p"]; counters["p"] += 1
            pid = f"{p_prefix}{i:04d}" if pad_p else f"{p_prefix}{i}"
            yield WalkedParagraph(part, pid, location, child)
        elif tag == TBL_TAG:
            k = counters["tbl"]; counters["tbl"] += 1
            base = f"{tbl_prefix}{k}"
            for r_i, tr in enumerate(child.findall(TR_TAG)):
                for c_i, tc in enumerate(tr.findall(TC_TAG)):
                    cell = f"{base}-r{r_i}-c{c_i}"
                    yield from _walk_container(
                        tc, part, "table" if location == "body" else location,
                        f"{cell}-p", f"{cell}-tbl")
        elif tag == SDT_TAG:
            content = child.find(SDT_CONTENT)
            if content is not None:   # content controls are transparent wrappers
                yield from _walk_container(content, part, location,
                                           p_prefix, tbl_prefix, pad_p, counters)


def _document_rels(pkg: "DocxPackage") -> list[tuple[str, str]]:
    """(rel_type, part_path) pairs from word/_rels/document.xml.rels."""
    name = "word/_rels/document.xml.rels"
    if not pkg.has(name):
        return []
    out = []
    for rel in pkg.tree(name):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        path = target.lstrip("/")
        if not path.startswith("word/"):
            path = "word/" + path
        out.append((rel.get("Type", ""), path))
    return out


def walk_package(pkg: "DocxPackage") -> Iterator[WalkedParagraph]:
    """Yield every reviewable paragraph in deterministic order:
    body (incl. tables) -> headers/footers -> footnotes -> endnotes.
    Called by ingest AND by the reassembler, on the same trees."""
    body = pkg.tree("word/document.xml").find(qn("w:body"))
    yield from _walk_container(body, "word/document.xml", "body",
                               "body-", "table-", pad_p=True)

    for rel_type, path in sorted(_document_rels(pkg), key=lambda x: x[1]):
        kind = "header" if rel_type.endswith("/header") else \
               "footer" if rel_type.endswith("/footer") else None
        if kind and pkg.has(path):
            stem = Path(path).stem                      # "header1"
            yield from _walk_container(pkg.tree(path), path, kind,
                                       f"{stem}-p", f"{stem}-tbl")

    for part, tag, loc in (("word/footnotes.xml", "w:footnote", "footnote"),
                           ("word/endnotes.xml",  "w:endnote",  "endnote")):
        if not pkg.has(part):
            continue
        for note in pkg.tree(part).findall(qn(tag)):
            if note.get(qn("w:type")) in ("separator", "continuationSeparator"):
                continue
            nid = note.get(qn("w:id"))
            yield from _walk_container(note, part, loc,
                                       f"{loc}-{nid}-p", f"{loc}-{nid}-tbl")


# --- Run normalization -------------------------------------------------------

def merge_adjacent_runs(p: etree._Element) -> None:
    """Coalesce adjacent runs with identical formatting whose content is
    purely w:t. Preserves canonical text exactly (so anchors stay valid) while
    drastically reducing how often the reassembler has to split runs. Called
    lazily, per edited paragraph, just before surgery."""
    parents = list(dict.fromkeys(
        t.getparent().getparent() for t in iter_text_elements(p)))
    for parent in parents:
        prev = None
        for run in list(parent):
            if run.tag != R_TAG or not _only_text_children(run):
                prev = None
                continue
            if prev is not None and _rpr_key(prev) == _rpr_key(run):
                _last_t(prev).text = (_last_t(prev).text or "") + \
                                     "".join(t.text or "" for t in run.iter(T_TAG))
                set_text(_last_t(prev), _last_t(prev).text)
                parent.remove(run)
            else:
                prev = run


def _only_text_children(run) -> bool:
    return all(c.tag in (RPR_TAG, T_TAG) for c in run) and \
           any(c.tag == T_TAG for c in run)


def _rpr_key(run) -> bytes:
    rpr = run.find(RPR_TAG)
    return etree.tostring(rpr) if rpr is not None else b""


def _last_t(run):
    return [c for c in run if c.tag == T_TAG][-1]


# --- The package wrapper ------------------------------------------------------

class DocxPackage:
    """A .docx opened as a zip held in memory. Parts are parsed lazily; only
    parts explicitly marked modified are re-serialized on save — everything
    else is written back byte-for-byte. That is the fidelity guarantee."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self._order = [i.filename for i in z.infolist()]
            self._raw = {n: z.read(n) for n in self._order}
        self._trees: dict[str, etree._Element] = {}
        self._dirty: set[str] = set()
        self._new: dict[str, etree._Element] = {}

    def has(self, name: str) -> bool:
        return name in self._raw or name in self._new

    def tree(self, name: str) -> etree._Element:
        if name in self._new:
            return self._new[name]
        if name not in self._trees:
            self._trees[name] = etree.fromstring(self._raw[name])
        return self._trees[name]

    def mark_modified(self, name: str) -> None:
        self._dirty.add(name)

    def add_part(self, name: str, root: etree._Element) -> None:
        self._new[name] = root
        self._dirty.add(name)

    def save(self, out_path: str | Path) -> None:
        def serialize(name):
            return etree.tostring(self.tree(name), xml_declaration=True,
                                  encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self._order:
                z.writestr(name, serialize(name) if name in self._dirty
                           else self._raw[name])
            for name in self._new:
                z.writestr(name, serialize(name))

    