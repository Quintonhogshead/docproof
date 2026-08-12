"""The job queue behind the app.

One record per document per run. "Run now" jobs go through a worker thread;
batch jobs are submitted and then picked up by a ticker that polls the vendor
and collects results when they land. No state lives only in memory — every
record is a file, so closing the app (or losing power) costs at most the
in-flight sync job.
"""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docproof import batch as batchlib
from docproof import prep as preplib
from docproof import promo as promolib
from docproof.batch import pass_prompts
from docproof.checkpoint import Checkpoint
from docproof.config import Config, load_config
from docproof.formats import get_format
from docproof.audit import AuditError
from docproof.ingest import IngestError
from docproof.pipeline import (JobCancelled, content_hash, finish, prepare,
                               run_sync)
from docproof.models import CoverageLedger
from docproof.prep.convert import ConversionError
from docproof.prep.styles import StyleSheetError
from docproof.prep.verify import VerificationFailed
from docproof.promo import PromoError, PromoTooLarge
from docproof.providers import ProviderError, build_provider, estimate_cost, \
    provider_for

from . import features
from .settings import Paths, Settings, get_api_key
from .spending import LedgerEntry, SpendingLedger
from .usage import _totals_for

log = logging.getLogger("docproof.app.jobs")

APP_MANIFEST = "app.json"
POLL_SECONDS = 120
# How many times the ticker will start collecting one batch before giving up and
# marking it failed. A collect that raises fails on the first try; this cap is
# for a collect that keeps killing the process (an OOM under the LanguageTool
# JVM, a machine restart) and so never reaches the except — without it the ticker
# retries forever and the card reads "almost done" the whole time. Three leaves
# room for a transient blip while still converging on a visible failure.
MAX_COLLECT_ATTEMPTS = 3

# State → what the user reads. Keep the vocabulary out of the vendor's world:
# no "batch", no "API", no "chunks".
PLAIN_STATE = {
    "scheduled": "Waiting until {when}",
    "queued": "Waiting to start",
    "running": "Reviewing ({done} of {total} sections)",
    "waiting": "Processing overnight — check back in the morning",
    "collecting": "Almost done — writing your document",
    "done": "Ready",
    "failed": "Needs attention",
    "cancelled": "Cancelled",
}

# The steps a running review moves through, in order. The per-chunk detector
# loop ("reviewing") is the one with a real count; the rest are whole-book passes
# that run after it, each a single stretch of work with no per-call progress —
# so the card names the step instead of leaving the bar frozen at 100%. Keyed by
# the stage ids the pipeline's on_phase callback emits, plus the two the app sets
# itself around the run (preparing, writing). See Job.plain_state.
STAGE_STATE = {
    "preparing": "Reading your manuscript",
    "reviewing": "Reviewing ({done} of {total} sections)",
    "glossary": "Building the glossary for the whole book",
    "adjudicate": "Checking for real-word typos",
    "rewrite": "Rewriting and comparing, line by line",
    "languagetool": "Running the mechanical check",
    "writing": "Almost done — writing your document",
}

# Prep does a different job, so it says so. Only the states that differ.
PREP_STATE = {
    "running": "Reading your manuscript ({done} of {total})",
    "collecting": "Almost done — writing your files",
}

# Promo, likewise: one call over the whole book, then two documents written.
PROMO_STATE = {
    "running": "Reading the book and writing your copy",
    "collecting": "Almost done — writing your files",
}

PREP_OUTPUTS = {"indesign": ["indesign"], "tracked": ["tracked"],
                "both": ["indesign", "tracked"]}


class _ReplayOnly:
    """A provider that refuses to call out. `download_anyway` replays a
    completed run from its checkpoint, so every call should already be cached;
    if one is not, this raises instead of quietly paying for a re-review."""
    name = "replay-only"

    def complete_structured(self, **kwargs):
        raise RuntimeError(
            "download-anyway expected every call to be in the checkpoint, but "
            "one was missing — refusing to re-run the review at cost.")


