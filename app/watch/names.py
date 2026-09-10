"""Comparison forms for author names; display names keep their spelling."""
from __future__ import annotations

import unicodedata


def flatten_accents(value: str) -> str:
    """Strip combining accents without discarding non-Latin letters."""
    return "".join(char for char in unicodedata.normalize("NFD", value)
                   if unicodedata.category(char) != "Mn")


def name_key(value: str) -> str:
    return " ".join(flatten_accents(value).split()).casefold()
