"""Promo: a teaser and a set of social posts from a finished manuscript.

The third DocProof pipeline, beside review and prep. Like them it is three
steps — prepare, run, finish — and like prep it reads the whole manuscript and
writes new documents. It stays FastAPI-free: the app layer drives it, never the
other way round.
"""
from __future__ import annotations

from .ingest import Manuscript, read_manuscript
from .model import PromoResult, SocialPost
from .pipeline import (OUTPUT_KINDS, PreparedPromo, PromoError, PromoOutputs,
                       PromoTooLarge, estimate_output_tokens, finish, prepare,
                       run, run_mock)
from .verify import ClaimCheck, verify_claims, verify_grounding

__all__ = [
    "Manuscript", "read_manuscript",
    "PromoResult", "SocialPost",
    "PreparedPromo", "PromoOutputs", "PromoError", "PromoTooLarge",
    "OUTPUT_KINDS",
    "prepare", "run", "run_mock", "finish", "estimate_output_tokens",
    "verify_grounding", "verify_claims", "ClaimCheck",
]
