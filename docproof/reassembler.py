from __future__ import annotations

import copy
import itertools
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence

from lxml import etree

from .config import Config
from .models import Anchor, DocumentModel, Finding, index_paragraphs
from .utils.xml_helpers import (CT_NS, DELTEXT_TAG, DEL_TAG, DR_NS, DocxPackage,
                                INS_TAG, P_TAG, PR_NS, RPR_TAG, R_TAG, T_TAG,
                                TEXT_SKIP_ANCESTORS,
                                W_NS, merge_adjacent_runs, paragraph_text, qn,
                                set_text, walk_package)

log = logging.getLogger("docproof.reassembler")


@dataclass(frozen=True)
class ReassemblyStats:
    applied: tuple[str, ...]   # finding_ids written into the document
    skipped: tuple[str, ...]   # finding_ids refused by defense-in-depth checks
    queried: tuple[str, ...] = ()    # finding_ids written as comments only
    unplaced: tuple[str, ...] = ()   # queries with nowhere to hang a comment
    # Formatting findings whose text was already set that way. Not a failure:
    # the model reads plain text and cannot see italics, so it reports every
    # title it finds and this is where the correct ones land.
    already_set: tuple[str, ...] = ()


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


# --- tracked formatting changes ----------------------------------------------

# w:rPr's children are a schema-enforced sequence, so a mark cannot simply be
# appended. These are the elements that legally precede w:i; the new mark goes
# in front of the first child that is not one of them. w:rPrChange is last in
# the sequence, so appending it is always right.
_BEFORE_ITALIC = {qn(t) for t in ("w:rStyle", "w:rFonts", "w:b", "w:bCs")}
# (primary mark, complex-script companion). Only the primary decides whether a
# run is already set: Word writes w:iCs alongside w:i for complex scripts, but
# plenty of documents carry w:i alone, and treating those as "not italic yet"
# would mark every correctly-italicised title as a change to click through.
_MARKS = {"italic": ("w:i", "w:iCs")}


def _is_set(rpr, tag: str) -> bool:
    """Whether a run property is on. A toggle present with w:val="0" is the
    document explicitly turning it OFF, which is not the same as absent."""
    el = rpr.find(qn(tag))
    return el is not None and (el.get(qn("w:val"))
                               not in ("0", "false", "off"))


def apply_format_change(p, a: Anchor, mark: str, author: str, date: str, ids
                        ) -> tuple[etree._Element, etree._Element] | None:
    """Mark [a.start, a.end) with a run property, as a tracked revision.

    A long-work title set in roman is a house-style error that changes no
    text, so it cannot be expressed as an insertion and a deletion. Word
    records it instead as w:rPrChange: the run carries its NEW properties, and
    the change element holds the OLD ones, which is what "reject" restores.

    Returns the runs to hang a comment between, or None when every run in the
    span already carries the mark — re-marking italic text would be a revision
    the author has to click through for no reason."""
    tags = _MARKS.get(mark)
    if tags is None:
        log.error("Unknown run formatting %r; skipping.", mark)
        return None

    _ensure_boundary(p, a.start)
    _ensure_boundary(p, a.end)
    covered = [t for (t, s, e) in _text_spans(p)
               if s >= a.start and e <= a.end and e > s]
    runs = list(dict.fromkeys(t.getparent() for t in covered))
    touched: list[etree._Element] = []

    for r in runs:
        rpr = r.find(RPR_TAG)
        if rpr is None:
            rpr = etree.Element(RPR_TAG)
            r.insert(0, rpr)
        if _is_set(rpr, tags[0]):
            continue                              # already set the right way

        # The old properties, minus any revision record of their own: a
        # rPrChange nested inside a rPrChange is not a thing.
        old = copy.deepcopy(rpr)
        for nested in old.findall(qn("w:rPrChange")):
            old.remove(nested)

        # One insertion point, then step forward: computing it afresh per tag
        # would put w:iCs in front of w:i, which the schema forbids.
        at = len(rpr)
        for i, child in enumerate(rpr):
            if child.tag not in _BEFORE_ITALIC:
                at = i
                break
        for tag in tags:
            if rpr.find(qn(tag)) is None:
                rpr.insert(at, etree.Element(qn(tag)))
                at += 1

        change = _rev_el("w:rPrChange", ids, author, date)
        change.append(old)
        rpr.append(change)                        # always last in the sequence
        touched.append(r)

    if not touched:
        return None
    return touched[0], touched[-1]


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
            elif c.tag in TEXT_SKIP_ANCESTORS:
                # A textbox's own text and an mc:Fallback duplicate are not this
                # paragraph's canonical text — iter_text_elements skips them, so
                # this view must too, or the reject-all audit sees text on the
                # title page (a cover text box + its fallback) that the ingested
                # baseline never counted, and fails a paragraph nothing touched.
                continue
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

    def attach_to_span(self, p, start: int, end: int, text: str) -> bool:
        """Anchor a comment to a range of the paragraph's text, with no
        revision around it — the query channel: this asks, and changes
        nothing.

        Must run before any tracked change is applied to the paragraph, because
        it works in canonical-text offsets and applying a deletion moves text
        out of w:t. Splitting runs and inserting range markers does not shift
        those offsets, so the edits that follow still land correctly."""
        _ensure_boundary(p, start)
        _ensure_boundary(p, end)
        covered = [t for (t, s, e) in _text_spans(p)
                   if s >= start and e <= end and e > s]
        if not covered:
            return False
        self.attach(p, covered[0].getparent(), covered[-1].getparent(), text)
        return True

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


