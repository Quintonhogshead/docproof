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
from .models import (CoverageLedger, DocumentModel, Finding, Usage,
                     index_paragraphs)
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


# --- the synchronous review adapter ------------------------------------------
#
# Drives `run_rounds` against the real pipeline on the sync path: each round
# builds the working document from the current layers, reviews it with
# prepare + run_sync, and the final result is assembled by the existing
# `finish` (validate -> apply -> save -> reports, including the audit that
# checks rejecting every change reproduces the original). The whole-book reads
# (glossary, story sheet) are done once and reused for later rounds when
# `rounds.reuse_whole_book_reads` is set — mechanical fixes change no names or
# narration, and it drops both the re-read cost and the run-to-run wobble.


class _ApproveAllJudge:
    """A judge stand-in used only when no real judge provider is available (a
    mock/offline run): approves every candidate, so the multi-round wiring can
    be exercised without a model call."""

    name = "approve-all"

    def complete_structured(self, *, user, **kwargs):
        import re

        from .providers import ProviderResult
        ids = re.findall(r"finding_id=(\S+)", user)
        return ProviderResult(parsed={"verdicts": [
            {"finding_id": fid, "verdict": "approve", "reason": ""}
            for fid in ids]})


def _merge_usage(dst: Usage, src) -> None:
    if src is None:
        return
    dst.api_calls += getattr(src, "api_calls", 0)
    for f in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        setattr(dst, f, getattr(dst, f) + getattr(src, f, 0))


def run_sync_rounds(cfg, input_path, error_dir, *, out_dir, source_path=None,
                    review_provider=None, judge_provider=None,
                    mock_findings=None, on_progress=None, should_cancel=None,
                    on_phase=None):
    """Run multi-round review synchronously and write the deliverable.

    Returns the same `Outputs` as a single review. `review_provider` and
    `judge_provider` are injectable for testing; in production they are built
    from `cfg` (the detector model, and the judge model/effort from
    `cfg.rounds`). `mock_findings`, if given, feeds each round the canned
    findings instead of calling the detector model (for an offline smoke run).

    `on_progress`, if given, is called with (round, total_rounds, done, total):
    once as each round starts (done and total both 0 — the working document is
    still being rebuilt and re-ingested then), and again per folded call as that
    round's run_sync counts its sections. The section count starts over every
    round; the round number is what keeps that legible on a card.

    `on_phase`, if given, is run_sync's stage callback, cycled once per round:
    "preparing" while the working document is rebuilt and re-ingested, the
    review stages as that round's run_sync walks them, then "round_judge" as
    the between-rounds judge takes this round's corrections. The final
    finish's passes speak through it too. A caller's step display starts over
    each round — the round number from `on_progress` is what keeps it legible.

    `should_cancel`, if given, is polled at each round's start and forwarded into
    that round's run_sync (which polls it per folded call); when it first returns
    true the run raises `JobCancelled`, so an abort stops before the next round's
    spend rather than only hiding the result."""
    from pathlib import Path

    from .pipeline import JobCancelled, prepare, run_sync
    from .providers import build_provider

    out_dir = Path(out_dir)
    source_path = source_path or input_path
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)

    prepared0 = prepare(cfg, input_path, error_dir)
    orig_doc = prepared0.doc
    base_path = rounds_dir / "base.docx"
    prepared0.pkg.save(base_path)                    # clean normalized original (W0)

    if review_provider is None and mock_findings is None:
        review_provider = build_provider(cfg)
    if judge_provider is None:
        judge_provider = _build_judge(cfg, mock_findings is not None)

    usage = Usage()
    reuse = cfg.rounds.reuse_whole_book_reads
    captured: dict = {"format": []}

    def review(k, edits_by_para):
        if should_cancel and should_cancel():
            raise JobCancelled()
        if on_progress:
            on_progress(k, cfg.rounds.count, 0, 0)
        if on_phase:
            on_phase("preparing")
        rcfg = _round_config(cfg, k, reuse)
        if k == 1:
            prepared = prepared0
        else:
            wpath = _working_docx(rounds_dir, orig_doc, base_path, k,
                                  edits_by_para)
            prepared = prepare(rcfg, wpath, error_dir)
            if reuse:                                # give detectors round 1's sheet
                prepared.story_sheet = prepared0.story_sheet
        if mock_findings is not None:
            from .__main__ import _run_mock
            raw, u = _run_mock(rcfg, prepared, mock_findings)
        else:
            progress = None if on_progress is None else (
                lambda done, total: on_progress(k, cfg.rounds.count,
                                                done, total))
            raw, u = run_sync(rcfg, prepared, review_provider,
                              progress=progress, should_cancel=should_cancel,
                              on_phase=on_phase)
        _merge_usage(usage, u)
        rr, fmt = _round_review(prepared, raw)
        if k == 1:
            captured["format"] = fmt
        # The judge takes this round's corrections the moment review() returns
        # (see run_rounds), so the stage is named here — the loop itself stays
        # free of phase plumbing.
        if on_phase:
            on_phase("round_judge")
        return rr

    result = _drive(cfg, prepared0, orig_doc, review, judge_provider, usage)
    return _finalize(cfg, prepared0, base_path, result, captured["format"],
                     usage, out_dir, source_path, on_phase=on_phase)


