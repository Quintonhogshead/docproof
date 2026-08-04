"""The .docx format, wired to the modules that already implement it.

Deliberately a re-export rather than a move: the OOXML ingest and reassembler
are the most heavily tested code in the project, and relocating them to prove a
point about symmetry would churn imports and git history for no behavioural
gain. The seam is what mattered; this file is the seam.
"""
from __future__ import annotations

from ..ingest import build_document_model, preflight
from ..reassembler import apply_tracked_changes

__all__ = ["preflight", "build_document_model", "apply_tracked_changes"]
