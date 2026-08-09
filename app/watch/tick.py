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

from . import drive, folders, hubspot, naming, notify, prep
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
    # Manuscripts a person must sort out before DocProof can act — chiefly a file
    # whose author key matches more than one Project flagged ready, where guessing
    # would be worse than waiting. Kept apart from `failed` (nothing broke) and
    # from plain `waiting` (nobody need do anything) because these are the events
    # worth a notification. Each is (filename, reason).
    needs_human: list[tuple[str, str]] = field(default_factory=list)
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
    a batch — which is why today there is nothing to collect.

    When it is built: in subfolder mode the tracked-changes file must be written
    into the book's own subfolder, the same as `run_prep` — take the destination
    from the routing map `_discover` returns, not from `ws.folder_id`."""


def submit_ready(token: str, ws: WatchSettings, listing: list[DriveFile],
                 state: WatchState, store: JobStore, *, opener,
                 report: TickReport) -> None:
    """Nothing yet.

    Copy edits will be submitted here. What makes a manuscript ready is a
    question for `stages.classify`: first a subfolder an editor drops the
    developmental-edit-complete file into, later a deal stage read from
    HubSpot. Either way this slot submits the overnight batch and returns —
    the answer arrives at some later tick, in `collect_finished`.

    When it is built: in subfolder mode it reads and writes the book's own
    subfolder from the routing map `_discover` returns, never `ws.folder_id`."""


def run_prep(token: str, home: Path, ws: WatchSettings,
             listing: list[DriveFile], state: WatchState, runner: JobRunner,
             store: JobStore, *, mock: bool, opener, hs_token: str | None = None,
             routes: dict[str, str] | None = None,
             report: TickReport) -> None:
    """Prepare every manuscript nobody has prepared yet."""
    routes = routes or {}
    todo = [f for f in listing if classify(f) is Stage.NEW_MANUSCRIPT]
    todo.sort(key=lambda f: (f.modified_time, f.name))
    report.new = len(todo)

    # When HubSpot is the gate, only the books it says are ready go on. The
    # token was resolved once in `tick`, the same road the Google one takes, so
    # a test can drive the gate through the injected key reader. Subfolder mode
    # has already gated in `_discover` — every manuscript in `listing` came from
    # a ready record — so the gate is not run again over it.
    if ws.hubspot_enabled and not ws.subfolders_enabled:
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
                 mock=mock, opener=opener, hs_token=hs_token,
                 dest_folder_id=routes.get(file.id, ws.folder_id),
                 report=report)
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

    An editor flags one Project at the status property's "ready" value. Among
    thousands of Projects only a handful are ever ready at once, so the gate
    fetches that short list once and matches each new manuscript to it by author
    key — which is what makes a shared surname safe: eleven "Smith" Projects in
    the CRM, but only the one an editor flagged is ever a candidate.

    A book DocProof already started — recognised by a record id it wrote on an
    earlier tick — is carried straight through to completion without asking
    again. That is both how a long book spanning several ticks is finished and
    how a crash between the HubSpot write and the Drive marker is repaired: the
    id is enough, and `_finish_hubspot`/`mark_source` are each safe to repeat.

    Waiting is the `FolderInUse` posture, not a failure: no key, no ready
    Project, or a ready list that could not be fetched leaves the file where it
    is, with no marker, so the next tick reconsiders. A bad token is the one
    thing that dooms the whole pass, so `HubSpotAuthError` propagates. And a file
    whose key matches *two* ready Projects is nobody's to guess: it waits, the
    pass goes on, and it is recorded in `needs_human` for a person to untangle."""
    want = [p for p in (ws.hubspot_status_property, ws.hubspot_key_property)
            if p]
    # The ready list is fetched once, and only if some file still needs it: a
    # pass that is all books we already started asks HubSpot nothing here.
    ready: list | None = None             # None: not fetched, or unreachable
    if any(not state.get(f.id).hubspot_id for f in todo):
        try:
            ready = hubspot.find_by_value(
                hs_token, ws.hubspot_object, ws.hubspot_status_property,
                ws.hubspot_format_ready_value, want_properties=want,
                opener=opener)
        except HubSpotAuthError:
            raise                         # the token is bad — stop the pass
        except HubSpotError as e:         # transient — the pass's new books wait
            log.info("Waiting: could not fetch the ready Projects from HubSpot "
                     "(%s); the next run will try again.", e)
            ready = None

    eligible: list[DriveFile] = []
    for file in todo:
        rec = state.get(file.id)
        if rec.hubspot_id:                # a book we already started
            eligible.append(file)         # carry it to completion, no questions
            continue
        key = key_from_name(file.name, ws.hubspot_key_pattern)
        if not key:
            log.info("Waiting: %s carries no author key in its name.",
                     file.name)
            report.waiting += 1
            continue
        if ready is None:                 # the ready list did not land this pass
            report.waiting += 1
            continue
        matches = [r for r in ready if hubspot.name_matches(
            r.properties.get(ws.hubspot_key_property, ""), key)]
        if not matches:
            log.info("Waiting: no Project is marked '%s' for %s.",
                     ws.hubspot_format_ready_value, key)
            report.waiting += 1
            continue
        if len(matches) > 1:
            reason = (f"{len(matches)} Projects are marked "
                      f"'{ws.hubspot_format_ready_value}' for "
                      f"{ws.hubspot_key_property} '{key}', so DocProof cannot "
                      f"tell which book this file is. Fix the flags in HubSpot "
                      f"so only one is ready.")
            log.warning("Needs a person: %s (%s)", file.name, reason)
            report.needs_human.append((file.name, reason))
            report.waiting += 1
            continue
        rec.hubspot_id = matches[0].id    # written before prep, never after
        rec.name = file.name
        state.record(rec)
        eligible.append(file)
    return eligible


