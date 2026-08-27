"""Each speaker's line as its own paragraph — the split made, not asked about.

Convention (and the house proofreader's ruling, Johnson Book 1) gives each
speaker their own paragraph. Where two quoted speeches share one with nothing
but whitespace between them — `…won't go.” “You will…` — the fix is mechanical
and this pass makes it: the paragraph is split at the boundary as a TRACKED
change (the first paragraph's mark carries `w:ins`, so rejecting it merges the
paragraphs back exactly), and a declarative margin comment states what was done
and why. The judgment cases — a second speaker behind narration, an ambiguous
reply — stay with the `speaker_change` error type, which asks; this pass owns
only the pattern a script can be certain of.

It runs in prepare, after normalization and BEFORE the ingest snapshot, so the
document model, every anchor, and the reject-all audit all measure against the
split text; para_ids are positional, so splitting later would renumber every
paragraph after the boundary out from under the model. The comment rides the
ordinary findings channel (a force_query minted per split once the model is
built), so it is counted, deduped, gated and written like any other query.

Only body paragraphs split: dialogue in a table cell, textbox or footnote is
rare enough to leave to the query, and a paragraph carrying a section break
(w:sectPr) is never touched.
"""
from __future__ import annotations

import copy
import itertools
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from .models import Finding, ParagraphRef
from .utils.xml_helpers import (T_TAG, TXBXCONTENT_TAG, iter_content_elements,
                                paragraph_text, qn, set_text, walk_package)

log = logging.getLogger("docproof.speakersplit")

PPR_TAG = qn("w:pPr")
RPR_TAG = qn("w:rPr")
SECTPR_TAG = qn("w:sectPr")
P_INS_TAG = qn("w:ins")

# A quoted speech ending in terminal punctuation, its closing quote, whitespace
# only, and the opening quote of the next speech. The split lands at the opening
# quote. Double-primary and single-primary variants each get their own marks;
# the single-primary pattern leans on the terminal-punctuation requirement to
# keep clear of apostrophes ("the boys’ ‘ouse" has an s, not a period, before
# its ’ and never matches).
_BOUNDARY_DOUBLE = re.compile(r"[.!?…—]”[  ]+“")
_BOUNDARY_SINGLE = re.compile(r"[.!?…—]’[  ]+‘")

# The declarative comment the head proofreader asked for — states the change,
# never asks whether to make it.
SPLIT_COMMENT = (
    "I’ve separated the dialogue so each new speaker begins on a new line. "
    "It’s standard practice in Chicago style and helps the reader follow the "
    "conversation more easily. The change is purely for clarity — the wording "
    "itself is untouched.")

_REV_ID_BASE = 900001   # clear of the reassembler's revision ids


@dataclass(frozen=True)
class SpeakerSplit:
    """One split already made: the paragraph the second speaker now owns."""
    part: str
    para_id: str          # the SECOND paragraph's id, from the post-split walk
    text: str             # its canonical text at split time


def find_split_offsets(text: str, variant=None) -> list[int]:
    """Offsets (at the opening quote) where a certain two-speaker boundary
    sits. Empty unless the paragraph's quotes are balanced and unambiguous."""
    single = bool(variant is not None and variant.opens_dialogue_with_single)
    if single:
        if "'" in text or not text.lstrip().startswith("‘"):
            return []
        pattern = _BOUNDARY_SINGLE
    else:
        if '"' in text:
            return []                 # the normalizer could not place these
        opens, closes = text.count("“"), text.count("”")
        if opens != closes or opens < 2:
            return []
        pattern = _BOUNDARY_DOUBLE
    return [m.end() - 1 for m in pattern.finditer(text)]


def _content_spans(p) -> list[tuple[object, int, int]]:
    """(element, lo, hi) for every content element, in paragraph_text
    coordinates — the same walk `paragraph_text` makes."""
    spans, off = [], 0
    for el in iter_content_elements(p):
        n = len(el.text or "") if el.tag == T_TAG else 1
        spans.append((el, off, off + n))
        off += n
    return spans


def _top_of(el, p):
    top = el
    while top.getparent() is not p:
        top = top.getparent()
    return top


def _content_els(subtree) -> list:
    """Content elements within one top-level child, in document order —
    mirrors iter_content_elements' tag set so indexes survive a deepcopy."""
    from .utils.xml_helpers import BR_TAG, CR_TAG, TAB_TAG
    return list(subtree.iter(T_TAG, BR_TAG, CR_TAG, TAB_TAG))


def _split_paragraph(p, offset: int):
    """Split `p` at `offset` (paragraph_text coordinates). The new paragraph is
    inserted after `p` and takes everything from the offset on; `p`'s own
    paragraph mark is tagged as inserted by the caller. Returns the new
    paragraph, or None when the offset cannot be resolved to the XML."""
    spans = _content_spans(p)
    inside = next(((el, lo) for el, lo, hi in spans
                   if el.tag == T_TAG and lo < offset < hi), None)

    tops = [c for c in p if c.tag != PPR_TAG]
    if inside is not None:
        el, lo = inside
        top = _top_of(el, p)
        at = offset - lo
        # Split the top-level child holding `el`: the original keeps content up
        # to the boundary, a deep copy keeps the rest, indexed by content-
        # element position so nested structures (hyperlinks, smart tags) carry.
        els = _content_els(top)
        idx = els.index(el)
        top2 = copy.deepcopy(top)
        els2 = _content_els(top2)
        for late in els[idx + 1:]:
            late.getparent().remove(late)
        set_text(el, (el.text or "")[:at])
        for early in els2[:idx]:
            early.getparent().remove(early)
        e2 = els2[idx]
        if e2.tag == T_TAG:
            set_text(e2, (e2.text or "")[at:])
        top.addnext(top2)
        moved_from = tops.index(top) + 1
        tops = [c for c in p if c.tag != PPR_TAG]
    else:
        # The boundary falls exactly between elements: move from the first
        # top-level child whose content starts at or after the offset.
        starts: dict[int, int] = {}
        for el, lo, hi in spans:
            t = _top_of(el, p)
            i = tops.index(t)
            starts.setdefault(i, lo)
        moved_from = next((i for i in sorted(starts)
                           if starts[i] >= offset), None)
        if moved_from is None:
            return None

    new_p = p.makeelement(p.tag, {})
    ppr = p.find(PPR_TAG)
    if ppr is not None:
        new_p.append(copy.deepcopy(ppr))
    for child in tops[moved_from:]:
        new_p.append(child)          # lxml moves, preserving order
    p.addnext(new_p)
    return new_p


