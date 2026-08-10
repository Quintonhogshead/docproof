"""What promo passes between its stages.

The review pipeline's unit is an edit inside a paragraph and prep's is the
paragraph itself; promo's is the whole book at once, in and a small bundle of
copy out. The wire model is deliberately loose in v1 — a teaser string and a
list of posts — because the house copy specs that would tighten it (how long a
teaser runs, which platform each post targets) have not landed yet. Tightening
it later is a change to this file and the prompt, nothing downstream.
"""
from __future__ import annotations

from pydantic import BaseModel


class SocialPost(BaseModel):
    # Which channel this post is written for. Left blank in v1: the copy specs
    # that decide the platform split across the twelve have not landed, so the
    # model is asked for varied posts and this stays "" until they do.
    platform: str = ""
    text: str
    # Coined on purpose, so the grounding check never reads these as invented
    # names — they are not scanned against the manuscript.
    hashtags: list[str] = []


class PromoResult(BaseModel):
    """The whole answer: one hook and a set of posts. `model_dump()` of this is
    `promo.json`, which the review panel loads, a human edits, and the two .docx
    are regenerated from — so this is the source of truth, not the documents."""
    # A short marketing hook in back-cover / catalogue voice, spoiler-safe.
    teaser: str
    posts: list[SocialPost]
