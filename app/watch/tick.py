"""One pass over the watched folder.

A tick is a series of slots run in order — collect what finished, submit what is
ready, prepare what is new, proofread what is flagged for it, then promo and the
marketing plan. The first two do nothing yet. They are not placeholders in the
apologetic sense: they are the order this has to happen in, written down now,
while the reason is obvious.

The order matters because it is the order that costs least. Collecting first
means work paid for last night is in the folder before anything new is
started; submitting before preparing means an overnight batch is with the
vendor while the synchronous work runs, rather than an hour behind it.

Each stage owns one value of the CRM's status dropdown and one Drive marker of
its own, so a book is only ever in one of them at a time and none of them can
read or overwrite another's "done". Formatting and proofing go further and each
get their own *listing*: `run_prep` works every candidate in its listing without
gating again, so a book flagged for proofing must never appear in it.

Nothing here runs on a clock of its own. `launchd` decides when a tick
happens, the tick does what it finds, and the process exits.
"""
from __future__ import annotations

import logging
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, get_api_key, resource_root

from . import drive, folders, hubspot, naming, notify, plan, prep, promo, proof
from .drive import DriveError, DriveFile
from .hubspot import HubSpotAuthError, HubSpotError
from .keys import key_from_name
from .settings import GOOGLE_KEY, HUBSPOT_KEY, WatchSettings
from .stages import (FORMATTED, JOB_PROP, OUTPUT_PROP, PLAN_DONE, PLAN_FAILED,
                     PLAN_PENDING, PROMO_DONE, PROMO_FAILED, PROMO_PENDING,
                     PROOF_AWAITING, PROOF_DONE, PROOF_FAILED, PROOF_HUMAN,
                     PROOF_PROP, PROOF_TERMINAL, SOURCE_PROP, Stage, classify,
                     is_plan_candidate, is_promo_candidate, is_proof_candidate)
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
    # Books promo wrote copy for this pass, kept apart from `prepped` (which is
    # formatting) so a pass can say which stage did what.
    promoted: list[str] = field(default_factory=list)
    # Books the marketing-plan stage wrote a plan for this pass. Its own list
    # again, so a pass reports the three stages separately. (Not to be confused
    # with `plan` below, which is the file-by-file classification a dry run
    # returns.)
    planned: list[str] = field(default_factory=list)
    # Books the proofing stage delivered a proofread for this pass — a run the
    # app finished, or an external practitioner's hand-off DocWatch picked up
    # and acted on. Its own list again, so a pass reports each stage separately.
    proofed: list[str] = field(default_factory=list)
    # Books handed to an external practitioner and not answered yet: discovered
    # at "Ready for Proofing", recorded, and waiting for the hand-off files to
    # appear in the author's folder. Nothing is wrong and nothing failed — but a
    # person is expected to act, so it earns a status line and the owner email
    # the same way `missing_source` does. Each is (book, reason).
    awaiting_proof: list[tuple[str, str]] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    # Manuscripts a person must sort out before DocProof can act — chiefly a file
    # whose author key matches more than one Project flagged ready, where guessing
    # would be worse than waiting. Kept apart from `failed` (nothing broke) and
    # from plain `waiting` (nobody need do anything) because these are the events
    # worth a notification. Each is (filename, reason).
    needs_human: list[tuple[str, str]] = field(default_factory=list)
    # Authors HubSpot flagged ready whose folder holds no Book Original to
    # prepare — the folder is empty, or the files in it are drafts and reviews,
    # never "<surname> - Book Original". Its own list, kept apart from
    # `needs_human` (nothing is ambiguous, a file simply needs uploading or
    # renaming) and from plain `waiting` (this one is worth an email, by request):
    # a ready author DocProof cannot act on is a gap a person wants told about.
    # Each is (author, reason).
    missing_source: list[tuple[str, str]] = field(default_factory=list)
    # Authors HubSpot flagged ready whose book is already formatted (the intake
    # file is present and marked done) but whose status never moved off ready — a
    # write-back that did not land, or a read-only run. Its own list: nothing is
    # missing and nothing failed, but a person may want to move the CRM on, so it
    # rides the same alert email. Each is (author, reason).
    stuck_ready: list[tuple[str, str]] = field(default_factory=list)
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

    # The house convention as a hard rule: with the label required, only
    # "<surname> - Book Original" is the book to prepare. Subfolder mode already
    # applied this in `_discover_ready`, where the surname is known from the
    # ready record; here in the flat path the surname is not known yet, so the
    # gate is the surname-free token — enough to leave a developmental review or
    # a questionnaire dropped in the folder alone rather than format it. Without
    # this, the switch silently did nothing outside subfolder mode.
    if ws.require_source_label and not ws.subfolders_enabled:
        labelled = [f for f in todo if naming.has_source_label(f.name)]
        left = len(todo) - len(labelled)
        if left:
            log.info("require_source_label: %d file(s) not named "
                     "'<surname> - Book Original' left alone this pass.", left)
        todo = labelled

    todo.sort(key=lambda f: (f.modified_time, f.name))
    report.new = len(todo)

    # When HubSpot is the gate, only the books it says are ready go on. The
    # token was resolved once in `tick`, the same road the Google one takes, so
    # a test can drive the gate through the injected key reader. Subfolder mode
    # has already gated in `_discover` — every manuscript in `listing` came from
    # a ready record — so the gate is not run again over it.
    if ws.hubspot_enabled and not ws.subfolders_enabled:
        todo = _gate_hubspot(hs_token, ws, todo, state,
                             ready_value=ws.hubspot_format_ready_value,
                             id_get=lambda r: r.hubspot_id,
                             id_set=lambda r, v: setattr(r, "hubspot_id", v),
                             opener=opener, report=report)

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
                  state: WatchState, *, ready_value: str, id_get, id_set,
                  opener, report: TickReport, status_property: str | None = None,
                  want_extra: tuple[str, ...] = (),
                  on_match=None) -> list[DriveFile]:
    """Keep only the manuscripts HubSpot says to work on, for one stage.

    An editor flags one Project at the gate property's "ready" value. Among
    thousands of Projects only a handful are ever ready at once, so the gate
    fetches that short list once and matches each new manuscript to it by author
    key — which is what makes a shared surname safe: eleven "Smith" Projects in
    the CRM, but only the one an editor flagged is ever a candidate.

    The stage is what `ready_value`, the `id_get`/`id_set` pair, and the gate
    property select: formatting and promo read the shared status dropdown
    (`hubspot_status_property`, the default) at their own value and store the
    match on `hubspot_id` / `promo_hubspot_id`; the marketing plan passes
    `status_property=hubspot_plan_property` — a *separate* property — and uses
    `plan_hubspot_id`. `want_extra` names any further properties to fetch on the
    matched record (the plan asks for the pen name here). The key property is the
    same across stages: one book, one row in the CRM.

    A book DocProof already started — recognised by a record id it wrote on an
    earlier tick — is carried straight through to completion without asking
    again. That is both how a long book spanning several ticks is finished and
    how a crash between the HubSpot write and the Drive marker is repaired: the
    id is enough, and the finish/mark steps are each safe to repeat.

    Waiting is the `FolderInUse` posture, not a failure: no key, no ready
    Project, or a ready list that could not be fetched leaves the file where it
    is, with no marker, so the next tick reconsiders. A bad token is the one
    thing that dooms the whole pass, so `HubSpotAuthError` propagates. And a file
    whose key matches *two* ready Projects is nobody's to guess: it waits, the
    pass goes on, and it is recorded in `needs_human` for a person to untangle."""
    prop = status_property or ws.hubspot_status_property
    want = [p for p in (prop, ws.hubspot_key_property, *want_extra) if p]
    # The ready list is fetched once, and only if some file still needs it: a
    # pass that is all books we already started asks HubSpot nothing here.
    ready: list | None = None             # None: not fetched, or unreachable
    if any(not id_get(state.get(f.id)) for f in todo):
        try:
            ready = hubspot.find_by_value(
                hs_token, ws.hubspot_object, prop,
                ready_value, want_properties=want, opener=opener)
        except HubSpotAuthError:
            raise                         # the token is bad — stop the pass
        except HubSpotError as e:         # transient — the pass's new books wait
            log.info("Waiting: could not fetch the ready Projects from HubSpot "
                     "(%s); the next run will try again.", e)
            ready = None

    eligible: list[DriveFile] = []
    for file in todo:
        rec = state.get(file.id)
        if id_get(rec):                   # a book we already started
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
                     ready_value, key)
            report.waiting += 1
            continue
        if len(matches) > 1:
            reason = (f"{len(matches)} Projects are marked '{ready_value}' for "
                      f"{ws.hubspot_key_property} '{key}', so DocProof cannot "
                      f"tell which book this file is. Fix the flags in HubSpot "
                      f"so only one is ready.")
            log.warning("Needs a person: %s (%s)", file.name, reason)
            report.needs_human.append((file.name, reason))
            report.waiting += 1
            continue
        id_set(rec, matches[0].id)        # written before work, never after
        if on_match is not None:
            # A hook for a stage that needs a field off the matched record the
            # others do not — the plan captures the pen name here, so it is
            # persisted with the id and no second HubSpot call is needed.
            on_match(rec, matches[0])
        rec.name = file.name
        state.record(rec)
        eligible.append(file)
    return eligible


