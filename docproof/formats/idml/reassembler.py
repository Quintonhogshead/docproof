"""Tracked-change surgery on IDML stories, and the queries beside them.

Both channels, as in Word (docs/deliverables.md): a correction the author
accepts or rejects, and a question that edits nothing. The question is an
inline Note — the same element an explanation uses, and the only element
InDesign offers for either.

The counterpart of docproof/reassembler.py, and it keeps the same defences:
paragraphs are relocated by re-running the shared walker, canonical text is
re-derived and compared before anything is touched, each anchor's slice is
re-asserted at apply time, and anchors within a paragraph are applied
right-to-left so earlier offsets stay valid.

IDML makes the actual cut easier than OOXML does. Formatting lives on the
enclosing CharacterStyleRange, never on Content, so splitting text is just
splitting a Content node — there is no run property block to clone and no
xml:space to get wrong.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from lxml import etree

from ...config import Config
from ...models import Anchor, DocumentModel, Finding, index_paragraphs
from ...queries import query_span, query_text, wanted_statuses
from .walker import (CHANGE, CONTENT, CSR, DELETED, DESIGNMAP, DOCUMENT_USER,
                     INSERTED, NOTE, PSR, IdmlPackage, paragraph_text,
                     walk_package)

log = logging.getLogger("docproof.formats.idml.reassembler")

_DOCPROOF_USER = "dDocProofUser"


@dataclass(frozen=True)
class ReassemblyStats:
    applied: tuple[str, ...]         # finding_ids written as tracked changes
    skipped: tuple[str, ...]         # finding_ids refused by a safety check
    queried: tuple[str, ...] = ()    # finding_ids written as Notes only
    unplaced: tuple[str, ...] = ()   # queries with nowhere to hang a Note


def _stamp() -> str:
    """InDesign writes local time to the second with no zone suffix."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# --- the live paragraph -------------------------------------------------------

class _Paragraph:
    """A paragraph's Content nodes, kept in step with the tree as we edit it.

    The list holds exactly the nodes that contribute to *accepted* text, which
    is what offsets are measured against: a node wrapped in a DeletedText
    Change leaves the list, and the Content of an InsertedText Change joins it.
    """

    def __init__(self, walked):
        self.walked = walked
        self.nodes: list[etree._Element] = list(walked.contents)

    @property
    def text(self) -> str:
        return paragraph_text(self.nodes)

    def spans(self) -> list[tuple[etree._Element, int, int]]:
        spans, off = [], 0
        for node in self.nodes:
            length = len(node.text or "")
            spans.append((node, off, off + length))
            off += length
        return spans

    def ensure_boundary(self, off: int) -> None:
        """Guarantee that character offset `off` falls between two Content
        nodes, splitting one if it does not."""
        for i, (node, start, end) in enumerate(self.spans()):
            if start < off < end:
                k = off - start
                text = node.text or ""
                left = etree.Element(CONTENT)
                left.text = text[:k]
                node.text = text[k:]
                parent = node.getparent()
                parent.insert(parent.index(node), left)
                self.nodes.insert(i, left)
                return

    def insert_point(self, off: int) -> tuple[etree._Element, int, int]:
        """(parent, index within parent, index within self.nodes) for new text
        at `off`. Call only after ensure_boundary(off)."""
        pos = 0
        for i, node in enumerate(self.nodes):
            if pos == off:
                parent = node.getparent()
                return parent, parent.index(node), i
            pos += len(node.text or "")
        last = self.nodes[-1]                    # off is the paragraph's end
        parent = last.getparent()
        return parent, parent.index(last) + 1, len(self.nodes)


# --- the core operation -------------------------------------------------------

def _change(kind: str, author: str, date: str) -> etree._Element:
    return etree.Element(CHANGE, {"ChangeType": kind, "Date": date,
                                  "UserName": author,
                                  "AppliedDocumentUser": _DOCPROOF_USER})


def apply_replacement(para: _Paragraph, anchor: Anchor, author: str,
                      date: str) -> etree._Element:
    """Apply one validated anchor as a tracked change. Returns the first
    element written, which is where a Note gets anchored.

    InDesign writes the insertion before the deletion for a replacement, and so
    do we — the Story Editor reads in that order."""
    para.ensure_boundary(anchor.start)
    para.ensure_boundary(anchor.end)

    covered = [(i, node) for i, (node, start, end) in enumerate(para.spans())
               if start >= anchor.start and end <= anchor.end and end > start]

    if covered:
        first_node = covered[0][1]
        ins_parent = first_node.getparent()
        ins_at = ins_parent.index(first_node)
        list_at = covered[0][0]
    else:                                        # pure insertion
        ins_parent, ins_at, list_at = para.insert_point(anchor.start)

    first_written: etree._Element | None = None

    if anchor.insert_text:
        change = _change(INSERTED, author, date)
        content = etree.SubElement(change, CONTENT)
        content.text = anchor.insert_text
        ins_parent.insert(ins_at, change)
        para.nodes.insert(list_at, content)
        first_written = change
        list_at += 1

    if covered:
        # Group runs of adjacent nodes sharing a CharacterStyleRange so the
        # output reads the way InDesign's own does: one Change per span, not
        # one per fragment left behind by splitting.
        groups: list[tuple[etree._Element, list]] = []
        for _, node in covered:
            parent = node.getparent()
            if (groups and groups[-1][0] is parent
                    and groups[-1][1][-1].getnext() is node):
                groups[-1][1].append(node)
            else:
                groups.append((parent, [node]))

        for parent, group in groups:
            change = _change(DELETED, author, date)
            # A deletion carries its own formatting: a full PSR/CSR subtree,
            # copied from the context the text is being lifted out of.
            inner_psr = etree.SubElement(change, PSR, dict(para.walked.psr.attrib))
            inner_csr = etree.SubElement(inner_psr, CSR, dict(parent.attrib))
            change.tail = group[-1].tail
            parent.insert(parent.index(group[0]), change)
            for node in group:
                parent.remove(node)
                node.tail = None
                inner_csr.append(node)
            if first_written is None:
                first_written = change

        for i, _ in reversed(covered):
            del para.nodes[i]

    return first_written


