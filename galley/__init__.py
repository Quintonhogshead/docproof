"""Galley — an editorial proofreading practitioner built on DocProof.

Galley talks to detectors through an adapter seam (see ``galley.adapters``) and
keeps its whole per-book state in a durable case file (see ``galley.casefile``).
DocProof is the first and best adapter, not the skeleton: nothing in this package
imports a vendor SDK, and everything above the seam speaks the frozen contracts
in ``galley.contracts``.
"""

from galley.contracts import (
    CASEFILE_SCHEMA_VERSION,
    Chapter,
    GFinding,
    Hypothesis,
    Manuscript,
    Provenance,
    Span,
    Verdict,
    WaveRecord,
)

__all__ = [
    "CASEFILE_SCHEMA_VERSION",
    "Chapter",
    "GFinding",
    "Hypothesis",
    "Manuscript",
    "Provenance",
    "Span",
    "Verdict",
    "WaveRecord",
]