def _discover(token: str, hs_token: str | None, ws: WatchSettings,
              state: WatchState, *, opener, report: TickReport,
              dry_run: bool) -> tuple[list[DriveFile], dict[str, str]]:
    """Subfolder mode's answer to "what is there": ask HubSpot who is ready,
    then look only in those authors' folders.

    HubSpot drives; Drive is touched once per author with a book to do, never
    once per author. The parent may hold a thousand subfolders — a flat listing
    of it is exactly the enumeration the design forbids — so nothing here lists
    it. Each ready record names an author, `folders.resolve` turns that into one
    scoped query for one subfolder, and only that subfolder is listed.

    Returns the pass's working listing — each ready author's subfolder contents,
    manuscript and the outputs beside it, so the existing orphan and `OUTPUT`
    logic still works — and a routing map from a manuscript's id to the
    subfolder its outputs belong in. A book a previous tick already started is
    re-listed from the subfolder it recorded, so `_drain` and resume still find
    it after its status has moved off "ready"."""
    want = [p for p in (ws.hubspot_status_property, ws.hubspot_key_property,
                        ws.hubspot_first_property, ws.hubspot_last_property)
            if p]
    try:
        ready = hubspot.find_by_value(
            hs_token, ws.hubspot_object, ws.hubspot_status_property,
            ws.hubspot_format_ready_value, want_properties=want, opener=opener)
    except HubSpotAuthError:
        raise                             # the token is bad — stop the pass
    except HubSpotError as e:             # transient — the pass's books wait
        log.info("Waiting: could not fetch the ready Projects from HubSpot "
                 "(%s); the next run will try again.", e)
        ready = []

    listing: list[DriveFile] = []
    routes: dict[str, str] = {}
    cache: dict[tuple[str, str], str | None] = {}
    seen_records: set[str] = set()
    for record in ready:
        seen_records.add(record.id)
        _discover_ready(token, ws, record, state, listing, routes, cache,
                        opener=opener, report=report, dry_run=dry_run)

    # A book already in flight — its record id is on the state file and it is
    # not yet delivered — is re-listed from the folder it recorded, whether or
    # not it is still flagged ready. Without this a job spanning ticks would
    # have its manuscript read as "gone from the folder" and be parked.
    for rec in list(state.files.values()):
        if (rec.hubspot_id and rec.hubspot_id not in seen_records
                and rec.subfolder_id and rec.marked != "formatted"):
            _adopt(token, rec.subfolder_id, listing, routes, opener=opener)

    uniq = {f.id: f for f in listing}     # a subfolder seen twice, deduped
    return list(uniq.values()), routes


