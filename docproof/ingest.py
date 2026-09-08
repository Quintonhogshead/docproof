from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from .config import Config
from .models import DocumentModel, ParagraphRef
from .utils.xml_helpers import (P_TAG, DocxPackage, paragraph_text, qn,
                                walk_package)

log = logging.getLogger("docproof.ingest")

OLE_MAGIC = b"\xd0\xcf\x11\xe0"
ZIP_MAGIC = b"PK\x03\x04"

REVISION_TAGS = {qn(t) for t in (
    "w:ins", "w:del",
    "w:moveFrom", "w:moveTo",
    "w:moveFromRangeStart", "w:moveFromRangeEnd",
    "w:moveToRangeStart", "w:moveToRangeEnd",
    "w:rPrChange", "w:pPrChange", "w:sectPrChange",
    "w:tblPrChange", "w:tblGridChange", "w:tcPrChange", "w:trPrChange",
    "w:cellIns", "w:cellDel", "w:cellMerge", "w:numberingChange",
)}

# accept_all_first knows how to resolve these; anything else aborts.
_UNWRAP = {qn("w:ins"), qn("w:moveTo")}                 # keep the content
_REMOVE = {qn("w:del"), qn("w:moveFrom"),               # drop content / markers
           qn("w:moveFromRangeStart"), qn("w:moveFromRangeEnd"),
           qn("w:moveToRangeStart"), qn("w:moveToRangeEnd"),
           qn("w:rPrChange"), qn("w:pPrChange"), qn("w:sectPrChange"),
           qn("w:tblPrChange"), qn("w:tblGridChange"),
           qn("w:tcPrChange"), qn("w:trPrChange"), qn("w:numberingChange")}
# A paragraph mark carrying one of these is a deleted mark: accepting it joins
# the paragraph to the next one rather than merely dropping a node.
_DELETED_MARK = {qn("w:del"), qn("w:moveFrom")}


class IngestError(Exception):
    """Raised for problems the user must fix. Message is user-facing."""


def preflight(path: str | Path, policy: str) -> DocxPackage:
    path = Path(path)
    if not path.exists():
        raise IngestError(f"File not found: {path}")

    with path.open("rb") as fh:
        head = fh.read(4)
    if head == OLE_MAGIC:
        raise IngestError(
            f"{path.name} is a legacy .doc file or a password-protected .docx. "
            "Open it in Word, remove any password, and Save As .docx, then rerun.")
    if head != ZIP_MAGIC:
        raise IngestError(f"{path.name} is not a .docx file (bad file signature).")

    try:
        pkg = DocxPackage(path)
    except zipfile.BadZipFile as e:
        raise IngestError(f"{path.name} is corrupt: {e}") from e

    for required in ("[Content_Types].xml", "word/document.xml"):
        if not pkg.has(required):
            raise IngestError(f"{path.name} is missing {required}; not a valid .docx.")

    found = find_revisions(pkg)
    if found:
        pretty = ", ".join(sorted(found))
        if policy == "abort":
            raise IngestError(
                f"Document already contains tracked changes ({pretty}). "
                "Accept or reject them in Word first, or rerun with "
                "tracked_changes_policy: accept_all_first | ignore.")
        if policy == "ignore":
            log.warning(
                "Existing tracked changes present in %s — proceeding per policy. "
                "Canonical text uses the accepted view; new edits near existing "
                "revisions may nest. Review the output carefully.", pretty)
        else:  # accept_all_first
            accept_all_revisions(pkg, found)
            log.info("Accepted all existing tracked changes in: %s", pretty)

    return pkg


def find_revisions(pkg: DocxPackage) -> dict[str, set]:
    """Every part that still carries a tracked change, with the revision
    element tags it uses. Empty when the document is clean."""
    parts = list(dict.fromkeys(wp.part for wp in walk_package(pkg)))
    found: dict[str, set] = {}
    for part in parts:
        tags = {el.tag for el in pkg.tree(part).iter() if el.tag in REVISION_TAGS}
        if tags:
            found[part] = tags
    return found


