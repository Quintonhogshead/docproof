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
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path

from docproof import batch as batchlib
from docproof.config import Config, load_config
from docproof.ingest import IngestError
from docproof.pipeline import finish, prepare, run_sync
from docproof.providers import ProviderError, build_provider, provider_for

from .settings import Paths, Settings, get_api_key

log = logging.getLogger("docproof.app.jobs")

APP_MANIFEST = "app.json"
POLL_SECONDS = 120

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
}


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
    error: str | None = None
    applied: int | None = None
    results_dir: str | None = None
    min_confidence: str = "medium"
    created_at: str = ""
    updated_at: str = ""

    def plain_state(self) -> str:
        template = PLAIN_STATE.get(self.state, self.state)
        return template.format(done=self.done, total=self.total,
                               when=self.schedule_at or "later")

    def to_api(self) -> dict:
        d = asdict(self)
        d["plain_state"] = self.plain_state()
        d["ready"] = self.state == "done"
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def all(self) -> list[Job]:
        jobs = [j for d in self.paths.jobs.glob("*")
                if (j := self.get(d.name)) is not None]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            return self.save(job)


class JobRunner:
    """Worker thread for immediate reviews + a ticker for everything on a
    clock: scheduled submissions and batch polling."""

    def __init__(self, store: JobStore, settings: Settings, *,
                 config_path: str | Path, poll_seconds: int = POLL_SECONDS):
        self.store = store
        self.settings = settings
        self.config_path = Path(config_path)
        self.error_dir = self.config_path.parent / "error_types"
        self.poll_seconds = poll_seconds
        self.queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._threads: list[threading.Thread] = []

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
        """A sync job that was mid-flight when the app closed cannot be
        resumed — it has no vendor-side state to reconnect to. Re-queue it.
        Batch jobs need nothing: the ticker finds them by their manifest."""
        for job in self.store.all():
            if job.state == "running" and job.mode == "now":
                log.info("Re-queueing %s, interrupted by a restart", job.id)
                self.store.update(job.id, state="queued", done=0)
                self.queue.put(job.id)
            elif job.state == "queued":
                self.queue.put(job.id)

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
        cfg.min_confidence = job.min_confidence
        cfg.comments = self.settings.comments
        return cfg

    def _provider(self, cfg: Config):
        name = provider_for(cfg.api.model, cfg.api.provider)
        return build_provider(cfg, api_key=get_api_key(name))

    def results_dir(self, job: Job) -> Path:
        base = Path(self.settings.output_dir).expanduser()
        return base / Path(job.filename).stem

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
                self._busy.clear()
                self.queue.task_done()

    def run_one(self, job_id: str) -> None:
        """Dispatch by mode. Public so a test can drive the worker's body
        without a thread."""
        job = self.store.get(job_id)
        if job is None:
            return
        if job.mode == "batch":
            self._submit_batch(job_id)
        else:
            self._run_now(job_id)

    def _run_now(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            prepared = prepare(cfg, job.source_path, self.error_dir)
        except (ProviderError, IngestError, FileNotFoundError, ValueError) as e:
            self.store.update(job_id, state="failed", error=str(e))
            return

        self.store.update(job_id, state="running", done=0,
                          total=prepared.request_count)

        def progress(done: int, total: int) -> None:
            self.store.update(job_id, done=done, total=total)

        findings, usage = run_sync(cfg, prepared, provider, progress=progress)
        out = self.results_dir(job)
        outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                         source_path=job.source_path)
        self.store.update(job_id, state="done", applied=outputs.applied,
                          results_dir=str(out), error=None)

    # -- batch ----------------------------------------------------------------

    def _submit_batch(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            batch_job = batchlib.submit(cfg, job.source_path, self.error_dir,
                                        provider, self.store.paths.jobs)
        except (ProviderError, IngestError, batchlib.BatchError,
                FileNotFoundError, ValueError) as e:
            self.store.update(job_id, state="failed", error=str(e))
            return
        # batchlib picked its own folder; record the link and adopt its total.
        self.store.update(job_id, state="waiting", total=batch_job.request_count,
                          error=None)
        (self.store.dir(job_id) / "batch_job_id").write_text(batch_job.job_id,
                                                             encoding="utf-8")

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
        tests can drive it without waiting on a timer."""
        for job in self.store.all():
            if job.state == "scheduled" and self._due(job):
                self.store.update(job.id, state="queued")
                self.queue.put(job.id)
            elif job.state == "waiting":
                self._advance_batch(job)

    def _due(self, job: Job) -> bool:
        if not job.schedule_at:
            return True
        try:
            hh, mm = (int(p) for p in job.schedule_at.split(":", 1))
            target = dtime(hour=hh, minute=mm)
        except (TypeError, ValueError):
            return True                       # unparseable: don't strand it
        return datetime.now().time() >= target

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

        self.store.update(job.id, state="collecting")
        out = self.results_dir(job)
        try:
            outputs = batchlib.collect(batch_job, provider, self.error_dir,
                                       self.store.paths.jobs, out_dir=out)
        except (batchlib.BatchError, IngestError, ValueError) as e:
            self.store.update(job.id, state="failed", error=str(e))
            return
        self.store.update(job.id, state="done", applied=outputs.applied,
                          results_dir=str(out), error=None)
