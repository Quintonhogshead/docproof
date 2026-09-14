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

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from .state import FileRecord, PendingCorrections, WatchState

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

__all__ = ["Work", "Submission", "CorrectionsFailed", "discover",
           "gather_submissions", "hold_or_release", "ready_at",
           "pending_summary", "run_stage", "pick_source", "proof_pdf_for",
           "hand_off_names", "artifacts", "make_job", "run_job",
           "upload_outputs", "verify_uploads", "mark_source",
           "extract_provider", "fetch_submission", "fetch_submissions"]


class CorrectionsFailed(RuntimeError):
    """A book did not come through for a reason that might not happen again — so
    it is counted against the file rather than marked on it."""


@dataclass(frozen=True)
class Work:
    """One ready record, resolved to the file to correct and what to apply.

    `submissions` is every submission folded into this job (see
    `gather_submissions`); `file_urls` and `text` are kept for compatibility
    with anything still reading them directly — derived from `submissions`,
    all urls across every submission and every submission's text joined,
    each prefixed with its marker when there is more than one."""

    record_id: str
    first: str
    last: str
    author: str
    folder_id: str                   # the "Interior Design" folder — outputs go here
    idml: DriveFile
    listing: list[DriveFile] = field(default_factory=list)
    proof_pdf: DriveFile | None = None
    submissions: tuple[dict, ...] = ()
    file_urls: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class Submission:
    """What the author sent, on disk: a file (PDF or .docx) and/or typed text.

    `tag` names which submission (its marker, or its file name when no marker
    applies) this came from, so a folded edit list can say which submission
    each edit rode in on — see `_tag_source`."""

    path: Path | None = None
    text: str = ""
    kind: str = ""                   # "pdf" | "docx" | "text"
    tag: str = ""

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
             *, opener, report, only_record: str | None = None,
             ignore_timer: bool = False, now: datetime | None = None
             ) -> list[Work]:
    """Ask HubSpot who is ready, hold each one for its quiet period, then look
    only in the released authors' folders.

    HubSpot drives, Drive follows — the parent Author Folder is never listed.
    A record still inside its quiet period (`hold_or_release`) is counted as
    waiting and left for a later pass, unless `ignore_timer` — a rehearsal or a
    `--record` rerun — says to run it now regardless. `only_record` narrows the
    whole pass to one HubSpot record id. Every author that cannot be resolved
    to exactly one file to correct is reported (`report.needs_human` /
    `missing_source` / `stuck_ready`) and left where it is; nothing here
    guesses. Returns the books this pass can work."""
    now = now or datetime.now(timezone.utc)
    _drop_finished_pending(state)
    want = [p for p in (ws.hubspot_status_property, ws.hubspot_first_property,
                        ws.hubspot_last_property,
                        ws.hubspot_corrections_file_property,
                        ws.hubspot_corrections_text_property,
                        ws.hubspot_corrections_book_property) if p]
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

    # Read the form once for the whole pass — every ready record is matched
    # against the same page of events, rather than one HTTP round trip apiece.
    # None (as opposed to an empty list) means "did not even try", which is
    # how `gather_submissions` tells "the form has nothing" apart from "the
    # form could not be read this pass".
    form_rows: list[dict] | None = None
    if ws.corrections_form_poll:
        try:
            form_rows = hubspot.form_submissions(hs_token, ws.corrections_form_id,
                                                 opener=opener)
        except HubSpotError as e:
            log.warning("Could not read the corrections form (%s); reading "
                        "each ready record's own properties this pass "
                        "instead.", e)
            form_rows = []

    works: list[Work] = []
    seen: set[str] = set()
    for record in ready:
        if only_record and record.id != only_record:
            continue
        seen.add(record.id)
        first = (record.properties.get(ws.hubspot_first_property) or "").strip()
        last = (record.properties.get(ws.hubspot_last_property) or "").strip()
        author = folders.compose(first, last) if first and last else record.id
        submissions = gather_submissions(hs_token, ws, record, opener=opener,
                                         form_rows=form_rows)
        if submissions:
            released, entry = hold_or_release(state, ws, record.id, author,
                                              submissions, now=now)
            if not released and not ignore_timer:
                report.waiting += 1
                log.info("Holding %s until %s (interior corrections quiet "
                        "period).", author, ready_at(entry, ws).isoformat())
                continue
        work = _resolve(token, ws, record, opener=opener, report=report,
                        submissions=submissions)
        if work is not None:
            entry = state.corrections_pending.get(record.id)
            if entry is not None and entry.source_name != work.idml.name:
                entry.source_name = work.idml.name
                state.corrections_pending[record.id] = entry
                state.save()
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


