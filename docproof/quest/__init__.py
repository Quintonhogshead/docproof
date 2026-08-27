"""Quest: the consumer-facing adventure layer over the review pipeline.

The first piece is the skin generator — one cheap call that reads a sample of
an uploaded manuscript and dresses the permanent party (Pip, Bram, Maple,
Cinder, Sage, Lark) in that book's register: aliases, job lines, Galley's
greeting, a palette. The party's identities and the detector lanes underneath
never change; only the costume does.
"""
from .model import DEFAULT_SKIN, CharacterSkin, SkinSpec
from .skin import (LUNA_MODEL, SkinResult, generate_skin, price_band,
                   read_sample_source)

__all__ = [
    "CharacterSkin", "SkinSpec", "DEFAULT_SKIN",
    "SkinResult", "generate_skin", "price_band", "read_sample_source",
    "LUNA_MODEL",
]