@dataclass
class Job:
    id: str
    filename: str
    source_path: str
    model: str
    mode: str                      # "now" | "batch"
    state: str = "queued"
    group_id: str = ""
    schedule_at: str | None = None      # "HH:MM" local, batch mode only
    done: int = 0
    total: int = 0
    # Which step a running review is on, so the results card reads the truth
    # while a whole-book pass (glossary, rewrite, …) runs after the per-chunk
    # loop — where done/total would otherwise sit frozen at 100%. Set by the
    # pipeline's on_phase callback; "" on older records and non-review jobs, and
    # cleared when the job finishes. See STAGE_STATE and _run_now.
    stage: str = ""
    error: str | None = None
    applied: int | None = None
    results_dir: str | None = None
    min_confidence: str = "medium"
    # Reasoning depth for the model. Older records predate this field; the
    # default keeps them on the shipped "low" behaviour.
    effort: str = "low"
    # Which model reads the whole book for the glossary pass, "off" to skip it,
    # or None on older records (config_for then leaves the config default).
    glossary_model: str | None = None
    # Per-run pass toggles: {feature_id: on}. Empty (older records, or a run that
    # touched nothing) means "use the config defaults". See app/features.py.
    features: dict[str, bool] = field(default_factory=dict)
    # Which sections the user picked, or None for the whole document.
    selection: list[str] | None = None
    created_at: str = ""
    updated_at: str = ""
    # What this job is: a grammar review, or manuscript prep for the house
    # InDesign template. Older records have no `kind` and are reviews.
    kind: str = "review"               # review | prep | promo
    prep_output: str = "indesign"      # indesign | tracked | both
    # Promo only: whether the generated copy still needs a human's sign-off
    # before it ships. "" on review and prep; on promo, "auto" ships with no
    # gate, "pending" waits in the panel for a person, "approved" once one has
    # okayed it. The generation itself doesn't read this — the delivery step does.
    approval: str = ""                 # "" | auto | pending | approved
    # Promo only: the human override for a book over the single-pass token limit.
    # False keeps the size guard; True was set by a person who saw the size at
    # drop time and chose to send the whole book in one call anyway. Older
    # records have no such field and were never overridden.
    allow_oversize: bool = False
    # Why a job failed, as a machine-readable tag when the reason needs handling
    # beyond the message string. Currently "oversize" for a promo book past the
    # single-pass limit, so the watcher can email a person about that case
    # specifically. "" on success and on ordinary failures.
    error_kind: str = ""
    # Which door this job came in by: somebody dropping a file on the window,
    # or the watcher finding one in a Drive folder. Two job stores adding up to
    # one bill, and a spending figure that cannot say which is a figure nobody
    # trusts. Older records have no `source` and were all the app.
    source: str = "app"                # app | watch
    # Whose review this is, in the web build. The id of the signed-in user who
    # created it; the desktop build has no users, so its records carry the
    # single local owner (or, from before this field existed, an empty string —
    # which the web build never writes and its ownership checks never match).
    owner_id: str = ""
    tagged: int | None = None          # paragraphs given a style
    flags: int | None = None           # things prep wants a human to decide
    verified: bool | None = None       # the author's words came through intact
    # Promo only: how many capitalised terms in the copy appear nowhere in the
    # manuscript — the grounding check's count, surfaced so a card can flag it.
    unverified: int | None = None
    words: int | None = None
    # What this job actually cost, recorded when it finishes so the dashboard
    # doesn't have to re-read every results folder to add it up.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    api_calls: int = 0
    cost: float | None = None
    # Set when a review failed the reject-all audit specifically (as opposed to
    # a provider or ingest error), so the results card can offer "download
    # anyway" only where it applies.
    audit_failed: bool = False
    # Set when the user chose "download anyway" on such a review: the file was
    # written with the audit downgraded to a warning, so it is clear the
    # integrity check did not pass.
    audit_overridden: bool = False
    # How many times we have started collecting this batch's results. A collect
    # that RAISES becomes a visible "failed"; this counts the other way it can
    # end — the process dying mid-collect (an OOM under the LanguageTool JVM, a
    # machine restart) — which leaves the job stuck in "collecting" for the
    # ticker to retry forever. Counted before each attempt so a crash still
    # counts, and capped at MAX_COLLECT_ATTEMPTS so a repeating crash becomes a
    # failure the user can see and recover, not "almost done" with no end.
    collect_attempts: int = 0

    @property
    def is_prep(self) -> bool:
        return self.kind == "prep"

    @property
    def is_promo(self) -> bool:
        return self.kind == "promo"

    def plain_state(self) -> str:
        extra = (PREP_STATE if self.is_prep
                 else PROMO_STATE if self.is_promo else {})
        states = {**PLAIN_STATE, **extra}
        # A running review names the actual step it is on, so the card doesn't
        # read "reviewing" while the rewrite pass retypes the book. Only reviews
        # set a stage; prep and promo never do, so they keep their own messages.
        if self.state in ("queued", "running", "collecting") and self.stage in STAGE_STATE:
            template = STAGE_STATE[self.stage]
        else:
            template = states.get(self.state, self.state)
        return template.format(done=self.done, total=self.total,
                               when=self.schedule_at or "later")

    def to_api(self) -> dict:
        d = asdict(self)
        d["plain_state"] = self.plain_state()
        d["ready"] = self.state == "done"
        d["is_prep"] = self.is_prep
        d["is_promo"] = self.is_promo
        # Which application the reviewed file opens in, so the results card can
        # say where the changes are instead of assuming Word.
        try:
            d["format"] = get_format(self.filename).to_api()
        except IngestError:
            d["format"] = None            # a record from before formats existed
        # Two reviews of one document are now two entries that look alike, so
        # each says which folder its results went to.
        d["results_name"] = (Path(self.results_dir).name
                             if self.results_dir else None)
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_usage(results_dir: Path | str) -> tuple[dict, float | None] | None:
    """The token counts a finished job left behind, whichever pipeline wrote
    them. Shared with the dashboard, which uses it to fill in jobs that
    finished before job records carried their own usage."""
    folder = Path(results_dir)
    for name in ("findings.json", "prep.json"):
        path = folder / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable usage in %s: %s", path, e)
            return None
        return (data.get("usage") or {}), data.get("cost")
    return None


