"""The .docx format, wired to the modules that already implement it.

Deliberately a re-export rather than a move: the OOXML ingest and reassembler
are the most heavily tested code in the project, and relocating them to prove a
point about symmetry would churn imports and git history for no behavioural
gain. The seam is what mattered; this file is the seam.
"""
from __future__ import annotations

from ..ingest import build_document_model, preflight
from ..normalize import normalize_package as normalize
from ..speakersplit import split_package as speaker_split
from ..reassembler import (annotate_excluded_words, apply_tracked_changes,
                           paragraph_view_text)
from ..utils.xml_helpers import paragraph_text, walk_package


def snapshot(pkg, mode: str = "current") -> dict[str, str]:
    """Every paragraph's text, keyed by para_id.

    "current" is what the paragraph says now; "reject" is what it would say
    with every tracked change rejected; "accept" is what it would say with every
    tracked change accepted — the text the author actually ends up reading, which
    the finished-text delivery gates (galley/verify.py) walk for residual errors
    and applied-edit damage. The audit compares the "reject" view against a
    "current" taken before any change was applied."""
    return {
        wp.para_id: (paragraph_text(wp.element) if mode == "current"
                     else paragraph_view_text(wp.element, mode))
        for wp in walk_package(pkg)
    }


__all__ = ["preflight", "build_document_model", "apply_tracked_changes",
           "annotate_excluded_words", "normalize", "snapshot",
           "speaker_split"]
