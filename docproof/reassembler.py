from __future__ import annotations

import copy
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from lxml import etree

from .config import Config
from .models import Anchor, DocumentModel, Finding, index_paragraphs
from .utils.xml_helpers import (CT_NS, DELTEXT_TAG, DEL_TAG, DR_NS, DocxPackage,
                                INS_TAG, P_TAG, PR_NS, RPR_TAG, R_TAG, T_TAG,
                                W_NS, merge_adjacent_runs, paragraph_text, qn,
                                set_text, walk_package)

log = logging.getLogger("docproof.reassembler")


@dataclass(frozen=True)
class ReassemblyStats:
    applied: tuple[str, ...]   # finding_ids written into the document
    skipped: tuple[str, ...]   # finding_ids refused by defense-in-depth checks


# --- offset map and run splitting --------------------------------------------

def _text_spans(p) -> list[tuple[etree._Element, int, int]]:
    spans, off = [], 0
    from .utils.xml_helpers import iter_text_elements
    for t in iter_text_elements(p):
        n = len(t.text or "")
        spans.append((t, off, off + n))
        off += n
    return spans


def _has_content(run) -> bool:
    for c in run:
        if c.tag == RPR_TAG:
            continue
        if c.tag == T_TAG and not (c.text or ""):
            continue
        return True
    return False


def _split_run(r, t, k: int) -> None:
    """Split run `r` around text element `t`: at character k of t.text when
    k > 0, or immediately before t when k == 0. Both halves keep a full copy
    of w:rPr; non-text children (tabs, breaks) land exactly once, on the side
    they originally preceded/followed."""
    parent = r.getparent()
    r_idx = parent.index(r)
    children = list(r)
    pos = children.index(t)

    left = copy.deepcopy(r)
    parent.insert(r_idx, left)
    left_children = list(left)

    if k > 0:
        lt = left_children[pos]
        full = t.text or ""
        set_text(lt, full[:k])
        for c in left_children[pos + 1:]:
            left.remove(c)
        set_text(t, full[k:])
        for c in children[:pos]:
            if c.tag != RPR_TAG:
                r.remove(c)
    else:
        for c in left_children[pos:]:
            left.remove(c)
        for c in children[:pos]:
            if c.tag != RPR_TAG:
                r.remove(c)

    for run in (left, r):                    # prune husks
        if not _has_content(run):
            run.getparent().remove(run)


def _ensure_boundary(p, off: int) -> None:
    """Guarantee that character offset `off` coincides with a run boundary,
    splitting a run if necessary."""
    spans = _text_spans(p)
    total = spans[-1][2] if spans else 0
    if off <= 0 or off >= total:
        return
    for t, s, e in spans:
        if s < off < e:
            _split_run(t.getparent(), t, off - s)
            return
        if s == off and e > s:
            r = t.getparent()
            prior_text = False
            for c in r:
                if c is t:
                    break
                if c.tag == T_TAG:
                    prior_text = True
            if prior_text:                   # boundary between two w:t of one run
                _split_run(r, t, 0)
            return


# --- the core operation -------------------------------------------------------

def _rev_el(tag: str, ids, author: str, date: str) -> etree._Element:
    return etree.Element(qn(tag), {qn("w:id"): str(next(ids)),
                                   qn("w:author"): author,
                                   qn("w:date"): date})