class JobStore:
    """Job records on disk, one directory each, shared with the batch
    manifest so a job is a single folder you can inspect or delete."""

    def __init__(self, paths: Paths):
        self.paths = paths.ensure()
        self._lock = threading.RLock()

    def dir(self, job_id: str) -> Path:
        return self.paths.jobs / job_id

    def save(self, job: Job) -> Job:
        with self._lock:
            d = self.dir(job.id)
            d.mkdir(parents=True, exist_ok=True)
            job.updated_at = _now()
            (d / APP_MANIFEST).write_text(json.dumps(asdict(job), indent=2),
                                          encoding="utf-8")
        return job

    def get(self, job_id: str) -> Job | None:
        path = self.dir(job_id) / APP_MANIFEST
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable job %s: %s", job_id, e)
            return None
        known = {f for f in Job.__dataclass_fields__}
        return Job(**{k: v for k, v in data.items() if k in known})

    def all(self, owner_id: str | None = None) -> list[Job]:
        """Every job, newest first. Pass owner_id to get only one user's — the
        web build does, so one person's list never shows another's work; the
        desktop build passes nothing and sees the single owner's lot."""
        jobs = [j for d in self.paths.jobs.glob("*")
                if (j := self.get(d.name)) is not None
                and (owner_id is None or j.owner_id == owner_id)]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            return self.save(job)

    def delete(self, job_id: str) -> bool:
        """Remove a job's record folder — the manifest, checkpoint, and any
        batch bookkeeping. The produced documents live elsewhere (results_dir);
        the runner clears those before calling this. Returns whether anything
        was there to remove."""
        with self._lock:
            d = self.dir(job_id)
            if not d.is_dir():
                return False
            shutil.rmtree(d, ignore_errors=True)
            return True

    def update_if(self, job_id: str, *, expect: str, **fields) -> Job | None:
        """`update`, but only if the job is still in the state the caller last
        saw it in. Cancelling reads a job's state and then, a moment later,
        writes a new one — without this, the worker thread submitting a batch
        or the ticker promoting a scheduled job could land in that gap and the
        cancellation would be silently lost. Returns None, changing nothing, if
        the state moved on first."""
        with self._lock:
            job = self.get(job_id)
            if job is None or job.state != expect:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            return self.save(job)