# --- the words the spell scan took on trust -----------------------------------

def _excluded_note(words: Sequence[str], limit: int) -> str:
    """The body of the top-of-document comment: what was excluded, and the ask."""
    shown = sorted(set(words), key=str.lower)
    n = len(shown)
    if n == 1:
        noun, took, flagged, confirm = "word", "it", "it was", "it is"
    else:
        noun, took, flagged, confirm = "words", "them", "they were", "each is"
    head = (f"DocProof left {n} {noun} out of the spell-check, taking {took} for "
            f"the author's own vocabulary rather than a misspelling — so "
            f"{flagged} never flagged. Worth a human eye to confirm {confirm} "
            f"spelled as intended: ")
    if n > limit:
        return head + ", ".join(shown[:limit]) + f", …and {n - limit} more."
    return head + ", ".join(shown) + "."


def annotate_excluded_words(pkg: DocxPackage, doc: DocumentModel,
                            words: Sequence[str], author: str, *,
                            date: str | None = None, limit: int = 120) -> bool:
    """Anchor one comment at the top of the document naming the words the spell
    scan excluded from checking — the ones it judged the author's own and so
    never flagged. A proofreader opening the delivered file sees, at the very
    first word, exactly which spellings were taken on trust and can check them by
    eye. This is a note, not a change: nothing in the text is edited.

    It hangs on the first word of the first body paragraph, and — like every
    query comment — inserts only range markers, which leave the canonical text
    (and so every later edit's offsets) untouched. Runs before the tracked
    changes for that reason. Returns True when the comment was placed, False when
    there is nothing to say or nowhere in the body to hang it."""
    if not words:
        return False
    first = next((p for p in doc.paragraphs
                  if p.part == "word/document.xml" and p.text.strip()), None)
    if first is None:
        return False
    elem = {wp.para_id: wp.element for wp in walk_package(pkg)
            if wp.part == "word/document.xml"}.get(first.para_id)
    if elem is None:
        return False
    merge_adjacent_runs(elem)
    if paragraph_text(elem) != first.text:        # canonical-text drift: refuse
        return False
    m = re.search(r"\S+", first.text)
    start, end = (m.start(), m.end()) if m else (0, min(1, len(first.text)))

    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = _Comments(pkg, author, date)
    pkg.mark_modified("word/document.xml")
    placed = comments.attach_to_span(elem, start, end, _excluded_note(words, limit))
    if placed:
        log.info("Top-of-document comment: named %d word(s) excluded from "
                 "spell-check.", len(set(words)))
    return placed


# --- untracked application (working copies for multi-round review) -----------

