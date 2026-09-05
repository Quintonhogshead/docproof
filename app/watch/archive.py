"""Every finished job's outputs, pushed to a Google Drive archive.

DocProof's hosted build runs on a server whose local disk is thrown away on
every redeploy — several times a day — and while the mounted volume survives
that, a volume can still be lost, a region moved, an app recreated. This is the
durable, off-box record of what DocProof produced: every finished job's files,
organised `Reviews|Prep|Promo -> YYYY-MM -> one folder per job`, so a person can
find any book and DocProof itself can rebuild its list from nothing.

The write is idempotent the way the watcher's own upload is (`prep.upload_outputs`):
every step is safe to repeat, ids are recorded one file at a time, an upload that
landed but was not written down is adopted rather than duplicated, and the
manifest — the file whose presence means "this archive is complete" — is written
last. Nothing here ever raises back into a job: a job that did its work must not
fail over an upload, so a Drive hiccup only leaves the record "pending" for the
ticker's sweep to try again.

Settings, the Google sign-in and the identity are the watcher's, reached through
the same watch home the completion email already uses
(`JobRunner.notify_home`) — one address, one archive, for app jobs and watched
books alike. An install that never turns it on behaves exactly as before.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from docproof import __version__

from . import drive
from .settings import GOOGLE_KEY, WatchSettings

log = logging.getLogger("docproof.app.watch.archive")

# What a job's kind is filed under. A kind that is not one of these files under
# "Other" rather than being dropped. "galley" is the proofing stage's job kind
# (see app/watch/proof.py), filed under its own name so a proofread's case file,
# letter and verdict are found where a person would look for them.
KIND_FOLDER = {"review": "Reviews", "prep": "Prep", "promo": "Promo",
               "galley": "Proofing"}

# The manifest's fixed name, and the marker that this folder/file is the
# archive's. `docproof.job` is spelled the same as the watcher's JOB_PROP on
# purpose — it means the same thing, "the job this came from" — but lives on
# different files, so there is no collision.
MANIFEST_NAME = "docproof.json"
ARCHIVE_PROP = "docproof.archive"
JOB_PROP = "docproof.job"
KIND_PROP = "docproof.kind"
JOBSTATE_PROP = "docproof.jobstate"
ROLE_PROP = "docproof.role"

JSON_MIME = "application/json"
MARKDOWN_MIME = "text/markdown"

# How many jobs one ticker sweep will archive. A large backfill (every job that
# finished before the archive was switched on) drains over several passes rather
# than holding the ticker for the length of a hundred uploads.
PER_TICK = 3
# How many times the sweep retries one job before giving up and marking it a
# visible "failed" the card can show — the archive's version of the collect cap.
MAX_ATTEMPTS = 8
# Backoff between retries, seconds, indexed by attempt count. The first failure
# waits a minute; a run of them backs off to four hours before MAX_ATTEMPTS ends
# it. Index 0 is unused (attempt 0 is always due).
BACKOFF = (0, 60, 300, 900, 1800, 3600, 7200, 14400)
# appProperties cap a value near 124 bytes; the whole error is on the record and
# in the log, so what rides to Drive-adjacent state is just enough to recognise.
ERROR_LIMIT = 300



def is_enabled(ws: WatchSettings) -> bool:
    """Whether the archive is switched on and has somewhere to write. Both are
    needed: a switch with no folder has nowhere to put anything."""
    return bool(ws.archive_enabled and ws.archive_folder_id)


def _archivable(job) -> bool:
    """Whether a job is one the archive should hold: finished (or failed with
    its notes still on disk) and pointing at a results folder. A cancelled run,
    or a failure that never wrote anything, has nothing worth keeping."""
    return job.state in ("done", "failed") and bool(job.results_dir)



def archive_done(home, store, job_id: str, *, get_key=None,
                 opener=None) -> bool:
    """The inline attempt when one job finishes. Best-effort and silent when the
    archive is off; returns whether the job reached "done" this call.

    Never raises: `_archive_job` turns every Drive failure into an archive state
    on the record, and a job that is not signed in to Google is left untouched
    for a later pass once it is."""
    # Resolved here, not in the signature: a default argument binds once at
    # import to the function it sits next to, so a test that swaps the opener
    # would be ignored and quietly reach Google. See tick.run's note.
    opener = opener or drive._open_url
    ws = WatchSettings.load(home)
    if not is_enabled(ws):
        return False
    job = store.get(job_id)
    if job is None or not _archivable(job):
        return False
    token = _token(ws, get_key, opener)
    if token is None:
        return False
    return _archive_job(token, ws, store, job, cache={}, opener=opener)


def sweep_once(home, store, *, get_key=None, opener=None,
               limit: int = PER_TICK) -> int:
    """The ticker's retry-and-backfill pass. One mechanism, three jobs: it
    retries an inline attempt that hit a hiccup, it backfills everything that
    finished before the archive was switched on (each such record is still at
    the default "" state and so is due at once), and it resumes a machine that
    died mid-archive. Bounded per pass. Returns how many reached "done"."""
    opener = opener or drive._open_url   # bound at call time; see archive_done
    ws = WatchSettings.load(home)
    if not is_enabled(ws):
        return 0
    due = due_jobs(store, limit=limit)
    if not due:
        return 0
    token = _token(ws, get_key, opener)
    if token is None:
        return 0
    cache: dict = {}
    done = 0
    for job in due:
        try:
            if _archive_job(token, ws, store, job, cache=cache, opener=opener):
                done += 1
        except Exception:                 # noqa: BLE001 - one job must not stop the rest
            log.exception("Archiving %s crashed unexpectedly", job.id)
    return done


def due_jobs(store, *, limit: int = PER_TICK, now: datetime | None = None):
    """The jobs a sweep should try, oldest first so a backfill catches up in
    order. A job is due when it is archivable, not yet done or given up on, and
    its backoff has elapsed."""
    now = now or datetime.now(timezone.utc)
    out = []
    for job in sorted(store.all(), key=lambda j: j.created_at):
        if not _archivable(job) or job.archive not in ("", "pending"):
            continue
        if not _due(job, now):
            continue
        out.append(job)
        if len(out) >= limit:
            break
    return out


def refresh_files(home, job, names, *, get_key=None, opener=None) -> int:
    """Re-push files whose local contents changed after they were archived — a
    corrected IDML the review screen just edited, and the report beside it.

    The archive's uploads are otherwise write-once (`_archive_job` skips any
    name already placed, and would re-adopt the stale orphan on a bare re-run),
    so a changed file is updated *in place* by its Drive id: same file, same
    manifest, fresh bytes. A name never archived is left for the ordinary sweep,
    which will upload the current contents anyway. Best-effort; returns how many
    were refreshed."""
    opener = opener or drive._open_url
    ws = WatchSettings.load(home)
    if not is_enabled(ws) or not job.results_dir:
        return 0
    wanted = [(n, job.drive_files.get(n)) for n in names
              if job.drive_files.get(n)]
    if not wanted:
        return 0
    token = _token(ws, get_key, opener)
    if token is None:
        return 0
    done = 0
    for name, file_id in wanted:
        path = Path(job.results_dir) / name
        if not path.is_file():
            continue
        try:
            drive.update_media(token, file_id, path, mime_type=_mime(name),
                               opener=opener)
            done += 1
        except drive.DriveError as e:
            log.warning("Could not refresh %s for %s in the archive: %s",
                        name, job.filename, e)
    return done



def fetch_file(home, job, name: str, dest_dir, *, save_as: str | None = None,
               get_key=None, opener=None) -> Path | None:
    """Bring one archived file back onto disk, or None if it cannot be.

    This is what makes a wiped results folder still serve: the file's Drive id is
    on the job record (`drive_files`), so it is fetched straight back and
    re-cached under `dest_dir`. `save_as` renames it on the way down — the source
    manuscript is archived as `source - <name>` but restored under its own name,
    ready to re-run. None whenever the archive is off, does not have this file,
    or Drive will not give it up; the caller then answers its own 404."""
    opener = opener or drive._open_url
    ws = WatchSettings.load(home)
    if not is_enabled(ws):
        return None
    file_id = job.drive_files.get(name)
    if not file_id:
        return None
    token = _token(ws, get_key, opener)
    if token is None:
        return None
    try:
        return drive.download(token, file_id, Path(dest_dir) / (save_as or name),
                              opener=opener)
    except drive.DriveError as e:
        log.warning("Could not fetch %s for %s from the archive: %s",
                    name, job.filename, e)
        return None


def restore(home, store, *, get_key=None, opener=None,
            limit: int | None = None) -> dict:
    """Rebuild job records from the archive's manifests — the recovery from a
    lost volume. Walk `Reviews|Prep|Promo -> month -> job folder`, read each
    `docproof.json`, and recreate every job id the store does not already have.

    Local records always win: a job already on disk is never overwritten by its
    own (never newer) archive. A job folder with no manifest is an incomplete
    archive — skipped and logged, never half-restored. The recreated record
    points at no local results (`results_dir=None`); its files stream back from
    Drive on first download. Returns a small tally. Never raises."""
    opener = opener or drive._open_url
    ws = WatchSettings.load(home)
    if not is_enabled(ws):
        return {"restored": 0, "skipped": 0, "scanned": 0, "ok": False}
    token = _token(ws, get_key, opener)
    if token is None:
        return {"restored": 0, "skipped": 0, "scanned": 0, "ok": False}

    restored = skipped = scanned = 0
    for kind_name in KIND_FOLDER.values():
        kinds = drive.find_children(token, ws.archive_folder_id, name=kind_name,
                                    folders_only=True, opener=opener)
        if not kinds:
            continue
        for month in drive.list_folder(token, kinds[0].id, opener=opener):
            if not month.is_folder:
                continue
            for folder in drive.list_folder(token, month.id, opener=opener):
                if not folder.is_folder:
                    continue
                scanned += 1
                if _restore_one(token, store, folder, opener=opener):
                    restored += 1
                else:
                    skipped += 1
                if limit is not None and restored >= limit:
                    return {"restored": restored, "skipped": skipped,
                            "scanned": scanned, "ok": True}
    log.info("Archive restore: %d recreated, %d skipped, %d folders scanned.",
             restored, skipped, scanned)
    return {"restored": restored, "skipped": skipped, "scanned": scanned,
            "ok": True}


def _restore_one(token: str, store, folder, *, opener) -> bool:
    """Recreate one job from its folder's manifest, or skip it. Returns whether a
    record was written."""
    manifests = drive.find_children(token, folder.id, name=MANIFEST_NAME,
                                    opener=opener)
    if not manifests:
        log.info("Skipping %r: no manifest, so the archive is incomplete.",
                 folder.name)
        return False
    try:
        data = json.loads(drive.download_bytes(
            token, manifests[0].id, opener=opener,
            what="read an archive manifest"))
    except (drive.DriveError, json.JSONDecodeError, ValueError) as e:
        log.warning("Skipping %r: its manifest would not read (%s).",
                    folder.name, e)
        return False
    job_dict = data.get("job") if isinstance(data, dict) else None
    if not isinstance(job_dict, dict) or not job_dict.get("id"):
        log.warning("Skipping %r: its manifest names no job.", folder.name)
        return False
    if store.get(job_dict["id"]) is not None:
        return False                          # a live record always wins
    _write_restored(store, job_dict, folder.id, data.get("files") or {},
                    manifests[0].id)
    return True


def _write_restored(store, job_dict: dict, folder_id: str,
                    files: dict, manifest_id: str) -> None:
    """Recreate `jobs/<id>/app.json` from a manifest's job record, pointing at no
    local results but at the archive that holds them."""
    from app.jobs import Job
    known = set(Job.__dataclass_fields__)
    fields = {k: v for k, v in job_dict.items() if k in known}
    drive_files = dict(files)
    drive_files[MANIFEST_NAME] = manifest_id
    fields.update(results_dir=None, archive="done", archive_error="",
                  drive_folder_id=folder_id, drive_files=drive_files)
    store.save(Job(**fields))


def wants_boot_restore(home, paths) -> bool:
    """Whether a fresh boot should rebuild from the archive: it is on, and there
    are no local job records — exactly the empty-volume disaster restore exists
    for. A machine that still has its jobs never triggers it."""
    if not is_enabled(WatchSettings.load(home)):
        return False
    jobs_dir = Path(paths.jobs)
    return not jobs_dir.is_dir() or not any(jobs_dir.iterdir())


def _due(job, now: datetime) -> bool:
    """Whether a job's backoff has elapsed. A never-tried job (attempts 0) is due
    at once; after that, wait the backoff for its attempt count. Any trouble
    reading the clock errs toward due — a stuck record is better retried than
    stranded."""
    if job.archive_attempts <= 0:
        return True
    try:
        last = datetime.fromisoformat(job.updated_at)
        wait = BACKOFF[min(job.archive_attempts, len(BACKOFF) - 1)]
        return (now - last).total_seconds() >= wait
    except (ValueError, TypeError):
        return True



def _archive_job(token: str, ws: WatchSettings, store, job, *, cache: dict,
                 opener) -> bool:
    """Push one job's files to its folder in the archive, idempotently, and
    record where they went. Owns the record's archive lifecycle: marks it
    "pending" (counting the attempt) before touching Drive, "done" when the
    manifest is written last, and — on a Drive failure — "pending" again to be
    retried, or "failed" once the attempts run out. Returns whether it finished.

    Every Drive error is caught here; the caller never has to. The one failure
    that is not retried is an empty or vanished results folder: there is nothing
    to archive and never will be, so it is marked failed once rather than swept
    forever."""
    artifacts = _artifact_paths(job, ws)
    if not artifacts:
        store.update(job.id, archive="failed",
                     archive_error="the results were gone, so there was "
                                   "nothing to archive.")
        log.warning("Nothing to archive for %s: its results folder is empty "
                    "or gone.", job.filename)
        return False

    attempt = job.archive_attempts + 1
    store.update(job.id, archive="pending", archive_attempts=attempt,
                 archive_error="")
    job = store.get(job.id) or job
    try:
        folder_id = job.drive_folder_id or _resolve_job_folder(
            token, ws, store, job, cache=cache, opener=opener)
        placed = dict(job.drive_files)
        listing: list | None = None
        for path, name in artifacts:
            if name in placed:
                continue
            if listing is None:
                listing = drive.list_folder(token, folder_id, opener=opener)
            orphan = _already_there(listing, job.id, name)
            if orphan is not None:
                placed[name] = orphan.id
            else:
                placed[name] = drive.upload(
                    token, folder_id, path, name=name, mime_type=_mime(name),
                    app_properties={JOB_PROP: job.id, ROLE_PROP: _role(name)},
                    opener=opener)
            store.update(job.id, drive_files=placed)   # one at a time
        files_map = {n: i for n, i in placed.items() if n != MANIFEST_NAME}
        placed[MANIFEST_NAME] = _write_manifest(
            token, ws, store, job, folder_id, files_map, opener=opener)
        store.update(job.id, drive_files=placed, archive="done",
                     archive_error="")
        log.info("Archived %s to Drive.", job.filename)
        return True
    except drive.DriveError as e:
        final = "failed" if attempt >= MAX_ATTEMPTS else "pending"
        store.update(job.id, archive=final, archive_error=str(e)[:ERROR_LIMIT])
        log.warning("Archiving %s did not finish (attempt %d of %d): %s",
                    job.filename, attempt, MAX_ATTEMPTS, e)
        return False


def _artifact_paths(job, ws: WatchSettings) -> list[tuple[Path, str]]:
    """What of this job to archive, and what to call each file in Drive.

    Every file at the top of the results folder — exactly the set the app's
    result routes serve — under its own name, minus any manifest from an earlier
    archive of the same job. Directories are skipped, which drops the multi-round
    `rounds-ws` workspace and any scratch. The submitted manuscript rides along
    as `source - <name>` when `archive_include_source` is on, so a job can be
    re-run after a total loss, not merely read."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    items = [(p, p.name) for p in sorted(out.iterdir())
             if p.is_file() and p.name != MANIFEST_NAME]
    if ws.archive_include_source and job.source_path:
        src = Path(job.source_path)
        if src.is_file():
            items.append((src, f"source - {Path(job.filename).name}"))
    return items