class JobRunner:
    """Worker thread for immediate reviews + a ticker for everything on a
    clock: scheduled submissions and batch polling."""

    def __init__(self, store: JobStore, settings: Settings, *,
                 config_path: str | Path, poll_seconds: int = POLL_SECONDS,
                 notify_home: str | Path | None = None):
        self.store = store
        self.settings = settings
        self.ledger = SpendingLedger(store.paths.spending_db)
        self.config_path = Path(config_path)
        # Where DocWatch keeps its email settings and Google sign-in. When set, a
        # finished job emails the completion log through that account — see
        # `_notify_done`. None (a runner told nothing about it) sends nothing.
        self.notify_home = Path(notify_home) if notify_home else None
        self.error_dir = self.config_path.parent / "error_types"
        self.poll_seconds = poll_seconds
        self.queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._tick_mutex = threading.Lock()
        self._threads: list[threading.Thread] = []
        # Ids the user has asked to abort mid-run. The worker polls this as it
        # folds each call in; a set + lock rather than per-job Events so the
        # request can land before the worker has even picked the job up.
        self._cancel_lock = threading.Lock()
        self._cancel_requested: set[str] = set()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._work, name="docproof-worker",
                             daemon=True),
            threading.Thread(target=self._tick, name="docproof-ticker",
                             daemon=True),
        ]
        for t in self._threads:
            t.start()
        self.resume_interrupted()

    def stop(self) -> None:
        self._stop.set()

    def resume_interrupted(self) -> None:
        """A sync job that was mid-flight when the app closed is re-queued.

        It is not started over: the run left a checkpoint of every call it
        completed, and the re-run replays that and pays only for the rest.
        `done` is deliberately left alone — the resumed run races back through
        the cached part in moments, and resetting the bar to zero used to be
        the visible face of resetting the *spend* to zero, which is the thing
        that no longer happens. Batch jobs need nothing: the ticker finds them
        by their manifest."""
        for job in self.store.all():
            # Prep and promo are included at "collecting" too: neither has
            # vendor-side state, and both claim their results folder before
            # writing, so starting again lands in the same place rather than
            # orphaning it. A re-run promo pays for its one call again — it keeps
            # no checkpoint — which is a rare price for a crash mid-write.
            interrupted = (job.state == "running" and job.mode == "now") or (
                (job.is_prep or job.is_promo) and job.state == "collecting")
            if interrupted:
                log.info("Re-queueing %s, interrupted by a restart", job.id)
                self.store.update(job.id, state="queued")
                self.queue.put(job.id)
            elif job.state == "queued":
                self.queue.put(job.id)

    # -- cancellation ---------------------------------------------------------

    def request_cancel(self, job_id: str) -> None:
        """Ask a running job to stop. The worker notices between calls and
        raises out of the run; harmless if the job isn't actually running,
        since the flag is cleared when the worker next finishes with this id."""
        with self._cancel_lock:
            self._cancel_requested.add(job_id)

    def _cancel_pending(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel_requested

    def _clear_cancel(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancel_requested.discard(job_id)

    def _abort(self, job_id: str) -> None:
        """Finalize an aborted run. The checkpoint goes the way a queued
        cancel's does — a cancelled job never runs again, so the calls it did
        pay for before the abort are unreusable clutter — and any results
        folder claimed but never written is handed back."""
        self._release_results_dir(job_id)
        self.discard_checkpoint(job_id)
        job = self.store.get(job_id)
        if job is not None and job.state not in ("done", "failed"):
            self.store.update(job_id, state="cancelled", error=None)
        log.info("Job %s aborted by request", job_id)

    # -- submission -----------------------------------------------------------

    def enqueue(self, job: Job) -> Job:
        """Hand a job to the worker. Batch submission is queued rather than
        run inline: talking to the vendor can take seconds, and the HTTP
        request that created the job should not wait for it."""
        self.store.save(job)
        if job.mode == "batch" and job.schedule_at:
            return self.store.update(job.id, state="scheduled") or job
        self.queue.put(job.id)
        return job

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until the worker has drained. Used by tests and by shutdown;
        the UI polls instead."""
        deadline = time.monotonic() + timeout
        while not self.queue.empty() or self._busy.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError("worker did not settle")
            time.sleep(0.01)

    # -- config ---------------------------------------------------------------

    def config_for(self, job: Job) -> Config:
        cfg = load_config(self.config_path)
        cfg.api.model = job.model
        cfg.api.effort = job.effort
        cfg.min_confidence = job.min_confidence
        # The glossary pass runs its own (usually stronger) model. "off" skips
        # it; None leaves the shipped config default for older job records.
        if job.glossary_model == "off":
            cfg.glossary.enabled = False
        elif job.glossary_model:
            cfg.glossary.model = job.glossary_model
        cfg.comments = self.settings.comments
        cfg.report_explanations = self.settings.explanations
        # Per-run feature switches land last, so a toggle the user set on the
        # submission panel wins over both the shipped config and the two
        # settings-backed defaults above (comments, explanations).
        features.apply_features(cfg, job.features)
        # Prompts the user has edited win over the shipped ones, per key.
        cfg.error_type_override_dir = str(self.store.paths.prompts)
        if job.is_prep:
            cfg.prep.outputs = PREP_OUTPUTS.get(job.prep_output, ["indesign"])
        return cfg

    def _checkpoint(self, job: Job, cfg: Config, prepared) -> "Checkpoint":
        """The record of what this job has already paid for.

        Fingerprinted the way the batch manifest is — document text, full
        config, the exact prompts — because cached answers are only reusable
        while all of those are unchanged. `load()` wipes a stale one itself."""
        if job.is_prep:
            fingerprint = {
                "kind": "prep",
                "content_hash": content_hash(prepared.structure),
                "config": cfg.model_dump(mode="json"),
                "prompts": {"tagging":
                            prepared.prompt.render(prepared.sheet)},
                "selection": None,
            }
        else:
            fingerprint = {
                "kind": "review",
                "content_hash": prepared.content_hash,
                "config": cfg.model_dump(mode="json"),
                "prompts": pass_prompts(cfg, prepared),
                "selection": job.selection,
            }
        checkpoint = Checkpoint(self.store.dir(job.id) / "checkpoint.json",
                                fingerprint=fingerprint)
        checkpoint.load()
        return checkpoint

    def discard_checkpoint(self, job_id: str) -> None:
        (self.store.dir(job_id) / "checkpoint.json").unlink(missing_ok=True)

    def download_anyway(self, job_id: str) -> Job | None:
        """Write the reviewed document for a review that failed the reject-all
        audit, with the audit downgraded to a warning.

        Costs nothing: every model call is already in the checkpoint, so the
        findings replay without touching the provider. The checkpoint is
        fingerprinted on the *original* config, so it is rebuilt and replayed
        under that config first; only then is the audit downgraded, for the
        write itself. The audit failure stays recorded (audit_overridden=True)
        so nothing pretends the check passed."""
        job = self.store.get(job_id)
        if job is None or job.is_prep or job.state != "failed":
            return None

        cfg = self.config_for(job)                      # original: audit strict
        prepared = prepare(cfg, job.source_path, self.error_dir,
                           selection=job.selection)
        checkpoint = self._checkpoint(job, cfg, prepared)
        # _ReplayOnly guarantees this makes no API calls: if the checkpoint is
        # somehow incomplete, it raises rather than silently charging for a
        # re-review.
        coverage = CoverageLedger()
        findings, usage = run_sync(cfg, prepared, _ReplayOnly(),
                                   checkpoint=checkpoint, coverage=coverage)

        cfg.audit = "warn"                              # now let the write pass
        out = (Path(job.results_dir) if job.results_dir
               else self._claim_results_dir(job))
        outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                         source_path=job.source_path, coverage=coverage)
        checkpoint.delete()
        updated = self.store.update(job_id, state="done", audit_overridden=True,
                                    applied=outputs.applied,
                                    results_dir=str(out), error=job.error)
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._notify_done(job_id)
        return updated

    def recover(self, job_id: str) -> Job | None:
        """Re-collect a batch review that failed AFTER its vendor batch landed.

        A collect-time failure — a post-step raising (the LanguageTool pass,
        say), a transient write error — leaves the overnight batch complete and
        untouched at the vendor: it is billed, and its results are still there
        to download. So recovery must NOT resubmit (which is what `retry`, the
        state="queued" path, does — paying for the same batch twice). Instead we
        send the job back through the ticker's collect path against the SAME
        batch by returning it to `waiting`; the next `_advance_batch` re-polls
        (finds it ready) and re-collects, reusing the paid results. Only the
        synchronous post-steps re-run and re-bill, which is small.

        Returns the updated job, or None when there is nothing to recover this
        way: a job that is not failed, a "now" run (or one that never reached
        submit) with no batch at the vendor, or one that failed the reject-all
        audit — that last has its own `download_anyway` path and would only
        fail the same audit again, so it is refused here rather than re-collected
        at cost."""
        job = self.store.get(job_id)
        if job is None or not self.can_recover(job):
            return None
        # A fresh collect budget: the user is asking to try again, and the last
        # run may have exhausted the cap (often why it is failed here at all).
        return self.store.update_if(job_id, expect="failed", state="waiting",
                                    error=None, error_kind="", collect_attempts=0)

    def can_recover(self, job: Job) -> bool:
        """Whether `recover` has something to do — used to decide the results
        card's "Finish collecting" affordance. A failed batch review whose
        vendor batch actually landed (so a re-collect reuses paid results), and
        not a reject-all audit failure (that has its own "Download anyway")."""
        return (job.state == "failed" and not job.audit_failed
                and self._batch_job_id(job) is not None)

    def _provider(self, cfg: Config):
        name = provider_for(cfg.api.model, cfg.api.provider)
        return build_provider(cfg, api_key=get_api_key(name))

    def results_dir(self, job: Job) -> Path:
        """Where this job's finished files go, claimed as it is chosen.

        Two reviews of one document must not share a folder. The second would
        overwrite the first, and — worse — the first review's download button
        would quietly start serving the second review's document. A name
        already taken gets a numbered suffix, the way a browser handles
        downloading the same file twice.

        The folder is created here rather than merely picked: the worker
        thread and the ticker can be finishing two jobs at the same moment,
        and looking before creating would let both settle on the same name."""
        if job.results_dir:
            return Path(job.results_dir)   # already claimed; a retry reuses it
        base = Path(self.settings.output_dir).expanduser()
        stem = Path(job.filename).stem or "document"
        n = 1
        while True:
            candidate = base / (stem if n == 1 else f"{stem} ({n})")
            try:
                candidate.mkdir(parents=True)
                return candidate
            except FileExistsError:
                n += 1

    def _claim_results_dir(self, job: Job) -> Path:
        """Claim the folder and record it, so a job interrupted between here
        and its last write comes back to the same place instead of claiming a
        second one and orphaning the first."""
        out = self.results_dir(job)
        self.store.update(job.id, results_dir=str(out))
        return out

    def _release_results_dir(self, job_id: str) -> None:
        """Give an unused claim back after a failure, so a run that never
        wrote anything doesn't leave an empty folder — or push the next
        review's name to (2)."""
        job = self.store.get(job_id)
        if job is None or not job.results_dir:
            return
        try:
            Path(job.results_dir).rmdir()      # refuses if anything is in it
        except OSError:
            return                             # it has results, or is gone
        self.store.update(job_id, results_dir=None)

    def delete_job(self, job_id: str) -> bool:
        """Remove a finished job for good: its produced documents and its
        record, so it leaves the results list and reclaims its disk.

        The results folder is deleted only when it sits inside the configured
        output directory — a guard so a malformed results_dir can never turn
        this into an rmtree of somewhere it shouldn't be. The abort flag is
        cleared too, in case a delete races a run that just finished."""
        job = self.store.get(job_id)
        if job is None:
            return False
        self._record_spending(job)
        if job.results_dir:
            results = Path(job.results_dir)
            base = Path(self.settings.output_dir).expanduser().resolve()
            try:
                inside = results.resolve().is_relative_to(base)
            except (OSError, ValueError):
                inside = False
            if inside and results.is_dir():
                shutil.rmtree(results, ignore_errors=True)
            elif not inside:
                log.warning("Leaving results dir outside the output base in "
                            "place: %s", job.results_dir)
        self._clear_cancel(job_id)
        return self.store.delete(job_id)

    def _record_spending(self, job: Job) -> None:
        """Snapshot a job's cost to the ledger before its folder is removed, so
        clearing jobs frees disk without erasing the bill. Only jobs that
        actually spent are kept — a cancelled job that never called the API
        leaves nothing worth a row — and the snapshot is taken while
        `results_dir` is still there, so its cost no longer depends on it."""
        numbers = _totals_for(job, read_usage)
        if not (numbers["api_calls"] or numbers["input_tokens"]):
            return
        try:
            self.ledger.record(LedgerEntry.from_job(job, numbers))
        except OSError as e:
            # A ledger write that fails must not block the delete the user
            # asked for; losing one job's snapshot is better than a job that
            # won't clear.
            log.warning("Could not record spending for %s: %s", job.id, e)

    # -- worker ---------------------------------------------------------------

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._busy.set()
            try:
                self.run_one(job_id)
            except Exception as e:            # noqa: BLE001 - never kill the worker
                log.exception("Job %s failed", job_id)
                self.store.update(job_id, state="failed", error=str(e))
            finally:
                # Whatever happened to the job, this id's abort request is spent:
                # clear it so it can't touch a later run that reuses nothing but
                # the same worker.
                self._clear_cancel(job_id)
                self._busy.clear()
                self.queue.task_done()

    def run_one(self, job_id: str) -> None:
        """Dispatch by what the job is, then by when. Public so a test can
        drive the worker's body without a thread."""
        job = self.store.get(job_id)
        if job is None:
            return
        if job.is_prep:
            self._run_prep(job_id)
        elif job.is_promo:
            self._run_promo(job_id)
        elif job.mode == "batch":
            self._submit_batch(job_id)
        else:
            self._run_now(job_id)

    def _run_now(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        # Ingesting and the whole-book reads (spell scan, story sheet) happen
        # here, before the run flips to "running", so the card reads "Reading
        # your manuscript" rather than a stale "Waiting to start" meanwhile.
        self.store.update(job_id, stage="preparing")
        try:
            provider = self._provider(cfg)
            prepared = prepare(cfg, job.source_path, self.error_dir,
                               selection=job.selection)
        except (ProviderError, IngestError, FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return

        # Compare-and-swap, not a plain write: building the provider and
        # ingesting the document took long enough for a cancel to land in the
        # meantime, and a job the user pulled back must not start paying for
        # model calls — or overwrite its own cancellation — now.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=prepared.request_count) is None:
            return

        def progress(done: int, total: int) -> None:
            self.store.update(job_id, done=done, total=total)

        def on_phase(name: str) -> None:
            # Which step the run is on. The detector loop ("reviewing") carries
            # the count; the whole-book passes after it have none, so this is all
            # the card gets to know the document has moved past the bar.
            self.store.update(job_id, stage=name)

        # The checkpoint outlives any failure below on purpose: a retry after
        # a crash or a mid-run exception resumes instead of paying again. Only
        # a finished job deletes it.
        checkpoint = self._checkpoint(job, cfg, prepared)
        coverage = CoverageLedger()
        try:
            findings, usage = run_sync(
                cfg, prepared, provider, progress=progress, on_phase=on_phase,
                checkpoint=checkpoint, coverage=coverage,
                should_cancel=lambda: self._cancel_pending(job_id))
        except JobCancelled:
            self._abort(job_id)
            return
        self.store.update(job_id, stage="writing")
        out = self._claim_results_dir(job)
        try:
            outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                             source_path=job.source_path, coverage=coverage)
        except AuditError as e:
            # No reviewed document was written — that is the point — but the
            # summary and findings were, and they name the paragraph that did
            # not come back. So the folder stays and the job points at it.
            #
            # The checkpoint stays too, unlike prep's: the findings did not
            # cause this. Something applied text without a revision mark
            # around it, which is a code fault, and a retry should not have to
            # pay for the review again to reproduce it.
            log.error("Review for %s failed its reject-all audit: %s",
                      job.id, e)
            self.store.update(job_id, state="failed", error=str(e),
                              results_dir=str(out), audit_failed=True)
            self._record_usage(job_id, out, cfg.api.model, batch=False)
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise
        checkpoint.delete()
        self.store.update(job_id, state="done", applied=outputs.applied,
                          results_dir=str(out), error=None, stage="")
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._notify_done(job_id)

    # -- prep -----------------------------------------------------------------

    def _run_prep(self, job_id: str) -> None:
        """Tag a manuscript into the house style set.

        Always synchronous: the windows have to be read in order, since what a
        paragraph is depends on what came before it, and a batch API answers
        out of order by design."""
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            prepared = preplib.prepare(
                cfg, job.source_path, config_dir=self.config_path.parent,
                override_dir=self.store.paths.prep)
        except (ProviderError, IngestError, StyleSheetError, ConversionError,
                FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return

        # Same compare-and-swap as _run_now, for the same reason: preparing a
        # whole manuscript leaves plenty of room for a cancel to land first.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=prepared.request_count,
                                words=prepared.structure.word_count) is None:
            return

        # Prep reads its windows in order, one call at a time, so an abort just
        # rides the progress callback: it raises between windows, after the last
        # finished one is checkpointed and before the next is paid for.
        def progress(done: int, total: int) -> None:
            if self._cancel_pending(job_id):
                raise JobCancelled()
            self.store.update(job_id, done=done, total=total)

        # Kept through failures — including a failed verification — so a retry
        # replays the windows already paid for rather than re-tagging the book.
        checkpoint = self._checkpoint(job, cfg, prepared)
        try:
            tags, usage = preplib.run(cfg, prepared, provider,
                                      checkpoint=checkpoint, progress=progress)
        except JobCancelled:
            self._abort(job_id)
            return
        self.store.update(job_id, state="collecting")
        out = self._claim_results_dir(job)
        try:
            outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                     source_path=job.source_path,
                                     outputs=cfg.prep.outputs)
        except VerificationFailed as e:
            # The notes were still written, and they are the most useful thing
            # here: they say what prep intended and where the text diverged. So
            # the folder stays and the job points at it.
            #
            # The checkpoint, though, goes: it holds the exact tags that just
            # produced the failing file, and a retry that replays them is a
            # retry that deterministically fails the same way. Verification
            # failure is the one failure where the cache is the problem.
            checkpoint.delete()
            log.error("Prep for %s failed verification: %s", job.id, e)
            self.store.update(job_id, state="failed", error=str(e),
                              results_dir=str(out), verified=False)
            self._record_usage(job_id, out, cfg.api.model, batch=False)
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        checkpoint.delete()
        self.store.update(job_id, state="done", results_dir=str(out),
                          error=None, tagged=outputs.tagged,
                          applied=outputs.tagged, flags=outputs.flags,
                          verified=all(c.ok for c in outputs.verifications),
                          words=outputs.words)
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._notify_done(job_id)

    # -- promo ----------------------------------------------------------------

    def _run_promo(self, job_id: str) -> None:
        """Write a teaser and a set of social posts from a finished manuscript.

        Synchronous like prep, but for a simpler reason: it is a single call
        with the whole book in front of the model, so there is nothing to run in
        parallel and no batch to wait on. What it produces — promo.json (the
        editable copy) and the two .docx — lands in the results folder. Whether
        that then ships to Drive or waits in the panel for a human is the
        approval field's business, settled by the caller above this run, not
        here: this method only generates."""
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            prepared = promolib.prepare(
                cfg, job.source_path, config_dir=self.config_path.parent,
                override_dir=self.store.paths.promo,
                allow_oversize=job.allow_oversize)
        except (ProviderError, IngestError, PromoError,
                FileNotFoundError, ValueError) as e:
            # An oversize book carries its numbers so the watcher can email a
            # person the size and how to run it by hand, rather than just logging
            # a generic failure.
            extra = ({"error_kind": "oversize", "words": e.words,
                      "input_tokens": e.tokens}
                     if isinstance(e, PromoTooLarge) else {})
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e), **extra)
            return

        # Same compare-and-swap as the other pipelines: reading a whole novel
        # leaves room for a cancel to land before the paid call goes out.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=1,
                                words=prepared.manuscript.word_count) is None:
            return

        # One call, so cancellation is a single check right before it: past this
        # point there is no safe seam to interrupt a request already in flight.
        if self._cancel_pending(job_id):
            self._abort(job_id)
            return
        try:
            result, usage = promolib.run(cfg, prepared, provider)
        except PromoError as e:
            # A refusal, or an answer that did not fit the schema. Nothing was
            # written and no results folder was claimed; fail with the sentence.
            self.store.update(job_id, state="failed", error=str(e))
            return

        # The opt-in second pass: a model check that each claim follows from the
        # book. It never fails the run — a grounding check that broke must not
        # sink the copy it was checking — and its tokens fold into `usage`.
        claims = ()
        if cfg.promo.verify_claims:
            claims = tuple(promolib.verify_claims(
                cfg, prepared, result, provider, usage,
                config_dir=self.config_path.parent,
                override_dir=self.store.paths.promo))

        self.store.update(job_id, state="collecting")
        out = self._claim_results_dir(job)
        try:
            outputs = promolib.finish(prepared, result, usage, cfg, out_dir=out,
                                      source_path=job.source_path, claims=claims)
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        self.store.update(
            job_id, state="done", results_dir=str(out), error=None,
            words=outputs.words, unverified=outputs.flag_count,
            # Reuse the prep card's clean/flagged light: grounding-clean copy
            # reads as "verified", flagged copy invites a look before it ships.
            verified=outputs.flag_count == 0)
        self._record_usage_inline(job_id, usage, cfg.api.model)
        self._notify_done(job_id)

    def _record_usage_inline(self, job_id: str, usage, model: str) -> None:
        """Copy token counts straight onto the record from the run in hand.

        Review and prep re-read what the engine wrote to disk; promo has the
        counts in memory (one call, no checkpoint to reconcile), so it records
        them directly. `_totals_for` reads the record before the folder, so the
        dashboard and the ledger see these with no re-read."""
        cost = estimate_cost(
            model,
            input_tokens=usage.input_tokens + usage.cache_creation_input_tokens,
            output_tokens=usage.output_tokens, batch=False)
        self.store.update(
            job_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            api_calls=usage.api_calls, cost=cost)

    # -- telling a person it finished -----------------------------------------

    def _notify_done(self, job_id: str) -> None:
        """Email the completion log for a job that just finished.

        One address and one switch for every pipeline, reused from DocWatch —
        see `notify.send_job_completion`. Watched *format* jobs are the one
        exception: the tick emails those itself, with the routing and Drive links
        a watched book carries and this record does not, so they are left to it
        and not emailed twice. Everything else — every app job, and watched promo
        — goes through here. Best-effort: a mail that will not send is logged,
        never raised, so it can never undo a finished job."""
        if self.notify_home is None:
            return
        job = self.store.get(job_id)
        if job is None or job.state != "done":
            return
        if job.source == "watch" and job.is_prep:
            return                            # the tick emails these, richer
        try:
            from .watch import notify
            notify.send_job_completion(self.notify_home, job)
        except Exception:                     # noqa: BLE001 - never over a job
            log.exception("Completion email for %s failed", job_id)

    # -- what it cost ---------------------------------------------------------

    def _record_usage(self, job_id: str, out: Path, model: str, *,
                      batch: bool) -> None:
        """Copy the token counts onto the job record.

        They are already in findings.json / prep.json, but the dashboard adds
        up every job the user has ever run, and re-reading a folder per job to
        do that gets slower every week."""
        totals = read_usage(out)
        if totals is None:
            return
        usage, cost = totals
        if cost is None:
            cost = estimate_cost(
                model,
                input_tokens=usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0), batch=batch)
        self.store.update(
            job_id,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            api_calls=usage.get("api_calls", 0), cost=cost)

    # -- batch ----------------------------------------------------------------

    def _submit_batch(self, job_id: str) -> None:
        job = self.store.get(job_id)
        # Same guard _run_now and _run_prep already have: a job cancelled while
        # its id was still sitting in the queue must not be submitted the
        # instant the worker gets to it.
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            batch_job = batchlib.submit(cfg, job.source_path, self.error_dir,
                                        provider, self.store.paths.jobs,
                                        selection=job.selection)
        except (ProviderError, IngestError, batchlib.BatchError,
                FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return
        # batchlib picked its own folder; record the link and adopt its total.
        # The id file goes down before the state does: `waiting` is the
        # ticker's cue to go looking for the id, and writing them the other
        # way round gave it a moment where a submitted, billable batch looked
        # lost and the job was failed for it.
        (self.store.dir(job_id) / "batch_job_id").write_text(batch_job.job_id,
                                                             encoding="utf-8")
        if self.store.update_if(job_id, expect=job.state, state="waiting",
                                total=batch_job.request_count,
                                error=None) is None:
            # Cancelled while the batch was on its way to the vendor. It can't
            # be unsubmitted, so say what it cost: the id file names it.
            log.warning("Job %s was cancelled during submission; batch %s is "
                        "at the vendor and will not be collected.", job_id,
                        batch_job.job_id)

    def _batch_job_id(self, job: Job) -> str | None:
        path = self.store.dir(job.id) / "batch_job_id"
        return path.read_text("utf-8").strip() if path.is_file() else None

    def _tick(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick_once()
            except Exception:                 # noqa: BLE001
                log.exception("Ticker pass failed")

    def tick_once(self) -> None:
        """One pass: submit anything due, advance anything waiting. Public so
        tests can drive it without waiting on a timer.

        One pass at a time, and a second caller is turned away rather than
        queued: the ticker thread and `POST /api/tick` both come through here,
        and two passes over the same ready batch would collect it twice into
        two folders. Whoever holds the lock is already doing the looking."""
        if not self._tick_mutex.acquire(blocking=False):
            return
        try:
            for job in self.store.all():
                if job.state == "scheduled" and self._due(job):
                    # Guards against a cancellation landing between this read
                    # and the write below: only queue it if it was still
                    # scheduled at the moment of the update, not just at the
                    # moment of this read.
                    if self.store.update_if(job.id, expect="scheduled",
                                            state="queued"):
                        self.queue.put(job.id)
                # `collecting` is included so a job interrupted partway through
                # collection is picked up again: the results are still at the
                # vendor, and both poll and collect can be re-run. Prep is
                # never at a vendor — the worker owns it start to finish — so
                # the ticker leaves it alone rather than looking for a batch
                # that isn't there.
                elif job.state in ("waiting", "collecting") and not job.is_prep:
                    self._advance_batch(job)
        finally:
            self._tick_mutex.release()

    def _due(self, job: Job) -> bool:
        target = self._scheduled_for(job)
        return target is None or datetime.now().astimezone() >= target

    def _scheduled_for(self, job: Job) -> datetime | None:
        """When a scheduled job should actually go, or None for "right now".

        The time is the first HH:MM on or after the moment the job was made,
        which is the difference between "tonight at 2 AM" and "2 AM already
        happened today, go immediately"."""
        if not job.schedule_at:
            return None
        try:
            hh, mm = (int(p) for p in job.schedule_at.split(":", 1))
            created = datetime.fromisoformat(job.created_at).astimezone()
            target = created.replace(hour=hh, minute=mm, second=0,
                                     microsecond=0)
        except (AttributeError, TypeError, ValueError):
            return None                       # unparseable: don't strand it
        return target if target >= created else target + timedelta(days=1)

    def _advance_batch(self, job: Job) -> None:
        batch_id = self._batch_job_id(job)
        if batch_id is None:
            self.store.update(job.id, state="failed",
                              error="Lost track of this review; start it again.")
            return
        cfg = self.config_for(job)
        try:
            batch_job = batchlib.load(self.store.paths.jobs, batch_id)
            provider = self._provider(cfg)
            status = batchlib.poll(batch_job, provider, self.store.paths.jobs)
        except (batchlib.BatchError, ProviderError) as e:
            self.store.update(job.id, state="failed", error=str(e))
            return

        self.store.update(job.id, done=status.succeeded + status.errored,
                          total=status.total or job.total)
        if batch_job.state == "failed":
            self.store.update(job.id, state="failed", error=batch_job.error)
            return
        if batch_job.state != "ready":
            return

        # Give up rather than retry forever when collecting keeps killing the
        # process before it can record a failure (an OOM under the LanguageTool
        # JVM, a machine restart mid-write). The count is written with the
        # collecting transition below — before the attempt — so a crash still
        # spends one; once they run out, the job becomes a visible, recoverable
        # failure instead of "almost done" with no end.
        attempt = job.collect_attempts + 1
        if attempt > MAX_COLLECT_ATTEMPTS:
            self.store.update(
                job.id, state="failed",
                error="Writing the reviewed document didn't finish after "
                      "several tries — the results are still here to collect. "
                      "Use “Finish collecting” to try again.")
            return

        # CAS for the same reason the scheduled→queued promotion has one: the
        # job was read at the top of the pass, and its state may have moved on.
        if self.store.update_if(job.id, expect=job.state, state="collecting",
                                collect_attempts=attempt) is None:
            return
        out = self._claim_results_dir(job)
        try:
            outputs = batchlib.collect(batch_job, provider, self.error_dir,
                                       self.store.paths.jobs, out_dir=out)
        except Exception as e:                # noqa: BLE001
            # Anything uncaught here would otherwise leave the job in
            # `collecting`, which the ticker now retries — so a permanent
            # failure has to become a state the user can see and retry.
            log.exception("Collecting %s failed", job.id)
            self._release_results_dir(job.id)
            self.store.update(job.id, state="failed", error=str(e))
            return
        self.store.update(job.id, state="done", applied=outputs.applied,
                          results_dir=str(out), error=None)
        self._record_usage(job.id, out, cfg.api.model, batch=True)
        self._notify_done(job.id)