def _discover_ready(token: str, ws: WatchSettings, record, state: WatchState,
                    listing: list[DriveFile], routes: dict[str, str],
                    cache: dict[tuple[str, str], str | None], *, opener,
                    report: TickReport, dry_run: bool) -> None:
    """One ready record: resolve its author's folder, find the one manuscript in
    it, and route that manuscript's outputs back into the same folder.

    Never writes into a guessed folder. A missing name, a name that resolves to
    zero folders or to more than one, or a folder holding more than one new
    manuscript are each nobody's to guess: the book is left where it is and a
    person is told."""
    first = (record.properties.get(ws.hubspot_first_property) or "").strip()
    last = (record.properties.get(ws.hubspot_last_property) or "").strip()
    if not first or not last:
        reason = ("its HubSpot record has no first or last name, so DocProof "
                  "cannot tell which folder is the author's.")
        log.warning("Needs a person: record %s (%s)", record.id, reason)
        report.needs_human.append((f"HubSpot record {record.id}", reason))
        report.waiting += 1
        return

    key = (first.casefold(), last.casefold())
    if key not in cache:
        cache[key] = folders.resolve(first, last, ws.folder_id, token,
                                     opener=opener)
    subfolder_id = cache[key]
    author = folders.compose(first, last)
    if subfolder_id is None:
        reason = (f"no single folder named '{author}' is in the Author Folder, "
                  f"so DocProof will not guess where the book goes.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return

    contents = drive.list_folder(token, subfolder_id, opener=opener)
    manuscripts = [f for f in contents if classify(f) is Stage.NEW_MANUSCRIPT]
    if not manuscripts:
        log.info("Waiting: %s is flagged ready but has no new manuscript yet.",
                 author)
        report.waiting += 1
        return
    if len(manuscripts) > 1:
        reason = (f"{len(manuscripts)} new manuscripts are in {author}'s "
                  f"folder, so DocProof cannot tell which is the book to do.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return

    book = manuscripts[0]
    if not dry_run:
        rec = state.get(book.id)
        rec.name = book.name
        rec.hubspot_id = record.id        # written before prep, never after
        rec.author_first = first
        rec.author_last = last
        rec.subfolder_id = subfolder_id
        rec.subfolder_name = author
        state.record(rec)
    listing.extend(contents)              # manuscript and its outputs
    routes[book.id] = subfolder_id


def _adopt(token: str, subfolder_id: str, listing: list[DriveFile],
           routes: dict[str, str], *, opener) -> None:
    """Re-list an in-flight book's recorded subfolder so the pass still sees it.
    Route every new manuscript it holds back into it, the same as discovery."""
    contents = drive.list_folder(token, subfolder_id, opener=opener)
    for f in contents:
        if classify(f) is Stage.NEW_MANUSCRIPT:
            routes[f.id] = subfolder_id
    listing.extend(contents)


def _one(token: str, home: Path, ws: WatchSettings, file: DriveFile,
         listing: list[DriveFile], state: WatchState, runner: JobRunner,
         store: JobStore, *, mock: bool, opener, hs_token: str | None,
         dest_folder_id: str, report: TickReport) -> None:
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
        _refuse(token, ws, file, job, rec, state, listing,
                dest_folder_id=dest_folder_id, opener=opener, report=report)
        return
    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "Preparing the manuscript did not finish.")

    report.prepped.append(file.name)
    uploaded = prep.upload_outputs(token, file, job, ws, rec, state, listing,
                                   dest_folder_id=dest_folder_id, opener=opener)
    report.uploaded.extend(uploaded)

    _finish_hubspot(hs_token, ws, file, rec, state, uploaded, opener=opener)

    prep.mark_source(token, file, job, rec, state, opener=opener)

    # The book is in the folder, marked, and moved on in HubSpot: a pass that
    # was asked to says so, once, with the whole log. Best-effort like the
    # needs-a-person mail — a send that fails is logged, never raised, so an
    # email server never undoes finished work.
    if ws.notify_on_complete and ws.notify_email:
        notify.maybe_complete(token, ws, job, file, rec, uploaded,
                              dest_folder_id, opener=opener)


def _finish_hubspot(hs_token: str | None, ws: WatchSettings, file: DriveFile,
                    rec, state: WatchState, uploaded: list[str], *,
                    opener) -> None:
    """Move the status property to its "done" value on the book we just put back.

    Between `upload_outputs` and `mark_source` on purpose, and the local record
    is written *before* the Drive marker: with the marker last, a file that
    reads `formatted` in Drive was `done` in HubSpot first, so the two can never
    disagree in the direction that would strand a book as done-but-unmarked.

    Writing a value the property already holds is harmless, which is what makes
    the whole step safe to repeat when a later tick finishes an interrupted one.

    In read-only mode nothing is written: the book was still gated on HubSpot and
    is still marked done in Drive, so it is not prepared twice — the CRM simply
    keeps whatever value it had."""
    if not (ws.hubspot_enabled and rec.hubspot_id and not rec.hubspot_done):
        return
    if not ws.hubspot_write_back:
        log.info("HubSpot is read-only: leaving %s at its current status.",
                 file.name)
        return
    props = {ws.hubspot_status_property: ws.hubspot_format_done_value}
    if ws.hubspot_output_property:
        name = _output_name(uploaded) or _output_name(list(rec.uploaded))
        if name:
            props[ws.hubspot_output_property] = name
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.hubspot_id, props,
                           opener=opener)
    rec.hubspot_done = True
    state.record(rec)                     # recorded before the Drive marker