def _drop_finished_pending(state: WatchState) -> None:
    """Forget a pending record once its file has reached a terminal marker —
    so an author submitting a second round of corrections after "Corrections
    Applied" starts a fresh quiet period rather than resuming the old one."""
    if not state.corrections_pending:
        return
    finished = {rec.corrections_hubspot_id for rec in state.files.values()
               if rec.corrections_hubspot_id
               and rec.corrections_marked in CORRECTIONS_TERMINAL}
    stale = finished & set(state.corrections_pending)
    if not stale:
        return
    for record_id in stale:
        del state.corrections_pending[record_id]
    state.save()


# --- submissions and the quiet period --------------------------------------------

def gather_submissions(hs_token: str, ws: WatchSettings, record, *, opener,
                       form_rows: list[dict] | None = None) -> list[dict]:
    """Every submission this ready record carries, oldest first.

    Form-poll mode matches the record against `form_rows` (fetched once by
    `discover` and passed down; fetched here when a caller has none to hand)
    by CRM association first and by first/last name otherwise, each row
    becoming `{"marker", "submitted_at", "urls", "text"}`. A 403 or any other
    `HubSpotError` reading the form falls back to property mode rather than
    stopping the pass. Property mode (the default) reads the two CRM
    properties the workflow copies the form into and returns them as one
    submission, its marker a hash of what it carries — deterministic, so the
    same unread submission is not recounted as new on every tick. Empty when
    the record carries nothing to correct."""
    if ws.corrections_form_poll:
        rows = form_rows
        if rows is None:
            try:
                rows = hubspot.form_submissions(hs_token, ws.corrections_form_id,
                                                opener=opener)
            except HubSpotError as e:
                log.warning("Could not read the corrections form (%s) for "
                            "%s; reading its own properties instead.",
                            e, record.id)
                rows = []
        if rows:
            matched = _match_form_rows(record, rows, ws)
            if matched:
                return matched
    urls, text = _property_submission(ws, record)
    if not urls and not text:
        return []
    marker = hashlib.sha256(json.dumps(
        {"urls": urls, "text": text}, sort_keys=True).encode()).hexdigest()[:16]
    return [{"marker": marker, "submitted_at": "", "urls": urls, "text": text}]


def _property_submission(ws: WatchSettings, record) -> tuple[list[str], str]:
    urls = hubspot.file_urls(
        record.properties.get(ws.hubspot_corrections_file_property, ""))
    text = (record.properties.get(ws.hubspot_corrections_text_property)
            or "").strip()
    return urls, text


def _mapped_values(row: dict) -> dict[str, str]:
    values = row.get("values") or row.get("fields") or []
    if isinstance(values, dict):
        values = [{"name": k, "value": v} for k, v in values.items()]
    return {str(item.get("name", "")).casefold(): str(item.get("value", "") or "")
            for item in values if isinstance(item, dict)}