def apply_replacement(p, a: Anchor, author: str, date: str, ids
                      ) -> tuple[etree._Element, etree._Element]:
    """Apply one validated anchor to paragraph `p` as a tracked change.
    Returns (first, last) inserted revision elements, for comment anchoring."""
    dels: list[etree._Element] = []
    rpr = None

    if a.start < a.end:                              # deletion (or replacement)
        _ensure_boundary(p, a.start)
        _ensure_boundary(p, a.end)
        spans = _text_spans(p)
        covered = [t for (t, s, e) in spans
                   if s >= a.start and e <= a.end and e > s]
        runs = list(dict.fromkeys(t.getparent() for t in covered))

        groups: list[tuple[etree._Element, list]] = []
        for r in runs:                               # adjacency grouping —
            parent = r.getparent()                   # handles hyperlink-wrapped
            if (groups and groups[-1][0] is parent   # runs and interleaved
                    and groups[-1][1][-1].getnext() is r):   # bookmarks
                groups[-1][1].append(r)
            else:
                groups.append((parent, [r]))

        for parent, grp in groups:
            d = _rev_el("w:del", ids, author, date)
            parent.insert(parent.index(grp[0]), d)
            for r in grp:
                d.append(r)                          # move, don't copy
            for t in d.iter(T_TAG):
                t.tag = DELTEXT_TAG                  # attrs (xml:space) survive
            dels.append(d)

        first_r = dels[0].find(R_TAG)
        rpr = first_r.find(RPR_TAG) if first_r is not None else None
        ins_parent = dels[-1].getparent()
        ins_at = ins_parent.index(dels[-1]) + 1

    else:                                            # pure insertion
        _ensure_boundary(p, a.start)
        spans = _text_spans(p)
        ins_parent, ins_at = p, len(p)
        prev_run = None
        for t, s, e in spans:
            if e <= a.start and e > s:
                prev_run = t.getparent()
            if s >= a.start and e > s:
                run = t.getparent()
                ins_parent, ins_at = run.getparent(), run.getparent().index(run)
                break
        if prev_run is not None:                     # inherit preceding format
            rpr = prev_run.find(RPR_TAG)

    ins = None
    if a.insert_text:
        ins = _rev_el("w:ins", ids, author, date)
        r = etree.SubElement(ins, R_TAG)
        if rpr is not None:
            r.insert(0, copy.deepcopy(rpr))
        t = etree.SubElement(r, T_TAG)
        set_text(t, a.insert_text)
        ins_parent.insert(ins_at, ins)

    first = dels[0] if dels else ins
    last = ins if ins is not None else dels[-1]
    return first, last


# --- accept / reject views (the invariant checkers) ---------------------------

def paragraph_view_text(p, mode: Literal["accept", "reject"]) -> str:
    parts: list[str] = []

    def walk(el):
        for c in el:
            if c.tag == INS_TAG:
                if mode == "accept":
                    walk(c)
            elif c.tag == DEL_TAG:
                if mode == "reject":
                    walk(c)
            elif c.tag in (T_TAG, DELTEXT_TAG):
                parts.append(c.text or "")
            else:
                walk(c)

    walk(p)
    return "".join(parts)


# --- Word comments -------------------------------------------------------------

_CT_COMMENTS = ("application/vnd.openxmlformats-officedocument."
                "wordprocessingml.comments+xml")
_REL_COMMENTS = f"{DR_NS}/comments"


class _Comments:
    """Minimal classic comments part (word/comments.xml + relationship +
    content-type override). Modern Word additionally writes commentsExtended /
    commentsIds / commentsExtensible parts for threading and resolved-state;
    plain comments open fine without them, but if a strict environment
    complains, set `comments: false` in config — the explanations still land
    in summary.md either way."""

    def __init__(self, pkg: DocxPackage, author: str, date: str):
        self.author, self.date = author, date
        name = "word/comments.xml"
        if pkg.has(name):
            self.root = pkg.tree(name)
            pkg.mark_modified(name)
        else:
            self.root = etree.Element(qn("w:comments"), nsmap={"w": W_NS})
            pkg.add_part(name, self.root)
            self._register(pkg)
        existing = [int(c.get(qn("w:id"))) for c in self.root
                    if (c.get(qn("w:id")) or "").isdigit()]
        self.ids = itertools.count(max(existing, default=-1) + 1)

    def _register(self, pkg: DocxPackage) -> None:
        ct = pkg.tree("[Content_Types].xml")
        pkg.mark_modified("[Content_Types].xml")
        etree.SubElement(ct, f"{{{CT_NS}}}Override",
                         {"PartName": "/word/comments.xml",
                          "ContentType": _CT_COMMENTS})
        rels_name = "word/_rels/document.xml.rels"
        rels = pkg.tree(rels_name)
        pkg.mark_modified(rels_name)
        used = {r.get("Id") for r in rels}
        n = 1
        while f"rId{n}" in used:
            n += 1
        etree.SubElement(rels, f"{{{PR_NS}}}Relationship",
                         {"Id": f"rId{n}", "Type": _REL_COMMENTS,
                          "Target": "comments.xml"})

    def attach(self, p, first_el, last_el, text: str) -> None:
        cid = str(next(self.ids))
        af, al = _p_level(first_el, p), _p_level(last_el, p)
        start = etree.Element(qn("w:commentRangeStart"), {qn("w:id"): cid})
        end = etree.Element(qn("w:commentRangeEnd"), {qn("w:id"): cid})
        p.insert(p.index(af), start)
        p.insert(p.index(al) + 1, end)
        ref = etree.Element(R_TAG)
        etree.SubElement(ref, qn("w:commentReference"), {qn("w:id"): cid})
        p.insert(p.index(end) + 1, ref)

        c = etree.SubElement(self.root, qn("w:comment"),
                             {qn("w:id"): cid, qn("w:author"): self.author,
                              qn("w:date"): self.date, qn("w:initials"): "dp"})
        cp = etree.SubElement(c, P_TAG)
        cr = etree.SubElement(cp, R_TAG)
        ctxt = etree.SubElement(cr, T_TAG)
        set_text(ctxt, text)


