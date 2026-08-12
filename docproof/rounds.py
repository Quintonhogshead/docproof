"""Multi-round review orchestration.

DocProof normally reviews a manuscript once. Multi-round review reviews it
several times, each round reading the previous round's *corrected* text, so a
mistake that only becomes visible once its neighbours are fixed still gets
caught. Between rounds a strong judge (docproof/verifier.py) rules on every
proposed correction, because an approval here is applied AND becomes what the
next round reads.

The hard part is coordinates. Round k's findings are expressed in the text as
round k-1 left it, but the delivered file must show every change as one tracked
change against the *original*. This module keeps a per-paragraph EditLayer
(docproof/editlayer.py) — the cumulative original->working diff — folds each
round's approved edits into it (composing where a later round edits text an
earlier round introduced), and at the end emits one findings list in original
coordinates for the existing validate -> apply path.

`run_rounds` is pure: no document I/O, no model client of its own. The caller
supplies a `review` callable that materialises the current working document and
reviews it, and a judge `provider`. This keeps the loop testable end to end with
fakes, and lets the same loop drive either the sync or the batch review path.
"""
from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Callable

from .editlayer import Contribution, EditLayer, RoundEdit
from .error_registry import ErrorType
from .models import DocumentModel, Finding, Usage, index_paragraphs
from .providers import Provider
from .validator import find_nth, validate_findings
from .verifier import adjudicate_round

log = logging.getLogger("docproof.rounds")


@dataclass(frozen=True)
class RoundReview:
    """One round's review output, in that round's (working) coordinates.

    `para_text` is the working text the round actually reviewed — it MUST equal
    the current render of the edit layers (the caller builds the working document
    from those layers), which `run_rounds` asserts. `model_findings` are the
    tracked-change candidates the judge rules on; `sweep_findings` are the
    deterministic, auto-approved edits; `query_findings` are findings already
    bound for the margin (forwarded, not judged)."""
    model_findings: list[Finding]
    para_text: dict[str, str]
    types: dict[str, ErrorType] = field(default_factory=dict)
    sweep_findings: list[Finding] = field(default_factory=list)
    query_findings: list[Finding] = field(default_factory=list)
    context: str = ""


@dataclass(frozen=True)
class RoundsResult:
    """The whole run, in ORIGINAL coordinates, ready for validate -> apply.
    `edits` are the composed tracked changes; `queries` the margin questions;
    `rejected` the corrections the judge set aside (report only). `layers` is the
    per-paragraph diff, exposed for reporting/tests."""
    edits: list[Finding]
    queries: list[Finding]
    rejected: list[Finding]
    layers: dict[str, EditLayer]
    rounds_run: int


# review(round_index, edits_by_para) -> RoundReview. `edits_by_para` is the
# cumulative layer as (start, end, replacement) tuples per paragraph, so the
# adapter can materialise the working document to review.
Review = Callable[[int, dict[str, list[tuple[int, int, str]]]], RoundReview]


