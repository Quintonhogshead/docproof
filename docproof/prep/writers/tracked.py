"""The same tagging, as tracked changes.

Every decision prep made, written so a human can walk it in Word and accept or
reject each one: a style change is a `w:pPrChange`, a removed blank line is a
deleted paragraph mark, an inserted scene break is an inserted run, and typed
whitespace is a deletion. Accept them all and you have the clean file; reject
them all and you have the manuscript exactly as it arrived.

Unlike the clean file, this one leaves the author's direct formatting alone.
Stripping a Google Docs export's fonts and sizes would be hundreds of further
revisions to click through, and it is the placed file that needs to be clean,
not the one being reviewed.
"""
from __future__ import annotations

import copy
import itertools
import logging
from datetime import datetime, timezone

from lxml import etree

from ...models import Anchor
from ...reassembler import _next_rev_id, apply_replacement
from ...utils.xml_helpers import (INS_TAG, R_TAG, RPR_TAG, T_TAG,
                                  merge_adjacent_runs, paragraph_text, qn,
                                  set_text)
from ..ingest import BODY_PART
from ..model import PrepPlan, Structure
from ..styles import StyleSheet, install_style_sheet
from .clean import WriteStats
from .common import (PPR_TAG, SECTPR_TAG, TAB_TAG, clean_content_controls,
                     element_map, ensure_ppr, link_ranges, set_paragraph_style)

log = logging.getLogger("docproof.prep.writers.tracked")

PPRCHANGE_TAG = qn("w:pPrChange")
RPRCHANGE_TAG = qn("w:rPrChange")
DEL_TAG = qn("w:del")
RSTYLE_TAG = qn("w:rStyle")


def write_tracked(pkg, structure: Structure, plan: PrepPlan, sheet: StyleSheet,
                  *, author: str = "docproof") -> WriteStats:
    elements = element_map(pkg)
    pkg.mark_modified(BODY_PART)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = itertools.count(_next_rev_id(pkg))
    link_style = sheet.link_style
    styled = dropped = inserted = trimmed = links = 0
    missing: list[str] = []

    for item in plan.plans:
        p = elements.get(item.para_id)
        if p is None:
            log.error("%s vanished between reading and writing.", item.para_id)
            missing.append(item.para_id)
            continue

        if item.drop:
            _delete_paragraph_mark(p, ids, author, date)
            dropped += 1
            continue

        if item.insert_glyph:
            _become_glyph(p, sheet.by_name(item.style).id, plan.glyph,
                          ids, author, date)
            inserted += 1
            styled += 1
            continue

        if item.strip_leading or item.strip_trailing:
            if _delete_edges(p, item.strip_leading, item.strip_trailing,
                             ids, author, date):
                trimmed += 1
        trimmed += _delete_leading_tabs(p, ids, author, date)

        if item.style:
            _retag(p, sheet.by_name(item.style).id, ids, author, date)
            styled += 1

        if link_style is not None:
            links += _style_links_tracked(p, link_style.id, ids, author, date)

    controls = clean_content_controls(pkg)
    # Merged, not replaced: rejecting a style change has to land back on a
    # style the document still defines.
    install_style_sheet(pkg, sheet, replace=False)

    log.info("Tracked file: %d style change(s), %d blank line(s) marked for "
             "deletion, %d scene break(s) inserted.", styled, dropped, inserted)
    return WriteStats(styled=styled, dropped=dropped, inserted=inserted,
                      trimmed=trimmed, links=links, controls=controls,
                      missing=tuple(missing))


# --- the four shapes ----------------------------------------------------------

def _rev_attrs(ids, author: str, date: str) -> dict:
    return {qn("w:id"): str(next(ids)), qn("w:author"): author,
            qn("w:date"): date}


def _retag(p, style_id: str, ids, author: str, date: str) -> None:
    """A paragraph's style, changed with its previous properties recorded.

    Word reads `w:pPrChange` as "Formatted:", and rejecting it restores every
    property inside it — which is why the whole previous `w:pPr` goes in, not
    just the style name."""
    ppr = ensure_ppr(p)
    previous = copy.deepcopy(ppr)
    for nested in previous.findall(PPRCHANGE_TAG):
        previous.remove(nested)              # a change inside a change
    set_paragraph_style(p, style_id)
    for stale in ppr.findall(PPRCHANGE_TAG):
        ppr.remove(stale)
    change = etree.SubElement(ppr, PPRCHANGE_TAG, _rev_attrs(ids, author, date))
    change.append(previous)


def _delete_paragraph_mark(p, ids, author: str, date: str) -> None:
    """Mark a paragraph's own mark deleted. On Accept, Word merges the
    paragraph into the one after it — which for a blank line means it simply
    disappears."""
    ppr = ensure_ppr(p)
    rpr = _mark_rpr(ppr)
    for existing in rpr.findall(DEL_TAG):
        rpr.remove(existing)
    rpr.insert(0, etree.Element(DEL_TAG, _rev_attrs(ids, author, date)))