def run_batch_rounds(cfg, input_path, error_dir, workspace, *, out_dir,
                     source_path=None, review_provider=None,
                     judge_provider=None, poll_interval=30.0,
                     on_progress=None, should_cancel=None, on_phase=None):
    """Run multi-round review on the batch path and write the deliverable.

    Each round submits a review batch on the current working document, waits for
    it, and collects the raw findings; the judge and the fold happen in this
    process between rounds. Runs in a single process (the poll loop blocks), so
    it is meant to be driven from a background worker rather than an interactive
    command. `review_provider`/`judge_provider` are injectable for testing;
    `poll_interval` is the seconds between polls (a fake batch completes on the
    first poll, so tests never sleep).

    `on_progress`, if given, is called with (round, total_rounds, done, total)
    as each round starts. A vendor batch reports no per-section progress while
    it runs, so done and total stay 0 here — the round boundary is the signal.

    `should_cancel`, if given, is polled at each round's start and between polls
    of the current round's batch; when it first returns true the run raises
    `JobCancelled`. The batch already submitted has been paid for, but no further
    round is submitted — the abort caps the spend at the round in flight."""
    import time
    from pathlib import Path

    from . import batch as batchlib
    from .pipeline import JobCancelled, prepare
    from .providers import build_provider

    out_dir = Path(out_dir)
    source_path = source_path or input_path
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)

    prepared0 = prepare(cfg, input_path, error_dir)
    orig_doc = prepared0.doc
    base_path = rounds_dir / "base.docx"
    prepared0.pkg.save(base_path)

    if review_provider is None:
        review_provider = build_provider(cfg)
    if judge_provider is None:
        judge_provider = _build_judge(cfg, False)

    usage = Usage()
    reuse = cfg.rounds.reuse_whole_book_reads
    captured: dict = {"format": []}

    def review(k, edits_by_para):
        if should_cancel and should_cancel():
            raise JobCancelled()
        if on_progress:
            on_progress(k, cfg.rounds.count, 0, 0)
        if on_phase:
            on_phase("preparing")
        rcfg = _round_config(cfg, k, reuse)
        wpath = _working_docx(rounds_dir, orig_doc, base_path, k, edits_by_para)
        job = batchlib.submit(rcfg, str(wpath), error_dir, review_provider,
                              workspace)
        # "reviewing" for the whole vendor wait: with total_rounds on the job
        # record this is what renders as "Round k — processing overnight"
        # rather than a stuck "Reading your manuscript". Collect then walks the
        # stages itself (re-ingest, fold, the post-passes).
        if on_phase:
            on_phase("reviewing")
        while True:
            batchlib.poll(job, review_provider, workspace)
            if job.state == "ready":
                break
            if job.state == "failed":
                raise batchlib.BatchError(
                    job.error or f"batch {job.batch_id} failed")
            if should_cancel and should_cancel():
                raise JobCancelled()
            time.sleep(poll_interval)
        r = batchlib.collect_findings(job, review_provider, error_dir,
                                      on_phase=on_phase)
        _merge_usage(usage, r.usage)
        if reuse and k > 1:                          # judge context; detectors already ran
            r.prepared.story_sheet = prepared0.story_sheet
        rr, fmt = _round_review(r.prepared, r.findings)
        if k == 1:
            captured["format"] = fmt
        # Same as the sync adapter: the judge starts on these corrections the
        # moment this returns.
        if on_phase:
            on_phase("round_judge")
        return rr

    result = _drive(cfg, prepared0, orig_doc, review, judge_provider, usage)
    return _finalize(cfg, prepared0, base_path, result, captured["format"],
                     usage, out_dir, source_path, on_phase=on_phase)


