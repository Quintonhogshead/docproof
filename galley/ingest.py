"""Ingest a source document into a galley :class:`~galley.contracts.Manuscript`.

Shared by the app's galley runner and the headless ``docproof galley`` CLI so the
two build paragraph ids identically. Built on DocProof's own ingest: the ids here
are the ids the ladder adapter's findings carry, which is what scope resolution
and the auditor's density-by-chapter both key on. A separate normalization pass is
not owed — the ladder normalizes inside its own ``prepare()``; this manuscript is
used for id-keyed scope work, not for anchoring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from galley.contracts import Chapter, Manuscript

if TYPE_CHECKING:
    from docproof.config import Config

log = logging.getLogger("galley.ingest")


def manuscript_from_source(source_path: str, cfg: "Config") -> Manuscript:
    """Read ``source_path`` into a galley :class:`Manuscript`.

    Chapters are best-effort: a book whose headings don't segment cleanly yields
    an empty ``chapters`` tuple, which every galley consumer already treats as
    "one chapter over the whole book" rather than an error.
    """
    from docproof.ingest import build_document_model, preflight

    pkg = preflight(source_path, cfg.tracked_changes_policy)
    doc = build_document_model(pkg, cfg)
    paragraphs = {p.para_id: p.text for p in doc.paragraphs}
    order = tuple(p.para_id for p in doc.paragraphs)
    chapters: tuple[Chapter, ...] = ()
    try:
        # is_heading takes a STYLE string; chapters() applies the text-based
        # looks_like_chapter_heading itself, so a style predicate is all we owe it
        # (the pipeline passes cfg.skip.is_sweep_only — the same one).
        from docproof.continuity import chapters as chapter_units

        is_heading = getattr(cfg.skip, "is_sweep_only", lambda _s: False)
        units = chapter_units(doc.paragraphs, is_heading)
        chapters = tuple(
            Chapter(u.index, u.title, tuple(p.para_id for p in u.paragraphs))
            for u in units
        )
    except Exception:  # noqa: BLE001 - chapters are best-effort
        log.debug("Could not derive galley chapters", exc_info=True)
    return Manuscript(paragraphs=paragraphs, order=order, chapters=chapters)


__all__ = ["manuscript_from_source"]