def _match_form_rows(record, rows: list[dict], ws: WatchSettings) -> list[dict]:
    """Every form row that belongs to `record` — a CRM association first, a
    case-insensitive first/last match otherwise — as submission dicts, oldest
    first."""
    first = (record.properties.get(ws.hubspot_first_property) or "").strip().casefold()
    last = (record.properties.get(ws.hubspot_last_property) or "").strip().casefold()
    file_field = (ws.corrections_form_file_property or "").casefold()
    notes_field = (ws.corrections_form_notes_property or "").casefold()
    cutoff = hubspot.timestamp_value(ws.corrections_form_start_after)
    matched: list[dict] = []
    for row in rows:
        submitted = hubspot.timestamp_value(row.get("submittedAt"))
        if cutoff and submitted and submitted <= cutoff:
            continue                      # a round the press handled by hand
        associated = str(row.get("recordId") or row.get("objectId") or "") == record.id
        mapped = _mapped_values(row)
        if not associated:
            row_first = mapped.get("firstname", "").strip().casefold()
            row_last = mapped.get("lastname", "").strip().casefold()
            if not (first and last and row_first == first and row_last == last):
                continue
        urls = hubspot.file_urls(mapped.get(file_field, "")) if file_field else []
        text = mapped.get(notes_field, "").strip() if notes_field else ""
        matched.append({
            "marker": hubspot.row_marker(row),
            "submitted_at": (datetime.fromtimestamp(submitted, tz=timezone.utc)
                             .isoformat(timespec="seconds") if submitted else ""),
            "urls": urls, "text": text})
    matched.sort(key=lambda s: s["submitted_at"])
    return matched


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ready_at(entry: PendingCorrections, ws: WatchSettings) -> datetime:
    """When this pending record's quiet period ends: `corrections_quiet_
    seconds` after the later of when it was first seen and its latest new
    submission — so every new submission earns its own fresh wait."""
    first = _parse_iso(entry.first_seen) or datetime.now(timezone.utc)
    last = _parse_iso(entry.last_submission_at) or first
    return max(first, last) + timedelta(seconds=ws.corrections_quiet_seconds)


def hold_or_release(state: WatchState, ws: WatchSettings, record_id: str,
                    author: str, submissions: list[dict], *,
                    now: datetime) -> tuple[bool, PendingCorrections]:
    """Fold newly seen `submissions` into this record's pending entry and say
    whether its quiet period has elapsed. Pure over `now`, so a test can move
    the clock instead of waiting on it.

    A marker already folded into the file this record has already produced (a
    job resumed after it went stale, or a stray re-poll) is never counted as
    new — only a marker genuinely not seen before resets the clock."""
    now_iso = now.isoformat(timespec="seconds")
    entry = state.corrections_pending.get(record_id)
    if entry is None:
        entry = PendingCorrections(record_id=record_id, author=author,
                                   first_seen=now_iso)
    if author and not entry.author:
        entry.author = author
    already_folded = {marker for rec in state.files.values()
                      if rec.corrections_hubspot_id == record_id
                      for marker in rec.corrections_submissions}
    known = {s.get("marker") for s in entry.submissions if s.get("marker")}
    known |= already_folded
    added = False
    for sub in submissions:
        marker = sub.get("marker") or ""
        if marker and marker in known:
            continue
        entry.submissions.append(dict(sub))
        if marker:
            known.add(marker)
        added = True
    if added:
        entry.last_submission_at = now_iso
    state.corrections_pending[record_id] = entry
    state.save()
    return now >= ready_at(entry, ws), entry


def pending_summary(state: WatchState, ws: WatchSettings, *,
                    now: datetime | None = None) -> list[dict]:
    """Every record currently held for its quiet period, for `status` and the
    panel to show without either re-reading HubSpot."""
    now = now or datetime.now(timezone.utc)
    out = []
    for entry in state.corrections_pending.values():
        due = ready_at(entry, ws)
        out.append({
            "record_id": entry.record_id, "author": entry.author,
            "first_seen": entry.first_seen,
            "last_submission_at": entry.last_submission_at,
            "ready_at": due.isoformat(timespec="seconds"),
            "ready": now >= due, "submissions": len(entry.submissions),
            "source_name": entry.source_name})
    return out


