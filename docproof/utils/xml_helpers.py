"""OOXML namespace helpers, the shared paragraph walker, and the zip-level
package wrapper. Every stage that reads or writes document XML goes through
this module — that single-source-of-truth is what makes text anchors safe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, NamedTuple

from lxml import etree

from .package import ZipPackage

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
BR_TAG       = qn("w:br")
CR_TAG       = qn("w:cr")
TAB_TAG      = qn("w:tab")
DELTEXT_TAG  = qn("w:delText")
TBL_TAG      = qn("w:tbl")
TR_TAG       = qn("w:tr")
TC_TAG       = qn("w:tc")
SDT_TAG      = qn("w:sdt")
SDT_CONTENT  = qn("w:sdtContent")
INS_TAG      = qn("w:ins")
DEL_TAG      = qn("w:del")
XML_SPACE    = f"{{{XML_NS}}}space"

# Text under these ancestors is NOT canonical paragraph text: a textbox holds
# its own separate story, and an mc:Fallback is a duplicate rendering of the
# mc:Choice beside it. Both the forward extractor (iter_text_elements) and the
# accept/reject views (paragraph_view_text) must honour this, or the two
# disagree and the reject-all audit fails on a paragraph nothing touched.
TEXT_SKIP_ANCESTORS = {qn("w:txbxContent"), f"{{{MC_NS}}}Fallback"}


# --- The canonical text contract --------------------------------------------

# Run content that IS a character but holds no w:t text: a soft line break, a
# carriage return, a tab. Left unrendered, the two halves of a Shift+Enter line
# fuse ("The Ripple Effect" + "QX Countdown" reads "...EffectQX..."), which
# poisons word-level scans and shows the model a run-on no author wrote. Each
# renders as the whitespace character Word itself treats it as. w:tab is only
# content INSIDE a run — the same tag under w:pPr/w:tabs is a tab-stop
# definition, not a character — hence the parent check in the iterator.
VIRTUAL_CHARS = {BR_TAG: "\n", CR_TAG: "\n", TAB_TAG: "\t"}


def virtual_char(el: etree._Element) -> str:
    """The character a non-text content element renders as, or ""."""
    return "" if el.tag == T_TAG else VIRTUAL_CHARS[el.tag]


def _skipped(el: etree._Element, p: etree._Element) -> bool:
    node = el.getparent()
    while node is not None and node is not p:
        if node.tag in TEXT_SKIP_ANCESTORS:
            return True
        node = node.getparent()
    return False


def iter_content_elements(p: etree._Element) -> Iterator[etree._Element]:
    """All character-bearing descendants of a paragraph, in document order:
    w:t text plus the break/tab elements that render as one character each
    (see VIRTUAL_CHARS). Excludes textbox content and mc:Fallback duplicates.
    THE anchoring contract: ingest, validator, and reassembler all derive
    paragraph text — and every character offset into it — from exactly this
    function."""
    for el in p.iter(T_TAG, BR_TAG, CR_TAG, TAB_TAG):
        if el.tag == TAB_TAG and (el.getparent() is None
                                  or el.getparent().tag != R_TAG):
            continue                       # a tab STOP under w:pPr, not content
        if not _skipped(el, p):
            yield el


def iter_text_elements(p: etree._Element) -> Iterator[etree._Element]:
    """Only the w:t descendants, same exclusions. For callers that edit text
    nodes in place (the normalizer, run merging) rather than measure offsets —
    offsets come from iter_content_elements, never from this."""
    for el in iter_content_elements(p):
        if el.tag == T_TAG:
            yield el


def paragraph_text(p: etree._Element) -> str:
    return "".join((el.text or "") if el.tag == T_TAG else VIRTUAL_CHARS[el.tag]
                   for el in iter_content_elements(p))


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

class DocxPackage(ZipPackage):
    """A .docx opened as a zip held in memory. Everything it does is the
    format-neutral behaviour in ZipPackage; the .docx part names live in the
    walker above."""

    