def _resolve_job_folder(token: str, ws: WatchSettings, store, job, *,
                        cache: dict, opener) -> str:
    """Find or make this job's folder, `Reviews/2026-08/<book>`, and remember
    its id on the record so a resumed archive writes to the same place.

    The kind and month folders are found by a scoped name query and made only if
    missing, cached within a pass so a run of jobs in one month asks once. The
    job's own folder is found by its id property, never its name — two books can
    share a title, and a folder name is human-typed and may drift, but the job id
    on the folder is exact."""
    kind = KIND_FOLDER.get(job.kind, "Other")
    kind_id = _child_folder(token, ws.archive_folder_id, kind, cache=cache,
                            opener=opener)
    month_id = _child_folder(token, kind_id, _month(job), cache=cache,
                             opener=opener)
    found = drive.find_children(token, month_id,
                                app_property=(JOB_PROP, job.id), opener=opener)
    if found:
        folder_id = found[0].id
    else:
        folder_id = drive.create_folder(
            token, month_id, _job_folder_name(job),
            app_properties={ARCHIVE_PROP: "1", JOB_PROP: job.id,
                            KIND_PROP: job.kind, JOBSTATE_PROP: job.state},
            opener=opener)
    store.update(job.id, drive_folder_id=folder_id)
    return folder_id