def _resolve(token: str, ws: WatchSettings, record, *, opener, report,
            submissions: list[dict] | None = None) -> Work | None:
    submissions = submissions if submissions is not None else []
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
        book_folders = _book_folders(token, author_folder, ws, opener=opener)
        if not book_folders:
            detail = (f"its folder has no '{ws.corrections_folder_name}' "
                      f"subfolder to hold the designer's IDML")
            log.info("Waiting: %s is flagged '%s' but %s.", author, ready, detail)
            report.missing_source.append(
                (author, f"flagged '{ready}' but {detail}."))
            report.waiting += 1
            return None
        # A multi-book author: each book has its own subfolder one level below
        # the author folder, each holding its own "Interior Design". The
        # record's own book title is the only thing that says which one this
        # round belongs to — see `_match_book_folder`.
        prop = ws.hubspot_corrections_book_property
        title = (record.properties.get(prop) or "").strip() if prop else ""
        if not title:
            reason = (f"the record has no '{prop or 'book title'}' so "
                      f"DocProof cannot tell which of {len(book_folders)} "
                      f"books this form is for.")
            log.warning("Needs a person: %s (%s)", author, reason)
            report.needs_human.append((author, reason))
            report.waiting += 1
            return None
        match = _match_book_folder(book_folders, title)
        if match is None:
            candidates = ", ".join(f"'{b.name}'" for b, _ in book_folders)
            reason = (f"'{title}' does not match exactly one of {author}'s "
                      f"book folders ({candidates}), so DocProof cannot tell "
                      f"which book this form is for.")
            log.warning("Needs a person: %s (%s)", author, reason)
            report.needs_human.append((author, reason))
            report.waiting += 1
            return None
        _book_folder, interior = match

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
        elif why == "indd-only":
            indd = _highest_indd(listing, last)
            stem = Path(indd.name).stem
            detail = (f"its '{ws.corrections_folder_name}' folder holds "
                      f"'{indd.name}' and no IDML export of it — in InDesign "
                      f"open it and choose File → Export → InDesign Markup "
                      f"(IDML), saved beside it as '{stem}.idml'")
            log.info("Waiting: %s is flagged '%s' but %s.", author, ready, detail)
            report.missing_source.append((author,
                                          f"flagged '{ready}' but {detail}."))
        elif why == "idml-stale":
            indd = _highest_indd(listing, last)
            idml_files = [f for f in listing if not f.is_folder
                         and naming.is_idml_source_name(f.name, last)]
            newest_idml = max(idml_files,
                              key=lambda f: naming.idml_version(f.name)[1])
            stem = Path(indd.name).stem
            detail = (f"its '{ws.corrections_folder_name}' folder holds "
                      f"'{indd.name}' but the newest IDML is "
                      f"'{newest_idml.name}' — export {_book_token(stem)} as "
                      f"IDML, saved beside it as '{stem}.idml'")
            log.info("Waiting: %s is flagged '%s' but %s.", author, ready, detail)
            report.missing_source.append((author,
                                          f"flagged '{ready}' but {detail}."))
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

    urls, text = _derive_urls_text(submissions)
    if not urls and not text:
        reason = (f"flagged '{ready}' but the record carries no corrections — "
                  f"neither an uploaded file nor typed text reached the "
                  f"'{ws.hubspot_corrections_file_property or '—'}' / "
                  f"'{ws.hubspot_corrections_text_property or '—'}' properties"
                  + (" or the corrections form" if ws.corrections_form_poll
                     else "") + ". Check the form workflow.")
        log.warning("Needs a person: %s (%s)", author, reason)
        report.needs_human.append((author, reason))
        report.waiting += 1
        return None

    return Work(record_id=record.id, first=first, last=last, author=author,
                folder_id=interior.id, idml=idml, listing=listing,
                proof_pdf=proof_pdf_for(listing, idml),
                submissions=tuple(submissions), file_urls=urls, text=text)


