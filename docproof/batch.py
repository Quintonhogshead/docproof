"""Batch review: submit now, collect later, at half price.

A review is (passes x chunks) independent requests, which is exactly the shape
batch APIs want. Both vendors discount batch work 50% at any hour of the day —
the "overnight" part is a workflow convenience, not a cheaper rate.

Everything needed to finish a job lives in a manifest on disk, so collection
can happen in a different process days after submission.
"""
from __future__ import annotations

import itertools
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import Config, cache_dir_for
from .models import CoverageLedger, Usage
from .pipeline import (Outputs, Prepared, analyze_with_retry, build_analyzers,
                       continuity_findings, finish, prepare)
from .providers import BatchRequest, BatchStatus, Provider
from .utils.files import write_atomic

log = logging.getLogger("docproof.batch")

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Job states. `submitted` and `in_progress` are both "waiting on the vendor";
# they differ only in whether we have seen a poll come back yet.
ACTIVE = ("submitted", "in_progress")


class BatchError(RuntimeError):
    """A job cannot proceed: the source changed, the manifest is unreadable,
    or the vendor gave up on the batch."""


@dataclass
class Job:
    job_id: str
    source_path: str
    source_name: str
    content_hash: str
    model: str
    provider: str
    # The detector/review batch. "" when a run has no detector requests at all
    # (a continuity-only run), whose whole batch is the continuity one below.
    batch_id: str
    state: str = "submitted"
    # The chapter-continuity propose batch, on its own model — the reads are N
    # independent requests, the textbook batch candidate. None when the pass is
    # off, and None on every manifest written before this field existed, which
    # `load()` restores as None: an in-flight job simply has no second batch.
    continuity_batch_id: str | None = None
    request_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None
    config: dict = field(default_factory=dict)
    # The exact chunk ids this batch was built from. Collection re-derives the
    # chunk list from the file on disk, so pinning the ids here is what keeps
    # a partial review (a picked subset, or --max-chunks) from silently
    # becoming a whole-document review hours later. None means "all of them",
    # which is what every manifest written before this field existed meant.
    selection: list[str] | None = None
    # The system prompts as actually sent, per pass. Prompts are editable, so
    # without this a collected review could not say what it had asked for.
    prompts: dict[str, str] = field(default_factory=dict)
    manifest_version: int = MANIFEST_VERSION

    @property
    def active(self) -> bool:
        return self.state in ACTIVE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id(name: str) -> str:
    """A readable, sortable, unique id for one review.

    The timestamp is only accurate to the second, and a job id is a directory
    name — so submitting one document twice in the same second used to
    produce two jobs that shared a folder, and the second silently replaced
    the first. The random tail is what makes it a distinct review rather than
    an overwrite."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "-" for c in Path(name).stem)[:40]
    return f"{stamp}-{safe.strip('-') or 'document'}-{secrets.token_hex(2)}"


def job_dir(workspace: str | Path, job_id: str) -> Path:
    return Path(workspace) / job_id


def custom_id(pass_index: int, chunk_id: str) -> str:
    return f"p{pass_index}-{chunk_id}"


def parse_custom_id(value: str) -> tuple[int, str]:
    head, _, chunk_id = value.partition("-")
    return int(head[1:]), chunk_id


# The rewrite pass rides the same batch as the detectors when its model matches
# the review model (the default — rewrite.model unset). A distinct id namespace
# keeps its per-chunk requests from colliding with the detector passes'.
REWRITE_PREFIX = "rw-"


def custom_id_rewrite(chunk_id: str, sample: int = 0) -> str:
    return f"{REWRITE_PREFIX}{sample}-{chunk_id}"


def _write_rewrite_rejects(out_dir: str | Path, rejects: list) -> None:
    """Persist the candidates confirm kept-as-not-an-error, for a compare to
    overlay on a human proofread. Diagnostic only — never part of the review."""
    try:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "rewrite_rejects.json").write_text(
            json.dumps({"rejects": rejects}, indent=1), encoding="utf-8")
        log.info("Wrote %d rewrite reject(s) to rewrite_rejects.json", len(rejects))
    except OSError as e:
        log.warning("could not write rewrite_rejects.json: %s", e)


def rewrite_batched(cfg: Config) -> bool:
    """Whether the rewrite propose calls can ride the batch. They can only when
    the pass is on and its model is the batch's model (a batch is one model);
    a different rewrite.model falls back to synchronous propose at collect."""
    return cfg.rewrite.enabled and (cfg.rewrite.model is None
                                    or cfg.rewrite.model == cfg.api.model)


# --- the chapter-continuity propose batch -------------------------------------
#
# Unlike the rewrite retype, the chapter reads do NOT ride the review batch: a
# continuity-only run has no review batch to ride, and the reader is usually a
# different (stronger) model than the detector — and a batch is one model. So
# they go out as their OWN batch, on the chapter model, polled and collected
# alongside the review one. The whole-book read stays synchronous at collect: it
# retries once at a doubled ceiling on truncation, and a call already in a batch
# cannot be retried.

def _cc_model(cfg: Config) -> str:
    """The chapter reader's model: its own, else the whole-book read's, else the
    review model — the same resolution the pipeline uses at run time."""
    cc = cfg.chapter_continuity
    return cc.model or cfg.continuity.model or cfg.api.model


def _provider_for(cfg: Config, model: str, effort: str | None) -> Provider:
    """A provider bound to `model` at `effort` (its vendor is chosen from the id),
    built off a copy of `cfg` so nothing else in the run is disturbed. Effort is
    explicit because it is baked into every batch request body at construction —
    the whole reason the sync path sets `pcfg.api.effort = cc.effort` before
    building its provider."""
    from .providers import build_provider
    c = cfg.model_copy(deep=True)
    c.api.model = model
    c.api.effort = effort
    return build_provider(c)


def _cc_provider(cfg: Config, provider: Provider) -> Provider:
    """The provider the chapter batch uses. Reuse the review `provider` ONLY when
    both its model AND its effort already match the chapter reader's — otherwise
    build one carrying the chapter model and effort, since effort is stamped into
    the request bodies and a mismatch would read the whole book at the wrong
    reasoning depth (the batch's divergence from the sync path)."""
    cc = cfg.chapter_continuity
    cc_model = _cc_model(cfg)
    if cc_model == cfg.api.model and cc.effort == cfg.api.effort:
        return provider
    return _provider_for(cfg, cc_model, cc.effort)


def continuity_batch_requests(cfg: Config, prepared: Prepared) -> list[BatchRequest]:
    """One read request per chapter unit, or [] when the pass is off. Empty-text
    units are skipped (nothing to read); their absence is recognised at collect."""
    cc = cfg.chapter_continuity
    if not (cc.enabled and prepared.whole_document):
        return []
    from .continuity import (chapter_read_custom_id, chapter_read_schema,
                             chapter_read_user, chapters, resolve_chapter_system)
    units = chapters(prepared.doc.paragraphs, cfg.skip.is_sweep_only,
                     min_tokens=cc.min_chapter_tokens,
                     max_tokens=cc.max_chapter_tokens)
    system = resolve_chapter_system(cc.prompt)
    schema = chapter_read_schema()
    reqs = []
    for u in units:
        user = chapter_read_user(u)
        if not user.strip():
            continue
        reqs.append(BatchRequest(custom_id=chapter_read_custom_id(u.index),
                                 system=system, user=user,
                                 schema=schema, schema_name="scene_breaks"))
    return reqs


def _combine_status(statuses: list[BatchStatus]) -> BatchStatus:
    """One status over the job's batches for the progress bar: failed if any
    failed, completed only when all completed, else in progress. Counts sum so a
    two-batch job's bar is honest. (Whether a failure fails the JOB is decided in
    `poll`, which distinguishes the primary batch from the optional chapter one —
    this is only the display roll-up.)"""
    total = sum(s.total for s in statuses)
    succeeded = sum(s.succeeded for s in statuses)
    errored = sum(s.errored for s in statuses)
    if any(s.state in ("failed", "expired", "cancelled") for s in statuses):
        state = "failed"
    elif all(s.state == "completed" for s in statuses):
        state = "completed"
    else:
        state = "in_progress"
    return BatchStatus(state=state, total=total, succeeded=succeeded,
                       errored=errored)


# --- persistence --------------------------------------------------------------

def save(job: Job, workspace: str | Path) -> Path:
    d = job_dir(workspace, job.job_id)
    d.mkdir(parents=True, exist_ok=True)
    job.updated_at = _now()
    path = d / MANIFEST_NAME
    # Atomic because the app reads this manifest from other threads (the
    # ticker loads it every poll) with no lock shared with the writer.
    write_atomic(path, json.dumps(asdict(job), indent=2))
    return path


def load(workspace: str | Path, job_id: str) -> Job:
    path = job_dir(workspace, job_id) / MANIFEST_NAME
    if not path.is_file():
        raise BatchError(f"No job {job_id!r} in {workspace}")
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.pop("manifest_version", 0)
    if version != MANIFEST_VERSION:
        raise BatchError(
            f"Job {job_id!r} was written by a different version of docproof "
            f"(manifest v{version}, this build expects v{MANIFEST_VERSION}).")
    known = {f for f in Job.__dataclass_fields__ if f != "manifest_version"}
    return Job(**{k: v for k, v in data.items() if k in known})


def load_all(workspace: str | Path) -> list[Job]:
    """Every readable job, newest first. Unreadable manifests are skipped with
    a warning rather than taking down the whole listing."""
    jobs = []
    for d in sorted(Path(workspace).glob("*")):
        if not (d / MANIFEST_NAME).is_file():
            continue
        try:
            jobs.append(load(workspace, d.name))
        except (BatchError, json.JSONDecodeError, TypeError) as e:
            log.warning("Skipping unreadable job %s: %s", d.name, e)
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


# --- lifecycle ----------------------------------------------------------------

def submit(cfg: Config, input_path: str | Path, error_dir: str | Path,
           provider: Provider, workspace: str | Path, *,
           max_chunks: int | None = None,
           selection: Sequence[str] | None = None) -> Job:
    prepared = prepare(cfg, input_path, error_dir, max_chunks=max_chunks,
                       selection=selection)
    requests = build_requests(cfg, prepared)
    cc_requests = continuity_batch_requests(cfg, prepared)
    if not requests and not cc_requests:
        raise BatchError(
            f"{Path(input_path).name} has no reviewable paragraphs.")

    # Build the chapter provider BEFORE anything is billed, so a construction
    # failure (a missing key for a different-vendor chapter model) fails fast with
    # no orphaned paid batch. Building a provider does not bill; submit_batch does.
    cc_provider = _cc_provider(cfg, provider) if cc_requests else None
    # The review/detector batch. Empty for a continuity-only run, whose whole
    # batch is the chapter one below — so "" is a real value here, not a bug.
    batch_id = ""
    if requests:
        batch_id = provider.submit_batch(model=cfg.api.model, requests=requests,
                                         max_tokens=cfg.api.max_output_tokens)
    # The chapter-continuity batch on its own model. Submitted second, so a
    # failure here leaves at most the review batch orphaned — the same one-batch
    # exposure submit has always had, not a new two-batch one.
    continuity_batch_id = None
    if cc_provider is not None:
        continuity_batch_id = cc_provider.submit_batch(
            model=_cc_model(cfg), requests=cc_requests,
            max_tokens=cfg.chapter_continuity.max_output_tokens)
    job = Job(
        job_id=new_job_id(Path(input_path).name),
        source_path=str(Path(input_path).resolve()),
        source_name=Path(input_path).name,
        content_hash=prepared.content_hash,
        model=cfg.api.model,
        provider=provider.name,
        batch_id=batch_id,
        continuity_batch_id=continuity_batch_id,
        request_count=len(requests) + len(cc_requests),
        created_at=_now(),
        config=cfg.model_dump(mode="json"),
        # Resolved rather than echoed: whatever narrowed the run, the manifest
        # records every chunk id that actually went out — across every pass's
        # chunk set (a custom-budget category has its own ids) plus the default
        # set the rewrite pass rides — so collect re-derives and pins exactly the
        # same partial review. Ids are budget-namespaced, so the union is
        # unambiguous.
        selection=sorted(
            {c.chunk_id for c in prepared.chunks}
            | {c.chunk_id for p in prepared.effective_pass_plan
               for c in p.chunks}),
        prompts=pass_prompts(cfg, prepared),
    )
    save(job, workspace)
    log.info("Job %s submitted: %d review + %d chapter request(s) "
             "(batches %r / %r)", job.job_id, len(requests), len(cc_requests),
             batch_id, continuity_batch_id)
    return job


def pass_prompts(cfg: Config, prepared: Prepared) -> dict[str, str]:
    """The system prompt each pass will send, keyed by the pass label."""
    analyzers = build_analyzers(cfg, prepared.groups, None,
                                itertools.count(1), prepared.vocabulary,
                                prepared.conventions, prepared.story_sheet)
    return {a.label: a.system_prompt for a in analyzers}


def build_requests(cfg: Config, prepared: Prepared) -> list[BatchRequest]:
    """One request per (pass, chunk). All passes share a single batch — each
    request carries its own system prompt, so nothing forces them apart. The
    rewrite pass adds one request per chunk when it can ride the batch, so its
    output-heavy calls get the batch discount instead of running (and hitting
    the rate limit) synchronously at collect."""
    from .analyzer import render_chunk

    analyzers = build_analyzers(cfg, prepared.pass_types, None,
                                itertools.count(1), prepared.vocabulary,
                                prepared.conventions, prepared.story_sheet)
    # One request per (pass, chunk), each pass over ITS chunk set (a repeated or
    # custom-budget category differs from the default). analyzers[prun.index]
    # lines up because build_analyzers consumed the same plan order.
    requests = [
        BatchRequest(custom_id=custom_id(prun.index, chunk.chunk_id),
                     system=analyzers[prun.index].system_prompt,
                     user=render_chunk(chunk),
                     schema=analyzers[prun.index].schema,
                     schema_name=analyzers[prun.index].schema_name)
        for prun in prepared.effective_pass_plan
        for chunk in prun.chunks
    ]
    if rewrite_batched(cfg) and prepared.whole_document:
        from .rewrite import PROPOSE_SYSTEM, lens_system, render, rewrite_schema
        schema = rewrite_schema()
        requests += [
            BatchRequest(
                custom_id=custom_id_rewrite(chunk.chunk_id, s),
                system=lens_system(s) if cfg.rewrite.diverse else PROPOSE_SYSTEM,
                user=render(chunk.paragraphs),
                schema=schema, schema_name="rewritten")
            for s in range(cfg.rewrite.samples)
            for chunk in prepared.chunks if chunk.paragraphs
        ]
    return requests


def poll(job: Job, provider: Provider, workspace: str | Path) -> BatchStatus:
    """Poll every batch this job holds — the review batch and, when present, the
    chapter-continuity batch (on its own model, so its own provider) — and move
    to `ready` only when ALL of them have completed. A job with only a continuity
    batch (continuity-only) polls just that one."""
    review = provider.poll_batch(job.batch_id) if job.batch_id else None
    chapter = None
    if job.continuity_batch_id:
        cfg = Config.model_validate(job.config)
        chapter = _cc_provider(cfg, provider).poll_batch(job.continuity_batch_id)
    present = [s for s in (review, chapter) if s is not None]
    status = _combine_status(present) if present else BatchStatus(state="completed")
    fail = ("failed", "expired", "cancelled")
    # The PRIMARY batch — the review one, or the chapter one for a continuity-only
    # run — is what the whole run turns on. The job fails only when IT failed. A
    # secondary chapter-batch failure does NOT discard a completed review: the
    # pass is additive and best-effort, so the review proceeds and the failure is
    # reported at collect as chapter reads that failed.
    primary = review if review is not None else chapter
    if primary is not None and primary.state in fail:
        job.state = "failed"
        job.error = f"The provider reported the primary batch as {primary.state}."
    elif (review is not None and review.state == "completed"
          and chapter is not None and chapter.state in fail):
        job.state = "ready"
        log.warning("Job %s: the chapter-continuity batch %s; the review "
                    "proceeds without those queries.", job.job_id, chapter.state)
    elif all(s.state == "completed" for s in present):
        job.state = "ready"
    else:
        job.state = "in_progress"
    save(job, workspace)
    return status


@dataclass(frozen=True)
class CollectResult:
    """A batch's raw findings plus everything needed to finish them: the config
    and prepared document the requests were built from, the accumulated usage and
    coverage, the source path, and any rewrite rejects to log."""
    cfg: Config
    prepared: Prepared
    findings: list
    usage: Usage
    coverage: CoverageLedger
    source: Path
    rewrite_rejects: list | None
    # The chapter-continuity batch's raw reads (custom_id -> ProviderResult), or
    # None when the pass had no batch. finish() parses these into queries; the
    # judge over them still runs synchronously there.
    chapter_reads: dict | None = None


def collect_findings(job: Job, provider: Provider,
                     error_dir: str | Path, *,
                     on_phase=None) -> CollectResult:
    """Fetch a batch's results and run every synchronous post-step, returning
    the RAW findings before the document is assembled — the point a caller can
    act on them. `collect` wraps this with finish; the multi-round driver runs
    the judge over these findings and folds them, finishing once at the end.

    The source document is re-ingested here: the walker is deterministic, so
    re-reading an unchanged file reproduces the exact paragraph list the
    requests were built from. The hash check enforces "unchanged".

    `on_phase` is `run_sync`'s callback, spoken here with the same stage ids:
    collect re-walks the same steps the synchronous path does — re-ingest
    ("preparing"), folding the batch's answers ("reviewing"), then each
    whole-book post-step — and without it the whole stretch reads as one
    opaque "almost done" however many paid passes it still has to make."""
    cfg = Config.model_validate(job.config)
    source = Path(job.source_path)
    if not source.is_file():
        raise BatchError(
            f"{job.source_name} is no longer at {source}. Put the file back, "
            f"or start a new review.")

    if on_phase:
        on_phase("preparing")
    prepared = prepare(cfg, source, error_dir, selection=job.selection)
    if prepared.content_hash != job.content_hash:
        raise BatchError(
            f"{job.source_name} has been edited since this review was "
            f"submitted, so the corrections no longer line up with the text. "
            f"Start a new review of the current version.")

    if on_phase:
        on_phase("reviewing")
    results = provider.collect_batch(job.batch_id) if job.batch_id else {}
    coverage = CoverageLedger()
    findings, usage = _assemble(cfg, prepared, results, provider, coverage)
    rewrite_rejects = None
    # The chapter-continuity reads, fetched from their own batch on their own
    # model. Parsed and judged in finish() (the judge is synchronous either way),
    # so here we only hand the raw reads forward. None when the pass had no batch.
    chapter_reads = None
    if job.continuity_batch_id:
        try:
            chapter_reads = _cc_provider(cfg, provider).collect_batch(
                job.continuity_batch_id)
        except Exception as e:
            # The chapter batch failed to complete (poll let the review through
            # anyway). An empty dict — NOT None — makes finish take the batch
            # path and report every chapter as unread, rather than silently
            # re-buying the reads live.
            log.error("chapter-continuity: could not collect batch %s (%s); "
                      "reporting its reads as failed.", job.continuity_batch_id, e)
            chapter_reads = {}

    # The glossary and adjudication passes are synchronous post-steps: the
    # detector work rode the batch, but the whole-book glossary read is one call
    # and ruling on the typo candidates is a few more, so they run here at
    # collect time rather than needing their own batch stage. Without this, a
    # batch review (the production path) would silently skip them.
    ids = itertools.count(1)
    findings = list(findings)
    # How many candidates each batched pass actually got a verdict for; a window
    # whose answer hit the token ceiling carries none. See docproof/windowing.py.
    window_losses: list = []
    glossary_cands: list = []
    if cfg.glossary.enabled and prepared.whole_document:
        if on_phase:
            on_phase("glossary")
        from .glossary import (build_glossary, case_drift_findings,
                               suspects_to_candidates)
        from .providers import build_provider
        gcfg = cfg.model_copy(deep=True)
        gcfg.api.model = cfg.glossary.model
        gcfg.api.effort = cfg.glossary.effort
        glossary = build_glossary(
            prepared.doc.paragraphs, build_provider(gcfg),
            model=cfg.glossary.model,
            max_tokens=cfg.glossary.max_output_tokens, usage=usage,
            effort=cfg.glossary.effort,
            cache_dir=cache_dir_for(cfg.glossary.cache_dir),
            on_degraded=lambda reason: coverage.record_degraded(
                "glossary read", reason))
        glossary_cands = suspects_to_candidates(glossary, prepared.doc.paragraphs)
        if cfg.glossary.case_drift:
            findings += case_drift_findings(
                glossary, prepared.doc.paragraphs, ids,
                scan=cfg.glossary.case_drift_scan,
                edit_dominance=cfg.glossary.case_edit_dominance,
                edit_min_count=cfg.glossary.case_edit_min_count)

    # The external-world read rides collect the same way the glossary does:
    # one synchronous call, queries only, cached per draft.
    if cfg.factcheck.enabled and prepared.whole_document:
        if on_phase:
            on_phase("factcheck")
        from .factcheck import factcheck_findings
        from .providers import build_provider
        findings += factcheck_findings(
            cfg, prepared.doc.paragraphs, usage, build_provider,
            coverage=coverage)

    if cfg.adjudicate.enabled and (prepared.adjudicate_candidates or glossary_cands):
        if on_phase:
            on_phase("adjudicate")
        from .adjudicate import adjudicate, merge_candidates
        cands = merge_candidates(prepared.adjudicate_candidates, glossary_cands)
        findings += adjudicate(
            cands, prepared.doc.paragraphs, provider,
            model=cfg.api.model, max_tokens=cfg.api.max_output_tokens,
            usage=usage, ids=ids,
            batch_size=cfg.adjudicate.batch_size,
            edit_confidence=cfg.adjudicate.edit_confidence,
            loss_sink=window_losses,
            concurrency=cfg.concurrency_for())

    # Rewrite-then-diff. When it can ride the batch (the default: rewrite.model
    # matches the review model), its output-heavy propose calls already rode the
    # batch above — here we just diff those results into candidates. Otherwise
    # (a different rewrite.model) they run synchronously now. Either way the
    # light confirm pass runs here.
    if cfg.rewrite.enabled and prepared.whole_document:
        if on_phase:
            on_phase("rewrite")
        from .providers import build_provider
        from .rewrite import (candidates_from_result, confirm,
                              dedup_candidates, propose)
        rcfg = cfg.model_copy(deep=True)
        rcfg.api.model = cfg.rewrite.model or cfg.api.model
        rcfg.api.effort = cfg.rewrite.effort
        rw_provider = build_provider(rcfg)
        if rewrite_batched(cfg):
            from .windowing import WindowReport, log_report
            rcands = []
            # The retypes were bought hours ago, so a truncated one cannot be
            # split and re-asked the way the synchronous path does — but it must
            # still be counted. Left uncounted, a paragraph whose retype hit the
            # ceiling produces no candidates, which is exactly what a paragraph
            # with nothing wrong in it produces.
            retype = WindowReport(label="rewrite retype (batch)")
            for chunk in prepared.chunks:
                for s in range(cfg.rewrite.samples):
                    res = results.get(custom_id_rewrite(chunk.chunk_id, s))
                    if res is None:
                        retype.asked += len(chunk.paragraphs)
                        continue
                    usage.add(res.usage, model=cfg.api.model)
                    if res.stop_reason != "ok" or res.parsed is None:
                        retype.asked += len(chunk.paragraphs)
                        if res.stop_reason == "max_tokens":
                            retype.truncated_calls += 1
                        log.warning("rewrite: chunk %s sample %d returned %s; "
                                    "%d paragraph(s) were not retyped.",
                                    chunk.chunk_id, s,
                                    res.error or res.stop_reason,
                                    len(chunk.paragraphs))
                        continue
                    rcands += candidates_from_result(
                        chunk, res.parsed, max_add=cfg.rewrite.max_added,
                        max_span=cfg.rewrite.max_span, report=retype)
            log_report(retype)
            window_losses.append(retype)
            rcands = dedup_candidates(rcands)
            log.info("Rewrite: %d candidate diff(s) from the batch "
                     "(%d sample(s))%s", len(rcands), cfg.rewrite.samples,
                     f" — {retype.lost} paragraph retype(s) LOST"
                     if retype.lost else "")
        else:
            rcands = propose(
                prepared.chunks, rw_provider, model=rcfg.api.model,
                max_tokens=cfg.rewrite.max_output_tokens, usage=usage,
                max_add=cfg.rewrite.max_added, max_span=cfg.rewrite.max_span,
                workers=cfg.rewrite.workers, samples=cfg.rewrite.samples,
                loss_sink=window_losses)
        confirm_provider, confirm_model = rw_provider, rcfg.api.model
        if cfg.rewrite.confirm_model:
            ccfg = cfg.model_copy(deep=True)
            ccfg.api.model = cfg.rewrite.confirm_model
            ccfg.api.effort = cfg.rewrite.confirm_effort
            confirm_provider, confirm_model = build_provider(ccfg), cfg.rewrite.confirm_model
        rewrite_rejects = [] if cfg.rewrite.log_rejects else None
        findings += confirm(
            rcands, prepared.doc.paragraphs, confirm_provider, model=confirm_model,
            max_tokens=cfg.rewrite.max_output_tokens, usage=usage, ids=ids,
            batch_size=cfg.rewrite.batch_size,
            edit_confidence=cfg.rewrite.edit_confidence,
            reject_sink=rewrite_rejects, loss_sink=window_losses,
            concurrency=cfg.concurrency_for(confirm_model))

    # LanguageTool mechanical-floor pass: local rules checker proposes, the shared
    # confirm valve rules. Propose is local (no batch requests), so it runs here
    # at collect either way; only the light confirm calls hit the API.
    if cfg.languagetool.enabled and prepared.whole_document:
        if on_phase:
            on_phase("languagetool")
        from .languagetool import (all_disabled_rules, propose as lt_propose,
                                   shutdown as lt_shutdown)
        from .providers import build_provider
        from .rewrite import confirm as lt_confirm
        try:
            lt_cands = lt_propose(
                prepared.doc.paragraphs, lexicon=prepared.spell.lexicon,
                dictionary=cfg.languagetool.dictionary,
                disabled_rules=all_disabled_rules(cfg.languagetool.disabled_rules),
                workers=cfg.languagetool.workers,
                scan_chars=cfg.languagetool.scan_chars,
                coverage=coverage)
            lt_provider, lt_model = provider, cfg.api.model
            if cfg.languagetool.confirm_model:
                lcfg = cfg.model_copy(deep=True)
                lcfg.api.model = cfg.languagetool.confirm_model
                lcfg.api.effort = cfg.languagetool.confirm_effort
                lt_provider, lt_model = build_provider(lcfg), cfg.languagetool.confirm_model
            findings += lt_confirm(
                lt_cands, prepared.doc.paragraphs, lt_provider, model=lt_model,
                max_tokens=cfg.languagetool.max_output_tokens, usage=usage, ids=ids,
                batch_size=cfg.languagetool.batch_size,
                edit_confidence=cfg.languagetool.edit_confidence,
                error_type="languagetool", chunk_id="languagetool", id_prefix="lt",
                loss_sink=window_losses,
                concurrency=cfg.concurrency_for(lt_model))
        finally:
            lt_shutdown()

    # Whole-book continuity read: like the glossary above it is one synchronous
    # whole-book call on its own model, so it cannot ride the batch — it runs
    # here at collect. Without this the app's default submission mode would do no
    # continuity reads at all while summary.md (and the pre-run estimate) both
    # said it had. Same helper the synchronous path uses, so they stay in step.
    from .providers import build_provider
    findings += continuity_findings(cfg, prepared, ids, usage, build_provider,
                                    on_phase=on_phase, coverage=coverage)

    coverage.record_windows(window_losses)

    return CollectResult(cfg, prepared, findings, usage, coverage, source,
                         rewrite_rejects, chapter_reads=chapter_reads)


def collect(job: Job, provider: Provider, error_dir: str | Path,
            workspace: str | Path, *,
            out_dir: str | Path | None = None, on_phase=None) -> Outputs:
    """Fetch a batch's results and assemble the reviewed document."""
    r = collect_findings(job, provider, error_dir, on_phase=on_phase)
    out = Path(out_dir) if out_dir else job_dir(workspace, job.job_id) / "results"
    if r.cfg.rewrite.enabled and r.prepared.whole_document and r.rewrite_rejects:
        _write_rewrite_rejects(out, r.rewrite_rejects)
    outputs = finish(r.prepared, r.findings, r.usage, r.cfg, out_dir=out,
                     source_path=r.source, batch=True, coverage=r.coverage,
                     chapter_batch_reads=r.chapter_reads, on_phase=on_phase)
    job.state = "done"
    save(job, workspace)
    log.info("Job %s collected: %d change(s) applied", job.job_id,
             outputs.applied)
    return outputs


def _assemble(cfg: Config, prepared: Prepared, results: dict,
              provider: Provider | None = None,
              coverage: CoverageLedger | None = None) -> tuple[list, Usage]:
    """Map batch results back onto (pass, chunk) and run them through the same
    finding checks the synchronous path uses.

    A request the batch never returned — or returned as a refusal or a
    truncation — used to be dropped with only a warning, silently under-
    reporting those paragraphs. When a `provider` is given, each such gap is
    recovered with one synchronous call now (the batch discount is lost on the
    few that failed, but a dropped section is worse than a small bill); whatever
    still fails is recorded in `coverage` so the report names it."""
    ids = itertools.count(1)
    usage = Usage()
    analyzers = build_analyzers(cfg, prepared.pass_types, provider, ids,
                                prepared.vocabulary, prepared.conventions,
                                prepared.story_sheet)
    plan = prepared.effective_pass_plan
    findings: list = []
    recovered = 0
    unrecovered = 0

    # Ordered by pass then chunk so finding IDs come out in the same order a
    # synchronous run would produce — including any recovered inline, which
    # keeps the validator's span-claim precedence between passes intact. Each
    # pass reads its own chunk set (custom-budget or repeated categories differ
    # from the default), so iterate the plan, not one shared chunk list.
    for prun in plan:
        analyzer = analyzers[prun.index]
        for chunk in prun.chunks:
            result = results.get(custom_id(prun.index, chunk.chunk_id))
            found: list = []
            ok = False
            if result is not None:
                found, ok = analyzer.process_result(result, chunk, usage)
            if not ok and provider is not None:
                found, ok = analyze_with_retry(analyzer, chunk, usage)
                if ok:
                    recovered += 1
            if not ok:
                unrecovered += 1
            findings.extend(found)
            if coverage is not None:
                coverage.record(analyzer.label, chunk, ok)

    # Rewrite requests (rw-*) ride this batch but are assembled separately, in
    # collect(); they are not "unknown", so keep them out of the warning.
    unknown = {r for r in set(results) - {custom_id(prun.index, c.chunk_id)
                                          for prun in plan
                                          for c in prun.chunks}
               if not r.startswith(REWRITE_PREFIX)}
    if recovered:
        log.info("%d batch request(s) had no usable result and were recovered "
                 "with a synchronous call.", recovered)
    if unrecovered:
        log.warning("%d request(s) could not be reviewed even after recovery; "
                    "those sections are reported as unreviewed.", unrecovered)
    if unknown:
        log.warning("Batch returned %d result(s) for unknown requests: %s",
                    len(unknown), sorted(unknown)[:5])
        if coverage is not None:
            coverage.note("batch", f"{len(unknown)} batch result(s) came back "
                          f"for requests that map to no known (pass, section) "
                          f"and were discarded — their verdicts were paid for "
                          f"but not applied", "partial")
    return findings, usage