# --- promo --------------------------------------------------------------------

def run_promo(token: str, home: Path, ws: WatchSettings,
              listing: list[DriveFile], state: WatchState, runner: JobRunner,
              store: JobStore, *, mock: bool, opener, hs_token: str | None,
              report: TickReport) -> None:
    """Write promo copy for every book HubSpot flagged for it, and deliver any
    a person has since approved.

    Two steps, in the tick's collect-then-start order: first ship anything a
    hold-mode run generated on an earlier pass and a person has now approved,
    then generate copy for newly-ready books. Independent of formatting end to
    end — its own marker, its own state, its own HubSpot value — so it neither
    reads nor writes anything the format stage owns."""
    if not ws.promo_enabled:
        return
    if ws.subfolders_enabled:
        # Flat-folder only for now: routing promo outputs into per-author
        # subfolders is not wired yet, so rather than guess a destination the
        # stage stands aside and says so, once, in the log.
        log.info("Promo is on but so is subfolder mode; the promo stage is "
                 "flat-folder only for now and stands aside this pass.")
        return

    _deliver_approved_promo(token, ws, listing, state, store, hs_token,
                            opener=opener, report=report)

    todo = [f for f in listing if is_promo_candidate(f)]
    todo.sort(key=lambda f: (f.modified_time, f.name))
    todo = _gate_hubspot(hs_token, ws, todo, state,
                         ready_value=ws.hubspot_promo_ready_value,
                         id_get=lambda r: r.promo_hubspot_id,
                         id_set=lambda r, v: setattr(r, "promo_hubspot_id", v),
                         opener=opener, report=report)
    if len(todo) > ws.max_files_per_tick:
        report.deferred += len(todo) - ws.max_files_per_tick
        log.info("%d books are waiting for promo; writing %d this run and "
                 "leaving the rest.", len(todo), ws.max_files_per_tick)
        todo = todo[:ws.max_files_per_tick]

    for file in todo:
        try:
            _one_promo(token, home, ws, file, listing, state, runner, store,
                       mock=mock, opener=opener, hs_token=hs_token,
                       dest_folder_id=ws.folder_id, report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Could not write promo for %s", file.name)
            report.failed.append((file.name, str(e)))
            rec = state.get(file.id)
            rec.name = file.name
            rec.promo_attempts += 1
            state.record(rec)


def _one_promo(token: str, home: Path, ws: WatchSettings, file: DriveFile,
               listing: list[DriveFile], state: WatchState, runner: JobRunner,
               store: JobStore, *, mock: bool, opener, hs_token: str | None,
               dest_folder_id: str, report: TickReport) -> None:
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time

    job = store.get(rec.promo_job_id) if rec.promo_job_id else None
    paid_for = (job is not None and job.state == "done" and job.results_dir
                and Path(job.results_dir).is_dir())

    if rec.promo_attempts >= ws.max_attempts and not paid_for:
        # The same three-strikes rule the format stage keeps: a book that failed
        # the same way on three separate runs stops being tried and starts being
        # visible. A generated job is exempt — the copy exists, only delivery
        # failed, which is free to retry.
        reason = f"Gave up after {rec.promo_attempts} attempts."
        log.error("%s: %s", file.name, reason)
        promo.mark_source(token, file, rec, state, status=PROMO_FAILED,
                          reason=reason, opener=opener)
        report.failed.append((file.name, reason))
        return

    job = _prepare_promo(token, home, ws, file, rec, state, runner, store,
                         mock=mock, opener=opener)

    if job.state == "failed" and job.error_kind == "oversize":
        # Too big for one pass. Retrying will not change the size, and the model
        # was never called, so this is not a transient failure to count against
        # the book. Email a person the numbers and how to run it by hand, mark it
        # so the next tick leaves it be, and stop.
        notify.promo_too_large(token, ws, file, job, opener=opener)
        promo.mark_source(token, file, rec, state, status=PROMO_FAILED,
                          reason="Too big for one-pass promo; run it by hand.",
                          opener=opener)
        log.info("Promo skipped for %s: over the single-pass limit; emailed a "
                 "person to run it by hand.", file.name)
        return

    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "Writing the promo copy did not finish.")

    report.promoted.append(file.name)

    if ws.promo_auto_upload:
        _deliver_promo(token, ws, file, job, rec, state, listing, hs_token,
                       dest_folder_id=dest_folder_id, opener=opener,
                       report=report)
    else:
        # Hold: the copy is generated and waiting. Mark the book `pending` so
        # the next tick doesn't rewrite it, and leave the two .docx for a person
        # to approve in the panel. Delivery happens once approval lands — the
        # panel may do it, or `_deliver_approved_promo` does on a later tick.
        promo.mark_source(token, file, rec, state, status=PROMO_PENDING,
                          opener=opener)
        log.info("Promo copy for %s is written and waiting for approval.",
                 file.name)


def _prepare_promo(token: str, home: Path, ws: WatchSettings, file: DriveFile,
                   rec, state: WatchState, runner: JobRunner, store: JobStore, *,
                   mock: bool, opener) -> Job:
    """The manuscript's promo job, run or resumed. The shortcut is the point of
    the state file: a job that already wrote its copy is not run again, because
    running it again means paying a model to read the whole novel twice."""
    existing = store.get(rec.promo_job_id) if rec.promo_job_id else None
    if existing is not None:
        finished = (existing.state == "done" and existing.results_dir
                    and Path(existing.results_dir).is_dir())
        if finished:
            log.info("%s already has promo copy; picking up from there.",
                     file.name)
            return existing
        existing.state = "queued"
        existing.error = None
        return promo.run_job(runner, store, existing, mock=mock)

    local = promo.fetch(token, file, home / DOWNLOADS / file.id, opener=opener)
    job = promo.make_job(local, ws)
    rec.promo_job_id = job.id
    state.record(rec)              # before the model is called, never after
    return promo.run_job(runner, store, job, mock=mock)


def _deliver_promo(token: str, ws: WatchSettings, file: DriveFile, job: Job,
                   rec, state: WatchState, listing: list[DriveFile],
                   hs_token: str | None, *, dest_folder_id: str, opener,
                   report: TickReport) -> None:
    """Ship a generated book: the two .docx to the folder, the status property
    to its done value, and the `done` marker last. Safe to repeat — every upload
    is recorded, a writeback of a value already set is a no-op, and the marker is
    written after both — so it does not matter whether this runs from an auto
    tick, a later approving tick, or the panel."""
    uploaded = promo.upload_outputs(token, file, job, ws, rec, state, listing,
                                    dest_folder_id=dest_folder_id, opener=opener)
    report.uploaded.extend(uploaded)
    _finish_hubspot_promo(hs_token, ws, file, rec, state, opener=opener)
    promo.mark_source(token, file, rec, state, status=PROMO_DONE, opener=opener)


