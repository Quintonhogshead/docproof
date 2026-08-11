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

from .config import Config
from .models import CoverageLedger, Usage
from .pipeline import (Outputs, Prepared, analyze_with_retry, build_analyzers,
                       finish, prepare)
from .providers import BatchRequest, BatchStatus, Provider

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
    batch_id: str
    state: str = "submitted"
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


def rewrite_batched(cfg: Config) -> bool:
    """Whether the rewrite propose calls can ride the batch. They can only when
    the pass is on and its model is the batch's model (a batch is one model);
    a different rewrite.model falls back to synchronous propose at collect."""
    return cfg.rewrite.enabled and (cfg.rewrite.model is None
                                    or cfg.rewrite.model == cfg.api.model)


# --- persistence --------------------------------------------------------------

def save(job: Job, workspace: str | Path) -> Path:
    d = job_dir(workspace, job.job_id)
    d.mkdir(parents=True, exist_ok=True)
    job.updated_at = _now()
    path = d / MANIFEST_NAME
    path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")
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
    if not requests:
        raise BatchError(
            f"{Path(input_path).name} has no reviewable paragraphs.")

    batch_id = provider.submit_batch(model=cfg.api.model, requests=requests,
                                     max_tokens=cfg.api.max_output_tokens)
    job = Job(
        job_id=new_job_id(Path(input_path).name),
        source_path=str(Path(input_path).resolve()),
        source_name=Path(input_path).name,
        content_hash=prepared.content_hash,
        model=cfg.api.model,
        provider=provider.name,
        batch_id=batch_id,
        request_count=len(requests),
        created_at=_now(),
        config=cfg.model_dump(mode="json"),
        # Resolved rather than echoed: whatever narrowed the run, the manifest
        # records the chunk ids that actually went out.
        selection=[c.chunk_id for c in prepared.chunks],
        prompts=pass_prompts(cfg, prepared),
    )
    save(job, workspace)
    log.info("Job %s submitted: %d request(s) as batch %s",
             job.job_id, len(requests), batch_id)
    return job


def pass_prompts(cfg: Config, prepared: Prepared) -> dict[str, str]:
    """The system prompt each pass will send, keyed by the pass label."""
    analyzers = build_analyzers(cfg, prepared.groups, None,
                                itertools.count(1), prepared.vocabulary,
                                prepared.conventions)
    return {a.label: a.system_prompt for a in analyzers}


def build_requests(cfg: Config, prepared: Prepared) -> list[BatchRequest]:
    """One request per (pass, chunk). All passes share a single batch — each
    request carries its own system prompt, so nothing forces them apart. The
    rewrite pass adds one request per chunk when it can ride the batch, so its
    output-heavy calls get the batch discount instead of running (and hitting
    the rate limit) synchronously at collect."""
    from .analyzer import render_chunk

    analyzers = build_analyzers(cfg, prepared.groups, None,
                                itertools.count(1), prepared.vocabulary,
                                prepared.conventions)
    requests = [
        BatchRequest(custom_id=custom_id(i, chunk.chunk_id),
                     system=analyzer.system_prompt,
                     user=render_chunk(chunk),
                     schema=analyzer.schema,
                     schema_name=analyzer.schema_name)
        for i, analyzer in enumerate(analyzers)
        for chunk in prepared.chunks
    ]
    if rewrite_batched(cfg) and prepared.whole_document:
        from .rewrite import PROPOSE_SYSTEM, render, rewrite_schema
        schema = rewrite_schema()
        requests += [
            BatchRequest(custom_id=custom_id_rewrite(chunk.chunk_id, s),
                         system=PROPOSE_SYSTEM, user=render(chunk.paragraphs),
                         schema=schema, schema_name="rewritten")
            for s in range(cfg.rewrite.samples)
            for chunk in prepared.chunks if chunk.paragraphs
        ]
    return requests


def poll(job: Job, provider: Provider, workspace: str | Path) -> BatchStatus:
    status = provider.poll_batch(job.batch_id)
    if status.state == "completed":
        job.state = "ready"
    elif status.state in ("failed", "expired", "cancelled"):
        job.state = "failed"
        job.error = f"The provider reported this batch as {status.state}."
    else:
        job.state = "in_progress"
    save(job, workspace)
    return status


