"""Ingest a source document into a Galley manuscript.

Uses DocProof's ingest so paragraph ids match adapter findings. The resulting
manuscript supports id-keyed scope and density work; the ladder performs its own
normalization for anchoring.
"""

from __future__ import annotations

import logging
from pathlib import Path
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


def write_manuscript_docx(source_path: str, cfg: "Config", manuscript: Manuscript,
                          out_path: str) -> Path:
    """Write manuscript text into a copy of its source DOCX.

    Replace each changed paragraph in full without tracked changes. The
    manuscript carries final text, so no edit offsets are needed. Raise
    ValueError for non-DOCX sources."""
    from docproof.ingest import build_document_model, preflight
    from docproof.reassembler import apply_untracked

    if Path(source_path).suffix.lower() != ".docx":
        raise ValueError(
            f"write_manuscript_docx only supports .docx sources, not "
            f"{Path(source_path).suffix!r} ({source_path})")

    pkg = preflight(source_path, cfg.tracked_changes_policy)
    doc = build_document_model(pkg, cfg)
    original_text = {p.para_id: p.text for p in doc.paragraphs}

    edits_by_para: dict[str, list[tuple[int, int, str]]] = {}
    for para_id, seeded_text in manuscript.paragraphs.items():
        original = original_text.get(para_id)
        if original is None or original == seeded_text:
            continue
        edits_by_para[para_id] = [(0, len(original), seeded_text)]

    if edits_by_para:
        apply_untracked(pkg, doc, edits_by_para)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pkg.save(out)
    return out


__all__ = ["manuscript_from_source", "write_manuscript_docx"]