def _derive_urls_text(submissions: list[dict]) -> tuple[tuple[str, ...], str]:
    """`Work.file_urls` / `Work.text` from every folded submission: every url
    (deduplicated, in order), and every submission's text joined with a blank
    line — each prefixed with its marker once there is more than one, so a
    person reading the job's `corrections_comments`/edit list can still tell
    which typed note came from which round."""
    urls: list[str] = []
    for sub in submissions:
        for u in sub.get("urls") or []:
            if u not in urls:
                urls.append(u)
    texts = [(sub.get("marker") or "", (sub.get("text") or "").strip())
             for sub in submissions if (sub.get("text") or "").strip()]
    if len(texts) > 1:
        joined = "\n\n".join(f"Submission {marker}:\n{text}" if marker else text
                             for marker, text in texts)
    else:
        joined = texts[0][1] if texts else ""
    return tuple(urls), joined


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


def _book_folders(token: str, author_folder: str, ws: WatchSettings, *,
                  opener) -> list[tuple[DriveFile, DriveFile]]:
    """A multi-book author's book folders, one level below the author folder —
    every subfolder that itself holds a `ws.corrections_folder_name` child —
    as `(book_folder, interior_folder)` pairs. Empty for a single-book author,
    whose "Interior Design" sits directly under the author folder and is
    already handled before this is ever called."""
    out: list[tuple[DriveFile, DriveFile]] = []
    for sub in drive.find_children(token, author_folder, folders_only=True,
                                   opener=opener):
        child = _child_folder(token, sub.id, ws.corrections_folder_name,
                              opener=opener)
        if child is not None:
            out.append((sub, child))
    return out


def _norm_title(text: str) -> str:
    """A book title reduced to its words, so case, spacing and punctuation
    never keep a real match from landing."""
    return " ".join(re.findall(r"\w+", text.casefold()))


def _match_book_folder(book_folders: list[tuple[DriveFile, DriveFile]],
                       title: str) -> tuple[DriveFile, DriveFile] | None:
    """The one `(book_folder, interior_folder)` pair whose book folder is
    `title` — exact (case/space/punctuation-insensitive) first; when nothing
    matches exactly, a folder name that contains the normalized title or is
    contained by it, but only when exactly one folder does. None when it is
    not exactly one either way."""
    want = _norm_title(title)
    exact = [pair for pair in book_folders if _norm_title(pair[0].name) == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    loose = [pair for pair in book_folders
             if want in _norm_title(pair[0].name)
             or _norm_title(pair[0].name) in want]
    return loose[0] if len(loose) == 1 else None


def _highest_indd(listing: list[DriveFile], last: str) -> DriveFile | None:
    """The designer's `.indd` at the highest integer version for `last` — read
    only to name it in a "no IDML export" report, never opened."""
    candidates = [f for f in listing if not f.is_folder
                 and naming.is_indd_source_name(f.name, last)]
    if not candidates:
        return None
    return max(candidates, key=lambda f: naming.indd_version(f.name)[1])


def _book_token(stem: str) -> str:
    """The "Book N" token out of a designer filename's stem, spelled exactly
    as the designer wrote it — for a short "export Book 4 as IDML" line that
    does not repeat the whole file name back."""
    m = re.search(r"book\s*\d+(?:\.\d+)?", stem, re.IGNORECASE)
    return m.group(0) if m else stem


def pick_source(listing: list[DriveFile], last: str
                ) -> tuple[DriveFile | None, str]:
    """The designer's latest export — the highest integer "<surname> - Book N
    .idml" — or `(None, why)`: "none" (no such file, and no `.indd` either),
    "indd-only" (the highest `.indd` has no `.idml` export at all), "idml-
    stale" (the highest `.idml` is behind the highest `.indd`), "tie" (two
    `.idml` at the top), "done" (the latest already has its corrections
    applied) or "failed" (it was tried and marked failed).

    The engine only ever reads IDML — a `.indd` is never opened — but it is
    still worth spotting, so a designer who forgot to export is told exactly
    that instead of a bare "no export here". Pure, so it is tested without
    Drive."""
    exports = [(naming.idml_version(f.name)[1], f) for f in listing
               if not f.is_folder and naming.is_idml_source_name(f.name, last)]
    indd_versions = [naming.indd_version(f.name)[1] for f in listing
                     if not f.is_folder and naming.is_indd_source_name(f.name, last)]
    top_indd = max(indd_versions) if indd_versions else None
    if not exports:
        return None, ("indd-only" if top_indd is not None else "none")
    top = max(v for v, _ in exports)
    latest = [f for v, f in exports if v == top]
    if len(latest) > 1:
        return None, "tie"
    if top_indd is not None and top < top_indd:
        return None, "idml-stale"
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
              hs_token: str | None, report, only_record: str | None = None,
              ignore_timer: bool = False, dry_run: bool = False) -> None:
    """Correct every book HubSpot flagged, one at a time, each inside its own
    guard so one bad book never stops the pass.

    `only_record` and `ignore_timer` pass straight through to `discover` — a
    manual rerun of one record, or a rehearsal that should not sit through the
    quiet period. `dry_run` (like `mock`) previews `_one` rather than running
    it: see there."""
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
    works = discover(hs_token, token, ws, state, opener=opener, report=report,
                     only_record=only_record, ignore_timer=ignore_timer)
    works.sort(key=lambda w: (w.idml.modified_time, w.idml.name))
    if len(works) > ws.max_files_per_tick:
        report.deferred += len(works) - ws.max_files_per_tick
        log.info("%d books are waiting for corrections; doing %d this run and "
                 "leaving the rest.", len(works), ws.max_files_per_tick)
        works = works[:ws.max_files_per_tick]
    for work in works:
        try:
            _one(token, home, ws, work, state, runner, store, mock=mock,
                 opener=opener, hs_token=hs_token, report=report,
                 dry_run=dry_run)
        except Exception as e:            # noqa: BLE001 - one book, not the run
            log.exception("Could not apply corrections to %s", work.idml.name)
            report.failed.append((work.idml.name, str(e)))
            rec = state.get(work.idml.id)
            rec.name = work.idml.name
            rec.corrections_attempts += 1
            state.record(rec)