def _output_name(names: list[str]) -> str:
    """The deliverable to name in HubSpot: the InDesign-ready file, which is the
    one uploaded under the bare base name — not the "- tracked changes" copy nor
    the "- notes". Chosen by suffix rather than by position so `prep_output=both`
    does not name the redline."""
    docs = [n for n in names if n.lower().endswith(".docx")
            and naming.TRACKED_SUFFIX.lower() not in n.lower()]
    if docs:
        return docs[0]
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
            state: WatchState, listing: list[DriveFile], *, dest_folder_id: str,
            opener, report: TickReport) -> None:
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
                new_id = drive.upload(token, dest_folder_id, note,
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
            ("hubspot_status_property", ws.hubspot_status_property),
            ("hubspot_format_ready_value", ws.hubspot_format_ready_value),
            ("hubspot_format_done_value", ws.hubspot_format_done_value),
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

    if ws.subfolders_enabled:
        # Routing into per-author subfolders needs a name to route by, and that
        # name comes from HubSpot — so subfolder mode without HubSpot, or
        # without the two name properties, is refused rather than left to guess
        # folders from filenames.
        if not ws.hubspot_enabled:
            raise NotConfigured(
                "Subfolders are switched on but HubSpot is not. The author's "
                "folder is named from the CRM record, so HubSpot has to be on. "
                "Run `docproof-watch init` to set it up, or turn subfolders off.")
        missing = [name for name, value in (
            ("hubspot_first_property", ws.hubspot_first_property),
            ("hubspot_last_property", ws.hubspot_last_property),
        ) if not value]
        if missing:
            raise NotConfigured(
                "Subfolders are switched on but " + ", ".join(missing)
                + " is not set — DocProof needs the author's name to find the "
                "folder. Run `docproof-watch init` to fill it in.")

    if not dry_run:
        # Stamped before the first network call, not after it — see
        # state.note_tick. Two clocks ask when this last happened, and a pass
        # that dies refreshing its token must still count as a look, or the
        # in-app clock retries it every minute instead of every
        # tick_every_minutes. A dry run stays read-only, stamp included.
        note_tick(root)

    token = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh,
                                       opener=opener)

    # The state file and the HubSpot token are read before the listing because
    # subfolder mode's `_discover` needs both to decide what to list at all: a
    # dry run reads them the same way, and both reads are read-only, so nothing
    # below the dry-run return has changed anything.
    state = WatchState.load(root / STATE_FILE)
    hs_token = ((get_key or get_api_key)(HUBSPOT_KEY)
                if ws.hubspot_enabled else None)

    if ws.subfolders_enabled:
        # HubSpot-first: the parent Author Folder is never listed. `_discover`
        # asks who is ready and looks only in those authors' folders, handing
        # back the same shape of listing the flat path builds plus a map of
        # where each book's outputs belong.
        listing, routes = _discover(token, hs_token, ws, state, opener=opener,
                                    report=report, dry_run=dry_run)
    else:
        listing = drive.list_folder(token, ws.folder_id, opener=opener)
        routes = {}
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
    if not mock:
        # A rehearsal leaves real leftovers alone: _drain finishes whatever a
        # dead pass left mid-flight through the real provider, and
        # `--mock-tags` is documented as costing nothing.
        _drain(runner, state, listing)

    collect_finished(token, ws, listing, state, store, opener=opener,
                     report=report)
    submit_ready(token, ws, listing, state, store, opener=opener, report=report)
    run_prep(token, root, ws, listing, state, runner, store, mock=mock,
             opener=opener, hs_token=hs_token, routes=routes, report=report)

    # Last, and on the same Google token the folder was read with: a pass that
    # left something for a person says so, once, by email. Best-effort — see
    # notify.maybe_notify — so the work above is never undone by a mail server.
    notify.maybe_notify(token, ws, report, opener=opener)
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
