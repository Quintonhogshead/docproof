"""Whether a margin comment's premise still holds in the delivered text.

A query is written against the text as it stood when the lane read it. Later
lanes edit that text: on 2026-09-07 two "a closing quotation mark may be
missing" comments reached the author beside passages that settle had already
closed. The comment was true when asked and false when delivered, and nothing
re-asked. For the error types whose premise is a deterministic fact about the
paragraph, this module re-asks."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

_OPEN, _CLOSE = "“", "”"


def quotes_unbalanced(text: str) -> bool:
    """True when the paragraph opens a quotation it never closes (curly), or
    carries an odd number of straight double quotes."""
    if text.count(_OPEN) != text.count(_CLOSE):
        return True
    return text.count('"') % 2 == 1


#: error_type -> predicate on the DELIVERED paragraph text. The comment's
#: premise holds while the predicate is True.
PREMISES: dict[str, Callable[[str], bool]] = {
    "unclosed_quote": quotes_unbalanced,
    "sweep_unbalanced_quote": quotes_unbalanced,
    "unbalanced_quote": quotes_unbalanced,
}


def stale_queries(rows: Sequence[Mapping[str, Any]],
                  delivered: Mapping[str, str]
                  ) -> list[tuple[Mapping[str, Any], str]]:
    """The query rows whose premise the delivered text no longer supports:
    ``(row, why)``. Rows of a type with no predicate are never stale here."""
    from galley.settle import terminal_state

    out = []
    for r in rows:
        state, reason = terminal_state(r)
        if state != "query" or reason == "withheld":
            continue
        pred = PREMISES.get(str(r.get("error_type") or ""))
        if pred is None:
            continue
        pid = str(r.get("para_id") or "")
        text = delivered.get(pid)
        if text is None:
            continue
        if not pred(text):
            out.append((r, f"{r.get('error_type')} on {pid}: the delivered "
                           f"paragraph no longer has that problem"))
    return out


__all__ = ["PREMISES", "quotes_unbalanced", "stale_queries"]
