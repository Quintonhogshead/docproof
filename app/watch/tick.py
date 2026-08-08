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

from . import drive, hubspot, prep
from .drive import DriveError, DriveFile
from .hubspot import HubSpotAuthError, HubSpotError
from .keys import key_from_name
from .settings import GOOGLE_KEY, HUBSPOT_KEY, WatchSettings
from .stages import JOB_PROP, OUTPUT_PROP, SOURCE_PROP, Stage, classify
from .state import WatchState, note_tick

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
    # Manuscripts left where they are because HubSpot did not say to touch them:
    # no key, no record, not marked ready, or a lookup that could not be reached
    # this pass. Stood aside, not failed — no marker is written, so the next
    # tick reconsiders — so it is counted apart from `failed`.
    waiting: int = 0
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
             store: JobStore, *, mock: bool, opener, hs_token: str | None = None,
             report: TickReport) -> None:
    """Prepare every manuscript nobody has prepared yet."""
    todo = [f for f in listing if classify(f) is Stage.NEW_MANUSCRIPT]
    todo.sort(key=lambda f: (f.modified_time, f.name))
    report.new = len(todo)

    # When HubSpot is the gate, only the books it says are ready go on. The
    # token was resolved once in `tick`, the same road the Google one takes, so
    # a test can drive the gate through the injected key reader.
    if ws.hubspot_enabled:
        todo = _gate_hubspot(hs_token, ws, todo, state, opener=opener,
                             report=report)

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
                 mock=mock, opener=opener, hs_token=hs_token, report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            # A manuscript that cannot be prepared must not take the four
            # behind it down with it.
            log.exception("Could not prepare %s", file.name)
            report.failed.append((file.name, str(e)))
            record = state.get(file.id)
            record.name = file.name
            record.attempts += 1
            state.record(record)


def _gate_hubspot(hs_token: str, ws: WatchSettings, todo: list[DriveFile],
                  state: WatchState, *, opener,
                  report: TickReport) -> list[DriveFile]:
    """Keep only the manuscripts HubSpot says to work on.

    Waiting is the `FolderInUse` posture, not a failure: no key, no record, or
    "ready" still off leaves the file exactly where it is, with no marker, so
    the next tick reconsiders — a book becomes eligible the moment an editor
    flips its toggle, without anyone touching the folder.

    Two failure shapes are kept apart on purpose. A bad token dooms the whole
    pass, so `HubSpotAuthError` is left to propagate and stop everything. A
    lookup that merely could not be reached — HubSpot busy, a dropped
    connection — is one file's bad luck, so it is caught here and that file
    waits while the rest go on, the same "one book, not the run" rule the prep
    loop keeps.

    The record id is written onto the file's record *before* it is prepared, so
    a crash anywhere in the round trip still knows which HubSpot record to
    finish. That is also what lets a book DocProof already started — recognised
    by a record id it wrote earlier — be carried through to completion even
    after a crash between the HubSpot write and the Drive marker."""
    want = [p for p in (ws.hubspot_ready_property, ws.hubspot_done_property)
            if p]
    eligible: list[DriveFile] = []
    for file in todo:
        key = key_from_name(file.name, ws.hubspot_key_pattern)
        if not key:
            log.info("Waiting: %s carries no HubSpot key in its name.",
                     file.name)
            report.waiting += 1
            continue
        try:
            record = hubspot.find_record(
                hs_token, ws.hubspot_object, ws.hubspot_key_property, key,
                want_properties=want, opener=opener)
        except HubSpotAuthError:
            raise                         # the token is bad — stop the pass
        except HubSpotError as e:         # this file only — the rest go on
            log.info("Waiting: could not look up %s in HubSpot (%s); the next "
                     "run will try again.", file.name, e)
            report.waiting += 1
            continue

        if record is None:
            log.info("Waiting: no HubSpot %s has %s = %s.", ws.hubspot_object,
                     ws.hubspot_key_property, key)
            report.waiting += 1
            continue

        rec = state.get(file.id)
        ours = bool(rec.hubspot_id)       # a record id we wrote on an earlier tick
        done = hubspot.is_on(record, ws.hubspot_done_property)
        ready = hubspot.is_on(record, ws.hubspot_ready_property)

        # done + ours   -> finish our own tail after a crash before the marker
        # not done+ours -> finish what we already started, ready or not: the
        #                  spend is already committed and the checkpoint holds
        # fresh + ready -> start it
        # anything else -> wait (done elsewhere, or simply not ready yet)
        proceed = ours if done else (ours or ready)
        if not proceed:
            why = "already marked done" if done else "not marked ready"
            log.info("Waiting: %s is %s in HubSpot.", file.name, why)
            report.waiting += 1
            continue

        if rec.hubspot_id != record.id:
            rec.hubspot_id = record.id    # written before prep, never after
            rec.name = file.name
            state.record(rec)
        eligible.append(file)
    return eligible