def _mark_rpr(ppr):
    """The run properties of the paragraph mark, placed where the schema wants
    them: after the paragraph's own properties, before any section break or
    property-change record."""
    rpr = ppr.find(RPR_TAG)
    if rpr is not None:
        return rpr
    rpr = etree.Element(RPR_TAG)
    anchor = None
    for tag in (SECTPR_TAG, PPRCHANGE_TAG):
        found = ppr.find(tag)
        if found is not None:
            anchor = found
            break
    if anchor is None:
        ppr.append(rpr)
    else:
        ppr.insert(ppr.index(anchor), rpr)
    return rpr


def _become_glyph(p, style_id: str, glyph: str, ids, author: str,
                  date: str) -> None:
    """The author's blank line, becoming a scene break.

    The paragraph itself is not new — it is the author's own blank line — so
    only its content is an insertion and only its style is a change. Accepting
    both gives the house glyph; rejecting both gives the blank line back."""
    previous = copy.deepcopy(ensure_ppr(p))
    for nested in previous.findall(PPRCHANGE_TAG):
        previous.remove(nested)
    attrs = dict(p.attrib)
    p.clear()
    for key, value in attrs.items():
        p.set(key, value)

    ppr = etree.SubElement(p, PPR_TAG)
    etree.SubElement(ppr, qn("w:pStyle"), {qn("w:val"): style_id})
    change = etree.SubElement(ppr, PPRCHANGE_TAG, _rev_attrs(ids, author, date))
    change.append(previous)

    ins = etree.SubElement(p, INS_TAG, _rev_attrs(ids, author, date))
    run = etree.SubElement(ins, R_TAG)
    set_text(etree.SubElement(run, T_TAG), glyph)


def _delete_edges(p, leading: int, trailing: int, ids, author: str,
                  date: str) -> bool:
    """Typed whitespace at the two ends of a paragraph, deleted as a revision.

    This is the review pipeline's own surgery, handed an anchor that happens to
    contain only spaces — the same tested code that splits runs and preserves
    xml:space, rather than a second implementation of it."""
    merge_adjacent_runs(p)
    text = paragraph_text(p)
    if not text.strip():
        return False
    did = False
    # Right to left: a deletion at the end must not move the offsets of one at
    # the start.
    if trailing:
        end = len(text)
        start = max(0, end - trailing)
        if start < end:
            apply_replacement(p, Anchor(start, end, text[start:end], ""),
                              author, date, ids)
            did = True
    if leading:
        cut = min(leading, len(text))
        if cut:
            apply_replacement(p, Anchor(0, cut, text[:cut], ""),
                              author, date, ids)
            did = True
    return did


def _delete_leading_tabs(p, ids, author: str, date: str) -> int:
    """Tab-only runs before the first word, deleted as a revision.

    Only runs that are nothing but tabs: wrapping a run that also holds text
    would delete the author's words to remove an indent."""
    deleted = 0
    for run in list(p.iter(R_TAG)):
        children = [c for c in run if c.tag != RPR_TAG]
        if not children:
            continue
        if any(c.tag == T_TAG and (c.text or "") for c in children):
            return deleted                    # reached the first word
        if all(c.tag == TAB_TAG for c in children):
            parent = run.getparent()
            wrapper = etree.Element(DEL_TAG, _rev_attrs(ids, author, date))
            parent.insert(parent.index(run), wrapper)
            wrapper.append(run)
            deleted += 1
    return deleted


def _style_links_tracked(p, style_id: str, ids, author: str, date: str) -> int:
    """Tag URLs with the hyperlink character style, recording what each run's
    formatting was so the change can be rejected like any other.

    The previous properties are read after the runs are split and before the
    style is set: splitting copies a run's `w:rPr`, so anything captured
    earlier would belong to a run that no longer exists — and rejecting the
    change would then strip the author's italics along with the link style."""
    from ...reassembler import _ensure_boundary, _text_spans

    ranges = link_ranges(paragraph_text(p))
    for start, end in reversed(ranges):        # right to left: offsets hold
        _ensure_boundary(p, start)
        _ensure_boundary(p, end)
        for t, s, e in _text_spans(p):
            if not (s >= start and e <= end and e > s):
                continue
            run = t.getparent()
            rpr = run.find(RPR_TAG)
            previous = (copy.deepcopy(rpr) if rpr is not None
                        else etree.Element(RPR_TAG))
            if rpr is None:
                rpr = etree.Element(RPR_TAG)
                run.insert(0, rpr)
            for existing in rpr.findall(RSTYLE_TAG):
                rpr.remove(existing)
            rpr.insert(0, etree.Element(RSTYLE_TAG, {qn("w:val"): style_id}))
            if rpr.find(RPRCHANGE_TAG) is None:
                change = etree.SubElement(rpr, RPRCHANGE_TAG,
                                          _rev_attrs(ids, author, date))
                change.append(previous)
    return len(ranges)
