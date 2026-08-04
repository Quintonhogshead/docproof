"""The XML surgery both writers share.

The clean writer and the tracked writer make the same decisions — the plan is
the same object — so everything that identifies *what* to change lives here and
each writer only implements *how* it records the change.
"""
from __future__ import annotations

import re

from lxml import etree

from ...utils.xml_helpers import (P_TAG, RPR_TAG, R_TAG, SDT_CONTENT, SDT_TAG,
                                  T_TAG, iter_text_elements, qn, set_text,
                                  walk_package)
from ..ingest import BODY_PART

PPR_TAG = qn("w:pPr")
PSTYLE_TAG = qn("w:pStyle")
RSTYLE_TAG = qn("w:rStyle")
SECTPR_TAG = qn("w:sectPr")
NUMPR_TAG = qn("w:numPr")
TAB_TAG = qn("w:tab")

# Run properties worth keeping when the file is cleaned up for placement.
# Italics are the author's own emphasis and the spec is explicit that they
# survive; the rest of a Google Docs export — fonts, sizes, colours, language
# tags — is formatting the template is about to override anyway, and every one
# of them arrives in InDesign as a local override a designer has to clear.
KEEP_RUN_PROPS = {qn(t) for t in (
    "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike",
    "w:dstrike", "w:vertAlign", "w:u", "w:rStyle", "w:lang")}

# Paragraph properties that survive a clean: the style itself, a section break
# (removing one would repaginate the manuscript), and list membership (removing
# it would silently turn a numbered list into loose paragraphs).
KEEP_PARA_PROPS = {PSTYLE_TAG, SECTPR_TAG, NUMPR_TAG}

_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'，、]+[^\s<>()\[\]{}\"'.,;:!?]",
                  re.IGNORECASE)


def element_map(pkg) -> dict[str, etree._Element]:
    """para_id → element, for the body part only.

    Built in one walk before anything is mutated: after surgery the walker's
    positional ids no longer describe the tree it is looking at."""
    return {wp.para_id: wp.element for wp in walk_package(pkg)
            if wp.part == BODY_PART}


# --- paragraph properties -----------------------------------------------------

def ensure_ppr(p: etree._Element) -> etree._Element:
    """The paragraph's w:pPr, created as its first child if absent — which is
    where the schema requires it."""
    ppr = p.find(PPR_TAG)
    if ppr is None:
        ppr = etree.Element(PPR_TAG)
        p.insert(0, ppr)
    return ppr


def set_paragraph_style(p: etree._Element, style_id: str) -> str | None:
    """Point the paragraph at a style, returning the style id it had before.
    w:pStyle is the first child of w:pPr, per the schema's element order."""
    ppr = ensure_ppr(p)
    existing = ppr.find(PSTYLE_TAG)
    previous = existing.get(qn("w:val")) if existing is not None else None
    if existing is not None:
        ppr.remove(existing)
    style = etree.Element(PSTYLE_TAG, {qn("w:val"): style_id})
    ppr.insert(0, style)
    return previous


def strip_direct_paragraph_formatting(p: etree._Element) -> bool:
    """Drop everything from w:pPr the template will supply itself."""
    ppr = p.find(PPR_TAG)
    if ppr is None:
        return False
    removed = False
    for child in list(ppr):
        if child.tag not in KEEP_PARA_PROPS:
            ppr.remove(child)
            removed = True
    return removed


def strip_direct_run_formatting(p: etree._Element) -> bool:
    """Prune every run's w:rPr down to the marks that carry meaning."""
    removed = False
    for run in p.iter(R_TAG):
        rpr = run.find(RPR_TAG)
        if rpr is None:
            continue
        for child in list(rpr):
            if child.tag not in KEEP_RUN_PROPS:
                rpr.remove(child)
                removed = True
        if not len(rpr):
            run.remove(rpr)
    return removed


# --- text edges ---------------------------------------------------------------

def leading_tab_count(p: etree._Element) -> int:
    """w:tab elements before the paragraph's first text.

    Tabs contribute no character to canonical text — the same convention the
    review pipeline uses — so they cannot be expressed as an offset and are
    counted separately."""
    n = 0
    for run in p.iter(R_TAG):
        for child in run:
            if child.tag == TAB_TAG:
                n += 1
            elif child.tag == T_TAG and (child.text or ""):
                return n
    return n


def remove_leading_tabs(p: etree._Element, count: int) -> None:
    for run in list(p.iter(R_TAG)):
        for child in list(run):
            if count <= 0:
                return
            if child.tag == TAB_TAG:
                run.remove(child)
                count -= 1
            elif child.tag == T_TAG and (child.text or ""):
                return