def accept_all_revisions(pkg: DocxPackage,
                         found: dict[str, set] | None = None) -> dict[str, int]:
    """Resolve every tracked change the way Word's Accept All would, in place.

    Insertions and moved-to text are kept, deletions and moved-from text
    dropped, and property-change records discarded in favour of the new
    properties. A deleted paragraph mark is the one revision that is not a
    node to unwrap or remove: accepting it joins the paragraph to the one
    after it, so that is what happens here — the runs move into the following
    paragraph, which keeps its own properties, exactly as Word does.

    Returns how many revision elements each part had. Raises IngestError on a
    revision kind this cannot resolve (table cell insertions and merges),
    naming it so the person can accept it in Word instead."""
    if found is None:
        found = find_revisions(pkg)
    unsupported = {t for tags in found.values() for t in tags} - _UNWRAP - _REMOVE
    if unsupported:
        names = sorted(t.split('}')[1] for t in unsupported)
        raise IngestError(
            f"Can't resolve tracked changes of these kinds: {names}. "
            "Accept the changes in Word instead, then rerun.")
    resolved: dict[str, int] = {}
    for part in found:
        tree = pkg.tree(part)
        resolved[part] = sum(1 for el in tree.iter() if el.tag in REVISION_TAGS)
        _join_deleted_paragraph_marks(tree)
        # Materialize before mutating; process removals/unwraps repeatedly
        # until fixed point (wrappers can nest).
        changed = True
        while changed:
            changed = False
            for el in list(tree.iter()):
                if el.tag in _UNWRAP:
                    parent, i = el.getparent(), el.getparent().index(el)
                    for child in list(el):
                        parent.insert(i, child)
                        i += 1
                    parent.remove(el)
                    changed = True
                elif el.tag in _REMOVE:
                    el.getparent().remove(el)
                    changed = True
        pkg.mark_modified(part)
    return resolved


def _join_deleted_paragraph_marks(tree) -> None:
    """Merge each paragraph whose mark is deleted into the paragraph after it.

    The mark lives at `w:p/w:pPr/w:rPr/w:del` (or `w:moveFrom`). Document
    order matters: a run of consecutive deleted marks folds forward one step
    at a time, so the whole run ends up in the first surviving paragraph. A
    paragraph with nothing after it in its container (the last one in a cell,
    a note, or the body) simply keeps its mark dropped — there is nothing to
    join it to, and that is also what Word shows."""
    ppr, rpr = qn("w:pPr"), qn("w:rPr")
    for p in list(tree.iter(P_TAG)):
        props = p.find(ppr)
        if props is None:
            continue
        run_props = props.find(rpr)
        if run_props is None or not any(
                c.tag in _DELETED_MARK for c in run_props):
            continue
        following = p.getnext()
        while following is not None and following.tag != P_TAG:
            following = following.getnext()
        if following is None:
            continue
        # Everything except the paragraph properties moves to the front of the
        # next paragraph, after its own w:pPr if it has one.
        content = [c for c in p if c.tag != ppr]
        at = 1 if following.find(ppr) is not None else 0
        for child in content:
            following.insert(at, child)
            at += 1
        p.getparent().remove(p)


def build_document_model(pkg: DocxPackage, cfg: Config) -> DocumentModel:
    paragraphs: list[ParagraphRef] = []
    skipped: list[tuple[str, str]] = []
    style_path = f"{qn('w:pPr')}/{qn('w:pStyle')}"

    for wp in walk_package(pkg):
        text = paragraph_text(wp.element)
        style_el = wp.element.find(style_path)
        style = style_el.get(qn("w:val")) if style_el is not None else "Normal"

        if not text.strip():
            skipped.append((wp.para_id, "empty"))
        elif cfg.skip.fully_skipped(style):
            skipped.append((wp.para_id, f"style:{style}"))
        else:
            # A heading is swept but never reviewed: a chapter title carries a
            # compound-number or punctuation fix, but is not the model's to
            # rewrite. Short lines used to land here too, on the theory that a
            # two-word line rarely holds a grammar error — but in fiction it is
            # usually dialogue ("“Who?” he asked."), which is exactly where a
            # missing word or a homophone slip hides, so skipping it was a
            # silent recall hole. min_paragraph_chars now defaults to 0, so the
            # `short` gate is off unless a press configures a floor; the sweeps
            # still reach every paragraph either way.
            sweep_only = cfg.skip.is_sweep_only(style)
            short = len(text) < cfg.chunking.min_paragraph_chars
            paragraphs.append(ParagraphRef(
                para_id=wp.para_id, part=wp.part, location=wp.location,
                text=text, style=style, reviewable=not (short or sweep_only)))
            reason = (" (sweeps only: heading)" if sweep_only else
                      " (sweeps only: short)" if short else "")
            log.debug("para %s [%s/%s] %d chars%s", wp.para_id, wp.location,
                      style, len(text), reason)

    n_sweep_only = sum(1 for p in paragraphs if not p.reviewable)
    log.info("Ingested %d paragraph(s) from %s: %d for the model, %d swept "
             "only (headings, and short lines only if a floor is set), "
             "%d skipped entirely",
             len(paragraphs), pkg.path.name, len(paragraphs) - n_sweep_only,
             n_sweep_only, len(skipped))
    return DocumentModel(source_path=str(pkg.path),
                         paragraphs=tuple(paragraphs), skipped=tuple(skipped))