def _p_level(el, p):
    while el.getparent() is not p:
        el = el.getparent()
    return el


# --- orchestration -------------------------------------------------------------

def _next_rev_id(pkg: DocxPackage) -> int:
    mx = 0
    for part in dict.fromkeys(wp.part for wp in walk_package(pkg)):
        for tag in (INS_TAG, DEL_TAG):
            for el in pkg.tree(part).iter(tag):
                v = el.get(qn("w:id")) or ""
                if v.isdigit():
                    mx = max(mx, int(v))
    return mx + 1


def apply_tracked_changes(pkg: DocxPackage, doc: DocumentModel,
                          findings: list[Finding], cfg: Config
                          ) -> ReassemblyStats:
    validated = [f for f in findings if f.status == "validated"]
    if not validated:
        log.info("No validated findings; document untouched.")
        return ReassemblyStats((), ())

    paras = index_paragraphs(doc)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = itertools.count(_next_rev_id(pkg))
    applied: list[str] = []
    skipped: list[str] = []
    comments: _Comments | None = None

    by_part: dict[str, list[Finding]] = {}
    for f in validated:
        by_part.setdefault(paras[f.para_id].part, []).append(f)

    for part, fs in by_part.items():
        pkg.mark_modified(part)
        elem_by_id = {wp.para_id: wp.element
                      for wp in walk_package(pkg) if wp.part == part}

        by_para: dict[str, list[Finding]] = {}
        for f in fs:
            by_para.setdefault(f.para_id, []).append(f)

        for para_id, plist in by_para.items():
            p = elem_by_id.get(para_id)
            if p is None:
                log.error("%s vanished between ingest and reassembly — refusing.",
                          para_id)
                skipped += [f.finding_id for f in plist]
                continue

            merge_adjacent_runs(p)                       # text-preserving
            if paragraph_text(p) != paras[para_id].text:  # defense-in-depth 1
                log.error("Canonical-text drift in %s — refusing to edit it.",
                          para_id)
                skipped += [f.finding_id for f in plist]
                continue

            for f in sorted(plist, key=lambda x: x.anchor.start, reverse=True):
                a = f.anchor
                if paragraph_text(p)[a.start:a.end] != a.delete_text:
                    log.error("%s: anchor slice mismatch at apply time — "
                              "skipping.", f.finding_id)   # defense-in-depth 2
                    skipped.append(f.finding_id)
                    continue
                first, last = apply_replacement(p, a, cfg.revision_author,
                                                date, ids)
                applied.append(f.finding_id)
                if cfg.comments and part == "word/document.xml":
                    if comments is None:
                        comments = _Comments(pkg, cfg.revision_author, date)
                    comments.attach(p, first, last,
                                    f.explanation or f"{f.error_type} fix")

    log.info("Applied %d tracked change(s); %d skipped by safety checks.",
             len(applied), len(skipped))
    return ReassemblyStats(tuple(applied), tuple(skipped))