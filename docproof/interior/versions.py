"""Shared native InDesign Book version rules."""
from __future__ import annotations

import math
from typing import Any


def native_version(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native Book versions must be positive integers or .5 versions")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("native Book versions must be finite")
    whole = int(value)
    if value < 1 or (value != whole and value != whole + 0.5):
        raise ValueError("native Book versions must be positive integers or .5 versions")
    return whole if value == whole else whole + 0.5


def next_version(value: Any) -> int | float:
    version = native_version(value)
    return version + 0.5 if isinstance(version, int) else version + 1


def version_text(value: Any) -> str:
    return str(native_version(value)).removesuffix(".0")


def native_filename(surname: str, version: Any, extension: str = ".indd") -> str:
    return f"{surname} - Book {version_text(version)}{extension}"
