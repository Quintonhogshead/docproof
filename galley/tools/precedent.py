"""Precedent query — how were similar flags ruled before?

The one tool that makes book N+1 better than book N: it turns the archive of
human accept/reject decisions (ingested by ``galley.memory.ingest``) into a
ranked answer for a new finding. The ruling's reason comes back verbatim, so the
agent quotes the precedent rather than paraphrasing it.
"""

from __future__ import annotations

from galley.contracts import GFinding
from galley.memory.store import MemoryStore, Precedent


def _levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings (small local implementation)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similarity(find_text: str, other: str) -> float:
    """1.0 for identical text, down to 0.0, by normalized edit distance."""
    if not find_text and not other:
        return 1.0
    longest = max(len(find_text), len(other)) or 1
    return 1.0 - _levenshtein(find_text.lower(), other.lower()) / longest


def score(finding: GFinding, precedent: Precedent) -> tuple[int, float]:
    """Rank key for a precedent against a finding: type match first, then text.

    Returns ``(type_match, text_similarity)`` — a same-error-type precedent always
    outranks a different-type one, and within a type the closest find-text wins.
    Sort descending.
    """

    type_match = 1 if precedent.error_type == finding.error_type else 0
    return (type_match, _similarity(finding.find, precedent.find_text))


def precedents_for(
    finding: GFinding, store: MemoryStore, *, limit: int = 5
) -> list[Precedent]:
    """The most similar past rulings for ``finding``, most relevant first.

    Same-error-type precedents rank above different-type ones; within a type the
    closest find-text wins. Each result carries the ruling's ``reason`` verbatim.
    An empty store returns an empty list — never an error.
    """

    # Prefer the type-matched rows the store can filter for cheaply, but fall back
    # to the whole set so a good cross-type text match is still found.
    candidates = store.precedents(error_type=finding.error_type)
    seen_ids = {p.id for p in candidates}
    for p in store.precedents():
        if p.id not in seen_ids:
            candidates.append(p)

    candidates.sort(key=lambda p: score(finding, p), reverse=True)
    return candidates[:limit]


# Registered as an agent tool (mirrors galley.tools.checkable.TOOLS).
TOOLS = {"precedents_for": precedents_for}


__all__ = ["TOOLS", "precedents_for", "score"]