def _one(token: str, home: Path, ws: WatchSettings, work: Work,
         state: WatchState, runner: JobRunner, store: JobStore, *, mock: bool,
         opener, hs_token: str | None, report, dry_run: bool = False) -> None:
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
        if mock or dry_run:
            report.corrected.append(_plan_line(file.name, work))
            log.info("%s pass: leaving the corrections for %s to a real run.",
                     "Rehearsal" if dry_run else "Mock", file.name)
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

    # Read every upload back from Drive before trusting it: an id in
    # `corrections_uploaded` only means the multipart POST answered, not that
    # the bytes really landed. A mismatch is given one reupload, since a
    # dropped connection or a stale id is often gone on retry; only a second
    # failure stops the book, and it stops it before HubSpot or the source
    # marker move, so a broken hand-off is never announced as finished.
    failed = verify_uploads(token, rec, job, file.name, state, opener=opener)
    if failed:
        upload_outputs(token, file, job, ws, rec, state, work.listing,
                       dest_folder_id=work.folder_id, opener=opener)
        failed = verify_uploads(token, rec, job, file.name, state, opener=opener)
    if failed:
        raise CorrectionsFailed(
            "the hand-off did not land intact in Drive: " + ", ".join(failed))
    log.info("Verified %d file(s) in Drive", len(artifacts(job, file.name)))

    _finish_hubspot(hs_token, ws, file, rec, state, opener=opener)
    mark_source(token, file, rec, state, status=CORRECTIONS_DONE, opener=opener)


def _plan_line(name: str, work: Work) -> str:
    """The one line a mock or dry-run pass reports instead of actually
    applying anything — what would have happened, in the same words either
    posture uses."""
    n = len(work.submissions) or (1 if (work.file_urls or work.text) else 0)
    k = len(work.file_urls)
    typed = "yes" if work.text else "no"
    return f"{name}: would apply {n} submission(s) ({k} file(s), typed text: {typed})"