# --- shared driver helpers (sync and batch) ----------------------------------

def _build_judge(cfg, mock: bool):
    from .providers import build_provider
    if mock:
        return _ApproveAllJudge()
    jcfg = cfg.model_copy(deep=True)
    jcfg.api.model = cfg.rounds.judge_model
    jcfg.api.effort = cfg.rounds.judge_effort
    return build_provider(jcfg)


def _round_config(cfg, k: int, reuse: bool):
    """Round k's config: round 1 uses cfg as-is; later rounds skip the
    whole-book re-reads (glossary, story sheet) and the mechanical-floor pass
    when reuse is on — round 1 already caught them, and the confirmed mechanical
    fixes it applies introduce no new commas/hyphens/missing words for a later
    round to find. Dropping LanguageTool also drops its per-round scan, which on
    a large manuscript is minutes of single-core work in its own right.

    Chapter continuity is forced off in EVERY per-round config: it is a
    whole-book pass that runs once in the final finish(), and its reads would
    otherwise buy a fresh batch every round. The final finish uses the original
    cfg, so the pass still runs there — synchronously, since the final finish is
    not a batch-submit boundary.

    Quote/space normalization is forced off for every round after the first. The
    base is normalized exactly once (round 1, W0); a later round reviews a
    working document rebuilt from the edit layers, whose text is that normalized
    base with the approved edits folded in. Re-normalizing it on re-ingest would
    curl a quote or collapse a double space that an edit introduced, so the
    re-ingested working text would no longer match the layer's render and
    run_rounds would abort ("the working document is out of step"). The layers
    are the source of truth after round 1, so the working copy must be read back
    verbatim."""
    if k == 1:
        # Round 1 runs the config as given — EXCEPT that chapter continuity, a
        # once-per-book pass, must never buy a per-round batch. Copy only when
        # there is something to force off, so the common case returns cfg as-is.
        if not cfg.chapter_continuity.enabled:
            return cfg
        rcfg = cfg.model_copy(deep=True)
        rcfg.chapter_continuity.enabled = False
        return rcfg
    rcfg = cfg.model_copy(deep=True)
    rcfg.chapter_continuity.enabled = False
    # Candidate generation/screens the book once in round 1. Its admitted
    # Findings are folded into the working text like every other correction;
    # rerunning the same recall lane in later rounds would rebill it while the
    # final candidate ledger can represent only one source-coordinate draft.
    rcfg.candidate_screening.mode = "off"
    rcfg.normalize.quotes = False
    rcfg.normalize.spaces = False
    if reuse:
        rcfg.glossary.enabled = False
        rcfg.storysheet.enabled = False
        rcfg.languagetool.enabled = False
    return rcfg