def _deliver_approved_promo(token: str, ws: WatchSettings,
                            listing: list[DriveFile], state: WatchState,
                            store: JobStore, hs_token: str | None, *, opener,
                            report: TickReport) -> None:
    """Ship any book a hold-mode run generated earlier and a person has since
    approved. Idempotent: a book the panel already delivered reads `done` in
    Drive and is out of `is_promo_candidate`, and its record is no longer
    `pending` here."""
    by_id = {f.id: f for f in listing}
    for rec in list(state.files.values()):
        if rec.promo_marked != "pending" or not rec.promo_job_id:
            continue
        job = store.get(rec.promo_job_id)
        if job is None or job.approval != "approved" or job.state != "done":
            continue
        file = by_id.get(rec.file_id)
        if file is None:
            continue                      # the manuscript left the folder
        try:
            _deliver_promo(token, ws, file, job, rec, state, listing, hs_token,
                           dest_folder_id=ws.folder_id, opener=opener,
                           report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Delivering approved promo for %s failed", file.name)
            report.failed.append((file.name, str(e)))


def _finish_hubspot_promo(hs_token: str | None, ws: WatchSettings,
                          file: DriveFile, rec, state: WatchState, *,
                          opener) -> None:
    """Move the status property to its promo done value on the book just shipped.

    The promo twin of `_finish_hubspot`, on promo's own record id and value, with
    the same guards: read-only mode leaves the CRM untouched, and a blank done
    value is refused rather than blanking the property. Writing a value already
    set is harmless, which is what makes delivery safe to repeat."""
    if not (ws.hubspot_enabled and rec.promo_hubspot_id
            and not rec.promo_hubspot_done):
        return
    if not ws.hubspot_write_back:
        log.info("HubSpot is read-only: leaving %s at its current status.",
                 file.name)
        return
    if not ws.hubspot_promo_done_value:
        log.warning("HubSpot promo done-value is empty: leaving %s at its "
                    "current status rather than blanking it.", file.name)
        return
    props = {ws.hubspot_status_property: ws.hubspot_promo_done_value}
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.promo_hubspot_id,
                           props, allow={ws.hubspot_status_property},
                           opener=opener)
    rec.promo_hubspot_done = True
    state.record(rec)


# --- marketing plan -----------------------------------------------------------

def run_plans(token: str, home: Path, ws: WatchSettings,
              listing: list[DriveFile], state: WatchState, runner: JobRunner,
              store: JobStore, *, mock: bool, opener, hs_token: str | None,
              report: TickReport) -> None:
    """Write a marketing plan for every book HubSpot flagged for it, and deliver
    any a person has since approved.

    The plan twin of `run_promo`, on its own HubSpot property (the "Marketing
    Plan" field the press flips Needed -> Uploaded) and its own Drive marker, so
    it neither reads nor writes anything the format or copy stages own. A book
    can be at any stage of formatting or promo and still get a plan — they gate
    on different values. Flat-folder only for now, standing aside in subfolder
    mode the same way promo does."""
    if not ws.plan_enabled:
        return
    if ws.subfolders_enabled:
        log.info("Marketing plans are on but so is subfolder mode; the plan "
                 "stage is flat-folder only for now and stands aside this pass.")
        return

    _deliver_approved_plan(token, ws, listing, state, store, hs_token,
                           opener=opener, report=report)

    todo = [f for f in listing if is_plan_candidate(f)]
    todo.sort(key=lambda f: (f.modified_time, f.name))

    def _capture_pen(rec, record) -> None:
        if ws.hubspot_pen_property:
            rec.plan_pen = (record.properties.get(ws.hubspot_pen_property)
                            or "").strip()

    todo = _gate_hubspot(hs_token, ws, todo, state,
                         ready_value=ws.hubspot_plan_needed_value,
                         id_get=lambda r: r.plan_hubspot_id,
                         id_set=lambda r, v: setattr(r, "plan_hubspot_id", v),
                         opener=opener, report=report,
                         status_property=ws.hubspot_plan_property,
                         want_extra=(ws.hubspot_pen_property,),
                         on_match=_capture_pen)
    if len(todo) > ws.max_files_per_tick:
        report.deferred += len(todo) - ws.max_files_per_tick
        log.info("%d books are waiting for a marketing plan; writing %d this "
                 "run and leaving the rest.", len(todo), ws.max_files_per_tick)
        todo = todo[:ws.max_files_per_tick]

    for file in todo:
        try:
            _one_plan(token, home, ws, file, listing, state, runner, store,
                      mock=mock, opener=opener, hs_token=hs_token,
                      dest_folder_id=ws.folder_id, report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Could not write a marketing plan for %s", file.name)
            report.failed.append((file.name, str(e)))
            rec = state.get(file.id)
            rec.name = file.name
            rec.plan_attempts += 1
            state.record(rec)


def _one_plan(token: str, home: Path, ws: WatchSettings, file: DriveFile,
              listing: list[DriveFile], state: WatchState, runner: JobRunner,
              store: JobStore, *, mock: bool, opener, hs_token: str | None,
              dest_folder_id: str, report: TickReport) -> None:
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time

    job = store.get(rec.plan_job_id) if rec.plan_job_id else None
    paid_for = (job is not None and job.state == "done" and job.results_dir
                and Path(job.results_dir).is_dir())

    if rec.plan_attempts >= ws.max_attempts and not paid_for:
        # The same three-strikes rule the other stages keep. A generated job is
        # exempt — the plan exists, only delivery failed, which is free to retry.
        reason = f"Gave up after {rec.plan_attempts} attempts."
        log.error("%s: %s", file.name, reason)
        plan.mark_source(token, file, rec, state, status=PLAN_FAILED,
                         reason=reason, opener=opener)
        report.failed.append((file.name, reason))
        return

    job = _prepare_plan(token, home, ws, file, listing, rec, state, runner,
                        store, mock=mock, opener=opener)

    if job.state == "failed" and job.error_kind == "oversize":
        # Too big for one pass; retrying will not change the size and the model
        # was never called. Email a person and mark it so the next tick leaves it.
        notify.plan_too_large(token, ws, file, job, opener=opener)
        plan.mark_source(token, file, rec, state, status=PLAN_FAILED,
                         reason="Too big for a one-pass plan; run it by hand.",
                         opener=opener)
        log.info("Marketing plan skipped for %s: over the single-pass limit; "
                 "emailed a person to run it by hand.", file.name)
        return

    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "Writing the marketing plan did not finish.")

    report.planned.append(file.name)

    if ws.plan_auto_upload:
        _deliver_plan(token, ws, file, job, rec, state, listing, hs_token,
                      dest_folder_id=dest_folder_id, opener=opener,
                      report=report)
    else:
        # Hold: the plan is written and waiting. Mark the book `pending` so the
        # next tick doesn't rewrite it, and leave the .docx for a person to
        # approve in the panel; delivery happens once approval lands.
        plan.mark_source(token, file, rec, state, status=PLAN_PENDING,
                         opener=opener)
        log.info("Marketing plan for %s is written and waiting for approval.",
                 file.name)