def run_rounds(orig_doc: DocumentModel, review: Review, provider: Provider, *,
               count: int, judge_model: str, judge_prompt: str = "",
               min_confidence: str = "medium",
               query_types=frozenset(), format_types=None, edit_guard=None,
               min_new_edits: int = 1, concurrency: int = 8,
               usage: Usage) -> RoundsResult:
    """Run up to `count` review rounds and compose the result.

    Each round: build the working edits from the layers, review, judge the
    candidates, validate the approved edits (with the round's sweeps) against the
    working text, translate that round's queries back to original coordinates,
    then fold the approved edits into the layers. Stops early once a round past
    the first approves fewer than `min_new_edits` corrections."""
    orig_text = {p.para_id: p.text for p in orig_doc.paragraphs}
    orig_ref = index_paragraphs(orig_doc)
    layers: dict[str, EditLayer] = {}
    collected_queries: list[_Query] = []
    rejected: list[Finding] = []
    rejections: frozenset = frozenset()
    rounds_run = 0

    for k in range(1, count + 1):
        rounds_run = k
        edits_by_para = {pid: [(e.orig_start, e.orig_end, e.replacement)
                               for e in lyr.edits]
                         for pid, lyr in layers.items()}
        rr = review(k, edits_by_para)

        judgment = adjudicate_round(
            list(rr.model_findings), rr.para_text, rr.types, provider,
            model=judge_model, instructions=judge_prompt, context=rr.context,
            concurrency=concurrency, prior_rejections=rejections, usage=usage)
        rejections = rejections | judgment.new_rejections
        rejected.extend(judgment.rejected)

        # Validate the approved edits, the round's sweeps, and every query
        # together against the working text: the validator anchors all of them
        # and routes force_query findings to the query channel.
        working_doc = _doc_with_text(orig_doc, rr.para_text)
        to_validate = (list(rr.sweep_findings) + list(judgment.edits)
                       + list(judgment.queries) + list(rr.query_findings))
        validated = validate_findings(
            to_validate, working_doc, min_confidence,
            query_types=query_types, format_types=format_types,
            edit_guard=edit_guard)

        # Translate this round's queries to original coordinates BEFORE folding
        # (their working anchors are relative to the pre-fold layers).
        for f in validated:
            if f.status == "query" and f.anchor is not None:
                lyr = layers.get(f.para_id, EditLayer())
                lo, hi = lyr.to_orig_span(orig_text[f.para_id],
                                          f.anchor.start, f.anchor.end)
                collected_queries.append(
                    _Query(f.para_id, lo, hi, f.corrected_text, f.explanation,
                           f.error_type))

        # Fold the approved tracked edits into the layers.
        per_para: dict[str, list[Finding]] = defaultdict(list)
        for f in validated:
            if f.status == "validated" and f.anchor is not None:
                per_para[f.para_id].append(f)

        new_edits = 0
        for pid, fs in per_para.items():
            base = orig_text[pid]
            lyr = layers.get(pid, EditLayer())
            if rr.para_text.get(pid, base) != lyr.render(base):
                raise ValueError(
                    f"round {k}: working text for {pid} does not match the "
                    f"folded layer — the working document is out of step")
            redits = [RoundEdit(
                f.anchor.start, f.anchor.end, f.anchor.insert_text,
                Contribution(k, f.finding_id, f.error_type, f.explanation))
                for f in sorted(fs, key=lambda x: x.anchor.start)]
            res = lyr.fold_round(base, redits, edit_guard)
            layers[pid] = res.layer
            new_edits += len(redits) - res.noops
            for ov in res.oversteps:                   # too large to compose -> query
                collected_queries.append(_Query(
                    pid, ov.orig_start, ov.orig_end, ov.replacement,
                    _join_reasons(ov.contributions),
                    ov.contributions[0].error_type))

        log.info("round %d: %d correction(s) folded, %d queried, %d rejected",
                 k, new_edits,
                 sum(1 for f in validated if f.status == "query"),
                 len(judgment.rejected))
        if k > 1 and new_edits < min_new_edits:
            log.info("stopping after round %d: %d < min_new_edits %d",
                     k, new_edits, min_new_edits)
            break

    return _compose(orig_ref, layers, collected_queries, rejected, rounds_run)


# --- composing the final, original-coordinate result -------------------------

@dataclass(frozen=True)
class _Query:
    para_id: str
    orig_lo: int
    orig_hi: int
    suggested: str
    reason: str
    error_type: str


def _compose(orig_ref, layers, queries, rejected, rounds_run) -> RoundsResult:
    ids = (f"rr-{i:04d}" for i in itertools.count(1))
    edits: list[Finding] = []
    for pid, lyr in layers.items():
        edits.extend(lyr.to_findings(orig_ref[pid], "rounds", ids))

    seen: set[tuple] = set()
    query_findings: list[Finding] = []
    for q in queries:
        key = (q.para_id, q.orig_lo, q.orig_hi)
        if key in seen:
            continue
        seen.add(key)
        para = orig_ref.get(q.para_id)
        if para is None:
            continue
        text = para.text
        if q.orig_lo < q.orig_hi:                       # quote the span
            quote = text[q.orig_lo:q.orig_hi]
            occ = text[:q.orig_lo].count(quote) + 1
        else:                                           # zero-width: quote the paragraph
            quote = text
            occ = 1
        if find_nth(text, quote, occ) == -1:            # cannot happen, but stay safe
            continue
        query_findings.append(Finding(
            finding_id=next(ids), chunk_id="rounds", para_id=q.para_id,
            error_type=q.error_type, original_text=quote, occurrence=occ,
            corrected_text=q.suggested or quote, explanation=q.reason,
            confidence="medium", force_query=True))
    return RoundsResult(edits, query_findings, rejected, layers, rounds_run)


def _doc_with_text(orig_doc: DocumentModel, para_text: dict[str, str]
                   ) -> DocumentModel:
    """`orig_doc`'s paragraphs with their text replaced by the current working
    text — the coordinate space this round's findings anchor into."""
    paras = tuple(replace(p, text=para_text.get(p.para_id, p.text))
                  for p in orig_doc.paragraphs)
    return DocumentModel(orig_doc.source_path, paras, orig_doc.skipped)


def _join_reasons(contribs) -> str:
    seen: list[str] = []
    for c in contribs:
        e = c.explanation.strip()
        if e and e not in seen:
            seen.append(e)
    return " ".join(seen)