def _working_docx(rounds_dir, orig_doc, base_path, k: int, edits_by_para):
    """The document round k reviews: the normalized-original base for round 1,
    else that base with the cumulative edits applied untracked."""
    from .reassembler import apply_untracked
    from .utils.xml_helpers import DocxPackage
    if k == 1:
        return base_path
    wpkg = DocxPackage(base_path)
    apply_untracked(wpkg, orig_doc, edits_by_para)
    wpath = rounds_dir / f"round-{k - 1}.docx"
    wpkg.save(wpath)
    return wpath


def _round_review(prepared, raw):
    """Split a round's raw findings into tracked-change candidates (judged),
    forwarded queries, and format findings (returned separately, applied once at
    the end), and build the RoundReview the orchestrator consumes."""
    query_types = prepared.query_types
    format_types = set(prepared.format_types or {})
    candidates, forwarded, fmt = [], [], []
    for f in raw:
        if f.error_type in format_types:             # italic etc.: apply at the end
            fmt.append(f)
        elif f.force_query or f.error_type in query_types:
            forwarded.append(f)
        else:
            candidates.append(f)
    para_text = {p.para_id: p.text for p in prepared.doc.paragraphs}
    types = {et.key: et for group in prepared.groups for et in group}
    context = "\n\n".join(x for x in (prepared.conventions, prepared.vocabulary,
                                      prepared.story_sheet) if x)
    rr = RoundReview(
        model_findings=candidates, para_text=para_text, types=types,
        sweep_findings=list(prepared.sweep_findings)
        + list(prepared.consistency_findings),
        query_findings=forwarded, context=context)
    return rr, fmt


def _drive(cfg, prepared0, orig_doc, review, judge_provider, usage):
    return run_rounds(
        orig_doc, review, judge_provider, count=cfg.rounds.count,
        judge_model=cfg.rounds.judge_model, judge_prompt=cfg.rounds.judge_prompt,
        min_confidence=cfg.min_confidence, query_types=prepared0.query_types,
        format_types=prepared0.format_types, edit_guard=cfg.edit_guard,
        min_new_edits=cfg.rounds.min_new_edits,
        concurrency=cfg.concurrency_for(cfg.rounds.judge_model),
        usage=usage)


def _finalize(cfg, prepared0, base_path, result, fmt_findings, usage, out_dir,
              source_path, *, on_phase=None):
    """Assemble the deliverable with the existing finish. The composed layers
    already contain the sweep and consistency edits (folded every round), so this
    prepared has those emptied to avoid applying them twice; a fresh package off
    the normalized-original base receives the tracked changes."""
    import dataclasses
    import json
    from dataclasses import replace as dc_replace

    from .checkpoint import finding_to_dict
    from .pipeline import finish
    from .utils.xml_helpers import DocxPackage
    final_pkg = DocxPackage(base_path)
    prepared_final = dc_replace(prepared0, pkg=final_pkg,
                                sweep_findings=[], consistency_findings=[])
    findings = result.edits + result.queries + fmt_findings

    # The composed result, kept beside the working files BEFORE finish runs: if
    # finish fails its reject-all audit, download-anyway rebuilds the deliverable
    # from this snapshot — the N rounds are already paid for, and this path has
    # no checkpoint to replay the way a single review does. Original-coordinate
    # findings; the same serialisation the checkpoint round-trips.
    snapshot = {"version": 1,
                "findings": [finding_to_dict(f) for f in findings],
                "usage": dataclasses.asdict(usage)}
    (base_path.parent / "composed.json").write_text(
        json.dumps(snapshot), encoding="utf-8")
    log.info("Multi-round review: %d round(s), %d change(s), %d quer(y/ies), "
             "%d rejected", result.rounds_run, len(result.edits),
             len(result.queries), len(result.rejected))
    # A fresh ledger for the finalizing finish(): its whole-book reads and judge
    # gates run once here, and a pass that falls open (a dead key) must reach the
    # job card from the multi-round path too, not only the single-review one.
    coverage = CoverageLedger()
    return finish(prepared_final, findings, usage, cfg,
                  out_dir=out_dir, source_path=source_path, on_phase=on_phase,
                  coverage=coverage)