def _prepare(token: str, home: Path, ws: WatchSettings, work: Work, rec,
             state: WatchState, runner: JobRunner, store: JobStore, *, opener,
             hs_token: str | None, report) -> Job | None:
    """Download the export and every folded submission, read each into an edit
    list, and run the corrections job over all of them merged. Returns the
    finished job, or None when a submission itself is the problem — reported
    and marked failed, since a person has to fix the form before a retry
    could go differently."""
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
    submissions = fetch_submissions(hs_token or "", work, dest, opener=opener)
    proof_pdf = None
    if work.proof_pdf is not None and not any(s.kind == "pdf" for s in submissions):
        proof_pdf = drive.download(token, work.proof_pdf.id,
                                   dest / _safe(work.proof_pdf.name),
                                   opener=opener)

    kinds = [s.kind for s in submissions if s.kind]
    names = [s.path.name for s in submissions if s.path is not None]
    rec.corrections_input_kind = ", ".join(dict.fromkeys(kinds))
    rec.corrections_input_name = ", ".join(dict.fromkeys(names))
    state.record(rec)

    # Tagging a source only matters once there is more than one submission to
    # tell apart on the spreadsheet — a single submission keeps its edits'
    # `source` exactly as the extractor left them (empty, unless a PDF's own
    # comment linked one).
    tag_sources = sum(1 for s in submissions if not s.empty) > 1
    provider_model = extract_provider()
    usage = Usage()
    intakes = []
    try:
        for sub in submissions:
            if sub.empty:
                continue
            source = sub.path if sub.path is not None else sub.text
            intake = read_submission(
                source, idml_path=local,
                provider=provider_model[0] if provider_model else None,
                model=provider_model[1] if provider_model else EXTRACT_MODEL,
                usage=usage, proof_pdf=proof_pdf)
            if tag_sources:
                intake = _tag_source(intake, sub.tag or sub.kind)
            intakes.append(intake)
        if not intakes:
            raise IntakeError("no readable file or typed text was attached")
        intake = _merge_intakes(*intakes)
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
                  f"({rec.corrections_input_kind or 'typed'}: "
                  f"{rec.corrections_input_name or 'typed'}).")
        log.warning("Needs a person: %s (%s)", work.idml.name, reason)
        report.needs_human.append((work.idml.name, reason))
        mark_source(token, work.idml, rec, state, status=CORRECTIONS_FAILED,
                    reason=reason, opener=opener)
        return None

    job = make_job(local, intake, ws)
    rec.corrections_job_id = job.id
    # Every submission this job folded, so a later pending record never
    # recounts one already accounted for here — see `hold_or_release`.
    rec.corrections_submissions = list(dict.fromkeys(
        s.get("marker") or "" for s in work.submissions if s.get("marker")))
    state.record(rec)                     # before the run, never after
    return run_job(runner, store, job)


def _tag_source(intake, tag: str):
    """`intake`, with `tag` set as every edit's `source` where the extractor
    left it empty — never overwriting a PDF-linked edit's own comment id. See
    `docproof.corrections.model.Edit.source`."""
    if not tag:
        return intake
    from docproof.corrections.intake import IntakeResult
    rows = json.loads(intake.corrections or "[]")
    changed = False
    for row in rows:
        if not row.get("source"):
            row["source"] = tag
            changed = True
    if not changed:
        return intake
    return IntakeResult(
        corrections=json.dumps(rows, indent=2, ensure_ascii=False),
        comments=intake.comments, pages=intake.pages, edits=intake.edits,
        issues=intake.issues, source_kind=intake.source_kind,
        comments_total=intake.comments_total,
        page_texts_from=intake.page_texts_from)