def _prepare_plan(token: str, home: Path, ws: WatchSettings, file: DriveFile,
                  listing: list[DriveFile], rec, state: WatchState,
                  runner: JobRunner, store: JobStore, *, mock: bool,
                  opener) -> Job:
    """The manuscript's plan job, run or resumed. Like promo's, the shortcut is
    the point of the state file — a job that already wrote its plan is not run
    again. The folder inputs (blurbs, questionnaire) and the pen name are read
    only when the job is first created; a resume reuses what was baked in, so a
    crash mid-flight never re-reads the folder or re-pays the model."""
    existing = store.get(rec.plan_job_id) if rec.plan_job_id else None
    if existing is not None:
        finished = (existing.state == "done" and existing.results_dir
                    and Path(existing.results_dir).is_dir())
        if finished:
            log.info("%s already has a marketing plan; picking up from there.",
                     file.name)
            return existing
        existing.state = "queued"
        existing.error = None
        return plan.run_job(runner, store, existing, mock=mock)

    dest = home / DOWNLOADS / file.id
    local = plan.fetch(token, file, dest, opener=opener)
    inputs = plan.gather_inputs(token, file, listing, ws, dest, opener=opener)
    job = plan.make_job(local, ws, pen_name=rec.plan_pen, blurbs=inputs.blurbs,
                        questionnaire=inputs.questionnaire)
    rec.plan_job_id = job.id
    state.record(rec)              # before the model is called, never after
    return plan.run_job(runner, store, job, mock=mock)


def _deliver_plan(token: str, ws: WatchSettings, file: DriveFile, job: Job,
                  rec, state: WatchState, listing: list[DriveFile],
                  hs_token: str | None, *, dest_folder_id: str, opener,
                  report: TickReport) -> None:
    """Ship a generated plan: the .docx to the folder, the Marketing Plan
    property to its Uploaded value, and the `done` marker last. Safe to repeat,
    exactly like promo's delivery — every upload is recorded, a writeback of a
    value already set is a no-op, and the marker is written after both."""
    uploaded = plan.upload_outputs(token, file, job, ws, rec, state, listing,
                                   dest_folder_id=dest_folder_id, opener=opener)
    report.uploaded.extend(uploaded)
    _finish_hubspot_plan(hs_token, ws, file, rec, state, opener=opener)
    plan.mark_source(token, file, rec, state, status=PLAN_DONE, opener=opener)


def _deliver_approved_plan(token: str, ws: WatchSettings,
                           listing: list[DriveFile], state: WatchState,
                           store: JobStore, hs_token: str | None, *, opener,
                           report: TickReport) -> None:
    """Ship any plan a hold-mode run generated earlier and a person has since
    approved. Idempotent, the promo twin: a plan already delivered reads `done`
    in Drive, is out of `is_plan_candidate`, and its record is no longer
    `pending` here."""
    by_id = {f.id: f for f in listing}
    for rec in list(state.files.values()):
        if rec.plan_marked != "pending" or not rec.plan_job_id:
            continue
        job = store.get(rec.plan_job_id)
        if job is None or job.approval != "approved" or job.state != "done":
            continue
        file = by_id.get(rec.file_id)
        if file is None:
            continue                      # the manuscript left the folder
        try:
            _deliver_plan(token, ws, file, job, rec, state, listing, hs_token,
                          dest_folder_id=ws.folder_id, opener=opener,
                          report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Delivering approved plan for %s failed", file.name)
            report.failed.append((file.name, str(e)))


def _finish_hubspot_plan(hs_token: str | None, ws: WatchSettings,
                         file: DriveFile, rec, state: WatchState, *,
                         opener) -> None:
    """Move the Marketing Plan property to its Uploaded value on the book just
    shipped. The plan twin of `_finish_hubspot_promo`, on the plan's own property
    and value, with the same guards: read-only mode leaves the CRM untouched, and
    a blank done value is refused rather than blanking the property. Writing a
    value already set is harmless, which is what makes delivery safe to repeat."""
    if not (ws.hubspot_enabled and rec.plan_hubspot_id
            and not rec.plan_hubspot_done):
        return
    if not ws.hubspot_write_back:
        log.info("HubSpot is read-only: leaving %s at its current plan status.",
                 file.name)
        return
    if not ws.hubspot_plan_done_value:
        log.warning("HubSpot plan done-value is empty: leaving %s at its current "
                    "plan status rather than blanking it.", file.name)
        return
    props = {ws.hubspot_plan_property: ws.hubspot_plan_done_value}
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.plan_hubspot_id,
                           props, allow={ws.hubspot_plan_property},
                           opener=opener)
    rec.plan_hubspot_done = True
    state.record(rec)


# --- proofing (Galley) --------------------------------------------------------
#
# The second pass over a book, on the same status dropdown as formatting moved
# to its own value pair: "Ready for Proofing" -> "Proofing Complete". Scope is
# the mechanical proofread — the review ladder, its sweeps and verify — and
# nothing else; copy-edit flights and the merge desk are not wired here.
#
# Two runners, one contract. `proof_runner="app"` runs the read here, through
# the app's galley job. `proof_runner="external"` runs nothing and waits for the
# Mac-side practitioner to leave the four hand-off files in the author's folder
# (see `proof.hand_off_names`). Either way the pass ends at the same place: read
# the verdict, and act on it.
#
# The verdict is the only thing that decides the CRM write, and both verdicts
# write: `done` moves the property to "Proofing Complete", `needs_human` to
# "Needs Human PR" — the option that puts the book in front of a human
# proofreader. Exactly one PATCH per book either way. A book never sits at
# "Ready for Proofing" after a verdict, because that is the state nobody would
# notice; `needs_human` also rides the needs-a-person email, because the CRM
# value alone does not say why.

