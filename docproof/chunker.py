from __future__ import annotations

import dataclasses
import logging
import re

from .config import Config
from .models import Chunk, DocumentModel, ParagraphRef
from .utils.tokens import estimate_tokens

log = logging.getLogger("docproof.chunker")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Even under a generous token budget, context is meant to be the paragraph or
# two right before — never a wall of preceding text. This caps the count so a
# run of very short paragraphs can't quietly fill the budget with a dozen.
_MAX_CONTEXT_PARAS = 3


def chunk_document(doc: DocumentModel, cfg: Config) -> tuple[Chunk, ...]:
    budget = cfg.chunking.token_budget
    chunks: list[Chunk] = []
    cur: list[ParagraphRef] = []
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur, cur_tokens
        if cur:
            chunks.append(Chunk(f"chunk-{len(chunks):03d}", tuple(cur), cur_tokens))
            cur, cur_tokens = [], 0

    for p in doc.paragraphs:
        # Short paragraphs are ingested so the sweeps can reach them, but a
        # model pass over a two-word line is not worth the request.
        if not p.reviewable:
            continue
        t = estimate_tokens(p.text)
        if t > budget:                       # oversized paragraph: its own chunk(s)
            flush()
            for piece in _oversized_pieces(p, cfg):
                chunks.append(Chunk(f"chunk-{len(chunks):03d}", (piece,),
                                    estimate_tokens(piece.text)))
            continue
        if cur and cur_tokens + t > budget:
            flush()
        cur.append(p)
        cur_tokens += t
    flush()

    log.info("Packed %d of %d paragraphs into %d chunks (budget %d tokens)",
             sum(1 for p in doc.paragraphs if p.reviewable),
             len(doc.paragraphs), len(chunks), budget)
    return _attach_context(chunks, cfg.chunking.context_token_budget)


def _attach_context(chunks: list[Chunk], budget: int) -> tuple[Chunk, ...]:
    """Give each chunk (after the first) the tail of the one before it as
    read-only context. Populated here rather than during packing so the context
    is always the immediately preceding text in document order, whatever a later
    selection keeps: a reaching-back pronoun's antecedent does not move because
    the caller chose to review only chapter three."""
    if budget <= 0 or len(chunks) < 2:
        return tuple(chunks)
    out = [chunks[0]]
    for prev, chunk in zip(chunks, chunks[1:]):
        out.append(dataclasses.replace(
            chunk, context_paragraphs=_trailing(prev.paragraphs, budget)))
    return tuple(out)


def _trailing(paragraphs: tuple[ParagraphRef, ...], budget: int
              ) -> tuple[ParagraphRef, ...]:
    """The last few paragraphs that fit inside the token budget, in order. If
    even the single preceding paragraph is larger than the budget, the context
    is empty — better no antecedent than paying to resend a dense paragraph on
    every chunk."""
    picked: list[ParagraphRef] = []
    used = 0
    for p in reversed(paragraphs):
        t = estimate_tokens(p.text)
        if used + t > budget or len(picked) >= _MAX_CONTEXT_PARAS:
            break
        picked.append(p)
        used += t
    return tuple(reversed(picked))


def _oversized_pieces(p: ParagraphRef, cfg: Config) -> list[ParagraphRef]:
    """A paragraph over the soft budget rides alone in its own chunk — modern
    context windows dwarf our budget, which exists for attention quality, not
    capacity. Only beyond hard_cap_tokens do we split at sentence boundaries,
    with a logged caveat: `occurrence` is counted by the model within the
    slice it saw but resolved by the validator against the full paragraph, so
    a duplicated sentence inside one mega-paragraph may mis-anchor. Rare
    enough to log rather than engineer around in v1."""
    if estimate_tokens(p.text) <= cfg.chunking.hard_cap_tokens:
        return [p]
    log.warning("Paragraph %s (~%d tokens) exceeds hard cap %d; splitting at "
                "sentence boundaries — occurrence disambiguation degrades here.",
                p.para_id, estimate_tokens(p.text), cfg.chunking.hard_cap_tokens)
    max_chars = int(cfg.chunking.hard_cap_tokens * 3.5)
    pieces, buf = [], ""
    for sent in _SENTENCE_SPLIT.split(p.text):
        if buf and len(buf) + len(sent) + 1 > max_chars:
            pieces.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip() if buf else sent
    if buf:
        pieces.append(buf)
    return [dataclasses.replace(p, text=piece) for piece in pieces]