"""IDML pre-flight and document model.

Mirrors docproof/ingest.py stage for stage, including the reason `abort` is the
default tracked-changes policy: laying new Change wrappers around existing ones
can produce a document whose accept/reject order silently changes the text.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from ...config import Config
from ...ingest import IngestError, ZIP_MAGIC
from ...models import DocumentModel, ParagraphRef
from .walker import (CHANGE, DELETED, DESIGNMAP, IDML_MIMETYPE, INSERTED,
                     MIMETYPE, IdmlPackage, paragraph_text, walk_package)

log = logging.getLogger("docproof.formats.idml")

# An .indd is a proprietary binary, not a zip. Its header string is the most
# reliable way to tell "you gave me the native file" from "this is corrupt".
INDD_MARKER = b"DOCUMENT"


def preflight(path: str | Path, policy: str) -> IdmlPackage:
    path = Path(path)
    if not path.exists():
        raise IngestError(f"File not found: {path}")

    with path.open("rb") as fh:
        head = fh.read(64)
    if head[:4] != ZIP_MAGIC:
        if INDD_MARKER in head:
            raise IngestError(
                f"{path.name} is a native InDesign document. Open it in "
                "InDesign and choose File → Export → InDesign Markup (IDML), "
                "then review the .idml file.")
        raise IngestError(
            f"{path.name} is not an .idml package (bad file signature).")

    try:
        pkg = IdmlPackage(path)
    except zipfile.BadZipFile as e:
        raise IngestError(f"{path.name} is corrupt: {e}") from e

    if not pkg.has(DESIGNMAP):
        raise IngestError(
            f"{path.name} is missing {DESIGNMAP}; it is not an IDML package. "
            "If this came from InDesign, re-export it with File → Export → "
            "InDesign Markup (IDML).")
    if pkg.has(MIMETYPE) and pkg.raw(MIMETYPE).strip() != IDML_MIMETYPE:
        log.warning("%s has an unexpected mimetype entry; continuing anyway.",
                    path.name)
    if not pkg.story_parts():
        raise IngestError(
            f"{path.name} has no text stories to review. An IDML with only "
            "placed images or empty frames has nothing for docproof to read.")

    found = {part: kinds for part in pkg.story_parts()
             if (kinds := _change_kinds(pkg, part))}
    if found:
        pretty = ", ".join(sorted(found))
        if policy == "abort":
            raise IngestError(
                f"Document already contains tracked changes ({pretty}). "
                "Accept or reject them in InDesign first (Story Editor, or "
                "Window → Editorial → Track Changes), or rerun with "
                "tracked_changes_policy: accept_all_first | ignore.")
        if policy == "ignore":
            log.warning(
                "Existing tracked changes present in %s — proceeding per "
                "policy. Canonical text uses the accepted view; new edits near "
                "existing revisions may nest. Review the output carefully.",
                pretty)
        else:
            _accept_all(pkg, found)
            log.info("Accepted all existing tracked changes in: %s", pretty)

    return pkg


def _change_kinds(pkg: IdmlPackage, part: str) -> set[str]:
    return {el.get("ChangeType") or "?"
            for el in pkg.tree(part).iter(CHANGE)}


def _accept_all(pkg: IdmlPackage, found: dict[str, set]) -> None:
    """Resolve every existing Change: keep what was inserted, drop what was
    deleted. Repeats to a fixed point because Change wrappers can nest."""
    unsupported = {k for kinds in found.values() for k in kinds} - {INSERTED,
                                                                   DELETED}
    if unsupported:
        raise IngestError(
            f"accept_all_first can't resolve change types: "
            f"{sorted(unsupported)}. Accept the changes in InDesign instead, "
            "then rerun.")
    for part in found:
        tree = pkg.tree(part)
        changed = True
        while changed:
            changed = False
            for el in list(tree.iter(CHANGE)):
                parent = el.getparent()
                if parent is None:
                    continue
                if el.get("ChangeType") == INSERTED:
                    at = parent.index(el)
                    # An insertion holds Content directly; lift it out in place.
                    for child in list(el):
                        parent.insert(at, child)
                        at += 1
                parent.remove(el)
                changed = True
        story = pkg.story(part)
        if story is not None:
            story.set("TrackChanges", "false")
        pkg.mark_modified(part)


def build_document_model(pkg: IdmlPackage, cfg: Config) -> DocumentModel:
    paragraphs: list[ParagraphRef] = []
    skipped: list[tuple[str, str]] = []

    for wp in walk_package(pkg):
        text = paragraph_text(wp.contents)
        if not text.strip():
            skipped.append((wp.para_id, "empty"))
        elif wp.location == "footnote":
            # InDesign does not track changes in footnote text: asked to edit a
            # footnote with tracking on, it makes the edit untracked, and a
            # Change element written there is markup it cannot read back — it
            # crashes on open. Reviewing prose we could never mark up would
            # bill for findings that can never appear, so footnotes are listed
            # as skipped instead. See docs/idml-notes.md.
            skipped.append((wp.para_id, "footnote:tracked-changes-unsupported"))
        elif cfg.skip.fully_skipped(wp.style) or cfg.skip.is_sweep_only(wp.style):
            # IDML has no sweep-only channel — there is no reviewable=False
            # tier — so a heading style is skipped outright here, as it always
            # was.
            skipped.append((wp.para_id, f"style:{wp.style}"))
        elif len(text) < cfg.chunking.min_paragraph_chars:
            # Off by default (min_paragraph_chars is 0): a floor skips short
            # lines outright on the IDML path, since there is no sweep-only tier
            # to catch them the way the .docx path does.
            skipped.append((wp.para_id, f"short:{len(text)}"))
        else:
            paragraphs.append(ParagraphRef(
                para_id=wp.para_id, part=wp.part, location=wp.location,
                text=text, style=wp.style))
            log.debug("para %s [%s/%s] %d chars", wp.para_id, wp.location,
                      wp.style, len(text))

    log.info("Ingested %d reviewable paragraphs (%d skipped) from %s",
             len(paragraphs), len(skipped), pkg.path.name)
    return DocumentModel(source_path=str(pkg.path),
                         paragraphs=tuple(paragraphs), skipped=tuple(skipped))