def run_proof(token: str, home: Path, ws: WatchSettings,
              listing: list[DriveFile], state: WatchState, runner: JobRunner,
              store: JobStore, *, mock: bool, opener, hs_token: str | None,
              routes: dict[str, str] | None = None,
              report: TickReport) -> None:
    """Proofread every book HubSpot flagged for it, and apply any verdict that
    has since landed.

    In subfolder mode `listing` is proofing's own discovery — the folders of the
    authors flagged "Ready for Proofing" — already gated, so the gate is not run
    again over it. In flat mode it is the shared folder listing and the gate
    runs here, the way promo's and the plan's do."""
    if not ws.proofing_enabled:
        return
    routes = routes or {}

    # The house convention is not optional here, `require_source_label` or not:
    # `is_proof_candidate` accepts only "<surname> - Book 1", the dev-edited
    # book. Proofing reads a whole novel at a novel's price, so which file it
    # reads is a name and never a guess — a draft or the author's original in
    # the same folder is left alone. In subfolder mode `_discover_ready` has
    # already tied that name to the ready record's own surname.
    todo = [f for f in listing if is_proof_candidate(f)]
    todo.sort(key=lambda f: (f.modified_time, f.name))

    if not ws.subfolders_enabled:
        todo = _gate_hubspot(hs_token, ws, todo, state,
                             ready_value=ws.hubspot_proof_ready_value,
                             id_get=lambda r: r.proof_hubspot_id,
                             id_set=lambda r, v: setattr(r, "proof_hubspot_id",
                                                         v),
                             opener=opener, report=report)

    if len(todo) > ws.max_files_per_tick:
        report.deferred += len(todo) - ws.max_files_per_tick
        log.info("%d books are waiting to be proofread; doing %d this run and "
                 "leaving the rest.", len(todo), ws.max_files_per_tick)
        todo = todo[:ws.max_files_per_tick]

    for file in todo:
        try:
            _one_proof(token, home, ws, file, listing, state, runner, store,
                       mock=mock, opener=opener, hs_token=hs_token,
                       dest_folder_id=routes.get(file.id, ws.folder_id),
                       report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Could not proofread %s", file.name)
            report.failed.append((file.name, str(e)))
            rec = state.get(file.id)
            rec.name = file.name
            rec.proof_attempts += 1
            state.record(rec)


def _one_proof(token: str, home: Path, ws: WatchSettings, file: DriveFile,
               listing: list[DriveFile], state: WatchState, runner: JobRunner,
               store: JobStore, *, mock: bool, opener, hs_token: str | None,
               dest_folder_id: str, report: TickReport) -> None:
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time

    # A verdict already in the folder wins, whoever put it there. That is how
    # `external` mode finishes, and it is also the repair for an `app` run that
    # uploaded its outcome and then died before the CRM write: the file is the
    # durable record, so the next tick reads it rather than re-reading the book.
    #
    # Looked for in the book's OWN folder, not in the pass's whole listing. In
    # subfolder mode that listing is every ready author's folder at once, and
    # two authors sharing a surname — "John Smith" and "Jane Smith", each with a
    # "Smith - Book Original" — would produce two files of the same name. One
    # scoped listing costs one request and cannot pick the wrong book's verdict.
    folder_files = (listing if not ws.subfolders_enabled
                    else drive.list_folder(token, dest_folder_id, opener=opener))
    landed = proof.outcome_in_folder(folder_files, file.name)
    if landed is not None and rec.proof_marked not in PROOF_TERMINAL:
        verdict = proof.read_outcome(token, landed, opener=opener)
        if verdict is not None:
            report.proofed.append(file.name)
            _apply_proof_outcome(hs_token, token, ws, file, rec, state, verdict,
                                 opener=opener, report=report)
            return

    if ws.proof_runner == "external":
        _await_external_proof(token, ws, file, rec, state,
                              dest_folder_id=dest_folder_id, opener=opener,
                              report=report)
        return

    if mock:
        # A rehearsal costs nothing by definition, and there is no version of a
        # galley wave loop that both exercises the round trip and is free.
        log.info("Mock pass: leaving the proofread of %s for a real run.",
                 file.name)
        return

    job = store.get(rec.proof_job_id) if rec.proof_job_id else None
    paid_for = (job is not None and job.state == "done" and job.results_dir
                and Path(job.results_dir).is_dir())

    if rec.proof_attempts >= ws.max_attempts and not paid_for:
        # The three-strikes rule every stage keeps. A finished job is exempt —
        # the read is paid for, only delivery failed, which is free to retry.
        reason = f"Gave up after {rec.proof_attempts} attempts."
        log.error("%s: %s", file.name, reason)
        proof.mark_source(token, file, rec, state, status=PROOF_FAILED,
                          reason=reason, opener=opener)
        report.failed.append((file.name, reason))
        return

    job = _prepare_proof(token, home, ws, file, rec, state, runner, store,
                         opener=opener)
    if job.state != "done":
        # Something transient — a model that would not answer, a disk that
        # filled. Raised so the caller counts an attempt against it.
        raise PrepFailed(job.error or "The proofread did not finish.")

    report.proofed.append(file.name)
    # The verdict is assessed BEFORE the upload, so outcome.json is written into
    # the results folder and then goes to Drive with everything else — one file,
    # one set of numbers, whichever side reads it.
    verdict = proof.assess(
        job, done_value=ws.hubspot_proof_done_value,
        needs_human_value=ws.hubspot_proof_needs_human_value)
    uploaded = proof.upload_outputs(token, file, job, ws, rec, state,
                                    folder_files,
                                    dest_folder_id=dest_folder_id,
                                    opener=opener)
    report.uploaded.extend(uploaded)
    _apply_proof_outcome(hs_token, token, ws, file, rec, state, verdict,
                         opener=opener, report=report)


def _prepare_proof(token: str, home: Path, ws: WatchSettings, file: DriveFile,
                   rec, state: WatchState, runner: JobRunner, store: JobStore,
                   *, opener) -> Job:
    """The manuscript's galley job, run or resumed.

    The shortcut is the point of the state file, and it matters more here than
    anywhere else in the watcher: a galley run is a wave loop over a whole
    novel, and running one twice is the most expensive mistake this program can
    make."""
    existing = store.get(rec.proof_job_id) if rec.proof_job_id else None
    if existing is not None:
        finished = (existing.state == "done" and existing.results_dir
                    and Path(existing.results_dir).is_dir())
        if finished:
            log.info("%s has already been proofread; picking up from there.",
                     file.name)
            return existing
        existing.state = "queued"
        existing.error = None
        return proof.run_job(runner, store, existing)

    local = proof.fetch(token, file, home / DOWNLOADS / file.id, opener=opener)
    job = proof.make_job(local, ws)
    rec.proof_job_id = job.id
    state.record(rec)              # before the model is called, never after
    return proof.run_job(runner, store, job)


def _await_external_proof(token: str, ws: WatchSettings, file: DriveFile, rec,
                          state: WatchState, *, dest_folder_id: str, opener,
                          report: TickReport) -> None:
    """Record a book as out with an external practitioner, and say so once.

    No work is started and nothing is spent: the Mac-side loop runs on a Claude
    Max subscription that cannot run on Fly, so all DocWatch does here is notice
    the book, mark the manuscript `awaiting` so the folder shows it, and tell
    the owner where to find it. The marker is deliberately not terminal — the
    next tick still looks, and picks the verdict up the moment it lands."""
    folder_link = (f"https://drive.google.com/drive/folders/{dest_folder_id}"
                   if dest_folder_id else "the watched folder")
    reason = (f"is flagged '{ws.hubspot_proof_ready_value}' and is waiting for "
              f"the proofreading practitioner. The book is in {folder_link}; "
              f"the run delivers '{proof.hand_off_names(file.name)['outcome']}' "
              f"beside it, and the next pass picks it up.")
    if rec.proof_marked != PROOF_AWAITING:
        proof.mark_source(token, file, rec, state, status=PROOF_AWAITING,
                          opener=opener)
        log.info("Waiting on a practitioner for %s.", file.name)
    if not rec.proof_awaiting_emailed:
        # Once per book, not once per tick: the alert email is for things a
        # person has not seen yet, and a book that waits a week is not news
        # every morning.
        report.awaiting_proof.append((file.name, reason))
        rec.proof_awaiting_emailed = True
        state.record(rec)


def _apply_proof_outcome(hs_token: str | None, token: str, ws: WatchSettings,
                         file: DriveFile, rec, state: WatchState,
                         verdict: "proof.Verdict", *, opener,
                         report: TickReport) -> None:
    """Act on a verdict: move the record on, either way.

    A proofread ends at one of two verdicts and BOTH move the book off "Ready
    for Proofing" — `done` to "Proofing Complete", `needs_human` to "Needs Human
    PR", the option that puts it in front of a human proofreader. Neither leaves
    a book sitting at ready, which is the state nobody would notice.

    Exactly one PATCH either way, guarded by `proof_hubspot_done`; the local
    record is written before the Drive marker, which is prep's order for prep's
    reason — a file that reads finished in Drive was finished in HubSpot first,
    so the two can never disagree in the direction that strands a book.

    `needs_human` also earns its report line and the owner's email, because the
    CRM value alone does not say *why*. The manuscript is marked terminally
    either way: the read has been paid for, and repeating it would buy the same
    answer at the same price."""
    rec.proof_outcome = verdict.outcome
    rec.proof_outcome_reason = verdict.reason
    state.record(rec)

    if verdict.done:
        _finish_hubspot_proof(hs_token, ws, file, rec, state,
                              value=ws.hubspot_proof_done_value, opener=opener)
        proof.mark_source(token, file, rec, state, status=PROOF_DONE,
                          opener=opener)
        return

    reason = (verdict.reason
              or "the proofread finished but the book needs a human "
                 "proofreader.")
    log.warning("Needs a person: %s (%s)", file.name, reason)
    _finish_hubspot_proof(hs_token, ws, file, rec, state,
                          value=ws.hubspot_proof_needs_human_value,
                          opener=opener)
    moved = (f"HubSpot was moved to '{ws.hubspot_proof_needs_human_value}'."
             if ws.hubspot_proof_needs_human_value
             else f"No value is configured for that verdict, so HubSpot was "
                  f"left at '{ws.hubspot_proof_ready_value}' for a person.")
    report.needs_human.append(
        (file.name,
         f"was proofread and needs a human proofreader: {reason} {moved}"))
    proof.mark_source(token, file, rec, state, status=PROOF_HUMAN,
                      reason=reason, opener=opener)


def _finish_hubspot_proof(hs_token: str | None, ws: WatchSettings,
                          file: DriveFile, rec, state: WatchState, *,
                          value: str, opener) -> None:
    """Move the status property to `value` on the book just proofread — either
    the proofing done value or the needs-a-human one, whichever the verdict
    named.

    The promo/plan twin, with the same guards: read-only mode leaves the CRM
    untouched, and a blank value is refused rather than blanking the property.
    `proof_hubspot_done` makes it exactly one write per book — writing a value
    already set is harmless, which is what makes the whole step safe to repeat
    after an interrupted tick.

    The property and the value come from the watcher's settings, never from the
    outcome.json that was read. That file carries a `hubspot` block, and in
    `external` mode it was placed in Drive by something outside DocProof: a file
    in a folder does not get to name the CRM field DocProof writes, nor the
    value it writes there."""
    if not (ws.hubspot_enabled and rec.proof_hubspot_id
            and not rec.proof_hubspot_done):
        return
    if not ws.hubspot_write_back:
        log.info("HubSpot is read-only: leaving %s at its current status.",
                 file.name)
        return
    if not value:
        log.warning("The HubSpot value for this proofing verdict is empty: "
                    "leaving %s at its current status rather than blanking it.",
                    file.name)
        return
    props = {ws.hubspot_status_property: value}
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.proof_hubspot_id,
                           props, allow={ws.hubspot_status_property},
                           opener=opener)
    rec.proof_hubspot_done = True
    state.record(rec)                     # recorded before the Drive marker