def _merge_intakes(*intakes):
    """Every submission's intake result as one job's inputs: the edit lists
    concatenated — any explicit id that collides across submissions
    re-prefixed so it stays unique — and the comments and pages from whichever
    result carried them first (only a PDF submission ever does)."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for n, intake in enumerate(intakes):
        for row in json.loads(intake.corrections or "[]"):
            eid = row.get("id")
            if eid and eid in seen_ids:
                new_id, suffix = f"{eid}-{n}", 2
                while new_id in seen_ids:
                    new_id = f"{eid}-{n}-{suffix}"
                    suffix += 1
                row = {**row, "id": new_id}
                eid = new_id
            if eid:
                seen_ids.add(eid)
            rows.append(row)
    from docproof.corrections.intake import IntakeResult
    return IntakeResult(
        corrections=json.dumps(rows, indent=2, ensure_ascii=False),
        comments=next((i.comments for i in intakes if i.comments), ""),
        pages=next((i.pages for i in intakes if i.pages), ""),
        edits=len(rows),
        issues=tuple(issue for i in intakes for issue in i.issues),
        source_kind=intakes[0].source_kind if intakes else "",
        comments_total=next((i.comments_total for i in intakes
                            if i.comments_total), 0),
        page_texts_from=next((i.page_texts_from for i in intakes
                             if i.page_texts_from), ""))


def _safe(name: str) -> str:
    return name.replace("/", "-").replace(":", "-").strip() or "file"


def fetch_submissions(hs_token: str, work: Work, dest_dir: Path, *,
                      opener) -> list[Submission]:
    """Every folded submission's file(s) onto disk, and its typed text: every
    .pdf/.docx url across every submission (any other type logged and
    skipped), plus one text `Submission` per submission that carries typed
    notes. Each carries its submission's marker as `tag`, so a folded edit
    list can say which round it came from."""
    out: list[Submission] = []
    subs = work.submissions or ({"marker": "", "urls": list(work.file_urls),
                                 "text": work.text},)
    for sub in subs:
        tag = sub.get("marker") or ""
        for url in sub.get("urls") or []:
            got = hubspot.download_file(hs_token, url, dest_dir, opener=opener)
            suffix = got.suffix.lower()
            if suffix == ".pdf":
                out.append(Submission(path=got, kind="pdf", tag=tag))
            elif suffix == ".docx":
                out.append(Submission(path=got, kind="docx", tag=tag))
            else:
                log.warning("Ignoring the form's %s: only a PDF proof or a "
                            ".docx list can be read.", got.name)
        text = (sub.get("text") or "").strip()
        if text:
            out.append(Submission(text=text, kind="text", tag=tag))
    return out


def fetch_submission(hs_token: str, work: Work, dest_dir: Path, *,
                     opener) -> Submission:
    """The first of `fetch_submissions` — kept for compatibility with anything
    still reading one submission at a time."""
    got = fetch_submissions(hs_token, work, dest_dir, opener=opener)
    return got[0] if got else Submission()


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
        from .flags import current_outputs
        orphan = _already_there(current_outputs(listing, rec, "corrections", file),
                                file.id, artifact.name)
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


def _local_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def verify_uploads(token: str, rec: FileRecord, job: Job, source_name: str,
                   state: WatchState, *, opener=drive._open_url) -> list[str]:
    """Read every artifact `upload_outputs` says it placed back from Drive, and
    check it is really there: the right name, the right size, and — when Drive
    sends one back — the right content, by md5. An id `rec.corrections_uploaded`
    has no entry for at all counts as failed too, the same as one that fails
    the check.

    Returns the artifact names that failed. A failed name is dropped from
    `rec.corrections_uploaded` (and the record saved) so the next attempt
    uploads it again rather than trusting a bad id forever."""
    failed: list[str] = []
    for artifact in artifacts(job, source_name):
        file_id = rec.corrections_uploaded.get(artifact.name)
        ok = False
        if file_id:
            found = drive.get_file(token, file_id, opener=opener,
                                   with_parents=True)
            ok = (found.name == artifact.name
                  and found.size == artifact.path.stat().st_size
                  and (not found.md5_checksum
                       or found.md5_checksum == _local_md5(artifact.path)))
        if not ok:
            failed.append(artifact.name)
            if file_id:
                rec.corrections_uploaded.pop(artifact.name, None)
                state.record(rec)
    return failed


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