# --- notes (the InDesign counterpart of a Word comment) -----------------------

def _note(text: str, author: str, date: str) -> etree._Element:
    """The Note element itself, in the shape InDesign 2026 writes its own
    (docs/idml-notes.md)."""
    note = etree.Element(NOTE, {"Collapsed": "false", "CreationDate": date,
                                "ModificationDate": date, "UserName": author,
                                "AppliedDocumentUser": _DOCPROOF_USER})
    inner_psr = etree.SubElement(note, PSR, {
        "AppliedParagraphStyle": "ParagraphStyle/$ID/[No paragraph style]"})
    inner_csr = etree.SubElement(inner_psr, CSR, {
        "AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"})
    content = etree.SubElement(inner_csr, CONTENT)
    content.text = text
    return note


def attach_note(anchor_el: etree._Element, text: str, author: str,
                date: str) -> None:
    """An inline Note beside the change, holding docproof's explanation.

    Notes live where the change lives — a direct child of the
    CharacterStyleRange — and are excluded from canonical text by the walker,
    so adding one never disturbs an anchor."""
    parent = anchor_el.getparent()
    parent.insert(parent.index(anchor_el), _note(text, author, date))


def attach_note_at(para: _Paragraph, off: int, text: str, author: str,
                   date: str) -> bool:
    """A Note at a character offset with no revision around it — the query
    channel: this asks, and changes nothing.

    The one real difference from the Word side. A Word comment spans a range,
    so a query there highlights the whole sentence it is asking about; an
    InDesign Note is a *point* in the text, with no range to highlight. So the
    note is anchored at the first character of that sentence, which is where a
    reader's eye goes anyway, and the question itself carries the rest.

    Like a Word comment's range markers, this inserts no text: splitting a
    Content node preserves canonical text exactly, and the Note is skipped by
    the walker. Every offset the tracked changes below rely on therefore still
    points where it did. Returns False when there is nowhere to hang it."""
    if not para.nodes:                       # an empty paragraph has no anchor
        return False
    para.ensure_boundary(off)
    parent, at, _ = para.insert_point(off)
    parent.insert(at, _note(text, author, date))
    return True


def ensure_document_user(pkg: IdmlPackage, author: str) -> None:
    """Register the revision author so InDesign shows a name on each change.

    Every IDML already carries at least one DocumentUser; ours is added beside
    them rather than replacing one, so a real user's identity is left alone."""
    root = pkg.tree(DESIGNMAP)
    for el in root.iterchildren(DOCUMENT_USER):
        if el.get("Self") == _DOCPROOF_USER:
            el.set("UserName", author)
            pkg.mark_modified(DESIGNMAP)
            return
    user = etree.Element(DOCUMENT_USER, {"Self": _DOCPROOF_USER,
                                         "UserName": author})
    existing = list(root.iterchildren(DOCUMENT_USER))
    if existing:
        last = existing[-1]
        root.insert(root.index(last) + 1, user)
    else:
        root.append(user)
    pkg.mark_modified(DESIGNMAP)


# --- accept / reject views (the invariant checkers) ---------------------------

def paragraph_view_text(contents_root, mode: str) -> str:
    """The text a paragraph would have if every tracked change were accepted
    or rejected. Used by the tests to prove the two invariants that matter:
    accept-all yields the corrected text, reject-all yields the original."""
    parts: list[str] = []

    def walk(el):
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if child.tag == CHANGE:
                kind = child.get("ChangeType")
                if (kind == INSERTED) == (mode == "accept"):
                    walk(child)
            elif child.tag == NOTE:
                continue
            elif child.tag == CONTENT:
                parts.append(child.text or "")
            else:
                walk(child)

    walk(contents_root)
    return "".join(parts)


# --- orchestration ------------------------------------------------------------