def _accept_all_revisions(p) -> None:
    """Turn the tracked changes just written into plain text: drop every
    deletion, unwrap every insertion. Only the revisions this module writes are
    present (the source is preflight-clean), so this is the whole of "accept
    all" — there are no move or format records to consider."""
    for d in list(p.iter(DEL_TAG)):
        parent = d.getparent()
        if parent is not None:
            parent.remove(d)
    for ins in list(p.iter(INS_TAG)):
        parent = ins.getparent()
        if parent is None:                       # was nested in a removed del
            continue
        at = parent.index(ins)
        for child in list(ins):
            parent.insert(at, child)
            at += 1
        parent.remove(ins)


def apply_untracked(pkg: DocxPackage, doc: DocumentModel,
                    edits_by_para: dict[str, list[tuple[int, int, str]]]) -> None:
    """Apply span edits to the document's text in place, with NO revision
    markup — the working copy a later review round reads.

    Multi-round review corrects the manuscript between rounds and reviews the
    corrected text; the delivered file's tracked changes are composed once at the
    end against the original (docproof/editlayer.py), so these intermediate
    copies only need the right text, never a revision. Each paragraph's edits are
    in its canonical-text coordinates (an EditLayer's entries) and must be
    disjoint.

    Reuses apply_replacement — all of the run-splitting, insertion, and
    hyperlink-adjacency handling — to make the edits as tracked changes, then
    accepts them: the text lands exactly as EditLayer.render would, run
    formatting is preserved, and nothing tracked remains, so the next round's
    preflight (which refuses a document that already carries tracked changes) is
    satisfied."""
    if not edits_by_para:
        return
    paras = index_paragraphs(doc)
    ids = itertools.count(_next_rev_id(pkg))         # transient; accepted away
    author, date = "docproof", "1970-01-01T00:00:00Z"    # discarded on accept
    parts = {paras[pid].part for pid in edits_by_para if pid in paras}
    for part in parts:
        pkg.mark_modified(part)
        elem_by_id = {wp.para_id: wp.element
                      for wp in walk_package(pkg) if wp.part == part}
        for para_id, edits in edits_by_para.items():
            if para_id not in paras or paras[para_id].part != part or not edits:
                continue
            p = elem_by_id.get(para_id)
            if p is None:
                log.error("%s vanished before untracked apply — skipping.",
                          para_id)
                continue
            merge_adjacent_runs(p)                        # text-preserving
            expected = paras[para_id].text
            if paragraph_text(p) != expected:            # defense-in-depth
                log.error("Canonical-text drift in %s — refusing untracked "
                          "edit.", para_id)
                continue
            # Descending start (then end) so an edit never shifts the offsets of
            # the ones still to come, exactly as apply_tracked_changes does; the
            # revisions are accepted once the paragraph is fully edited.
            for start, end, repl in sorted(edits, key=lambda e: (e[0], e[1]),
                                           reverse=True):
                apply_replacement(
                    p, Anchor(start, end, expected[start:end], repl),
                    author, date, ids)
            _accept_all_revisions(p)


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


def _query_span(f: Finding, para_text: str) -> tuple[int, int]:
    """What a query's comment should highlight: the sentence it is asking
    about, not the characters an edit would have touched.

    A below-gate finding carries the shrunk anchor of the change that was not
    made, which for a comma splice is one comma. A comment hanging off a
    single comma tells the author nothing about what is being questioned."""
    from .validator import find_nth
    s = find_nth(para_text, f.original_text, f.occurrence)
    if s == -1:                                  # cannot happen after the
        return f.anchor.start, f.anchor.end      # validator, but cheap to keep
    return s, s + len(f.original_text)


