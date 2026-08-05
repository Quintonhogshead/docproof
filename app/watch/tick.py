"""One pass over the watched folder.

A tick is three slots run in order — collect what finished, submit what is
ready, prepare what is new — and today only the third does anything. The
other two are not placeholders in the apologetic sense: they are the order
this has to happen in, written down now, while the reason is obvious.

The order matters because it is the order that costs least. Collecting first
means work paid for last night is in the folder before anything new is
started; submitting before preparing means an overnight batch is with the
vendor while the synchronous work runs, rather than an hour behind it.

Nothing here runs on a clock of its own. `launchd` decides when a tick
happens, the tick does what it finds, and the process exits.
"""
from __future__ import annotations

import logging
import queue
from dataclasses import dataclass, field
from pathlib import Path

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, get_api_key, resource_root

from . import drive, prep
from .drive import DriveError, DriveFile
from .settings import GOOGLE_KEY, WatchSettings
from .stages import JOB_PROP, OUTPUT_PROP, SOURCE_PROP, Stage, classify
from .state import WatchState

log = logging.getLogger("docproof.app.watch.tick")

STATE_FILE = "state.json"
DOWNLOADS = "downloads"


class NotConfigured(DriveError):
    """Something has to be set up before a tick can do anything. The message
    says which command does it."""


class PrepFailed(RuntimeError):
    """A manuscript did not come through, for a reason that might not happen
    again — so it is counted against the file rather than marked on it."""


@dataclass
class TickReport:
    """What one pass did, in the words the CLI prints and `status` repeats."""

    listed: int = 0
    new: int = 0
    skipped: int = 0
    deferred: int = 0
    prepped: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    plan: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


def config_path() -> Path:
    """The shipped pipeline config. Computed here rather than imported from
    `app.main`, which would drag FastAPI into a launchd process."""
    return resource_root() / "config" / "default.yaml"


# --- the slots ----------------------------------------------------------------

def collect_finished(token: str, ws: WatchSettings, listing: list[DriveFile],
                     state: WatchState, store: JobStore, *, opener,
                     report: TickReport) -> None:
    """Nothing yet.

    Copy edits will be collected here. A review submitted to a vendor's batch
    queue by an earlier tick is answered hours later at half price, so this is
    where a tick asks whether last night's work has landed and, if it has,
    writes the tracked-changes file into the folder for a human editor.

    Prep never waits — its windows have to be read in order, so it cannot be
    a batch — which is why today there is nothing to collect."""


def submit_ready(token: str, ws: WatchSettings, listing: list[DriveFile],
                 state: WatchState, store: JobStore, *, opener,
                 report: TickReport) -> None:
    """Nothing yet.

    Copy edits will be submitted here. What makes a manuscript ready is a
    question for `stages.classify`: first a subfolder an editor drops the
    developmental-edit-complete file into, later a deal stage read from
    HubSpot. Either way this slot submits the overnight batch and returns —
    the answer arrives at some later tick, in `collect_finished`."""


def run_prep(token: str, home: Path, ws: WatchSettings,
             listing: list[DriveFile], state: WatchState, runner: JobRunner,
             store: JobStore, *, mock: bool, opener,
             report: TickReport) -> None:
    """Prepare every manuscript nobody has prepared yet."""
    todo = [f for f in listing if classify(f) is Stage.NEW_MANUSCRIPT]
    todo.sort(key=lambda f: (f.modified_time, f.name))
    report.new = len(todo)

    if len(todo) > ws.max_files_per_tick:
        # Said out loud, not swallowed: a cap that quietly drops work reads
        # afterwards as a folder that was fully handled.
        report.deferred = len(todo) - ws.max_files_per_tick
        log.info("%d manuscripts are waiting; preparing %d this run and "
                 "leaving %d for the next.", len(todo), ws.max_files_per_tick,
                 report.deferred)
        todo = todo[:ws.max_files_per_tick]

    for file in todo:
        try:
            _one(token, home, ws, file, listing, state, runner, store,
                 mock=mock, opener=opener, report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            # A manuscript that cannot be prepared must not take the four
            # behind it down with it.
            log.exception("Could not prepare %s", file.name)
            report.failed.append((file.name, str(e)))
            record = state.get(file.id)
            record.name = file.name
            record.attempts += 1
            state.record(record)


def _one(token: str, home: Path, ws: WatchSettings, file: DriveFile,
         listing: list[DriveFile], state: WatchState, runner: JobRunner,
         store: JobStore, *, mock: bool, opener, report: TickReport) -> None:
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time

    if rec.attempts >= ws.max_attempts:
        # Tried on three separate runs and failed the same way each time. That
        # is a fact about the file, not about the weather, so it stops being
        # tried and starts being visible.
        reason = f"Gave up after {rec.attempts} attempts."
        log.error("%s: %s", file.name, reason)
        job = store.get(rec.job_id) if rec.job_id else None
        prep.mark_source(token, file, job or _placeholder(file, ws), rec,
                         state, failed=reason, opener=opener)
        report.failed.append((file.name, reason))
        return

    job = _prepare(token, home, ws, file, rec, state, runner, store, mock=mock,
                   opener=opener)

    if job.state == "failed" and job.verified is False:
        _refuse(token, ws, file, job, rec, state, opener=opener, report=report)
        return
    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "Preparing the manuscript did not finish.")

    report.prepped.append(file.name)
    report.uploaded.extend(
        prep.upload_outputs(token, file, job, ws, rec, state, listing,
                            opener=opener))
    prep.mark_source(token, file, job, rec, state, opener=opener)