def apply_tracked_changes(pkg: IdmlPackage, doc: DocumentModel,
                          findings: list[Finding], cfg: Config
                          ) -> ReassemblyStats:
    validated = [f for f in findings if f.status == "validated"]
    # Two channels, never blurred, in InDesign exactly as in Word: a correction
    # the author accepts or rejects, and a question that edits nothing. Which
    # findings take the second is shared policy (docproof/queries.py); only the
    # anchoring below is InDesign's own.
    wanted = wanted_statuses(cfg.query_comments)
    # A Note is placed from the finding's anchor, so a query with no span
    # cannot be written into the file at all — the same loss the .docx side
    # takes, counted the same way. Silently filtering these would leave
    # summary.md promising a note the InDesign file does not carry.
    queries: list[Finding] = []
    unplaced: list[str] = []
    for f in findings:
        if f.status not in wanted:
            continue
        if f.anchor:
            queries.append(f)
            continue
        unplaced.append(f.finding_id)
        log.warning("%s (%s) has no anchor — the question stays out of the "
                    "file; it is in summary.md and findings.json only.",
                    f.finding_id, f.error_type)
    if not validated and not queries:
        log.info("No validated findings; document untouched.")
        return ReassemblyStats((), (), (), tuple(unplaced))

    paras = index_paragraphs(doc)
    date = _stamp()
    author = cfg.revision_author
    applied: list[str] = []
    skipped: list[str] = []
    queried: list[str] = []

    def refuse(fs: list[Finding]) -> None:
        """A paragraph we will not touch. A withheld correction is `skipped`;
        a question that never reached the file is `unplaced`."""
        for f in fs:
            (skipped if f.status == "validated" else unplaced).append(
                f.finding_id)

    by_part: dict[str, list[Finding]] = {}
    for f in validated + queries:
        by_part.setdefault(paras[f.para_id].part, []).append(f)

    ensure_document_user(pkg, author)

    for part, part_findings in by_part.items():
        walked_by_id = {wp.para_id: wp for wp in walk_package(pkg)
                        if wp.part == part}

        by_para: dict[str, list[Finding]] = {}
        for f in part_findings:
            by_para.setdefault(f.para_id, []).append(f)

        touched = changed = False
        for para_id, para_findings in by_para.items():
            walked = walked_by_id.get(para_id)
            if walked is None:
                log.error("%s vanished between ingest and reassembly — "
                          "refusing.", para_id)
                refuse(para_findings)
                continue

            if walked.location == "footnote":        # defense-in-depth 0
                # Ingest already skips these; a finding reaching here would
                # mean a stale document model, and the cost of being wrong is
                # an .idml InDesign crashes on rather than a bad edit. A Note
                # is not the element InDesign chokes on, but nothing about
                # footnote markup has been verified past the crash, so the
                # question stays out too rather than being the thing that
                # proves it.
                log.error("%s is in a footnote, where InDesign cannot carry a "
                          "tracked change — refusing.", para_id)
                refuse(para_findings)
                continue

            para = _Paragraph(walked)
            if para.text != paras[para_id].text:      # defense-in-depth 1
                log.error("Canonical-text drift in %s — refusing to edit it.",
                          para_id)
                refuse(para_findings)
                continue

            # Questions go on first, for the reason the Word side does it: a
            # Note inserts no text, so the offsets the edits below rely on are
            # unchanged — whereas applying a deletion first moves Content out
            # of the paragraph's node list and every later offset points at the
            # wrong place.
            for f in sorted((x for x in para_findings if x.status != "validated"),
                            key=lambda x: x.anchor.start):
                lo, _hi = query_span(f, paras[para_id].text)
                if attach_note_at(para, lo, query_text(f), author, date):
                    queried.append(f.finding_id)
                    touched = True
                else:
                    unplaced.append(f.finding_id)

            for f in sorted((x for x in para_findings if x.status == "validated"),
                            key=lambda x: x.anchor.start,
                            reverse=True):
                anchor = f.anchor
                if para.text[anchor.start:anchor.end] != anchor.delete_text:
                    log.error("%s: anchor slice mismatch at apply time — "
                              "skipping.", f.finding_id)   # defense-in-depth 2
                    skipped.append(f.finding_id)
                    continue
                first = apply_replacement(para, anchor, author, date)
                applied.append(f.finding_id)
                touched = changed = True
                if cfg.comments and not f.silent and first is not None:
                    attach_note(first, f.explanation or f"{f.error_type} fix",
                                author, date)

        if touched:
            if changed:
                story = pkg.story(part)
                if story is not None:
                    # Without this InDesign hides the changes it is carrying.
                    # A story that only gained Notes has no change to hide, so
                    # it is left as the designer set it.
                    story.set("TrackChanges", "true")
            pkg.mark_modified(part)

    log.info("Applied %d tracked change(s); %d skipped by safety checks.",
             len(applied), len(skipped))
    if queried or unplaced:
        log.info("Wrote %d margin quer%s as Note(s), which change nothing%s.",
                 len(queried), "y" if len(queried) == 1 else "ies",
                 f"; {len(unplaced)} had nowhere to hang" if unplaced else "")
    return ReassemblyStats(tuple(applied), tuple(skipped), tuple(queried),
                           tuple(unplaced))
