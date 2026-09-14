"""Rejoin paragraphs a typeset export split across page boundaries.

A galley exported from a page layout (an InDesign or PDF-derived .docx) stores
each page's text separately, so a paragraph that spills from one page onto the
next arrives as TWO consecutive ``<w:p>`` elements. Read as two paragraphs, the
seam is a trap for every reviewer: the first half "ends" mid-sentence and the
second half "starts" without an opening quotation mark, so a terminal period
gets appended to the first and a quotation mark prepended to the second, and a
word hyphenated across the page break stays broken.

The export does record what happened, in paragraph geometry rather than text.
Under a first-line-indent convention a true paragraph's first line starts one
indent in from the body margin, whether the export wrote that as
``left=248 firstLine=300`` (a multi-line paragraph) or ``left=548 firstLine=0``
(a one-line paragraph, whose indent it folds into ``left``). A continuation's
first line starts flush at the body margin: ``left=248 firstLine=0``. So the
first-line START (``left + firstLine``) separates the two shapes exactly, and
that is the only evidence this module consults.

Text shape is deliberately NOT consulted. A continuation can begin with a
capital letter or an opening quotation mark (a speech that runs on across the
page) and a true paragraph can begin lowercase, so quote and period shape at a
seam is exactly the unreliable signal that produced the errors in the first
place. The geometry is the typesetter's own record of where each line began.

Joins are applied physically to the working baseline: the head paragraph keeps
its properties, indent and section break; the continuation's content moves onto
its end; the continuation element is dropped. The join is recorded, never
tracked: the receipt lists every join with its seam offsets so the change
report and any audit can name them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import logging
import re
from collections import Counter

from lxml import etree

from .utils.xml_helpers import (P_TAG, R_TAG, RPR_TAG, T_TAG, WalkedParagraph,
                                iter_text_elements, paragraph_text, qn,
                                set_text, walk_package)

log = logging.getLogger(__name__)

POLICY = "join-indent-continuations-v1"
CONVENTION_SHARE = 0.60   # prose body paragraphs that carry explicit geometry
INDENTED_SHARE = 0.25     # ...of which at least this share start indented
MIN_INDENTED = 10

_BODY_PART = "word/document.xml"
_PPR = qn("w:pPr")
_IND = qn("w:ind")
_SECT = qn("w:sectPr")
_STYLE = qn("w:pStyle")
_WORD = re.compile(r"\w+", re.UNICODE)


class RunoverError(ValueError):
    pass


@dataclass(frozen=True)
class Seam:
    separator: str           # " " or ""
    drop_hyphen: bool
    hyphen: str | None       # None | "dropped" | "kept" | "undecided"
    offset: int              # index in the joined text where the continuation begins
    section_break: str | None  # None | "kept" | "transferred"


@dataclass
class RunoverJoin:
    para_id: str                          # head id in the pre-join walk
    element: etree._Element = field(repr=False)
    absorbed: list[str] = field(default_factory=list)
    absorbed_elements: list[etree._Element] = field(default_factory=list, repr=False)
    seams: list[Seam] = field(default_factory=list)
    text: str = ""                        # the joined text, as the walker will read it


def _first_line(p: etree._Element) -> tuple[float, float] | None:
    """(left, firstLine) in twentieths of a point, or None when the paragraph
    carries no explicit indent geometry (it inherits its style's, which this
    module never reads)."""
    ppr = p.find(_PPR)
    ind = None if ppr is None else ppr.find(_IND)
    if ind is None:
        return None
    try:
        left = float(ind.get(qn("w:left"), 0) or 0)
        first = float(ind.get(qn("w:firstLine"), 0) or 0)
    except ValueError:
        return None
    return left, first


def _start(p: etree._Element) -> int | None:
    geometry = _first_line(p)
    return None if geometry is None else round(geometry[0] + geometry[1])


def _style(p: etree._Element) -> str:
    ppr = p.find(_PPR)
    el = None if ppr is None else ppr.find(_STYLE)
    return el.get(qn("w:val")) if el is not None else "Normal"


def _heading(style: str, cfg) -> bool:
    return cfg.skip.fully_skipped(style) or cfg.skip.is_sweep_only(style)


def _sectpr(p: etree._Element):
    ppr = p.find(_PPR)
    return None if ppr is None else ppr.find(_SECT)


def _body_paragraphs(pkg) -> list[WalkedParagraph]:
    return [wp for wp in walk_package(pkg)
            if wp.part == _BODY_PART and wp.location == "body"]


def indent_convention(pkg, cfg) -> dict:
    """Whether the body uses a first-line-indent convention, and its two
    positions: the body margin (where a continuation's first line starts) and
    the indented start (where a true paragraph's first line starts)."""
    prose = [wp for wp in _body_paragraphs(pkg)
             if paragraph_text(wp.element).strip() and not _heading(_style(wp.element), cfg)]
    explicit = [wp for wp in prose if _first_line(wp.element) is not None]
    starts = Counter(_start(wp.element) for wp in explicit)
    lefts = Counter(round(_first_line(wp.element)[0]) for wp in explicit
                    if _first_line(wp.element)[1] > 0)
    indented = [wp for wp in explicit if _first_line(wp.element)[1] > 0]
    margin = lefts.most_common(1)[0][0] if lefts else None
    indent_start = None
    if indented:
        indent_start = Counter(_start(wp.element) for wp in indented).most_common(1)[0][0]
    applies = (bool(prose) and margin is not None and indent_start is not None
               and indent_start != margin
               and len(explicit) / len(prose) >= CONVENTION_SHARE
               and len(indented) >= max(MIN_INDENTED, INDENTED_SHARE * len(explicit)))
    return {"prose_paragraphs": len(prose), "explicit_geometry": len(explicit),
            "indented": len(indented), "margin": margin, "indented_start": indent_start,
            "starts": {str(k): v for k, v in sorted(starts.items(), key=lambda x: (x[0] is None, x[0]))},
            "share_threshold": CONVENTION_SHARE, "applies": bool(applies)}


def _seam(left: str, right: str, knows) -> tuple[str, bool, str | None]:
    """How the two halves meet: (separator, drop_hyphen, hyphen_status)."""
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return "", False, None
    if left.endswith("-"):
        if right[0].islower():
            last = _WORD.findall(left[:-1])
            first = _WORD.findall(right)
            candidate = (last[-1] if last else "") + (first[0] if first else "")
            known = knows(candidate) if (knows and last and first) else None
            if known is True:
                return "", True, "dropped"
            return "", False, "undecided" if known is None else "kept"
        return "", False, "kept"
    if left.endswith(("—", "…")):
        return "", False, None
    return " ", False, None


def find_runover_joins(pkg, cfg, *, dictionary: str | None = "en_US") -> tuple[list[RunoverJoin], dict]:
    """Every page-runover chain in the body, without touching the package."""
    convention = indent_convention(pkg, cfg)
    refusals: Counter = Counter()
    diagnostics = {"policy": POLICY, "convention": convention, "refusals": refusals,
                   "joins": 0, "absorbed": 0}
    if not convention["applies"]:
        diagnostics["gate"] = "no_indent_convention"
        return [], diagnostics
    knows = None
    if dictionary:
        from .spellscan import dictionary_knows
        knows = lambda word: dictionary_knows(word, dictionary)  # noqa: E731
    margin, indent_start = convention["margin"], convention["indented_start"]
    joins: list[RunoverJoin] = []
    chain: RunoverJoin | None = None
    previous: WalkedParagraph | None = None
    for wp in _body_paragraphs(pkg):
        p = wp.element
        text = paragraph_text(p)
        continuation = (bool(text.strip()) and _start(p) == margin
                        and _first_line(p) is not None and _first_line(p)[1] == 0)
        if not continuation:
            chain = None
            previous = wp
            continue
        head = chain
        if head is not None and head.absorbed_elements[-1].getnext() is not p:
            head, chain = None, None
        if head is None:
            if previous is None or not paragraph_text(previous.element).strip():
                refusals["empty_between"] += 1
            elif previous.element.getnext() is not p:
                refusals["not_adjacent"] += 1
            elif _heading(_style(previous.element), cfg):
                refusals["heading"] += 1
            elif _start(previous.element) != indent_start:
                refusals["head_not_indented"] += 1
            else:
                head = RunoverJoin(previous.para_id, previous.element,
                                   text=paragraph_text(previous.element))
        if head is None:
            chain = None
            previous = wp
            continue
        if _heading(_style(p), cfg):
            refusals["heading"] += 1
        elif _style(p) != _style(head.element):
            refusals["style_mismatch"] += 1
        elif _sectpr(p) is not None and (_sectpr(head.element) is not None or any(
                s.section_break == "transferred" for s in head.seams)):
            refusals["both_section_breaks"] += 1
            log.info("Runover join refused at %s: both halves end a section", wp.para_id)
        else:
            separator, drop, hyphen = _seam(head.text, text, knows)
            base = head.text[:-1] if drop else head.text
            section = None
            if _sectpr(p) is not None:
                section = "transferred"
            elif _sectpr(head.element) is not None and not head.seams:
                section = "kept"
            head.seams.append(Seam(separator, drop, hyphen, len(base) + len(separator), section))
            head.text = base + separator + text
            head.absorbed.append(wp.para_id)
            head.absorbed_elements.append(p)
            if chain is None:
                joins.append(head)
                chain = head
            previous = wp
            continue
        chain = None
        previous = wp
    diagnostics["joins"] = len(joins)
    diagnostics["absorbed"] = sum(len(j.absorbed) for j in joins)
    diagnostics["refusals"] = dict(refusals)
    return joins, diagnostics


def _last_text(p: etree._Element):
    last = None
    for t in iter_text_elements(p):
        if t.text:
            last = t
    return last


def apply_runover_joins(pkg, joins: list[RunoverJoin]) -> list[dict]:
    """Merge each chain into its head element. Returns receipt rows."""
    if not joins:
        return []
    for join in joins:
        head = join.element
        for b, seam in zip(join.absorbed_elements, join.seams):
            if seam.section_break == "transferred":
                head_ppr = head.find(_PPR)
                if head_ppr is None:
                    head_ppr = etree.Element(_PPR)
                    head.insert(0, head_ppr)
                if head_ppr.find(_SECT) is not None:
                    raise RunoverError("A runover join would carry two section breaks")
                head_ppr.append(_sectpr(b))
            if seam.drop_hyphen:
                t = _last_text(head)
                if t is None or not t.text.endswith("-"):
                    raise RunoverError("A runover seam lost its line-break hyphen")
                set_text(t, t.text[:-1])
            if seam.separator:
                run = etree.Element(R_TAG)
                runs = list(head.iter(R_TAG))
                if runs and runs[-1].find(RPR_TAG) is not None:
                    run.append(copy.deepcopy(runs[-1].find(RPR_TAG)))
                set_text(etree.SubElement(run, T_TAG), seam.separator)
                head.append(run)
            for child in list(b):
                if child.tag != _PPR:
                    head.append(child)
            b.getparent().remove(b)
    pkg.mark_modified(_BODY_PART)
    baseline_ids = {id(wp.element): wp.para_id for wp in walk_package(pkg)}
    rows = []
    for join in joins:
        text = paragraph_text(join.element)
        if text != join.text:
            raise RunoverError("A runover join did not produce the text it recorded")
        rows.append({
            "para_id": join.para_id,
            "baseline_para_id": baseline_ids[id(join.element)],
            "absorbed": list(join.absorbed),
            "seam_offsets": [s.offset for s in join.seams],
            "separators": [s.separator for s in join.seams],
            "hyphens": [s.hyphen for s in join.seams],
            "section_break": next((s.section_break for s in join.seams if s.section_break), None),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return rows


def join_runover_paragraphs(pkg, cfg, *, dictionary: str | None = "en_US") -> tuple[list[dict], dict]:
    """Find and apply every join, then prove the result is a fixed point."""
    joins, diagnostics = find_runover_joins(pkg, cfg, dictionary=dictionary)
    if not joins:
        return [], diagnostics
    rows = apply_runover_joins(pkg, joins)
    remaining, _ = find_runover_joins(pkg, cfg, dictionary=dictionary)
    if remaining:
        raise RunoverError("Runover joining did not reach a fixed point")
    log.info("Rejoined %d page-runover paragraph(s) (%d continuation line(s)).",
             len(rows), sum(len(r["absorbed"]) for r in rows))
    return rows, diagnostics
