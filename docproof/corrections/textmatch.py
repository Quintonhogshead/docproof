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
#
# The set is widened past the ASCII space and hyphen to the whole family a PDF
# reader emits and `fold_punct` does not canonicalize: the narrow, thin and other
# fixed-width spaces a justified line is set with; the zero-width joiners and the
# BOM a reader can leave inside a word; the soft hyphen and non-breaking hyphen a
# line break carries; the figure dash, horizontal bar and minus that stand in for
# a dash. Any of these, carried on an anchor quoted from the proof, would
# otherwise survive every tier and report a mark as "not found" against a book
# that sets the same words without it. (The curly quotes, the en/em dashes and the
# ordinary nbsp are already folded onto "/-/space by `fold_punct` upstream.)
_DROP = frozenset(
    " \t\r\n\f\v"
    "            "
    "  　"                        # fixed-width and no-break spaces
    "​‌‍⁠﻿"            # zero-width joiners, word joiner, BOM
    "-­‐‑‒―−")    # hyphen, soft/no-break hyphen, dashes

# One-to-many rewrites applied before the drop. Folding an ellipsis to three
# stops means an anchor typed either way matches the book either way; the offset
# map carries all three back to the single real character.
_EXPAND = {"…": "..."}

# The same three rules as one `str.translate` table, so building a view is a
# single C-level pass instead of a Python loop over every character of the book.
# A dropped character maps to None, an expanded one to its replacement, and every
# other character to itself — which is exactly what the loop below did, one
# character at a time, several million times per corrections run.
_VIEW_TABLE: dict[int, str | None] = {ord(c): None for c in _DROP}
_VIEW_TABLE.update({ord(k): v for k, v in _EXPAND.items()})
# What each expanded character contributes to the view *after* the drop, so the
# offset map below stays in step with the table without re-deriving it.
_EXPAND_KEPT = {k: "".join(c for c in v if c not in _DROP)
                for k, v in _EXPAND.items()}


def _case_table(view: str) -> dict[int, str]:
    """A lowercasing translate table for exactly the characters `view` contains.

    `str.lower()` is not always length-preserving — "İ" lowercases to two
    characters — and a view that grows under folding no longer lines up with the
    offset map built beside it, which is how a match would come back pointing at
    the wrong characters. So the table carries only the single-character foldings
    and leaves the rest of them alone: an anchor that needed one of those to match
    is reported as not found, which is the direction this engine fails in."""
    table: dict[int, str] = {}
    for ch in set(view):
        low = ch.lower()
        if low != ch and len(low) == 1:
            table[ord(ch)] = low
    return table


class NormIndex:
    """A text's normalized view, and the map back to real offsets.

    `spans(needle)` returns the real `(start, end)` spans of the text that
    correspond to each occurrence of `needle` in the view, so a caller edits the
    real characters even though it matched the canonical ones."""

    __slots__ = ("_text", "_view", "_src", "_fold_case")

    def __init__(self, text: str, *, fold_case: bool = False,
                 base: "NormIndex | None" = None) -> None:
        self._text = text
        self._fold_case = fold_case
        # `base` is this same text's un-folded index, when one has already been
        # built: case folding is length-preserving, so the folded view is the
        # plain one translated, and re-deriving it from the text would repeat the
        # punctuation fold and the drop for nothing.
        view = base.view if base is not None else fold_punct(text).translate(_VIEW_TABLE)
        if fold_case:
            view = view.translate(_case_table(view))
        self._view = view
        # The offset map is built on demand — see `_sources`. Locating an edit
        # searches every paragraph of the book, so all but a handful of these
        # indexes are asked whether they contain the anchor and nothing more;
        # only the ones that say yes ever need to say where.
        self._src: list[int] | None = None

    def _sources(self) -> list[int]:
        """The offset map, built the first time a match has to be placed.

        The character rules here mirror `_VIEW_TABLE` exactly — drop, expand,
        keep — so the map lines up with the view built from that table. Case
        folding cannot move a character (`_case_table` only carries the
        length-preserving foldings), so it does not enter into this."""
        src = self._src
        if src is None:
            folded = fold_punct(self._text)
            if any(k in folded for k in _EXPAND):
                src = []
                for i, ch in enumerate(folded):
                    kept = _EXPAND_KEPT.get(ch)
                    if kept is not None:
                        src.extend([i] * len(kept))
                    elif ch not in _DROP:
                        src.append(i)
            else:
                src = [i for i, ch in enumerate(folded) if ch not in _DROP]
            self._src = src
        return src

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
        return self._sources()

    def real_offset(self, view_offset: int) -> int:
        """The real offset the view character at `view_offset` came from. The
        end of the view maps to one past the last real character used."""
        src = self._sources()
        if view_offset >= len(src):
            return (src[-1] + 1) if src else 0
        return src[view_offset]

    def real_span(self, start: int, end: int) -> tuple[int, int]:
        """The real `(start, end)` for a half-open span of the view."""
        if start >= end:
            return (0, 0)
        src = self._sources()
        if not src:
            return (0, 0)
        return src[start], src[end - 1] + 1

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


# Normalizing an anchor is the single most repeated operation in a corrections
# run: `find` and `context` are re-normalized once per paragraph they are looked
# for in, so one edit against a novel normalizes the same short string thousands
# of times. The result depends on nothing but the arguments, so it is memoized.
# Only short strings are cached — an anchor is a phrase, while `pagemap` hands
# this whole pages of a proof, and caching those would evict the anchors that
# earn the cache in the first place.
_NORM_CACHE_MAX_LEN = 4000
_NORM_CACHE_MAX_ENTRIES = 8192
_norm_cache: dict[tuple[str, bool], str] = {}


def normalize(text: str, *, fold_case: bool = False) -> str:
    """The normalized view of `text`, without the offset map. For comparing two
    strings when no real span has to come back out of the comparison."""
    if len(text) > _NORM_CACHE_MAX_LEN:
        return _normalize(text, fold_case)
    key = (text, fold_case)
    got = _norm_cache.get(key)
    if got is None:
        got = _normalize(text, fold_case)
        if len(_norm_cache) >= _NORM_CACHE_MAX_ENTRIES:
            _norm_cache.clear()
        _norm_cache[key] = got
    return got


def _normalize(text: str, fold_case: bool) -> str:
    view = fold_punct(text).translate(_VIEW_TABLE)
    return view.translate(_case_table(view)) if fold_case else view


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
            # The folded index is built off the plain one when it is already
            # here — the two views differ only by a length-preserving fold, and
            # every caller that wants the folded view has just consulted the
            # plain one.
            idx = NormIndex(text, fold_case=fold_case,
                            base=self._plain.get(text) if fold_case else None)
            store[text] = idx
        return idx
