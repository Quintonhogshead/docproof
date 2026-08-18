"""Locating a quoted anchor in the book when the two renderings disagree.

A correction's anchor is quoted from a *typeset PDF*, read back by a PDF library;
the book it has to land in is an IDML. The same sentence is not the same string in
those two places. A kerning jump between a full stop and a closing quote comes
back as a space (`. ’` for the book's `.’`); a word broken over a line end comes
back with its hyphen (`conces-`); an em dash is spaced in one and not the other.
`validator.fold_punct` cannot bridge any of that, because it is deliberately
length-preserving so its offsets stay valid — and every one of those differences
changes the length.

So this builds a *normalized view* of a text alongside a map from each character
of the view back to the character of the real text that produced it. A match
found in the view is reported as a span of the **real** text, which is what an
edit is written over. Whitespace and hyphens carry no anchoring signal across two
renderings, so the view drops them; curly punctuation is folded; an ellipsis is
spelled out.

This is a wider net, not a looser one: it is still an exact substring match, just
of a canonical form, so nothing is scored, guessed or thresholded. A normalization
that pulls in a second, unintended match does not produce a wrong edit — it
produces an *ambiguous* one, which is flagged for a human, and that is the
direction this engine is allowed to fail in.
"""
from __future__ import annotations

from ..validator import fold_punct

# Characters the normalized view drops entirely, after folding.
#
# Whitespace: the PDF reader reports a kerning jump as a space, so the book's
# `.’` arrives as `. ’`; a word split across chunks arrives as `un nam ed`. Word
# boundaries are worth less here than the ability to match at all.
#
# Hyphens (and the dashes folded onto them): a word broken over a line end keeps
# its hyphen in the PDF and has none in the book, and an em dash is spaced in one
# rendering and unspaced in the other. Dropping the character covers every one of
# those without a special case for which is which.
_DROP = frozenset(" \t\r\n-")

# One-to-many rewrites applied before the drop. Folding an ellipsis to three
# stops means an anchor typed either way matches the book either way; the offset
# map carries all three back to the single real character.
_EXPAND = {"…": "..."}


class NormIndex:
    """A text's normalized view, and the map back to real offsets.

    `spans(needle)` returns the real `(start, end)` spans of the text that
    correspond to each occurrence of `needle` in the view, so a caller edits the
    real characters even though it matched the canonical ones."""

    __slots__ = ("_view", "_src", "_fold_case")

    def __init__(self, text: str, *, fold_case: bool = False) -> None:
        self._fold_case = fold_case
        view: list[str] = []
        src: list[int] = []                 # view offset -> real offset
        folded = fold_punct(text)
        for i, ch in enumerate(folded):
            for c in _EXPAND.get(ch, ch):
                if c in _DROP:
                    continue
                view.append(c.lower() if fold_case else c)
                src.append(i)
        self._view = "".join(view)
        self._src = src

    @property
    def view(self) -> str:
        """The normalized text itself — exposed for callers that align two of
        these against each other rather than search one."""
        return self._view

    @property
    def sources(self) -> list[int]:
        """The real offset each view character came from, positionally. For a
        caller assembling many of these into one stream; asking `real_offset` per
        character instead costs a Python call per character of the book."""
        return self._src

    def real_offset(self, view_offset: int) -> int:
        """The real offset the view character at `view_offset` came from. The
        end of the view maps to one past the last real character used."""
        if view_offset >= len(self._src):
            return (self._src[-1] + 1) if self._src else 0
        return self._src[view_offset]

    def real_span(self, start: int, end: int) -> tuple[int, int]:
        """The real `(start, end)` for a half-open span of the view."""
        if not self._src or start >= end:
            return (0, 0)
        return self._src[start], self._src[end - 1] + 1

    def spans(self, needle: str) -> list[tuple[int, int]]:
        """Every real span matching `needle`, compared in the normalized view.

        Empty when `needle` normalizes to nothing — an anchor made only of
        whitespace and dashes is not an anchor, and saying so beats matching
        everywhere."""
        probe = normalize(needle, fold_case=self._fold_case)
        if not probe:
            return []
        out: list[tuple[int, int]] = []
        start = self._view.find(probe)
        while start != -1:
            out.append(self.real_span(start, start + len(probe)))
            start = self._view.find(probe, start + 1)
        return out


def normalize(text: str, *, fold_case: bool = False) -> str:
    """The normalized view of `text`, without the offset map. For comparing two
    strings when no real span has to come back out of the comparison."""
    folded = fold_punct(text)
    out: list[str] = []
    for ch in folded:
        for c in _EXPAND.get(ch, ch):
            if c not in _DROP:
                out.append(c.lower() if fold_case else c)
    return "".join(out)


class IndexCache:
    """`NormIndex` objects keyed by the exact text they were built from.

    Applying a list of corrections searches every paragraph once per edit, so
    building the index each time is quadratic on a novel. Keying on the text
    itself — not on the paragraph — is what keeps that cache correct while edits
    mutate paragraphs in place: an edited paragraph simply has a new key, and its
    stale index is never consulted again."""

    __slots__ = ("_plain", "_folded")

    def __init__(self) -> None:
        self._plain: dict[str, NormIndex] = {}
        self._folded: dict[str, NormIndex] = {}

    def get(self, text: str, *, fold_case: bool = False) -> NormIndex:
        store = self._folded if fold_case else self._plain
        idx = store.get(text)
        if idx is None:
            idx = NormIndex(text, fold_case=fold_case)
            store[text] = idx
        return idx