def _child_folder(token: str, parent_id: str, name: str, *, cache: dict,
                  opener) -> str:
    """The id of a named subfolder of `parent_id`, found or made once and reused
    within the pass."""
    key = (parent_id, name)
    if key in cache:
        return cache[key]
    found = drive.find_children(token, parent_id, name=name, folders_only=True,
                                opener=opener)
    folder_id = found[0].id if found else drive.create_folder(
        token, parent_id, name, opener=opener)
    cache[key] = folder_id
    return folder_id


def _write_manifest(token: str, ws: WatchSettings, store, job, folder_id: str,
                    files: dict[str, str], *, opener) -> str:
    """Write `docproof.json` last, and return its id.

    Its presence is what marks the archive complete, so it goes after every
    other file. A second archive of the same job (a download-anyway that added a
    reviewed .docx) updates the one manifest in place by its id rather than
    leaving a stale sibling. It is written into the results folder too, so the
    local copy carries the same record as the archived one."""
    body = _manifest(job, files)
    path = Path(job.results_dir) / MANIFEST_NAME
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    existing = job.drive_files.get(MANIFEST_NAME)
    if existing:
        drive.update_media(token, existing, path, mime_type=JSON_MIME,
                           opener=opener)
        return existing
    return drive.upload(token, folder_id, path, name=MANIFEST_NAME,
                        mime_type=JSON_MIME,
                        app_properties={JOB_PROP: job.id, ROLE_PROP: "manifest"},
                        opener=opener)