def _placeholder(file: DriveFile, ws: WatchSettings) -> Job:
    """A job to name in the marker when giving up happened before there was
    one — a manuscript that never got as far as being downloaded."""
    return Job(id="", filename=file.name, source_path="", model=ws.model,
               mode="now", kind="prep")


def _prepare(token: str, home: Path, ws: WatchSettings, file: DriveFile,
             rec, state: WatchState, runner: JobRunner, store: JobStore, *,
             mock: bool, opener) -> Job:
    """The manuscript's job, run or resumed.

    The shortcut in the middle is the point of the state file: a job that
    finished before the last tick died is not run again, because running it
    again means asking a model to read a novel that has already been read."""
    existing = store.get(rec.job_id) if rec.job_id else None

    if existing is not None:
        finished = (existing.state == "done"
                    and existing.results_dir
                    and Path(existing.results_dir).is_dir())
        if finished or (existing.state == "failed"
                        and existing.verified is False):
            log.info("%s was already prepared; picking up where the last run "
                     "stopped.", file.name)
            return existing
        # Half-done, or failed for a reason worth another go. The checkpoint in
        # its folder means the windows already paid for are replayed, not
        # re-asked.
        existing.state = "queued"
        existing.error = None
        return prep.run_job(runner, store, existing, mock=mock)

    local = prep.fetch(token, file, home / DOWNLOADS / file.id, opener=opener)
    job = prep.make_job(local, ws)
    rec.job_id = job.id
    state.record(rec)              # before the model is called, never after
    return prep.run_job(runner, store, job, mock=mock)


def _refuse(token: str, ws: WatchSettings, file: DriveFile, job: Job, rec,
            state: WatchState, *, opener, report: TickReport) -> None:
    """Prep wrote a file and then proved it no longer said what the author
    said, so the file was deleted rather than shipped.

    Nothing goes into the folder — putting a manuscript there that failed the
    one check protecting the author's words would be worse than putting
    nothing there. The file is marked so it is not tried nightly forever, and
    the failure is loud everywhere a person looks."""
    reason = job.error or "The finished file did not match the manuscript."
    log.error("%s was not formatted: %s", file.name, reason)

    if ws.upload_failure_note:
        note = prep.failure_note(job, file, reason)
        if note is not None:
            new_id = drive.upload(token, ws.folder_id, note, name=note.name,
                                  mime_type=prep.MARKDOWN_MIME,
                                  app_properties={OUTPUT_PROP: "1",
                                                  SOURCE_PROP: file.id,
                                                  JOB_PROP: job.id},
                                  opener=opener)
            rec.uploaded[note.name] = new_id
            state.record(rec)
            report.uploaded.append(note.name)

    prep.mark_source(token, file, job, rec, state, failed=reason, opener=opener)
    report.failed.append((file.name, reason))


# --- one pass -----------------------------------------------------------------

def tick(home: str | Path, ws: WatchSettings, *, dry_run: bool = False,
         mock: bool = False, opener=None, get_key=None) -> TickReport:
    """Look once, do what is there, and hand back what happened.

    The folder lock is the caller's job, not this function's: `once` takes it
    before anything else so a tick that overruns its schedule is skipped
    rather than doubled, and the tests drive `tick` without one."""
    root = Path(home)
    report = TickReport(dry_run=dry_run)
    # Both of these resolve here rather than in the signature. A default
    # argument binds once, at import, to the function it was written next to —
    # so a caller that swaps the name gets the old one anyway, and the way you
    # find that out is a test quietly reaching Google.
    opener = opener or drive._open_url

    if not ws.folder_id:
        raise NotConfigured("No folder is being watched yet. Run "
                            "`docproof-watch init` to say which one.")
    if not ws.client_id or not ws.client_secret:
        raise NotConfigured("There is no Google sign-in set up yet. Run "
                            "`docproof-watch auth` — docs/watch.md walks "
                            "through making the OAuth client it asks for.")
    refresh = (get_key or get_api_key)(GOOGLE_KEY)
    if not refresh:
        raise NotConfigured("DocProof is not signed in to Google. Run "
                            "`docproof-watch auth`.")

    token = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh,
                                       opener=opener)
    listing = drive.list_folder(token, ws.folder_id, opener=opener)
    report.listed = len(listing)
    report.plan = [(f.name, classify(f).value) for f in listing]
    report.skipped = sum(1 for _, stage in report.plan
                         if stage == Stage.SKIP.value)

    if dry_run:
        # Read-only by construction: nothing below this line has run, so no
        # folder was made, no manuscript downloaded and no model called.
        report.new = sum(1 for _, stage in report.plan
                         if stage == Stage.NEW_MANUSCRIPT.value)
        return report

    paths = Paths(root).ensure()
    store = JobStore(paths)
    runner = JobRunner(store, ws.app_settings(root), config_path=config_path())
    _drain(runner)
    state = WatchState.load(root / STATE_FILE)

    collect_finished(token, ws, listing, state, store, opener=opener,
                     report=report)
    submit_ready(token, ws, listing, state, store, opener=opener, report=report)
    run_prep(token, root, ws, listing, state, runner, store, mock=mock,
             opener=opener, report=report)
    return report


def _drain(runner: JobRunner) -> None:
    """Finish anything a previous tick left in flight, before starting more.

    `resume_interrupted` puts the ids on the runner's queue expecting a worker
    thread to be reading it. There isn't one — this process does the work
    itself — so the queue is emptied by hand."""
    runner.resume_interrupted()
    while True:
        try:
            job_id = runner.queue.get_nowait()
        except queue.Empty:
            return
        try:
            runner.run_one(job_id)
        except Exception as e:            # noqa: BLE001 - mirrors _work
            log.exception("Resuming job %s failed", job_id)
            runner.store.update(job_id, state="failed", error=str(e))