@dataclass(frozen=True)
class DiscoveryStage:
    """One HubSpot-gated pass over a book, as subfolder discovery sees it.

    Formatting and proofing are two passes over the same manuscript, in the same
    author folder, gated on two values of the same dropdown. Everything that
    differs between them lives here — which value means "ready", which record id
    and marker the pass owns, what counts as a book to work on and what counts
    as one already finished — so `_discover` stays one function rather than two
    that drift apart the first time either is fixed.
    """

    name: str                                    # for the log
    done_word: str                               # "formatted" | "proofread"
    ready_value: str
    id_get: Callable[[Any], str]
    id_set: Callable[[Any, str], None]
    candidate: Callable[[DriveFile], bool]
    already_done: Callable[[DriveFile], bool]
    # Whether this record is still this stage's to finish — the test that
    # re-lists an in-flight book's folder after its status has moved off ready.
    in_flight: Callable[[Any], bool]
    # The file this stage READS, by house name. Each stage in the series reads
    # what the one before it left — formatting reads "<surname> - Book Original",
    # proofing reads the dev-edited "<surname> - Book 1" — so "which file in this
    # author's folder is the book" has a different answer per stage, and the
    # sentence a person is emailed has to name the right one.
    source_stage: str
    source_name: Callable[[str, str], bool]      # (filename, surname) -> bool
    # Whether that name is required, or only checked when `require_source_label`
    # is on. Formatting leaves it optional for an install that does not follow
    # the house convention; proofing does not, because a proofread costs a
    # novel's worth of model time and "which file is the book" must never be a
    # guess.
    label_always: bool = False


def format_stage(ws: WatchSettings) -> DiscoveryStage:
    """Formatting: prepare the new manuscript, mark it `formatted`."""
    return DiscoveryStage(
        name="formatting", done_word="formatted",
        ready_value=ws.hubspot_format_ready_value,
        id_get=lambda r: r.hubspot_id,
        id_set=lambda r, v: setattr(r, "hubspot_id", v),
        candidate=lambda f: classify(f) is Stage.NEW_MANUSCRIPT,
        already_done=lambda f: classify(f) is Stage.DONE,
        in_flight=lambda r: r.marked != FORMATTED,
        source_stage=naming.SOURCE_STAGE,
        source_name=naming.is_source_name,
    )


def proof_stage(ws: WatchSettings) -> DiscoveryStage:
    """Proofing: read the same manuscript again, on proofing's own marker.

    The candidate test is `is_proof_candidate`, which is blind to the formatting
    marker — a book marked `formatted` is exactly the book to proofread — and
    treats only proofing's *terminal* marker values as finished, so a book out
    with an external practitioner stays visible until its verdict lands."""
    return DiscoveryStage(
        name="proofing", done_word="proofread",
        ready_value=ws.hubspot_proof_ready_value,
        id_get=lambda r: r.proof_hubspot_id,
        id_set=lambda r, v: setattr(r, "proof_hubspot_id", v),
        candidate=is_proof_candidate,
        already_done=lambda f: (f.app_properties.get(PROOF_PROP)
                                in PROOF_TERMINAL),
        in_flight=lambda r: r.proof_marked not in PROOF_TERMINAL,
        source_stage=naming.PROOF_SOURCE_STAGE,
        source_name=naming.is_proof_source_name,
        label_always=True,
    )


