"""Promo: a teaser and a set of social posts from a finished manuscript.

The third DocProof pipeline, beside review and prep. Like them it is three
steps — prepare, run, finish — and like prep it reads the whole manuscript and
writes new documents. It stays FastAPI-free: the app layer drives it, never the
other way round.
"""
from __future__ import annotations

from .ingest import Manuscript, read_manuscript
from .model import MarketingPlan, PromoResult, SocialPost
from .pipeline import (OUTPUT_KINDS, PlanOutputs, PreparedPlan, PreparedPromo,
                       PromoError, PromoOutputs, PromoTooLarge,
                       estimate_output_tokens, finish, finish_plan, prepare,
                       prepare_plan, run, run_mock, run_plan, run_plan_mock)
from .prompts import PlanMeta
from .verify import ClaimCheck, verify_claims, verify_grounding

__all__ = [
    "Manuscript", "read_manuscript",
    "PromoResult", "SocialPost", "MarketingPlan",
    "PreparedPromo", "PromoOutputs", "PromoError", "PromoTooLarge",
    "PreparedPlan", "PlanOutputs", "PlanMeta",
    "OUTPUT_KINDS",
    "prepare", "run", "run_mock", "finish", "estimate_output_tokens",
    "prepare_plan", "run_plan", "run_plan_mock", "finish_plan",
    "verify_grounding", "verify_claims", "ClaimCheck",
]
