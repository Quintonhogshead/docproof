"""Concordance / KWIC (keyword-in-context) search over a :class:`Manuscript`.

The auditor and the prompts it feeds a model both want to *see* a term where it
lives — every occurrence with a little context on each side — rather than a bare
count. :func:`kwic` returns one :class:`Hit` per occurrence in reading order,
each carrying its paragraph id, char offsets, the matched text, and a clipped
left/right window.

Fuzzy mode exists for proper-name drift (``Kathryn`` vs ``Katherine``): with
``fuzzy=True`` a capitalized token within Levenshtein distance 2 of the term is
surfaced alongside the exact/substring matches. The Levenshtein is implemented
locally — standard library only, no external dependency.

Rendering mirrors a KWIC line: ``…left «match» right…``. No existing
corrections search-hit helper wraps matches this way (checked
``docproof/corrections/*``), so this guillemet form is the local choice; the
ellipses mark that the window was clipped from a longer paragraph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from galley.contracts import Manuscript

__all__ = ["Hit", "kwic", "levenshtein"]

# A token that could be a proper name: leading uppercase letter, letters after.
# Unicode-aware so accented names (José, Zoë) count as capitalized tokens.
_TOKEN = re.compile(r"\w+", re.UNICODE)

_ELLIPSIS = "…"  # …
_OPEN = "«"  # «
_CLOSE = "»"  # »


@dataclass(frozen=True)
class Hit:
    """One keyword-in-context occurrence.

    ``start``/``end`` index into the paragraph's canonical text (``end`` is
    exclusive), so ``ms.text_of(para_id)[start:end] == match``. ``left`` and
    ``right`` are the clipped context windows, already trimmed to paragraph
    boundaries; ``left_clipped``/``right_clipped`` record whether the paragraph
    extended past the window (i.e. whether an ellipsis belongs on that side).
    """

    para_id: str
    start: int
    end: int
    match: str
    left: str
    right: str
    fuzzy: bool = False
    left_clipped: bool = False
    right_clipped: bool = False

    def line(self) -> str:
        """Render as a KWIC line: ``…left «match» right…``.

        Ellipses appear only on a side that was actually clipped, so a match at
        the very start of a paragraph has no leading ellipsis.
        """

        lead = _ELLIPSIS if self.left_clipped else ""
        tail = _ELLIPSIS if self.right_clipped else ""
        return f"{lead}{self.left}{_OPEN}{self.match}{_CLOSE}{self.right}{tail}"

    def __str__(self) -> str:  # pragma: no cover - thin delegate
        return self.line()


def levenshtein(a: str, b: str, *, max_distance: int | None = None) -> int:
    """Edit distance between ``a`` and ``b`` (standard library only).

    ``max_distance`` lets the caller bail early: once every cell in a row exceeds
    the cap the true distance can only grow, so we return ``max_distance + 1`` as
    a sentinel "greater than the cap" without finishing the matrix.
    """

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if max_distance is not None and abs(len(a) - len(b)) > max_distance:
        return max_distance + 1

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            val = min(
                previous[j] + 1,  # deletion
                current[j - 1] + 1,  # insertion
                previous[j - 1] + cost,  # substitution
            )
            current.append(val)
            if val < best:
                best = val
        if max_distance is not None and best > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _window(text: str, start: int, end: int, window: int) -> tuple[str, str, bool, bool]:
    """Clip a left/right context window to paragraph boundaries.

    Returns ``(left, right, left_clipped, right_clipped)`` where the clipped
    flags say whether the paragraph continued past the window on that side.
    """

    left_start = max(0, start - window)
    right_end = min(len(text), end + window)
    return (
        text[left_start:start],
        text[end:right_end],
        left_start > 0,
        right_end < len(text),
    )


def _is_capitalized(token: str) -> bool:
    return bool(token) and token[0].isupper()


def kwic(
    ms: Manuscript,
    term: str,
    *,
    window: int = 60,
    fuzzy: bool = False,
    fold_case: bool = True,
) -> list[Hit]:
    """Keyword-in-context search across ``ms`` in reading order.

    Every occurrence of ``term`` (exact/substring match, case-insensitive when
    ``fold_case``) becomes a :class:`Hit` with a ``window``-char context on each
    side, clipped at paragraph boundaries. With ``fuzzy=True`` a capitalized
    token within Levenshtein distance 2 of ``term`` is also surfaced (for
    proper-name drift); exact/substring matching for the base term still applies.

    Results are ordered by manuscript reading order (``ms.order``), then by
    offset within a paragraph. Overlapping exact and fuzzy matches at the same
    offset are de-duplicated, preferring the exact match.
    """

    if not term:
        return []

    hits: list[Hit] = []
    for para_id in ms.order:
        text = ms.text_of(para_id)
        if not text:
            continue

        # Collect candidate spans keyed by start offset so exact wins over fuzzy.
        by_start: dict[int, tuple[int, int, str, bool]] = {}

        # Exact / substring matches. Search the ORIGINAL text so offsets are true;
        # for case-insensitivity, lower a working copy but keep length parity by
        # only folding when it does not change length (the common ASCII case);
        # otherwise scan char-by-char.
        for start, end in _find_exact(text, term, fold_case):
            by_start[start] = (start, end, text[start:end], False)

        # Fuzzy: capitalized tokens within edit distance 2 of the term.
        if fuzzy:
            key = term.casefold() if fold_case else term
            for m in _TOKEN.finditer(text):
                tok = m.group(0)
                if not _is_capitalized(tok):
                    continue
                start = m.start()
                if start in by_start:
                    continue  # already an exact hit here
                cand = tok.casefold() if fold_case else tok
                if cand == key:
                    continue  # exact equality handled by the substring pass
                if levenshtein(cand, key, max_distance=2) <= 2:
                    by_start[start] = (start, m.end(), tok, True)

        for start in sorted(by_start):
            s, e, matched, is_fuzzy = by_start[start]
            left, right, lclip, rclip = _window(text, s, e, window)
            hits.append(
                Hit(
                    para_id=para_id,
                    start=s,
                    end=e,
                    match=matched,
                    left=left,
                    right=right,
                    fuzzy=is_fuzzy,
                    left_clipped=lclip,
                    right_clipped=rclip,
                )
            )

    return hits


def _find_exact(text: str, term: str, fold_case: bool) -> list[tuple[int, int]]:
    """All non-overlapping (start, end) spans of ``term`` in ``text``.

    Offsets always index the original ``text``. When ``fold_case`` and the term
    contains characters whose casefold changes string length, we fall back to a
    regex with ``IGNORECASE`` so offsets stay honest.
    """

    if not term:
        return []

    if not fold_case:
        spans: list[tuple[int, int]] = []
        pos = 0
        n = len(term)
        while True:
            idx = text.find(term, pos)
            if idx < 0:
                break
            spans.append((idx, idx + n))
            pos = idx + max(1, n)
        return spans

    # Case-insensitive: a regex over the original text keeps offsets exact even
    # when casefold would change length (e.g. ß). Escape the term so it is
    # matched literally, not as a pattern.
    spans = []
    for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
        spans.append((m.start(), m.end()))
    return spans