def _discover(token: str, hs_token: str | None, ws: WatchSettings,
              state: WatchState, *, stage: DiscoveryStage, opener,
              report: TickReport,
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
    it after its status has moved off "ready".

    `stage` says which pass this is. Formatting and proofing each call this
    with their own value and their own record id, and each gets back its own
    listing — never one shared one — because a book flagged "Ready for Proofing"
    must not fall into the formatting pass's ungated `run_prep`."""
    want = [p for p in (ws.hubspot_status_property, ws.hubspot_key_property,
                        ws.hubspot_first_property, ws.hubspot_last_property)
            if p]
    try:
        ready = hubspot.find_by_value(
            hs_token, ws.hubspot_object, ws.hubspot_status_property,
            stage.ready_value, want_properties=want, opener=opener)
    except HubSpotAuthError:
        raise                             # the token is bad — stop the pass
    except HubSpotError as e:             # transient — the pass's books wait
        log.info("Waiting: could not fetch the %s Projects from HubSpot "
                 "(%s); the next run will try again.", stage.name, e)
        ready = []

    listing: list[DriveFile] = []
    routes: dict[str, str] = {}
    cache: dict[tuple[str, str], str | None] = {}
    seen_records: set[str] = set()
    for record in ready:
        seen_records.add(record.id)
        _discover_ready(token, ws, record, state, listing, routes, cache,
                        stage=stage, opener=opener, report=report,
                        dry_run=dry_run)

    # A book already in flight — its record id is on the state file and it is
    # not yet delivered — is re-listed from the folder it recorded, whether or
    # not it is still flagged ready. Without this a job spanning ticks would
    # have its manuscript read as "gone from the folder" and be parked.
    for rec in list(state.files.values()):
        if (stage.id_get(rec) and stage.id_get(rec) not in seen_records
                and rec.subfolder_id and stage.in_flight(rec)):
            _adopt(token, rec.subfolder_id, listing, routes, stage=stage,
                   opener=opener)

    uniq = {f.id: f for f in listing}     # a subfolder seen twice, deduped
    return list(uniq.values()), routes


def _discover_ready(token: str, ws: WatchSettings, record, state: WatchState,
                    listing: list[DriveFile], routes: dict[str, str],
                    cache: dict[tuple[str, str], str | None], *,
                    stage: DiscoveryStage, opener,
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
    manuscripts = [f for f in contents if stage.candidate(f)]

    # A multi-book author keeps each book in its own folder one level down —
    # author folder -> book folder -> book original — so an author's several
    # books do not pile into one folder and get read as "which of these is the
    # one?". Only when the author folder holds no manuscript of its own does the
    # pass descend: a single-book author's folder is read exactly as before, and
    # the extra Drive listing is paid only for authors who actually nest.
    if not manuscripts:
        book_folders = [f for f in contents if f.is_folder]
        if book_folders:
            _discover_nested(token, ws, record, first, last, author,
                             book_folders, state, listing, routes, stage=stage,
                             opener=opener, report=report, dry_run=dry_run)
            return

    # Is the file this stage reads in the folder at all — a fresh one to work
    # on, or one already finished with (marked done, so out of `manuscripts`)?
    # It is asked by NAME, not by marker: an output a human placed and named is
    # an output, not the intake, so a folder holding only that still counts as
    # missing its source and is reported. Asking instead whether *any* output
    # was present was the bug — it silently skipped exactly that author. And
    # which state it is in matters: a source marked done is a finished book
    # whose HubSpot status simply never moved, not a missing one.
    intake_files = [f for f in contents if stage.source_name(f.name, last)]
    intake_done = any(stage.already_done(f) for f in intake_files)

    def _unprepared(missing_detail: str) -> None:
        """Account for a ready author with no book to prepare — none dropped
        silently. A finished book whose status stuck gets its own alert (so a
        person can move HubSpot on); a genuine absence is a missing source file
        — the `Book Original` for formatting, the dev-edited `Book 1` for
        proofing; an intake present but unfinished was already reported when the
        run failed, so it is not raised again."""
        ready = stage.ready_value
        if intake_done:
            log.info("Waiting: %s is flagged ready but its book is already "
                     "%s; the status did not move off ready.", author,
                     stage.done_word)
            report.stuck_ready.append(
                (author, f"flagged '{ready}' but its "
                         f"'{last} - {stage.source_stage}' is already "
                         f"{stage.done_word} — the status never moved on, so "
                         f"check the write-back."))
        elif intake_files:
            log.info("Waiting: %s is flagged ready; its intake file is present "
                     "but not yet prepared (a prior run may have failed).",
                     author)
        else:
            log.info("Waiting: %s is flagged ready but %s.", author,
                     missing_detail)
            report.missing_source.append(
                (author, f"flagged '{ready}' but {missing_detail}."))

    if not manuscripts:
        _unprepared("its folder is empty" if not contents else
                    f"its folder holds {len(contents)} file(s) but none is a "
                    f"'{last} - {stage.source_stage}'")
        report.waiting += 1
        return

    # Only "<surname> - <this stage's source>" is the book to work on — the
    # surname coming from the author's own HubSpot record, so the file is tied
    # to the right book and a draft beside it is left alone rather than guessed
    # at. Formatting makes this optional (`require_source_label`, off on an
    # install that does not follow the house convention). Proofing does not:
    # `label_always` is set because a proofread costs a novel's worth of model
    # time, which is far too much to spend on a file nobody named.
    if ws.require_source_label or stage.label_always:
        labelled = [f for f in manuscripts if stage.source_name(f.name, last)]
        if not labelled:
            _unprepared(f"no file is named '{last} - {stage.source_stage}' "
                        f"({len(manuscripts)} other manuscript(s) in the folder)")
            report.waiting += 1
            return
        manuscripts = labelled

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
        stage.id_set(rec, record.id)      # written before the work, never after
        rec.author_first = first
        rec.author_last = last
        rec.subfolder_id = subfolder_id
        rec.subfolder_name = author
        state.record(rec)
    # The stage's own runner works every candidate in the listing without gating
    # again in subfolder mode, so only the chosen book may ride along — a rival
    # manuscript the label filter set aside must not. Everything that is not a
    # candidate (the book's outputs) stays, so orphan and OUTPUT logic still
    # works. With the filter off a single manuscript reached here anyway, so this
    # keeps the same contents it always did.
    listing.extend(f for f in contents if f.id == book.id
                   or not stage.candidate(f))
    routes[book.id] = subfolder_id


def _discover_nested(token: str, ws: WatchSettings, record, first: str,
                     last: str, author: str, book_folders: list[DriveFile],
                     state: WatchState, listing: list[DriveFile],
                     routes: dict[str, str], *, stage: DiscoveryStage, opener,
                     report: TickReport, dry_run: bool) -> None:
    """A multi-book author, whose books sit one level down: author folder ->
    book folder -> book original. Prepare the one book in each book folder and
    route its outputs back into that same folder, so an author with several
    books ready has each done in place and none confused for another.

    One book folder is one book. The same gates the flat author folder uses
    apply per folder: the house label picks the intake when it is required, and
    a folder holding two new manuscripts is nobody's to guess — it is left for a
    person, without stopping the author's other books. If not one book folder
    yields a book, the ready author is reported as missing its Book Original, the
    same as an empty author folder is."""
    queued = 0
    flagged = 0
    for folder in book_folders:
        contents = drive.list_folder(token, folder.id, opener=opener)
        manuscripts = [f for f in contents if stage.candidate(f)]
        if ws.require_source_label or stage.label_always:
            manuscripts = [f for f in manuscripts
                           if stage.source_name(f.name, last)]
        if not manuscripts:
            continue
        if len(manuscripts) > 1:
            reason = (f"{len(manuscripts)} new manuscripts are in {author}'s "
                      f"'{folder.name}' folder, so DocProof cannot tell which is "
                      f"the book to do.")
            log.warning("Needs a person: %s (%s)", author, reason)
            report.needs_human.append((f"{author} / {folder.name}", reason))
            report.waiting += 1
            flagged += 1
            continue
        book = manuscripts[0]
        if not dry_run:
            rec = state.get(book.id)
            rec.name = book.name
            stage.id_set(rec, record.id)  # written before the work, never after
            rec.author_first = first
            rec.author_last = last
            rec.subfolder_id = folder.id      # its outputs, and its resume, here
            rec.subfolder_name = author
            state.record(rec)
        # As in the flat path: only the chosen book rides along, and everything
        # that is not a candidate (its outputs) stays so orphan and OUTPUT
        # logic still works.
        listing.extend(f for f in contents if f.id == book.id
                       or not stage.candidate(f))
        routes[book.id] = folder.id
        queued += 1

    if queued or flagged:
        return
    ready = stage.ready_value
    detail = (f"none of its {len(book_folders)} book folder(s) holds a "
              f"'{last} - {stage.source_stage}'")
    log.info("Waiting: %s is flagged ready but %s.", author, detail)
    report.missing_source.append((author, f"flagged '{ready}' but {detail}."))
    report.waiting += 1


def _adopt(token: str, subfolder_id: str, listing: list[DriveFile],
           routes: dict[str, str], *, stage: DiscoveryStage, opener) -> None:
    """Re-list an in-flight book's recorded subfolder so the pass still sees it.
    Route every candidate it holds back into it, the same as discovery."""
    contents = drive.list_folder(token, subfolder_id, opener=opener)
    for f in contents:
        if stage.candidate(f):
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
    # was asked to says so, once, with the whole log. `completion_emailed`
    # guards the "once" — a book reconsidered after a lost marker is not emailed
    # again — and is set only on a confirmed send, so a mail that failed retries.
    # Best-effort like the needs-a-person mail: a send that fails is logged,
    # never raised, so an email server never undoes finished work.
    if (ws.notify_on_complete and ws.notify_email
            and not rec.completion_emailed):
        if notify.maybe_complete(token, ws, job, file, rec, uploaded,
                                 dest_folder_id, opener=opener):
            rec.completion_emailed = True
            state.record(rec)


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
    if not ws.hubspot_format_done_value:
        # Defence behind preflight, which already refuses a blank done-value: a
        # patch of an empty value would blank the status property rather than
        # move it on, so it is refused here too. The book is left at whatever
        # value it had, exactly as read-only mode leaves it.
        log.warning("HubSpot done-value is empty: leaving %s at its current "
                    "status rather than blanking it.", file.name)
        return
    # The allowlist set_properties enforces: DocProof may write the status
    # property, and the output property only when one is configured — never
    # anything else, whatever props ends up holding.
    props = {ws.hubspot_status_property: ws.hubspot_format_done_value}
    allow = {ws.hubspot_status_property}
    if ws.hubspot_output_property:
        allow.add(ws.hubspot_output_property)
        name = _output_name(uploaded) or _output_name(list(rec.uploaded))
        if name:
            props[ws.hubspot_output_property] = name
    hubspot.set_properties(hs_token, ws.hubspot_object, rec.hubspot_id, props,
                           allow=allow, opener=opener)
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

    if ws.promo_enabled:
        # Promo is driven by a HubSpot status value, so HubSpot must be on and
        # its own value pair set — half-configured would either write copy for
        # everything or never move a book on.
        if not ws.hubspot_enabled:
            raise NotConfigured(
                "Promo is switched on but HubSpot is not. Promo is triggered by "
                "a HubSpot status value, so HubSpot has to be on. Run "
                "`docproof-watch init`, or turn promo off.")
        blanks = [name for name, value in (
            ("hubspot_promo_ready_value", ws.hubspot_promo_ready_value),
            ("hubspot_promo_done_value", ws.hubspot_promo_done_value),
        ) if not value]
        if blanks:
            raise NotConfigured(
                "Promo is switched on but " + ", ".join(blanks)
                + " is not set. Run `docproof-watch init` to fill it in.")

    if ws.proofing_enabled:
        # Proofing is driven by a value of the same HubSpot dropdown formatting
        # uses, so HubSpot must be on and its own value pair set —
        # half-configured would either proofread everything or never move a
        # book on.
        if not ws.hubspot_enabled:
            raise NotConfigured(
                "Proofing is switched on but HubSpot is not. Proofing is "
                "triggered by a HubSpot status value, so HubSpot has to be on. "
                "Run `docproof-watch init`, or turn proofing off.")
        # The ready and done values are what the stage cannot run without: one
        # says which books to read, the other says where to move a clean one.
        #
        # The needs-a-human value is deliberately NOT here. Blank is a real
        # choice on it — `_finish_hubspot_proof` refuses an empty value rather
        # than blanking the status property, so the book stays at the ready
        # value for a person and the reason still reaches the owner by email.
        # A press with no such option on its dropdown clears the box and gets
        # exactly that; refusing the whole pass over it would make the panel's
        # own helper text a lie.
        blanks = [name for name, value in (
            ("hubspot_proof_ready_value", ws.hubspot_proof_ready_value),
            ("hubspot_proof_done_value", ws.hubspot_proof_done_value),
        ) if not value]
        if blanks:
            raise NotConfigured(
                "Proofing is switched on but " + ", ".join(blanks)
                + " is not set. Run `docproof-watch init` to fill it in.")
        if ws.proof_runner not in ("app", "external"):
            raise NotConfigured(
                f"proof_runner is {ws.proof_runner!r}; it has to be 'app' "
                f"(DocWatch reads the book itself) or 'external' (a "
                f"practitioner does, and DocWatch waits for the hand-off).")

    if ws.plan_enabled:
        # The marketing plan is driven by its own HubSpot property, so HubSpot
        # must be on and the plan's property and its value pair set —
        # half-configured would either plan everything or never move a book on.
        if not ws.hubspot_enabled:
            raise NotConfigured(
                "Marketing plans are switched on but HubSpot is not. The plan is "
                "triggered by a HubSpot property, so HubSpot has to be on. Run "
                "`docproof-watch init`, or turn marketing plans off.")
        blanks = [name for name, value in (
            ("hubspot_plan_property", ws.hubspot_plan_property),
            ("hubspot_plan_needed_value", ws.hubspot_plan_needed_value),
            ("hubspot_plan_done_value", ws.hubspot_plan_done_value),
        ) if not value]
        if blanks:
            raise NotConfigured(
                "Marketing plans are switched on but " + ", ".join(blanks)
                + " is not set. Run `docproof-watch init` to fill it in.")

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
        listing, routes = _discover(token, hs_token, ws, state,
                                    stage=format_stage(ws), opener=opener,
                                    report=report, dry_run=dry_run)
    else:
        listing = drive.list_folder(token, ws.folder_id, opener=opener)
        routes = {}

    # Proofing gets its OWN listing in subfolder mode, discovered at its own
    # ready value. It must not share formatting's: `run_prep` prepares every
    # candidate in that listing without gating again, so a book flagged "Ready
    # for Proofing" landing in it would be formatted a second time. In flat mode
    # there is one folder and one listing, and `run_proof` gates it itself.
    proof_listing: list[DriveFile] = []
    proof_routes: dict[str, str] = {}
    if ws.proofing_enabled and ws.subfolders_enabled:
        proof_listing, proof_routes = _discover(
            token, hs_token, ws, state, stage=proof_stage(ws), opener=opener,
            report=report, dry_run=dry_run)
    elif ws.proofing_enabled:
        proof_listing = listing

    seen = {f.id for f in listing}
    extra = [f for f in proof_listing if f.id not in seen]
    every = listing + extra
    report.listed = len(every)
    report.plan = [(f.name, classify(f).value) for f in every]
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
    # notify_home is the watch home itself: a watched promo job emails its
    # completion log through the runner, the same as an app job. Watched format
    # jobs are left to `_one`'s richer mail (routing, Drive links) and the runner
    # skips them — see JobRunner._notify_done.
    runner = JobRunner(store, ws.app_settings(root), config_path=config_path(),
                       notify_home=root)
    if not mock:
        # A rehearsal leaves real leftovers alone: _drain finishes whatever a
        # dead pass left mid-flight through the real provider, and
        # `--mock-tags` is documented as costing nothing. It is handed every
        # file this pass can see, proofing's folders included, so a galley job
        # spanning ticks is not parked as "the manuscript left the folder".
        _drain(runner, state, every)

    collect_finished(token, ws, listing, state, store, opener=opener,
                     report=report)
    submit_ready(token, ws, listing, state, store, opener=opener, report=report)
    run_prep(token, root, ws, listing, state, runner, store, mock=mock,
             opener=opener, hs_token=hs_token, routes=routes, report=report)
    # Proofing runs after formatting, on its own value of the same dropdown and
    # its own marker, over its own listing — so a book is never in both stages
    # at once (one dropdown, one value at a time) and neither touches the
    # other's markers. Unlike promo and the plan it works in subfolder mode,
    # which is the mode production runs in.
    run_proof(token, root, ws, proof_listing, state, runner, store, mock=mock,
              opener=opener, hs_token=hs_token, routes=proof_routes,
              report=report)
    # Promo runs after formatting and only in flat mode. It gates on its own
    # HubSpot value, so a book is never in both stages at once — one dropdown,
    # one value at a time — and it never touches the format stage's markers.
    run_promo(token, root, ws, listing, state, runner, store, mock=mock,
              opener=opener, hs_token=hs_token, report=report)
    # The marketing plan runs after copy, on its own HubSpot property and marker,
    # so a book can be formatted, promo'd, and planned in any combination without
    # the three stages touching each other. Flat mode only, like promo.
    run_plans(token, root, ws, listing, state, runner, store, mock=mock,
              opener=opener, hs_token=hs_token, report=report)

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
    # Promo jobs hang off their own field, so a resumed one is tied back to its
    # manuscript the same way — otherwise a promo whose book left the folder
    # would be finished at cost with nowhere to deliver it.
    owner.update({rec.promo_job_id: fid for fid, rec in state.files.items()
                  if rec.promo_job_id})
    # Plan jobs the same, on their own field.
    owner.update({rec.plan_job_id: fid for fid, rec in state.files.items()
                  if rec.plan_job_id})
    # And proofing's galley jobs, which matter most: a wave loop over a whole
    # novel is the most expensive thing here to finish for nothing.
    owner.update({rec.proof_job_id: fid for fid, rec in state.files.items()
                  if rec.proof_job_id})
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
