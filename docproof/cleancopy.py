"""The clean copy: the proofread as it will read once every change is taken.

The tracked-changes file is the record — every correction rejectable, every
question in the margin — and it stays the primary deliverable. But the person
who opens the folder next (the author reading straight through, the designer
flowing text) wants the text with the corrections in and the markup gone, and
until this module existed nobody got one: proofing handed back the redline and
nothing else (2026-09-08, "the clean version is not in drive").

So the clean copy is DERIVED, at hand-off time, from the very file certify
passed — never built by a second pipeline pass — which is what makes it safe
to ship without its own certificate: its text is the accepted view of the
certified file, by construction, and `paragraph_view_text(..., "accept")` in
`docproof.reassembler` is the same reading the verifiers used.

Accepting means, per part:

- deleted paragraph marks (`w:pPr/w:rPr/w:del`) join the paragraph with the
  one that follows, the following paragraph's properties winning, exactly as
  Word does when it accepts that deletion;
- insertions and moves-to are unwrapped (the content stays); deletions, moves-
  from and every property-change record are dropped — the same table
  `docproof.ingest` uses for `tracked_changes_policy: accept_all_first`. An
  inserted paragraph mark (`w:pPr/w:rPr/w:ins`, what a speaker split writes)
  is an empty insertion, so unwrapping it simply removes the marker and the
  paragraph stands;
- comments go: the range markers and reference runs in the text, and the
  comment bodies themselves, so a "clean" file does not carry the author's
  unanswered questions in a part Word merely hides.

Everything else — styles, images, fields, headers, the package layout — is
untouched: only the parts that carried markup are re-serialized.
"""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from .utils.xml_helpers import DocxPackage, qn, walk_package

log = logging.getLogger(__name__)

P_TAG = qn("w:p")
R_TAG = qn("w:r")
PPR_TAG = qn("w:pPr")
RPR_TAG = qn("w:rPr")
INS_TAG = qn("w:ins")
DEL_TAG = qn("w:del")

#: Revision wrappers whose content survives acceptance.
_UNWRAP = {INS_TAG, qn("w:moveTo")}
#: Revision elements (and property-change records) that acceptance drops whole.
_REMOVE = {DEL_TAG, qn("w:moveFrom"),
           qn("w:moveFromRangeStart"), qn("w:moveFromRangeEnd"),
           qn("w:moveToRangeStart"), qn("w:moveToRangeEnd"),
           qn("w:rPrChange"), qn("w:pPrChange"), qn("w:sectPrChange"),
           qn("w:tblPrChange"), qn("w:tblGridChange"),
           qn("w:tcPrChange"), qn("w:trPrChange"), qn("w:numberingChange")}
_COMMENT_MARKS = {qn("w:commentRangeStart"), qn("w:commentRangeEnd")}
_COMMENT_REF = qn("w:commentReference")
#: The comment parts modern Word writes: bodies, threading, resolved state.
_COMMENT_PARTS = ("word/comments.xml", "word/commentsExtended.xml",
                  "word/commentsIds.xml", "word/commentsExtensible.xml")


class CleanCopyError(RuntimeError):
    """The clean copy could not be derived from the tracked-changes file."""


def write_clean_copy(source: str | Path, dest: str | Path) -> Path:
    """Write `dest`: `source` with every tracked change accepted and every
    comment removed. Returns `dest`. Raises CleanCopyError if `source` is not
    a .docx this module can read."""
    src, out = Path(source), Path(dest)
    try:
        pkg = DocxPackage(str(src))
    except Exception as e:                                  # noqa: BLE001
        raise CleanCopyError(f"{src.name} is not a readable .docx: {e}") from e
    accept_all(pkg)
    strip_comments(pkg)
    out.parent.mkdir(parents=True, exist_ok=True)
    pkg.save(out)
    return out


def _story_parts(pkg: DocxPackage) -> list[str]:
    """Every part that carries paragraphs — the body, headers, footers,
    footnotes, text boxes — in walk order, the main document first."""
    parts = ["word/document.xml"] if pkg.has("word/document.xml") else []
    for wp in walk_package(pkg):
        if wp.part not in parts:
            parts.append(wp.part)
    return parts