def _one(token: str, home: Path, ws: WatchSettings, file: DriveFile,
         listing: list[DriveFile], state: WatchState, runner: JobRunner,
         store: JobStore, *, mock: bool, opener, hs_token: str | None,
         report: TickReport) -> None:
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time

    job = store.get(rec.job_id) if rec.job_id else None
    paid_for = (job is not None and job.state == "done" and job.results_dir
                and Path(job.results_dir).is_dir())

    if rec.attempts >= ws.max_attempts and not paid_for:
        # Tried on three separate runs and failed the same way each time. That
        # is a fact about the file, not about the weather, so it stops being
        # tried and starts being visible. A job that *finished* is exempt: the
        # model has been paid and the files exist, so what failed three times
        # was only the upload — free to retry, ruinous to give up on.
        reason = f"Gave up after {rec.attempts} attempts."
        log.error("%s: %s", file.name, reason)
        prep.mark_source(token, file, job or _placeholder(file, ws), rec,
                         state, failed=reason, opener=opener)
        report.failed.append((file.name, reason))
        return

    job = _prepare(token, home, ws, file, rec, state, runner, store, mock=mock,
                   opener=opener)

    if job.state == "failed" and job.verified is False:
        _refuse(token, ws, file, job, rec, state, listing, opener=opener,
                report=report)
        return
    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "Preparing the manuscript did not finish.")

    report.prepped.append(file.name)
    uploaded = prep.upload_outputs(token, file, job, ws, rec, state, listing,
                                   opener=opener)
    report.uploaded.extend(uploaded)

    _finish_hubspot(hs_token, ws, file, rec, state, uploaded, opener=opener)

    prep.mark_source(token, file, job, rec, state, opener=opener)


def _finish_hubspot(hs_token: str | None, ws: WatchSettings, file: DriveFile,
                    rec, state: WatchState, uploaded: list[str], *,
                    opener) -> None:
    """Flip the completion toggle on the book we just put back.

    Between `upload_outputs` and `mark_source` on purpose, and the local record
    is written *before* the Drive marker: with the marker last, a file that
    reads `formatted` in Drive was `done` in HubSpot first, so the two can never
    disagree in the direction that would strand a book as done-but-unmarked.

    Setting a boolean that is already true is harmless, which is what makes the
    whole step safe to repeat when a later tick finishes an interrupted one."""
    if not (ws.hubspot_enabled and rec.hubspot_id and not rec.hubspot_done):
        return
    props = {ws.hubspot_done_property: "true"}
    if ws.hubspot_output_property:
        name = _output_name(uploaded) or _output_name(list(rec.uploaded))
        if name:
            props[ws.hubspot_output_property] = name
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.hubspot_id, props,
                           opener=opener)
    rec.hubspot_done = True
    state.record(rec)                     # recorded before the Drive marker