def collect(job: Job, provider: Provider, error_dir: str | Path,
            workspace: str | Path, *,
            out_dir: str | Path | None = None) -> Outputs:
    """Fetch results and run the rest of the pipeline.

    The source document is re-ingested here: the walker is deterministic, so
    re-reading an unchanged file reproduces the exact paragraph list the
    requests were built from. The hash check enforces "unchanged"."""
    cfg = Config.model_validate(job.config)
    source = Path(job.source_path)
    if not source.is_file():
        raise BatchError(
            f"{job.source_name} is no longer at {source}. Put the file back, "
            f"or start a new review.")

    prepared = prepare(cfg, source, error_dir, selection=job.selection)
    if prepared.content_hash != job.content_hash:
        raise BatchError(
            f"{job.source_name} has been edited since this review was "
            f"submitted, so the corrections no longer line up with the text. "
            f"Start a new review of the current version.")

    results = provider.collect_batch(job.batch_id)
    coverage = CoverageLedger()
    findings, usage = _assemble(cfg, prepared, results, provider, coverage)

    # The glossary and adjudication passes are synchronous post-steps: the
    # detector work rode the batch, but the whole-book glossary read is one call
    # and ruling on the typo candidates is a few more, so they run here at
    # collect time rather than needing their own batch stage. Without this, a
    # batch review (the production path) would silently skip them.
    ids = itertools.count(1)
    findings = list(findings)
    glossary_cands: list = []
    if cfg.glossary.enabled and prepared.whole_document:
        from .glossary import (build_glossary, case_drift_findings,
                               suspects_to_candidates)
        from .providers import build_provider
        gcfg = cfg.model_copy(deep=True)
        gcfg.api.model = cfg.glossary.model
        gcfg.api.effort = cfg.glossary.effort
        glossary = build_glossary(
            prepared.doc.paragraphs, build_provider(gcfg),
            model=cfg.glossary.model,
            max_tokens=cfg.glossary.max_output_tokens, usage=usage)
        glossary_cands = suspects_to_candidates(glossary, prepared.doc.paragraphs)
        if cfg.glossary.case_drift:
            findings += case_drift_findings(
                glossary, prepared.doc.paragraphs, ids,
                scan=cfg.glossary.case_drift_scan,
                edit_dominance=cfg.glossary.case_edit_dominance,
                edit_min_count=cfg.glossary.case_edit_min_count)

    if cfg.adjudicate.enabled and (prepared.adjudicate_candidates or glossary_cands):
        from .adjudicate import adjudicate, merge_candidates
        cands = merge_candidates(prepared.adjudicate_candidates, glossary_cands)
        findings += adjudicate(
            cands, prepared.doc.paragraphs, provider,
            model=cfg.api.model, max_tokens=cfg.api.max_output_tokens,
            usage=usage, ids=ids,
            batch_size=cfg.adjudicate.batch_size,
            edit_confidence=cfg.adjudicate.edit_confidence)

    # Rewrite-then-diff. When it can ride the batch (the default: rewrite.model
    # matches the review model), its output-heavy propose calls already rode the
    # batch above — here we just diff those results into candidates. Otherwise
    # (a different rewrite.model) they run synchronously now. Either way the
    # light confirm pass runs here.
    if cfg.rewrite.enabled and prepared.whole_document:
        from .providers import build_provider
        from .rewrite import (candidates_from_result, confirm,
                              dedup_candidates, propose)
        rcfg = cfg.model_copy(deep=True)
        rcfg.api.model = cfg.rewrite.model or cfg.api.model
        rcfg.api.effort = cfg.rewrite.effort
        rw_provider = build_provider(rcfg)
        if rewrite_batched(cfg):
            rcands = []
            for chunk in prepared.chunks:
                for s in range(cfg.rewrite.samples):
                    res = results.get(custom_id_rewrite(chunk.chunk_id, s))
                    if res is None:
                        continue
                    usage.add(res.usage)
                    rcands += candidates_from_result(
                        chunk, res.parsed, max_add=cfg.rewrite.max_added,
                        max_span=cfg.rewrite.max_span)
            rcands = dedup_candidates(rcands)
            log.info("Rewrite: %d candidate diff(s) from the batch "
                     "(%d sample(s))", len(rcands), cfg.rewrite.samples)
        else:
            rcands = propose(
                prepared.chunks, rw_provider, model=rcfg.api.model,
                max_tokens=cfg.rewrite.max_output_tokens, usage=usage,
                max_add=cfg.rewrite.max_added, max_span=cfg.rewrite.max_span,
                workers=cfg.rewrite.workers, samples=cfg.rewrite.samples)
        findings += confirm(
            rcands, prepared.doc.paragraphs, rw_provider, model=rcfg.api.model,
            max_tokens=cfg.rewrite.max_output_tokens, usage=usage, ids=ids,
            batch_size=cfg.rewrite.batch_size,
            edit_confidence=cfg.rewrite.edit_confidence)

    out = Path(out_dir) if out_dir else job_dir(workspace, job.job_id) / "results"
    outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                     source_path=source, batch=True, coverage=coverage)
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
    analyzers = build_analyzers(cfg, prepared.groups, provider, ids,
                                prepared.vocabulary, prepared.conventions)
    findings: list = []
    recovered = 0
    unrecovered = 0

    # Ordered by pass then chunk so finding IDs come out in the same order a
    # synchronous run would produce — including any recovered inline, which
    # keeps the validator's span-claim precedence between passes intact.
    for i, analyzer in enumerate(analyzers):
        for chunk in prepared.chunks:
            result = results.get(custom_id(i, chunk.chunk_id))
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
    unknown = {r for r in set(results) - {custom_id(i, c.chunk_id)
                                          for i in range(len(analyzers))
                                          for c in prepared.chunks}
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
    return findings, usage