def _manifest(job, files: dict[str, str]) -> dict:
    """The record that makes a job restorable from the archive alone: the whole
    job record verbatim (so `jobs/<id>/app.json` can be recreated field for
    field), plus the Drive id of every archived file."""
    return {
        "schema": 1,
        "app_version": __version__,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "job": asdict(job),
        "files": files,
    }



def _already_there(listing: list, job_id: str, name: str):
    """A file from a previous attempt at this job that the record lost track of.
    Recognised by name *and* the job-id property it carries, so it is a fact
    about the folder, not a guess from a name two jobs might share."""
    for candidate in listing:
        if (candidate.name == name
                and candidate.app_properties.get(JOB_PROP) == job_id):
            return candidate
    return None


def _month(job) -> str:
    """The `YYYY-MM` a job files under, from when it was created."""
    stamp = job.created_at or job.updated_at or ""
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m")
    except (ValueError, TypeError):
        return "undated"


def _job_folder_name(job) -> str:
    """A human-scannable, sortable, unique folder name:
    `<book> - 2026-08-12 1432 - <job id>`. The id guarantees uniqueness; the
    date and title are for a person reading the folder list."""
    parts = [Path(job.filename).stem or "document"]
    try:
        parts.append(datetime.fromisoformat(job.created_at).strftime(
            "%Y-%m-%d %H%M"))
    except (ValueError, TypeError):
        pass
    parts.append(job.id)
    return " - ".join(parts)