def accept_all(pkg: DocxPackage) -> int:
    """Accept every tracked change in every story part. Returns how many
    revision elements were resolved (a zero means the file carried none)."""
    resolved = 0
    for part in _story_parts(pkg):
        tree = pkg.tree(part)
        n = _join_deleted_marks(tree)
        n += _resolve_revisions(tree)
        if n:
            pkg.mark_modified(part)
        resolved += n
    return resolved


def _join_deleted_marks(tree: etree._Element) -> int:
    """A deleted paragraph mark joins its paragraph to the next one.

    Word's rule on accepting the deletion: the text of both paragraphs becomes
    one paragraph carrying the SECOND paragraph's properties (the mark that
    survives is the second one's). A deleted mark on the last paragraph of a
    story has nothing to join and is simply dropped."""
    joined = 0
    for p in list(tree.iter(P_TAG)):
        ppr = p.find(PPR_TAG)
        rpr = ppr.find(RPR_TAG) if ppr is not None else None
        mark = rpr.find(DEL_TAG) if rpr is not None else None
        if mark is None:
            continue
        rpr.remove(mark)
        joined += 1
        nxt = p.getnext()
        if nxt is None or nxt.tag != P_TAG:
            continue
        # Move every content child (everything but the pPr) of `p` to the front
        # of `nxt`, after nxt's own pPr, in order.
        content = [c for c in p if c.tag != PPR_TAG]
        at = 1 if nxt.find(PPR_TAG) is not None else 0
        for child in content:
            nxt.insert(at, child)
            at += 1
        p.getparent().remove(p)
    return joined


def _resolve_revisions(tree: etree._Element) -> int:
    """Unwrap what survives, drop what does not, to a fixed point (wrappers
    nest). The same walk as `docproof.ingest._accept_all`."""
    resolved = 0
    changed = True
    while changed:
        changed = False
        for el in list(tree.iter()):
            parent = el.getparent()
            if parent is None:
                continue
            if el.tag in _UNWRAP:
                at = parent.index(el)
                for child in list(el):
                    parent.insert(at, child)
                    at += 1
                parent.remove(el)
                changed = True
                resolved += 1
            elif el.tag in _REMOVE:
                parent.remove(el)
                changed = True
                resolved += 1
    return resolved


def strip_comments(pkg: DocxPackage) -> int:
    """Remove every comment: the anchors in the text and the bodies in the
    comment parts. Returns how many anchors were removed. The comment parts
    stay in the package, emptied, so the relationships and content types that
    name them remain valid."""
    removed = 0
    for part in _story_parts(pkg):
        tree = pkg.tree(part)
        n = 0
        for el in list(tree.iter(*_COMMENT_MARKS)):
            el.getparent().remove(el)
            n += 1
        for ref in list(tree.iter(_COMMENT_REF)):
            run = ref.getparent()
            ref_only = (run is not None and run.tag == R_TAG and
                        all(c.tag in (RPR_TAG, _COMMENT_REF) for c in run))
            if ref_only:
                run.getparent().remove(run)
            else:
                run.remove(ref)
            n += 1
        if n:
            pkg.mark_modified(part)
        removed += n
    for name in _COMMENT_PARTS:
        if pkg.has(name):
            root = pkg.tree(name)
            if len(root):
                for child in list(root):
                    root.remove(child)
                pkg.mark_modified(name)
    return removed


def has_markup(source: str | Path) -> bool:
    """True if the .docx still carries a tracked change or a comment anchor
    in any story part — the check a test (or a nervous hand-off) runs on the
    clean copy it just wrote."""
    pkg = DocxPackage(str(source))
    tags = _UNWRAP | _REMOVE | _COMMENT_MARKS | {_COMMENT_REF}
    for part in _story_parts(pkg):
        for el in pkg.tree(part).iter():
            if el.tag in tags:
                return True
    return False
