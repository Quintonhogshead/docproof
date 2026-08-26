"""The one structural heading predicate every component shares.

A paragraph is a heading only if BOTH hold: its Word STYLE marks it a heading
(the skip config's sweep-only styles — "Heading*" by default) AND it has the
SHAPE of a heading — a short line, in the body, that does not end like a
sentence. The style alone is not enough: a manuscript that mis-styles two long
body paragraphs as "Heading 3" would otherwise have them treated as chapter
breaks by the profiler and continuity segmentation, AND title-cased by the
heading sweep (a wrong edit to real prose). Requiring the shape as well as the
style closes both.

Before this module the two consumers — `docproof.continuity.chapters` (via its
`is_break`) and `docproof.sweeps.heading_case_findings` — each keyed off the
style name alone, so "heading" meant subtly different things depending on which
component asked. Now they call `is_structural_heading` and agree.

The text-convention chapter markers ("Chapter Nine", "Prologue", a bare number
on a line) are a SEPARATE, additive signal handled by
`docproof.continuity.looks_like_chapter_heading`; they already carry their own
structural guards (body location, short standalone line). This predicate governs
only the STYLE-based path.
"""
from __future__ import annotations

from typing import Callable

from .models import ParagraphRef

# A heading is a short line. A styled paragraph longer than this is body text a
# heading style was mistakenly applied to — generous enough for a long chapter
# title, short enough to exclude a real paragraph.
HEADING_MAX_CHARS = 120


def is_structural_heading(p: ParagraphRef,
                          is_heading_style: Callable[[str], bool]) -> bool:
    """Whether paragraph `p` is a heading by STYLE and by SHAPE.

    `is_heading_style` is the style-name predicate — normally
    `cfg.skip.is_sweep_only`. A paragraph passes only if it is in the body, its
    style is a heading style, its trimmed text is non-empty and at most
    `HEADING_MAX_CHARS`, and it does not end with sentence-terminal punctuation
    (a period, or a period wrapped by a closing quote/paren) — the reliable
    signal that a "heading" is really a body sentence."""
    if getattr(p, "location", "body") != "body":
        return False
    if not is_heading_style(p.style):
        return False
    t = p.text.strip()
    if not t or len(t) > HEADING_MAX_CHARS:
        return False
    # Trailing sentence punctuation, allowing one closing quote/paren after it
    # ("...to come." or "...done.)"). '?' and '!' are left alone — a heading may
    # legitimately be a question or an exclamation.
    tail = t.rstrip("\"')”’")
    if tail.endswith("."):
        return False
    return True


__all__ = ["HEADING_MAX_CHARS", "is_structural_heading"]
