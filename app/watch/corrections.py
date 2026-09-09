"""Interior corrections: the author's Pre-Proof Interior Design Corrections Form,
applied to the designer's exported IDML, with a spreadsheet that accounts for
every correction — and the whole thing back in the author's folder.

The fifth stage, and the first over a designer's file rather than a manuscript.
A HubSpot workflow flips the status dropdown to "Ready for Corrections" when
the form comes in; a pass then, per ready record:

    author folder -> "Interior Design" -> the highest "<surname> - Book N.idml"
    the form's file (a marked-up PDF proof, or a Word list) and/or typed text
    -> read into an edit list (docproof.corrections.intake)
    -> applied by the app's own corrections job (docproof.corrections.run)
    -> "<surname> - Book N.5.idml", "<surname> - Book N.5 - corrections.xlsx"
       (Applied / Not applied), notes and the InDesign check tour, uploaded
       beside the source
    -> HubSpot moved to the done value; the source IDML marked done in Drive.

Every book then goes to a human designer for what could not be applied — the
spreadsheet is written for that designer, so its page numbers are the file's
own folios and nothing is ever guessed onto a page. The stage never overwrites:
a `Book N.5` already in the folder that is not its own is left alone and a
person is told. Requires HubSpot and per-author subfolders, which is the mode
production runs in.

Same idempotent shape as the other stages: record before the paid-for step,
upload what the state file says never landed, write HubSpot exactly once, and
write the Drive marker last so it means "everything before me finished".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from docproof.batch import new_job_id
from docproof.models import Usage

from app.jobs import Job, JobRunner, JobStore
from app.settings import get_api_key, resource_root

from . import drive, folders, hubspot, naming
from .drive import DriveFile
from .hubspot import HubSpotAuthError, HubSpotError
from .prep import _already_there
from .settings import WatchSettings
from .stages import (AT_PROP, CORRECTIONS_DONE, CORRECTIONS_FAILED,
                     CORRECTIONS_PROP, CORRECTIONS_TERMINAL, JOB_PROP,
                     OUTPUT_PROP, REASON_PROP, SOURCE_PROP)
from .state import FileRecord, WatchState

log = logging.getLogger("docproof.app.watch.corrections")

IDML_MIME = "application/vnd.adobe.indesign-idml-package"
XLSX_MIME = ("application/vnd.openxmlformats-officedocument.spreadsheetml"
             ".sheet")
MARKDOWN_MIME = "text/markdown"
JSX_MIME = "text/plain"
DOWNLOADS = "downloads"
REASON_LIMIT = 90

# The model that reads a typed or marked-up list into exact edits — the same
# one the app's panel uses. Read from the app's routes when they are importable
# (the server), spelled here for a bare CLI that has no FastAPI.
EXTRACT_MODEL = "gpt-5.6-luna"

__all__ = ["Work", "Submission", "CorrectionsFailed", "discover", "run_stage",
           "pick_source", "proof_pdf_for", "hand_off_names", "artifacts",
           "make_job", "run_job", "upload_outputs", "mark_source",
           "extract_provider"]


class CorrectionsFailed(RuntimeError):
    """A book did not come through for a reason that might not happen again — so
    it is counted against the file rather than marked on it."""


@dataclass(frozen=True)
class Work:
    """One ready record, resolved to the file to correct and what to apply."""

    record_id: str
    first: str
    last: str
    author: str
    folder_id: str                   # the "Interior Design" folder — outputs go here
    idml: DriveFile
    listing: list[DriveFile] = field(default_factory=list)
    proof_pdf: DriveFile | None = None
    file_urls: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class Submission:
    """What the author sent, on disk: a file (PDF or .docx) and/or typed text."""

    path: Path | None = None
    text: str = ""
    kind: str = ""                   # "pdf" | "docx" | "text"

    @property
    def empty(self) -> bool:
        return self.path is None and not self.text.strip()


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    mime: str


# --- discovery -----------------------------------------------------------------

def discover(hs_token: str, token: str, ws: WatchSettings, state: WatchState,
             *, opener, report) -> list[Work]:
    """Ask HubSpot who is ready, then look only in those authors' folders.

    HubSpot drives, Drive follows — the parent Author Folder is never listed.
    Every author that cannot be resolved to exactly one file to correct is
    reported (`report.needs_human` / `missing_source` / `stuck_ready`) and left
    where it is; nothing here guesses. Returns the books this pass can work."""
    want = [p for p in (ws.hubspot_status_property, ws.hubspot_first_property,
                        ws.hubspot_last_property,
                        ws.hubspot_corrections_file_property,
                        ws.hubspot_corrections_text_property) if p]
    try:
        ready = hubspot.find_by_value(
            hs_token, ws.hubspot_object, ws.hubspot_status_property,
            ws.hubspot_corrections_ready_value, want_properties=want,
            opener=opener)
    except HubSpotAuthError:
        raise
    except HubSpotError as e:
        log.info("Waiting: could not fetch the corrections Projects from "
                 "HubSpot (%s); the next run will try again.", e)
        return []

    works: list[Work] = []
    seen: set[str] = set()
    for record in ready:
        seen.add(record.id)
        work = _resolve(token, ws, record, opener=opener, report=report)
        if work is not None:
            works.append(work)

    # A book already paid for — its job exists on the state file and it is not
    # yet delivered — is picked up again from the folder it recorded, whether or
    # not a person has since moved its status. The submission is not re-read:
    # the job carries the edit list, so only delivery is left to do.
    for rec in list(state.files.values()):
        if (rec.corrections_hubspot_id and rec.corrections_job_id
                and rec.corrections_hubspot_id not in seen
                and rec.corrections_marked not in CORRECTIONS_TERMINAL
                and rec.subfolder_id):
            listing = drive.list_folder(token, rec.subfolder_id, opener=opener)
            idml = next((f for f in listing if f.id == rec.file_id), None)
            if idml is None:
                continue
            works.append(Work(record_id=rec.corrections_hubspot_id,
                              first=rec.author_first, last=rec.author_last,
                              author=rec.subfolder_name, folder_id=rec.subfolder_id,
                              idml=idml, listing=listing))
    uniq = {w.idml.id: w for w in works}
    return list(uniq.values())


def _resolve(token: str, ws: WatchSettings, record, *, opener,
             report) -> Work | None:
    first = (record.properties.get(ws.hubspot_first_property) or "").strip()
    last = (record.properties.get(ws.hubspot_last_property) or "").strip()
    ready = ws.hubspot_corrections_ready_value
    if not first or not last:
        reason = ("its HubSpot record has no first or last name, so DocProof "
                  "cannot tell which folder is the author's.")
        log.warning("Needs a person: record %s (%s)", record.id, reason)
        report.needs_human.append((f"HubSpot record {record.id}", reason))
        report.waiting += 1
        return None
    author = folders.compose(first, last)

    author_folder = folders.resolve(first, last, ws.folder_id, token,
                                    opener=opener)
    if author_folder is None:
        reason = (f"no single folder named '{author}' is in the Author Folder, "
                  f"so DocProof will not guess where the book is.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return None

    interior = _child_folder(token, author_folder, ws.corrections_folder_name,
                             opener=opener)
    if interior is None:
        detail = (f"its folder has no '{ws.corrections_folder_name}' subfolder "
                  f"to hold the designer's IDML")
        log.info("Waiting: %s is flagged '%s' but %s.", author, ready, detail)
        report.missing_source.append((author, f"flagged '{ready}' but {detail}."))
        report.waiting += 1
        return None

    listing = drive.list_folder(token, interior.id, opener=opener)
    idml, why = pick_source(listing, last)
    if idml is None:
        if why == "done":
            done_file = next(f for f in listing
                             if naming.is_idml_source_name(f.name, last)
                             and f.app_properties.get(CORRECTIONS_PROP)
                             == CORRECTIONS_DONE)
            report.stuck_ready.append(
                (author, f"flagged '{ready}' but its '{done_file.name}' already "
                         f"has its corrections applied — the status never moved "
                         f"on, so check the write-back, or wait for the "
                         f"designer's next export."))
        elif why == "failed":
            failed_file = next(f for f in listing
                               if naming.is_idml_source_name(f.name, last)
                               and f.app_properties.get(CORRECTIONS_PROP)
                               == CORRECTIONS_FAILED)
            props = failed_file.app_properties
            when = (props.get(AT_PROP) or "")[:10]
            reason = (f"flagged '{ready}' but its '{failed_file.name}' was "
                      f"already tried{' on ' + when if when else ''} and marked "
                      f"failed: {props.get(REASON_PROP) or 'no reason recorded'}."
                      f" Fix the form or the file and clear the marker to try "
                      f"again, or move the status on.")
            log.warning("Needs a person: %s (%s)", author, reason)
            report.needs_human.append((author, reason))
        elif why == "tie":
            reason = (f"two files in {author}'s '{ws.corrections_folder_name}' "
                      f"folder carry the same highest Book number, so DocProof "
                      f"cannot tell which is the designer's latest export.")
            log.warning("Needs a person: %s (%s)", author, reason)
            report.needs_human.append((author, reason))
        else:
            detail = (f"its '{ws.corrections_folder_name}' folder holds no "
                      f"'{last} - Book N.idml'"
                      + (f" ({len(listing)} other file(s) there)" if listing
                         else " (it is empty)"))
            log.info("Waiting: %s is flagged '%s' but %s.", author, ready, detail)
            report.missing_source.append((author,
                                          f"flagged '{ready}' but {detail}."))
        report.waiting += 1
        return None

    # Never overwrite. A "Book N.5" already there that this stage did not write
    # (no source marker pointing at this IDML) is somebody's file.
    names = hand_off_names(idml.name)
    rival = next((f for f in listing if f.name == names["idml"]
                  and f.app_properties.get(SOURCE_PROP) != idml.id), None)
    if rival is not None:
        reason = (f"a '{rival.name}' is already in the folder and DocProof did "
                  f"not write it, so it will not overwrite it. Remove or rename "
                  f"it, or move the status on.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return None

    urls = tuple(hubspot.file_urls(
        record.properties.get(ws.hubspot_corrections_file_property, "")))
    text = (record.properties.get(ws.hubspot_corrections_text_property)
            or "").strip()
    if not urls and not text:
        reason = (f"flagged '{ready}' but the record carries no corrections — "
                  f"neither an uploaded file nor typed text reached the "
                  f"'{ws.hubspot_corrections_file_property or '—'}' / "
                  f"'{ws.hubspot_corrections_text_property or '—'}' properties. "
                  f"Check the form workflow.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return None

    return Work(record_id=record.id, first=first, last=last, author=author,
                folder_id=interior.id, idml=idml, listing=listing,
                proof_pdf=proof_pdf_for(listing, idml), file_urls=urls,
                text=text)


def _child_folder(token: str, parent_id: str, name: str, *,
                  opener) -> DriveFile | None:
    """The one subfolder called `name` — exact first, then case- and
    whitespace-insensitive among the parent's folders. None when there is not
    exactly one."""
    hits = drive.find_children(token, parent_id, name=name, folders_only=True,
                               opener=opener)
    if not hits:
        wanted = " ".join(name.split()).casefold()
        hits = [f for f in drive.find_children(token, parent_id,
                                               folders_only=True, opener=opener)
                if " ".join(f.name.split()).casefold() == wanted]
    ids = {f.id: f for f in hits}
    if len(ids) == 1:
        return next(iter(ids.values()))
    if len(ids) > 1:
        log.warning("%d folders are named %r under %s; not guessing.",
                    len(ids), name, parent_id)
    return None


def pick_source(listing: list[DriveFile], last: str
                ) -> tuple[DriveFile | None, str]:
    """The designer's latest export — the highest integer "<surname> - Book N
    .idml" — or `(None, why)`: "none" (no such file), "tie" (two at the top),
    "done" (the latest already has its corrections applied) or "failed" (it
    was tried and marked failed). Pure, so it is tested without Drive."""
    exports = [(naming.idml_version(f.name)[1], f) for f in listing
               if not f.is_folder and naming.is_idml_source_name(f.name, last)]
    if not exports:
        return None, "none"
    top = max(v for v, _ in exports)
    latest = [f for v, f in exports if v == top]
    if len(latest) > 1:
        return None, "tie"
    chosen = latest[0]
    marker = chosen.app_properties.get(CORRECTIONS_PROP, "")
    if marker == CORRECTIONS_DONE:
        return None, "done"
    if marker == CORRECTIONS_FAILED:
        return None, "failed"
    return chosen, ""


def proof_pdf_for(listing: list[DriveFile], idml: DriveFile
                  ) -> DriveFile | None:
    """A proof PDF beside the export under the same stem ("Johnson - Book
    3.pdf"), whose page texts let a typed correction's "page 47" narrow to the
    text page 47 set. Optional: without it the file's own folios still place
    every page the marks name."""
    stem = Path(idml.name).stem.casefold()
    for f in listing:
        if f.is_folder:
            continue
        if Path(f.name).suffix.lower() == ".pdf" and \
                Path(f.name).stem.casefold() == stem:
            return f
    return None


def hand_off_names(source_name: str) -> dict[str, str]:
    return naming.corrections_hand_off_names(source_name)


# --- the pass ------------------------------------------------------------------

def run_stage(token: str, home: Path, ws: WatchSettings, state: WatchState,
              runner: JobRunner, store: JobStore, *, mock: bool, opener,
              hs_token: str | None, report) -> None:
    """Correct every book HubSpot flagged, one at a time, each inside its own
    guard so one bad book never stops the pass."""
    if not ws.corrections_enabled or not hs_token:
        return
    if getattr(ws, "corrections_engine", "idml") == "native":
        # Native InDesign packages have a separate durable ledger and lock;
        # keeping the dispatch here means existing IDML jobs retain exactly
        # their old state and upload semantics.
        from . import native_corrections
        return native_corrections.run_stage(
            token, home, ws, state, runner, store, mock=mock, opener=opener,
            hs_token=hs_token, report=report)
    works = discover(hs_token, token, ws, state, opener=opener, report=report)
    works.sort(key=lambda w: (w.idml.modified_time, w.idml.name))
    if len(works) > ws.max_files_per_tick:
        report.deferred += len(works) - ws.max_files_per_tick
        log.info("%d books are waiting for corrections; doing %d this run and "
                 "leaving the rest.", len(works), ws.max_files_per_tick)
        works = works[:ws.max_files_per_tick]
    for work in works:
        try:
            _one(token, home, ws, work, state, runner, store, mock=mock,
                 opener=opener, hs_token=hs_token, report=report)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Could not apply corrections to %s", work.idml.name)
            report.failed.append((work.idml.name, str(e)))
            rec = state.get(work.idml.id)
            rec.name = work.idml.name
            rec.corrections_attempts += 1
            state.record(rec)


def _one(token: str, home: Path, ws: WatchSettings, work: Work,
         state: WatchState, runner: JobRunner, store: JobStore, *, mock: bool,
         opener, hs_token: str | None, report) -> None:
    file = work.idml
    rec = state.get(file.id)
    rec.name = file.name
    rec.modified_time = file.modified_time
    rec.corrections_hubspot_id = work.record_id
    rec.author_first, rec.author_last = work.first, work.last
    rec.subfolder_id, rec.subfolder_name = work.folder_id, work.author
    state.record(rec)                     # before the work, never after

    if rec.corrections_marked in CORRECTIONS_TERMINAL:
        return

    job = store.get(rec.corrections_job_id) if rec.corrections_job_id else None
    paid_for = (job is not None and job.state == "done" and job.results_dir
                and Path(job.results_dir).is_dir())

    if not paid_for:
        if mock:
            log.info("Mock pass: leaving the corrections for %s to a real run.",
                     file.name)
            return
        if rec.corrections_attempts >= ws.max_attempts:
            reason = f"Gave up after {rec.corrections_attempts} attempts."
            log.error("%s: %s", file.name, reason)
            mark_source(token, file, rec, state, status=CORRECTIONS_FAILED,
                        reason=reason, opener=opener)
            report.failed.append((file.name, reason))
            return
        job = _prepare(token, home, ws, work, rec, state, runner, store,
                       opener=opener, hs_token=hs_token, report=report)
        if job is None:
            return                        # reported; a person owns it now
        if job.state != "done":
            raise CorrectionsFailed(job.error or "The corrections did not finish.")
    else:
        log.info("%s already has its corrections applied; picking up from "
                 "there.", file.name)

    report.corrected.append(
        f"{file.name}: {job.applied_comments or job.applied or 0} of "
        f"{job.total_comments or (job.applied or 0) + (job.flags or 0)} applied")
    uploaded = upload_outputs(token, file, job, ws, rec, state, work.listing,
                              dest_folder_id=work.folder_id, opener=opener)
    report.uploaded.extend(uploaded)
    _finish_hubspot(hs_token, ws, file, rec, state, opener=opener)
    mark_source(token, file, rec, state, status=CORRECTIONS_DONE, opener=opener)


def _prepare(token: str, home: Path, ws: WatchSettings, work: Work, rec,
             state: WatchState, runner: JobRunner, store: JobStore, *, opener,
             hs_token: str | None, report) -> Job | None:
    """Download the export and the submission, read the submission into an edit
    list, and run the corrections job. Returns the finished job, or None when
    the submission itself is the problem — reported and marked failed, since a
    person has to fix the form before a retry could go differently."""
    from docproof.corrections.intake import IntakeError, read_submission

    existing = store.get(rec.corrections_job_id) if rec.corrections_job_id else None
    if existing is not None and existing.state not in ("done",):
        # A run that died mid-way: the edit list is on the record, so run it
        # again rather than re-reading the submission at a model's price.
        existing.state = "queued"
        existing.error = None
        return run_job(runner, store, existing)

    dest = Path(home) / DOWNLOADS / work.idml.id
    dest.mkdir(parents=True, exist_ok=True)
    local = drive.download(token, work.idml.id, dest / _safe(work.idml.name),
                           opener=opener)
    submission = fetch_submission(hs_token or "", work, dest, opener=opener)
    proof_pdf = None
    if work.proof_pdf is not None and submission.kind != "pdf":
        proof_pdf = drive.download(token, work.proof_pdf.id,
                                   dest / _safe(work.proof_pdf.name),
                                   opener=opener)

    rec.corrections_input_kind = submission.kind
    rec.corrections_input_name = submission.path.name if submission.path else ""
    state.record(rec)

    provider_model = extract_provider()
    usage = Usage()
    try:
        source = submission.path if submission.path is not None else submission.text
        intake = read_submission(
            source, idml_path=local,
            provider=provider_model[0] if provider_model else None,
            model=provider_model[1] if provider_model else EXTRACT_MODEL,
            usage=usage, proof_pdf=proof_pdf)
        # A file AND typed text: the text is read too, and its edits ride along.
        if submission.path is not None and submission.text:
            extra = read_submission(
                submission.text, idml_path=local,
                provider=provider_model[0] if provider_model else None,
                model=provider_model[1] if provider_model else EXTRACT_MODEL,
                usage=usage, proof_pdf=proof_pdf)
            intake = _merge_intakes(intake, extra)
    except IntakeError as e:
        reason = f"the corrections could not be read: {e}"
        log.warning("Needs a person: %s (%s)", work.idml.name, reason)
        report.needs_human.append((work.idml.name, reason))
        mark_source(token, work.idml, rec, state, status=CORRECTIONS_FAILED,
                    reason=reason, opener=opener)
        return None
    _record_intake_spend(runner, work, usage,
                         provider_model[1] if provider_model else "")
    if not intake.edits and not intake.comments:
        reason = ("the submission was read but yielded no corrections to apply "
                  f"({submission.kind}: {rec.corrections_input_name or 'typed'}).")
        log.warning("Needs a person: %s (%s)", work.idml.name, reason)
        report.needs_human.append((work.idml.name, reason))
        mark_source(token, work.idml, rec, state, status=CORRECTIONS_FAILED,
                    reason=reason, opener=opener)
        return None

    job = make_job(local, intake, ws)
    rec.corrections_job_id = job.id
    state.record(rec)                     # before the run, never after
    return run_job(runner, store, job)


def _merge_intakes(a, b):
    """Two intake results as one job's inputs: the edit lists concatenated, the
    comments and pages from whichever carried them (a PDF; text has none)."""
    import json
    from docproof.corrections.intake import IntakeResult
    edits = json.loads(a.corrections or "[]") + json.loads(b.corrections or "[]")
    return IntakeResult(
        corrections=json.dumps(edits, indent=2, ensure_ascii=False),
        comments=a.comments or b.comments, pages=a.pages or b.pages,
        edits=len(edits), issues=tuple(a.issues) + tuple(b.issues),
        source_kind=a.source_kind, comments_total=a.comments_total
        or b.comments_total, page_texts_from=a.page_texts_from
        or b.page_texts_from)


def _safe(name: str) -> str:
    return name.replace("/", "-").replace(":", "-").strip() or "file"


def fetch_submission(hs_token: str, work: Work, dest_dir: Path, *,
                     opener) -> Submission:
    """The form's file onto disk, and its typed text. One file is read; a form
    that attached several has its first PDF or .docx taken and the rest logged,
    because the corrections are one list for one book."""
    path: Path | None = None
    kind = ""
    for url in work.file_urls:
        got = hubspot.download_file(hs_token, url, dest_dir, opener=opener)
        suffix = got.suffix.lower()
        if suffix == ".pdf":
            path, kind = got, "pdf"
            break
        if suffix == ".docx":
            path, kind = got, "docx"
            break
        log.warning("Ignoring the form's %s: only a PDF proof or a .docx list "
                    "can be read.", got.name)
    if path is None and work.text:
        kind = "text"
    return Submission(path=path, text=work.text, kind=kind)


def extract_provider():
    """The `(provider, model)` that reads a typed list, or None when its vendor
    has no key here — the intake then runs the deterministic paths only and
    says so for the rest."""
    from docproof.config import load_config
    from docproof.providers import build_provider, lookup
    model = EXTRACT_MODEL
    try:
        from app.routes.jobs import CORRECTIONS_EXTRACT_MODEL
        model = CORRECTIONS_EXTRACT_MODEL
    except Exception:                     # noqa: BLE001 - no FastAPI here
        pass
    try:
        info = lookup(model)
        key = get_api_key(info.provider) if info else None
        if not key:
            log.warning("No key for %s; corrections will be read without a "
                        "model (tracked changes and PDF rules only).", model)
            return None
        cfg = load_config(resource_root() / "config" / "default.yaml")
        cfg.api.model = model
        return build_provider(cfg, api_key=key), model
    except Exception:                     # noqa: BLE001 - a missing reader, not a crash
        log.warning("Could not build the corrections reader; reading without "
                    "a model", exc_info=True)
        return None


def _record_intake_spend(runner: JobRunner, work: Work, usage: Usage,
                         model: str) -> None:
    """The small model spend of reading the submission, on the ledger under the
    corrections kind so the dashboard is honest. Best-effort."""
    if not usage.api_calls:
        return
    try:
        from docproof.providers import cost_of_usage
        from app.spending import LedgerEntry
        cost = cost_of_usage(usage, fallback_model=model, batch=False)
        stamp = datetime.now(timezone.utc)
        runner.ledger.record(LedgerEntry(
            id=f"cx-{stamp.strftime('%Y%m%d%H%M%S%f')}",
            filename=f"corrections intake - {work.idml.name}",
            kind="corrections", model=model, mode="now", source="watch",
            owner_id="", created_at=stamp.isoformat(), words=0,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            api_calls=usage.api_calls, cost=cost or 0.0))
    except Exception:                     # noqa: BLE001 - spend logging is not the job
        log.warning("Could not record the corrections intake spend",
                    exc_info=True)


def make_job(local_idml: Path, intake, ws: WatchSettings) -> Job:
    """The app's own corrections job over the downloaded export, with the model
    passes at the panel's defaults unless the watcher turned them off."""
    on = bool(ws.corrections_model_passes)
    return Job(id=new_job_id(local_idml.name), filename=local_idml.name,
               source_path=str(local_idml), model="", mode="now",
               kind="corrections", corrections=intake.corrections,
               corrections_comments=intake.comments,
               corrections_pages=intake.pages,
               corrections_sanity=on, corrections_second_look=on,
               corrections_escalate=on, source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job) -> Job:
    store.save(job)
    try:
        runner.run_one(job.id)
    except Exception as e:                # noqa: BLE001 - mirrors the other stages
        log.exception("Applying corrections to %s failed", job.filename)
        store.update(job.id, state="failed", error=str(e))
    return store.get(job.id) or job


# --- delivery ------------------------------------------------------------------

def artifacts(job: Job, source_name: str) -> list[Artifact]:
    """The hand-off set under the "<surname> - Book N.5" base: the corrected
    IDML, the two-sheet spreadsheet, the notes and the InDesign check tour.
    `corrections.json` stays local — it is the fix screen's record."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    names = hand_off_names(source_name)
    found: list[Artifact] = []
    idml = next(iter(sorted(out.glob("*_corrected.idml"))), None)
    if idml is None:
        return []
    found.append(Artifact(idml, names["idml"], IDML_MIME))
    sheet = next(iter(sorted(out.glob("*.xlsx"))), None)
    if sheet is not None:
        found.append(Artifact(sheet, names["sheet"], XLSX_MIME))
    notes = out / "corrections_notes.md"
    if notes.is_file():
        found.append(Artifact(notes, names["notes"], MARKDOWN_MIME))
    tour = next(iter(sorted(out.glob("*_checks.jsx"))), None)
    if tour is not None:
        found.append(Artifact(tour, names["checks"], JSX_MIME))
    return found


def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str,
                   opener=drive._open_url) -> list[str]:
    """Put the hand-off in the folder, once, recording each id as it lands."""
    placed = []
    for artifact in artifacts(job, file.name):
        if artifact.name in rec.corrections_uploaded:
            continue
        orphan = _already_there(listing, file.id, artifact.name)
        if orphan is not None:
            log.info("%s is already in the folder from an earlier run",
                     artifact.name)
            rec.corrections_uploaded[artifact.name] = orphan.id
            state.record(rec)
            continue
        new_id = drive.upload(token, dest_folder_id, artifact.path,
                              name=artifact.name, mime_type=artifact.mime,
                              app_properties={OUTPUT_PROP: "1",
                                              SOURCE_PROP: file.id,
                                              JOB_PROP: job.id},
                              opener=opener)
        rec.corrections_uploaded[artifact.name] = new_id
        state.record(rec)
        placed.append(artifact.name)
    return placed


def _finish_hubspot(hs_token: str | None, ws: WatchSettings, file: DriveFile,
                    rec, state: WatchState, *, opener) -> None:
    """Move the status to the done value — the designer's cue — exactly once."""
    if not (ws.hubspot_enabled and rec.corrections_hubspot_id
            and not rec.corrections_hubspot_done):
        return
    if not ws.hubspot_write_back:
        log.info("HubSpot is read-only: leaving %s at its current status.",
                 file.name)
        return
    value = ws.hubspot_corrections_done_value
    if not value:
        log.warning("The HubSpot corrections done value is empty: leaving %s "
                    "at its current status rather than blanking it.", file.name)
        return
    hubspot.set_properties(hs_token, ws.hubspot_object,
                           rec.corrections_hubspot_id,
                           {ws.hubspot_status_property: value},
                           allow={ws.hubspot_status_property}, opener=opener)
    rec.corrections_hubspot_done = True
    state.record(rec)                     # recorded before the Drive marker


def mark_source(token: str, file: DriveFile, rec: FileRecord,
                state: WatchState, *, status: str, reason: str | None = None,
                opener=drive._open_url) -> None:
    """Write the stage's marker on the source IDML — last, so `done` means the
    hand-off is in the folder and HubSpot has moved on."""
    props = {CORRECTIONS_PROP: status,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason:
        props[REASON_PROP] = reason[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.corrections_marked = status
    state.record(rec)
