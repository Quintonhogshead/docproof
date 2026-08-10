"""Cross-chunk context: each chunk after the first carries the tail of the one
before it as read-only reference, so a pronoun or name whose antecedent sits in
the previous paragraph still resolves. The model is told never to report on it,
and the analyzer's para_id guard drops any finding that lands there anyway.
"""
from __future__ import annotations

import itertools

from docproof.analyzer import MockAnalyzer, render_chunk
from docproof.chunker import chunk_document
from docproof.config import load_config
from docproof.error_registry import load_error_types
from docproof.models import Chunk, DocumentModel, ParagraphRef, Usage

from .test_error_types import ERROR_DIR

# Two ~12-token sentences; a 20-token budget puts each in its own chunk, so the
# chunk boundaries are exact and easy to reason about.
A = "Mara opened the box that had waited a decade."
B = "She could not believe what was inside it."


def _doc(*texts) -> DocumentModel:
    paras = tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                               "Normal") for i, t in enumerate(texts))
    return DocumentModel(source_path="x.docx", paragraphs=paras)


def _cfg(**chunking):
    cfg = load_config("config/default.yaml")
    for k, v in chunking.items():
        setattr(cfg.chunking, k, v)
    return cfg


# --- the chunker populates context -------------------------------------------

def test_each_chunk_after_the_first_gets_the_previous_tail():
    chunks = chunk_document(_doc(A, B), _cfg(token_budget=20,
                                             context_token_budget=300))
    assert len(chunks) == 2
    assert chunks[0].context_paragraphs == ()          # nothing precedes it
    assert [p.para_id for p in chunks[1].context_paragraphs] == ["body-0000"]
    assert chunks[1].context_paragraphs[0].text == A


def test_context_can_be_disabled():
    chunks = chunk_document(_doc(A, B), _cfg(token_budget=20,
                                             context_token_budget=0))
    assert all(c.context_paragraphs == () for c in chunks)


def test_a_previous_paragraph_larger_than_the_budget_yields_no_context():
    """Better no antecedent than resending a dense paragraph on every chunk."""
    chunks = chunk_document(_doc(A, B), _cfg(token_budget=20,
                                             context_token_budget=3))
    assert len(chunks) == 2
    assert chunks[1].context_paragraphs == ()          # A is ~12 tokens > 3


def test_context_paragraph_count_is_capped():
    """Even under a large token budget, context is the last paragraph or two,
    never a wall of preceding short lines."""
    tiny = ["Line one is here.", "Line two is here.", "Line three is here.",
            "Line four is here.", "Line five is here.", "The final long line."]
    # A huge budget would otherwise pull all five prior lines into the last one.
    chunks = chunk_document(_doc(*tiny), _cfg(token_budget=6,
                                              context_token_budget=100000))
    assert len(chunks) == len(tiny)
    assert len(chunks[-1].context_paragraphs) <= 3


# --- what the model is shown -------------------------------------------------

def test_render_chunk_wraps_context_in_a_read_only_block():
    ctx = ParagraphRef("body-0000", "word/document.xml", "body", A, "Normal")
    target = ParagraphRef("body-0001", "word/document.xml", "body", B, "Normal")
    rendered = render_chunk(Chunk("chunk-001", (target,), 12,
                                  context_paragraphs=(ctx,)))
    head, _, tail = rendered.partition("</context>")
    assert head.startswith("<context>")
    assert '<paragraph id="body-0000">' in head      # context inside the block
    assert '<paragraph id="body-0001">' in tail      # the real paragraph after


def test_render_chunk_has_no_wrapper_without_context():
    target = ParagraphRef("body-0001", "word/document.xml", "body", B, "Normal")
    assert "<context>" not in render_chunk(Chunk("chunk-000", (target,), 12))


# --- context never leaks into findings ---------------------------------------

def _canned(para_id):
    return [{"para_id": para_id, "error_type": "comma_splice",
             "original_text": A, "corrected_text": A,
             "explanation": "x", "confidence": "high", "occurrence": 1}]


def test_a_finding_reported_on_context_is_dropped():
    group = list(load_error_types(ERROR_DIR, ["comma_splice"]).values())
    ctx = ParagraphRef("body-0000", "word/document.xml", "body", A, "Normal")
    target = ParagraphRef("body-0001", "word/document.xml", "body", B, "Normal")
    chunk = Chunk("chunk-001", (target,), 12, context_paragraphs=(ctx,))

    # A finding on the read-only context paragraph is discarded...
    on_context, _ = MockAnalyzer(group, _canned("body-0000"),
                                 itertools.count(1)).analyze_chunk(chunk, Usage())
    assert on_context == []
    # ...while one on the real paragraph survives.
    on_target, _ = MockAnalyzer(group, _canned("body-0001"),
                                itertools.count(1)).analyze_chunk(chunk, Usage())
    assert [f.para_id for f in on_target] == ["body-0001"]
