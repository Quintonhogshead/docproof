"""Drive-only Book Original intake using the existing manuscript exporter. No author-name/CRM matching."""
from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
import re

from app.jobs import JobRunner, JobStore
from app.settings import Paths, get_api_key
from . import drive
from .settings import GOOGLE_KEY
from .stages import AT_PROP, FAILED, FORMATTED, JOB_PROP, OUTPUT_PROP, SOURCE_PROP, STATE_PROP
from .state import WatchState, note_tick

# Match the label wherever it occurs, without needing a surname or separator.
ORIGINAL = re.compile(r"\bbook[\s_‐‑‒–—―−-]+original\b", re.I)
DONE = re.compile(r"\bbook[\s_‐‑‒–—―−-]+original_done\b", re.I)
DELIVERED = re.compile(
    r"(?:^|[\s‐‑‒–—―−-])book\s*(?:\d+(?:\.\d+)?|zero|one|two)\s*$", re.I)


def source(file):
    return (not file.is_folder and not file.app_properties.get(OUTPUT_PROP)
            and not DONE.search(file.name) and bool(ORIGINAL.search(file.name))
            and (Path(file.name).suffix.lower() == ".docx"
                 or (not Path(file.name).suffix and file.mime_type == drive.DOCX_MIME)))


def done_name(name):
    return ORIGINAL.sub(lambda m: m.group() + "_done", name, count=1)


def output_name(name):
    stem = Path(name).stem if Path(name).suffix else name
    return ORIGINAL.sub("Book One", stem, count=1) + ".docx"


def _folders(token, parent, opener, report):
    pending = [parent]
    visited = set()
    while pending:
        folder = pending.pop()
        if folder in visited:
            continue
        visited.add(folder)
        try:
            listing = drive.list_folder(token, folder, opener=opener)
        except Exception as exc:
            report.failed.append((folder, f"Could not scan folder: {exc}"))
            continue
        pending.extend(f.id for f in listing if f.is_folder)
        yield folder, listing


def _evidence(file, listing, rec):
    # State and Drive markers from the old watcher survive this migration.
    if file.app_properties.get(STATE_PROP) == FORMATTED or rec.marked == FORMATTED:
        return True
    if any(name == output_name(file.name) or DELIVERED.search(Path(name).stem)
           for name in rec.uploaded):
        return True
    if any(f.app_properties.get(SOURCE_PROP) == file.id
           and (f.app_properties.get(OUTPUT_PROP))
           and (f.app_properties.get(OUTPUT_PROP) == "format"
                or DELIVERED.search(Path(f.name).stem)) for f in listing):
        return True
    # Manual/older deliveries may have no private metadata. The folder is the
    # identity boundary; a typo in a surname must not make an old book new.
    return any(not f.is_folder and Path(f.name).suffix.lower() in (".docx", ".idml")
               and (f.name.casefold() == output_name(file.name).casefold()
                    or DELIVERED.search(Path(f.name).stem)) for f in listing)


def _mark(token, file, rec, state, opener):
    name = done_name(file.name)
    props = {STATE_PROP: FORMATTED, AT_PROP: datetime.now(timezone.utc).isoformat()}
    if rec.job_id:
        props[JOB_PROP] = rec.job_id
    # One metadata update: rename and completion flag succeed together.
    drive.set_app_properties(token, file.id, props, name=name, opener=opener)
    rec.name, rec.marked = name, FORMATTED
    state.record(rec)


def _one(token, file, folder, listing, root, ws, state, store, runner,
         opener, report, *, mock=False):
    from .tick import _one as prepare_one
    rec = state.get(file.id)
    rec.name, rec.subfolder_id = file.name, folder
    # Re-list before starting: old delivered books and uploads surviving a
    # lost local checkpoint must never buy another preparation run.
    current = drive.list_folder(token, folder, opener=opener)
    if _evidence(file, current, rec):
        _mark(token, file, rec, state, opener)
        return
    prepare_one(token, root, replace(ws, hubspot_enabled=False), file, current,
                state, runner, store, mock=mock, opener=opener, hs_token=None,
                dest_folder_id=folder, report=report)


def tick(home, ws, *, dry_run=False, mock=False, opener=None, get_key=None):
    from .tick import NotConfigured, TickReport, _legacy_tick, config_path
    report = TickReport(dry_run=dry_run)
    opener = opener or drive._open_url
    root = Path(home)
    if ws.formatting_enabled:
        if not ws.folder_id:
            raise NotConfigured("No folder is being watched yet. Run `docproof-watch init` to say which one.")
        if not ws.client_id or not ws.client_secret:
            raise NotConfigured("There is no Google sign-in set up yet. Run `docproof-watch auth`.")
        refresh = (get_key or get_api_key)(GOOGLE_KEY)
        if not refresh:
            raise NotConfigured("DocProof is not signed in to Google. Run `docproof-watch auth`.")
        if not dry_run:
            note_tick(root)
        token = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh, opener=opener)
        state = WatchState.load(root / "state.json")
        store = JobStore(Paths(root).ensure()) if not dry_run else None
        runner = (JobRunner(store, ws.app_settings(root), config_path=config_path(),
                            notify_home=root) if store else None)
        prepared = 0
        for folder, listing in _folders(token, ws.folder_id, opener, report):
            report.listed += len(listing)
            candidates = [f for f in listing if source(f)]
            for file in sorted(candidates, key=lambda f: (f.modified_time, f.name, f.id)):
                rec = state.get(file.id)
                completed = _evidence(file, listing, rec)
                if file.app_properties.get(STATE_PROP) == FAILED or rec.marked == FAILED:
                    report.needs_human.append((file.name, "Previously failed; fix the file and clear its marker."))
                    continue
                if len(candidates) > 1:
                    # No guessing which copy is canonical, even with old output.
                    report.needs_human.append((file.name, "Multiple Book Originals in this folder; choose the current original."))
                    continue
                if not completed and rec.attempts >= ws.max_attempts:
                    report.needs_human.append((file.name, "Formatting failed repeatedly; fix the file and clear its marker."))
                    if not dry_run:
                        drive.set_app_properties(token, file.id, {STATE_PROP: FAILED}, opener=opener)
                        rec.marked = FAILED
                        state.record(rec)
                    continue
                if not completed:
                    report.new += 1
                    report.plan.append((file.name, "new"))
                else:
                    report.left_alone += 1
                if dry_run:
                    continue
                if not completed and prepared >= ws.max_files_per_tick:
                    report.deferred += 1
                    continue
                if not completed:
                    prepared += 1
                try:
                    _one(token, file, folder, listing, root, ws, state, store, runner,
                         opener, report, mock=mock)
                except Exception as exc:
                    rec = state.get(file.id)
                    rec.attempts += 1
                    state.record(rec)
                    report.failed.append((file.name, str(exc)))
    # Formatting completes independently before any optional CRM workflow.
    if any((ws.proofing_enabled, ws.promo_enabled, ws.plan_enabled, ws.corrections_enabled)):
        other_ws = replace(ws, formatting_drive_only=False, formatting_enabled=False)
        try:
            other = _legacy_tick(home, other_ws, dry_run=dry_run, mock=mock,
                                 opener=opener, get_key=get_key)
            for field in fields(report):
                value = getattr(other, field.name)
                if isinstance(value, list):
                    getattr(report, field.name).extend(value)
                elif isinstance(value, int) and not isinstance(value, bool):
                    setattr(report, field.name, getattr(report, field.name) + value)
        except Exception as exc:
            report.failed.append(("Other automations", str(exc)))
    return report