def trim_text_edges(p: etree._Element, leading: int, trailing: int) -> None:
    """Remove typed whitespace at the two ends of a paragraph, in place.

    The first-line indent is NOT touched — that comes from the paragraph style,
    not from spaces someone typed."""
    texts = [t for t in iter_text_elements(p)]
    if not texts:
        return
    for t in texts:
        if leading <= 0:
            break
        value = t.text or ""
        cut = min(leading, len(value) - len(value.lstrip(" \t　")))
        if cut:
            set_text(t, value[cut:])
            leading -= cut
        if (t.text or ""):
            break
    for t in reversed(texts):
        if trailing <= 0:
            break
        value = t.text or ""
        cut = min(trailing, len(value) - len(value.rstrip(" \t　")))
        if cut:
            set_text(t, value[:len(value) - cut])
            trailing -= cut
        if (t.text or ""):
            break


# --- links --------------------------------------------------------------------

def link_ranges(text: str) -> list[tuple[int, int]]:
    """Character spans of every URL in a paragraph's canonical text."""
    return [(m.start(), m.end()) for m in _URL.finditer(text)]


def apply_character_style(p: etree._Element, start: int, end: int,
                          style_id: str) -> None:
    """Put a character style on the runs covering [start, end), splitting runs
    at the boundaries so the style lands on exactly the URL."""
    from ...reassembler import _ensure_boundary, _text_spans

    _ensure_boundary(p, start)
    _ensure_boundary(p, end)
    for t, s, e in _text_spans(p):
        if s >= start and e <= end and e > s:
            run = t.getparent()
            rpr = run.find(RPR_TAG)
            if rpr is None:
                rpr = etree.Element(RPR_TAG)
                run.insert(0, rpr)
            for existing in rpr.findall(RSTYLE_TAG):
                rpr.remove(existing)
            rpr.insert(0, etree.Element(RSTYLE_TAG, {qn("w:val"): style_id}))


def style_links(p: etree._Element, style_id: str) -> int:
    """Tag every URL in the paragraph with the hyperlink character style.
    Invisible in print; it is what lets an EPUB export emit a real link."""
    from ...utils.xml_helpers import paragraph_text

    ranges = link_ranges(paragraph_text(p))
    for start, end in reversed(ranges):        # right to left: offsets hold
        apply_character_style(p, start, end, style_id)
    return len(ranges)


# --- new paragraphs -----------------------------------------------------------

def glyph_paragraph(style_id: str, glyph: str) -> etree._Element:
    """A scene-break paragraph: the house glyph, in the scene-break style."""
    p = etree.Element(P_TAG)
    ppr = etree.SubElement(p, PPR_TAG)
    etree.SubElement(ppr, PSTYLE_TAG, {qn("w:val"): style_id})
    run = etree.SubElement(p, R_TAG)
    text = etree.SubElement(run, T_TAG)
    set_text(text, glyph)
    return p


# --- Google Docs artifacts ----------------------------------------------------

def clean_content_controls(pkg) -> int:
    """Remove empty content controls and unwrap Google's suggestion tags.

    A Google Docs export is littered with `goog_rdk` structured tags, most of
    them empty. The walker already treats a content control as transparent, so
    unwrapping one moves no paragraph and changes no id."""
    body = pkg.tree(BODY_PART)
    removed = 0
    for sdt in list(body.iter(SDT_TAG)):
        parent = sdt.getparent()
        if parent is None:
            continue
        content = sdt.find(SDT_CONTENT)
        if content is None or not len(content):
            parent.remove(sdt)
            removed += 1
            continue
        has_text = any((t.text or "").strip() for t in content.iter(T_TAG))
        has_paragraph = content.find(P_TAG) is not None
        if not has_text and not has_paragraph:
            parent.remove(sdt)
            removed += 1
            continue
        if _is_google_artifact(sdt):
            index = parent.index(sdt)
            for child in list(content):
                parent.insert(index, child)
                index += 1
            parent.remove(sdt)
            removed += 1
    return removed


def _is_google_artifact(sdt: etree._Element) -> bool:
    pr = sdt.find(qn("w:sdtPr"))
    if pr is None:
        return False
    for tag in (qn("w:alias"), qn("w:tag")):
        el = pr.find(tag)
        if el is not None and (el.get(qn("w:val")) or "").startswith("goog_rdk"):
            return True
    return False