def _output_name(names: list[str]) -> str:
    """The deliverable to name in HubSpot: the InDesign-ready file if there is
    one, otherwise the first thing we uploaded. Chosen by prefix rather than by
    position so `prep_output="both"` does not name the tracked-changes file."""
    for name in names:
        if name.startswith("tagged_"):
            return name
    return names[0] if names else ""


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
            state: WatchState, listing: list[DriveFile], *, opener,
            report: TickReport) -> None:
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
        # The same two guards upload_outputs keeps: a note that landed on a
        # tick whose marker never did is adopted, not uploaded again beside
        # itself.
        if note is not None and note.name not in rec.uploaded:
            orphan = prep._already_there(listing, file.id, note.name)
            if orphan is not None:
                rec.uploaded[note.name] = orphan.id
                state.record(rec)
            else:
                new_id = drive.upload(token, ws.folder_id, note,
                                      name=note.name,
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
    if ws.hubspot_enabled:
        # Half-configured is worse than off: a gate that cannot ask HubSpot
        # would either prep everything ungated or nothing at all, so it is
        # refused here with the field that is missing named.
        blanks = [name for name, value in (
            ("hubspot_object", ws.hubspot_object),
            ("hubspot_key_property", ws.hubspot_key_property),
            ("hubspot_ready_property", ws.hubspot_ready_property),
            ("hubspot_done_property", ws.hubspot_done_property),
        ) if not value]
        if blanks:
            raise NotConfigured(
                "HubSpot is switched on but " + ", ".join(blanks)
                + " is not set. Run `docproof-watch init` to fill it in.")
        if not (get_key or get_api_key)(HUBSPOT_KEY):
            raise NotConfigured(
                "HubSpot is switched on but there is no token. Run "
                "`docproof-watch hubspot-token` on the desktop, or set the "
                "HUBSPOT_TOKEN secret on the server.")

    if not dry_run:
        # Stamped before the first network call, not after it — see
        # state.note_tick. Two clocks ask when this last happened, and a pass
        # that dies refreshing its token must still count as a look, or the
        # in-app clock retries it every minute instead of every
        # tick_every_minutes. A dry run stays read-only, stamp included.
        note_tick(root)

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
    state = WatchState.load(root / STATE_FILE)
    if not mock:
        # A rehearsal leaves real leftovers alone: _drain finishes whatever a
        # dead pass left mid-flight through the real provider, and
        # `--mock-tags` is documented as costing nothing.
        _drain(runner, state, listing)

    # Resolved the same way the Google token is — through the injected reader,
    # so a test drives the gate without a keychain. The preflight above has
    # already refused the pass if it is enabled and missing.
    hs_token = ((get_key or get_api_key)(HUBSPOT_KEY)
                if ws.hubspot_enabled else None)

    collect_finished(token, ws, listing, state, store, opener=opener,
                     report=report)
    submit_ready(token, ws, listing, state, store, opener=opener, report=report)
    run_prep(token, root, ws, listing, state, runner, store, mock=mock,
             opener=opener, hs_token=hs_token, report=report)
    return report


def _drain(runner: JobRunner, state: WatchState,
           listing: list[DriveFile]) -> None:
    """Finish anything a previous tick left in flight, before starting more.

    `resume_interrupted` puts the ids on the runner's queue expecting a worker
    thread to be reading it. There isn't one — this process does the work
    itself — so the queue is emptied by hand.

    A job whose manuscript has left the folder is parked rather than run:
    finishing it pays the model for work that can never be delivered. The
    checkpoint stays, so a manuscript that comes back resumes from what was
    already bought."""
    present = {f.id for f in listing}
    owner = {rec.job_id: fid for fid, rec in state.files.items() if rec.job_id}
    runner.resume_interrupted()
    while True:
        try:
            job_id = runner.queue.get_nowait()
        except queue.Empty:
            return
        file_id = owner.get(job_id)
        if file_id is not None and file_id not in present:
            log.info("Job %s is for a manuscript no longer in the folder; "
                     "parking it.", job_id)
            runner.store.update(job_id, state="failed",
                                error="The manuscript left the folder before "
                                      "this finished.")
            continue
        try:
            runner.run_one(job_id)
        except Exception as e:            # noqa: BLE001 - mirrors _work
            log.exception("Resuming job %s failed", job_id)
            runner.store.update(job_id, state="failed", error=str(e))