def _query_text(f: Finding) -> str:
    """What a query says in the margin. A question has to read as one — an
    author who cannot tell a query from a correction has lost the distinction
    the two channels exist to draw."""
    if f.status == "query":
        return f.explanation or f"{f.error_type.replace('_', ' ')}: worth a look."
    kind = f.error_type.replace("_", " ")
    if f.status == "rejected_oversized":
        # A real catch whose fix rewrites more than a minimal edit should — a
        # run-on split in two, a restructured list. Not applied automatically,
        # but the author should still see it and make the change by hand.
        parts = [f"Possibly {kind} — the suggested fix was too large to apply "
                 f"as a minimal tracked change, so it is left for you to make "
                 f"by hand."]
    else:
        parts = [f"Possibly {kind} — left as written, because it may be deliberate."]
    if f.explanation:
        parts.append(f.explanation)
    if f.corrected_text and f.corrected_text != f.original_text:
        parts.append(f"Suggested: {f.corrected_text}")
    return " ".join(parts)


def apply_tracked_changes(pkg: DocxPackage, doc: DocumentModel,
                          findings: list[Finding], cfg: Config
                          ) -> ReassemblyStats:
    validated = [f for f in findings if f.status == "validated"]
    # Two channels, never blurred: a correction the author accepts or rejects,
    # and a question that edits nothing. Below-gate findings join the second —
    # the model thought something was wrong but not confidently enough to
    # touch it, which is exactly what a margin query is for. An oversized edit
    # joins it too: the catch is real, only its fix is too large to auto-apply,
    # so surfacing it as a comment beats dropping the information silently.
    wanted = {"query"}
    if cfg.query_comments:
        wanted.add("skipped_low_confidence")
        wanted.add("rejected_oversized")
    queries = [f for f in findings if f.status in wanted and f.anchor]
    if not validated and not queries:
        log.info("No validated findings; document untouched.")
        return ReassemblyStats((), ())

    paras = index_paragraphs(doc)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = itertools.count(_next_rev_id(pkg))
    applied: list[str] = []
    skipped: list[str] = []
    queried: list[str] = []
    unplaced: list[str] = []
    already: list[str] = []
    comments: _Comments | None = None

    by_part: dict[str, list[Finding]] = {}
    for f in validated + queries:
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

            # Queries go on first. They insert range markers rather than text,
            # so the offsets the edits below rely on are unchanged — whereas
            # applying a deletion first would move text out of w:t and leave
            # every later offset pointing at the wrong place.
            for f in sorted((x for x in plist if x.status != "validated"),
                            key=lambda x: x.anchor.start):
                if part != "word/document.xml":
                    unplaced.append(f.finding_id)
                    continue
                if comments is None:
                    comments = _Comments(pkg, cfg.revision_author, date)
                lo, hi = _query_span(f, paras[para_id].text)
                if comments.attach_to_span(p, lo, hi, _query_text(f)):
                    queried.append(f.finding_id)
                else:
                    unplaced.append(f.finding_id)

            # Descending start, so edits never shift the offsets of the ones
            # still to come — and descending end within a start, so a deletion
            # goes before a pure insertion at the same offset. The validator
            # lets that pair coexist; applied the other way round, the
            # inserted text lands inside the slice the deletion re-checks
            # below, and a validated edit is silently skipped.
            for f in sorted((x for x in plist if x.status == "validated"),
                            key=lambda x: (x.anchor.start, x.anchor.end),
                            reverse=True):
                a = f.anchor
                if paragraph_text(p)[a.start:a.end] != a.delete_text:
                    log.error("%s: anchor slice mismatch at apply time — "
                              "skipping.", f.finding_id)   # defense-in-depth 2
                    skipped.append(f.finding_id)
                    continue
                if f.format:
                    marked = apply_format_change(p, a, f.format,
                                                 cfg.revision_author, date, ids)
                    if marked is None:
                        already.append(f.finding_id)
                        continue
                    first, last = marked
                else:
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
    if queried or unplaced:
        log.info("Wrote %d margin quer%s, which change nothing%s.",
                 len(queried), "y" if len(queried) == 1 else "ies",
                 f"; {len(unplaced)} had nowhere to hang (Word keeps comments "
                 f"in the body only)" if unplaced else "")
    if already:
        log.info("%d title(s) were already set the way house style wants; "
                 "left alone rather than marked as a change to click through.",
                 len(already))
    return ReassemblyStats(tuple(applied), tuple(skipped), tuple(queried),
                           tuple(unplaced), tuple(already))