def _mime(name: str) -> str:
    low = name.lower()
    if low.endswith(".docx"):
        return drive.DOCX_MIME
    if low.endswith(".json"):
        return JSON_MIME
    if low.endswith(".md"):
        return MARKDOWN_MIME
    return "application/octet-stream"


def _role(name: str) -> str:
    """A short label for what a file is, stamped on it for a person browsing the
    archive. Not load-bearing — restore reads the manifest, not this."""
    low = name.lower()
    if low == MANIFEST_NAME:
        return "manifest"
    if low.startswith("source - "):
        return "source"
    if low in ("findings.json", "prep.json", "promo.json"):
        return "data"
    if low == "summary.md":
        return "summary"
    if low.endswith(".md"):
        return "notes"
    return "document"


def _token(ws: WatchSettings, get_key, opener) -> str | None:
    """A Drive access token for this pass, or None when there is no sign-in to
    make one from. A missing sign-in is a quiet skip, not a failure: the jobs
    wait, unchanged, for the next pass once someone has signed in."""
    from app.settings import get_api_key
    if not (ws.client_id and ws.client_secret):
        log.info("Drive archive skipped: Google sign-in is not set up.")
        return None
    refresh = (get_key or get_api_key)(GOOGLE_KEY)
    if not refresh:
        log.info("Drive archive skipped: DocProof is not signed in to Google.")
        return None
    try:
        return drive.refresh_access_token(ws.client_id, ws.client_secret,
                                          refresh, opener=opener)
    except drive.DriveError as e:
        log.warning("Drive archive skipped: could not sign in to Google (%s).",
                    e)
        return None


__all__ = ["is_enabled", "archive_done", "sweep_once", "due_jobs",
           "fetch_file", "restore", "wants_boot_restore"]