def _mark_paragraph_inserted(p, rev_id: int, author: str, date: str) -> None:
    """Tag `p`'s paragraph mark as a tracked insertion, so rejecting the
    change merges `p` with the paragraph that follows it."""
    ppr = p.find(PPR_TAG)
    if ppr is None:
        ppr = p.makeelement(PPR_TAG, {})
        p.insert(0, ppr)
    rpr = ppr.find(RPR_TAG)
    if rpr is None:
        rpr = ppr.makeelement(RPR_TAG, {})
        # rPr sits after the paragraph properties but before any sectPr.
        sect = ppr.find(SECTPR_TAG)
        ppr.insert(list(ppr).index(sect) if sect is not None else len(ppr), rpr)
    # Exactly one w:ins per mark: a paragraph split twice would otherwise
    # accumulate a stale entry each round (the fragment that inherits the mark
    # takes the old one with it in its pPr copy).
    for stale in rpr.findall(P_INS_TAG):
        rpr.remove(stale)
    rpr.append(rpr.makeelement(P_INS_TAG, {
        qn("w:id"): str(rev_id), qn("w:author"): author, qn("w:date"): date}))


def split_package(pkg, variant=None, *, author: str,
                  date: str | None = None,
                  max_splits: int = 300) -> list[SpeakerSplit]:
    """Find and perform every certain speaker split in the package. Mutates the
    package in place; returns one record per split, carrying the para_id the
    SECOND speaker's paragraph holds after the surgery (from a fresh walk, so
    it matches the ids the document model will assign)."""
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sites: list[tuple[str, object, list[int]]] = []
    total = 0
    for wp in walk_package(pkg):
        if wp.location != "body":
            continue
        ppr = wp.element.find(PPR_TAG)
        if ppr is not None and ppr.find(SECTPR_TAG) is not None:
            continue
        # A paragraph anchoring a textbox owns two stories; splitting it would
        # have to divide the box's content elements from the flow's, and the
        # speaker_change query covers the case instead.
        if wp.element.find(f".//{TXBXCONTENT_TAG}") is not None:
            continue
        offsets = find_split_offsets(paragraph_text(wp.element), variant)
        if not offsets:
            continue
        if total + len(offsets) > max_splits:
            log.warning("Speaker splits capped at %d; %s and later boundaries "
                        "left to the speaker_change query.", max_splits,
                        wp.para_id)
            break
        total += len(offsets)
        sites.append((wp.part, wp.element, offsets))
    if not sites:
        return []

    new_elements: list = []
    rev = itertools.count(_REV_ID_BASE)
    parts: set[str] = set()
    for part, el, offsets in sites:
        # Right to left, so each split's offsets stay valid; every fragment
        # BEFORE a boundary gets its mark tagged as inserted (a fragment
        # created by a later pass inherits the mark in its pPr copy). The
        # boundary whitespace stays behind as a trailing space on each earlier
        # fragment ON PURPOSE: the snapshot baseline must equal the split text
        # exactly, and sweep_trailing_space later deletes it as an ordinary
        # audited tracked change.
        for off in sorted(offsets, reverse=True):
            new_p = _split_paragraph(el, off)
            if new_p is None:
                log.warning("A speaker boundary could not be resolved to the "
                            "XML; paragraph left whole.")
                continue
            _mark_paragraph_inserted(el, next(rev), author, date)
            new_elements.append(new_p)
            parts.add(part)
    for part in parts:
        pkg.mark_modified(part)
    if not new_elements:
        return []

    wanted = {id(e) for e in new_elements}
    records = [SpeakerSplit(wp.part, wp.para_id, paragraph_text(wp.element))
               for wp in walk_package(pkg) if id(wp.element) in wanted]
    log.info("Speaker splits: %d paragraph break(s) inserted as tracked "
             "changes.", len(records))
    return records


def split_findings(records: Sequence[SpeakerSplit],
                   paragraphs: Sequence[ParagraphRef]) -> list[Finding]:
    """The declarative margin comment for each split, as an ordinary query
    finding — counted, deduped and written like any other."""
    from .sweeps import sentence_window

    by_id = {p.para_id: p for p in paragraphs}
    findings: list[Finding] = []
    n = 0
    for r in records:
        para = by_id.get(r.para_id)
        if para is None:
            continue
        window, _lo, occurrence = sentence_window(para.text, 0, 1)
        if not window:
            continue
        n += 1
        findings.append(Finding(
            finding_id=f"ss-{n:04d}",
            chunk_id="sweep",
            para_id=r.para_id,
            error_type="speaker_split",
            original_text=window,
            occurrence=occurrence,
            corrected_text=window,
            explanation=SPLIT_COMMENT,
            confidence="high",
            force_query=True))
    return findings
