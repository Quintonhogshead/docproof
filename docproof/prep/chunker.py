"""Windows of paragraphs, in document order.

Review chunks are independent by design — that is what lets a whole review go
out as one batch. Prep windows are the opposite: what a paragraph is depends on
what came before it, so windows run in order and each one is told how the tail
of the previous one was labelled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..utils.tokens import estimate_tokens
from .model import StructureParagraph

log = logging.getLogger("docproof.prep.chunker")


@dataclass(frozen=True)
class Window:
    index: int
    paragraphs: tuple[StructureParagraph, ...]
    # The paragraphs immediately before this window. Sent as already-decided
    # context, never labelled again.
    context: tuple[StructureParagraph, ...] = ()

    @property
    def ids(self) -> set[str]:
        return {p.para_id for p in self.paragraphs}


def windows(paragraphs, *, max_paragraphs: int, token_budget: int,
            context: int, preview_chars: int) -> list[Window]:
    """Split the manuscript into ordered windows.

    Two limits, whichever comes first: a paragraph count, because the model is
    answering one line per paragraph and a long list is where it starts
    skipping; and a token budget, because a window of dense prose is far bigger
    than a window of dialogue. Long paragraphs are truncated before either is
    measured — see `preview` — so the budget reflects what is actually sent."""
    items = list(paragraphs)
    out: list[Window] = []
    start = 0
    while start < len(items):
        end, tokens = start, 0
        while end < len(items) and end - start < max_paragraphs:
            cost = estimate_tokens(preview(items[end], preview_chars)) + 12
            if end > start and tokens + cost > token_budget:
                break
            tokens += cost
            end += 1
        out.append(Window(index=len(out),
                          paragraphs=tuple(items[start:end]),
                          context=tuple(items[max(0, start - context):start])))
        start = end
    log.info("Split %d paragraphs into %d window(s)", len(items), len(out))
    return out


def preview(p: StructureParagraph, limit: int) -> str:
    """What the model actually sees of a paragraph.

    A 300-word paragraph is body text and is recognisable as body text from its
    first two lines. The model returns ids and labels, never the text, so
    sending the tail of every paragraph would buy nothing and bill for it —
    on a novel this truncation is most of the difference in what prep costs."""
    text = " ".join(p.text.split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def split(window: Window) -> list[Window]:
    """Halve a window that failed, keeping document order and context.

    A window is lost as a unit — one over-long answer costs every paragraph in
    it — so a failure is retried smaller before it is given up on."""
    n = len(window.paragraphs)
    if n < 2:
        return []
    mid = n // 2
    first = Window(index=window.index, paragraphs=window.paragraphs[:mid],
                   context=window.context)
    second = Window(index=window.index, paragraphs=window.paragraphs[mid:],
                    context=(window.context + window.paragraphs[:mid])[-8:])
    return [first, second]
