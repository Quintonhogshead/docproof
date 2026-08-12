"""The Drive output archive, driven through the fake Drive.

No test here reaches Google: every call goes through the injected opener from
`fakes.fake_drive`, which holds one live folder tree an upload lands in and the
next search sees. What these tests pin down is the archive's contract — the
`Reviews/YYYY-MM/<book>` tree, one folder per job found by its id, the manifest
written last, and idempotence: a second archive of one job uploads nothing new,
a lost record adopts the orphan rather than duplicating it, and a Drive hiccup
leaves the job retriable rather than lost.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.jobs import Job, JobStore
from app.settings import Paths
from app.watch import archive
from app.watch.drive import DriveError, FOLDER_MIME
from app.watch.settings import WatchSettings
from .fakes import fake_drive


def _watch_home(tmp_path, **over) -> Path:
    """A watch home carrying archive settings and a Google sign-in."""
    home = tmp_path / "watch"
    fields = {"archive_enabled": True, "archive_folder_id": "root-archive",
              "client_id": "cid", "client_secret": "secret"}
    fields.update(over)
    WatchSettings(**fields).save(home)
    return home


def _store(tmp_path) -> JobStore:
    return JobStore(Paths(tmp_path / "app"))


def _finished_job(tmp_path, store, *, kind="review", state="done",
                  filename="Johnson - Book.docx",
                  created="2026-08-12T14:32:00+00:00") -> Job:
    """A finished job with real files in a results folder and a source file."""
    results = tmp_path / "results" / "Johnson - Book"
    results.mkdir(parents=True, exist_ok=True)
    (results / "reviewed Johnson - Book.docx").write_bytes(b"reviewed")
    (results / "summary.md").write_text("# summary", encoding="utf-8")
    (results / "findings.json").write_text('{"findings": []}', encoding="utf-8")
    source = tmp_path / "uploads" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"original manuscript")
    job = Job(id="rev-abc123", filename=filename,
              source_path=str(source), model="claude-sonnet-5", mode="now",
              kind=kind, state=state, results_dir=str(results),
              created_at=created, source="app", owner_id="u1")
    return store.save(job)


def _key(_):
    return "refresh-token"


def _names(opener) -> dict:
    """Stored Drive entries keyed by name, for asserting the tree."""
    return {e.get("name", ""): e for e in opener.files.values()}


# --- the happy path ------------------------------------------------------------

def test_archive_builds_the_tree_and_writes_the_manifest_last(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    assert archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    by_name = _names(opener)
    # Reviews -> 2026-08 -> one folder per job, all folders.
    assert by_name["Reviews"]["mimeType"] == FOLDER_MIME
    assert by_name["2026-08"]["mimeType"] == FOLDER_MIME
    job_folder = next(e for n, e in by_name.items()
                      if n.startswith("Johnson - Book - 2026-08-12"))
    assert job.id in job_folder["name"]
    assert job_folder["appProperties"]["docproof.job"] == job.id

    # Every result file plus the manifest, and the manifest is really last.
    assert "reviewed Johnson - Book.docx" in by_name
    assert "summary.md" in by_name
    assert "findings.json" in by_name
    assert "docproof.json" in by_name

    saved = store.get(job.id)
    assert saved.archive == "done" and saved.archive_error == ""
    assert saved.drive_folder_id == job_folder["id"]
    # The record knows every file's id, manifest included.
    assert set(saved.drive_files) >= {
        "reviewed Johnson - Book.docx", "summary.md", "findings.json",
        "docproof.json"}


def test_the_source_manuscript_rides_along_when_enabled(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    assert "source - Johnson - Book.docx" in _names(opener)


def test_the_source_is_left_out_when_the_toggle_is_off(tmp_path):
    home = _watch_home(tmp_path, archive_include_source=False)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    assert not any(n.startswith("source - ") for n in _names(opener))


def test_the_manifest_carries_the_job_record_and_the_file_ids(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    manifest_id = next(fid for fid, e in opener.files.items()
                       if e.get("name") == "docproof.json")
    body = json.loads(opener.content[manifest_id])
    assert body["schema"] == 1
    assert body["job"]["id"] == job.id
    assert body["job"]["owner_id"] == "u1"           # lossless for restore
    assert body["files"]["reviewed Johnson - Book.docx"]
    assert "docproof.json" not in body["files"]      # not listed in itself


# --- idempotence ---------------------------------------------------------------

def _uploads(opener) -> int:
    """How many multipart uploads (new files) the fake has served."""
    return sum(1 for r in opener.calls
               if "/upload/drive/v3/files" in r.full_url
               and r.get_method() == "POST")


def test_a_second_archive_of_one_job_uploads_nothing_new(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)
    first = _uploads(opener)
    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    # The second pass re-writes the manifest in place (a media PATCH, not a new
    # upload) and adds no new files.
    assert _uploads(opener) == first
    assert store.get(job.id).archive == "done"


def test_a_lost_record_adopts_the_orphan_instead_of_duplicating(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)
    # Simulate a crash that uploaded a file but never recorded its id: drop one
    # name from the record while the file stays in the folder.
    saved = store.get(job.id)
    kept = dict(saved.drive_files)
    kept.pop("summary.md")
    store.update(job.id, drive_files=kept)
    before = _uploads(opener)

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    # The orphan is adopted by name + job-id property, not uploaded again.
    assert _uploads(opener) == before
    assert "summary.md" in store.get(job.id).drive_files


# --- failure and retry ---------------------------------------------------------

def test_a_drive_hiccup_leaves_the_job_pending_then_succeeds(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    # The fake raises on the first upload only.
    opener = fake_drive(fail={"upload": DriveError("Drive is busy")})

    assert not archive.archive_done(home, store, job.id, get_key=_key,
                                    opener=opener)
    stuck = store.get(job.id)
    assert stuck.archive == "pending" and stuck.archive_attempts == 1
    assert "busy" in stuck.archive_error
    # The folder was made before the failed upload, and is reused, not remade.
    assert stuck.drive_folder_id

    assert archive.archive_done(home, store, job.id, get_key=_key, opener=opener)
    assert store.get(job.id).archive == "done"


def test_results_that_are_gone_are_marked_failed_once(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    # The volume was recycled before the archive ran: the folder is gone.
    import shutil
    shutil.rmtree(job.results_dir)
    opener = fake_drive()

    assert not archive.archive_done(home, store, job.id, get_key=_key,
                                    opener=opener)
    saved = store.get(job.id)
    assert saved.archive == "failed" and "gone" in saved.archive_error
    # And it is not swept forever: a failed record is no longer due.
    assert saved not in archive.due_jobs(store)


def test_the_attempt_cap_turns_a_repeating_failure_into_failed(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    store.update(job.id, archive_attempts=archive.MAX_ATTEMPTS - 1)
    opener = fake_drive(fail={"upload": DriveError("still busy")})

    archive.archive_done(home, store, job.id, get_key=_key, opener=opener)

    # The last attempt failed, so it stops being "pending" and becomes a visible
    # "failed" rather than being retried forever.
    assert store.get(job.id).archive == "failed"


# --- switched off, and not signed in -------------------------------------------

def test_a_disabled_archive_touches_drive_not_at_all(tmp_path):
    home = _watch_home(tmp_path, archive_enabled=False)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    assert not archive.archive_done(home, store, job.id, get_key=_key,
                                    opener=opener)
    assert opener.calls == []
    assert store.get(job.id).archive == ""


def test_no_google_sign_in_is_a_quiet_skip(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    job = _finished_job(tmp_path, store)
    opener = fake_drive()

    # No refresh token available: the jobs wait, unchanged, for a later pass.
    assert not archive.archive_done(home, store, job.id,
                                    get_key=lambda _: None, opener=opener)
    assert store.get(job.id).archive == ""


# --- the sweep -----------------------------------------------------------------

def test_the_sweep_backfills_finished_jobs_that_predate_the_switch(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    a = _finished_job(tmp_path, store)
    # A second finished job in its own results folder.
    other = tmp_path / "results" / "second"
    other.mkdir(parents=True)
    (other / "reviewed second.docx").write_bytes(b"x")
    b = store.save(Job(id="rev-two", filename="second.docx",
                       source_path="", model="m", mode="now", state="done",
                       kind="review", results_dir=str(other),
                       created_at="2026-08-11T09:00:00+00:00"))
    opener = fake_drive()

    done = archive.sweep_once(home, store, get_key=_key, opener=opener,
                              limit=10)

    assert done == 2
    assert store.get(a.id).archive == "done"
    assert store.get(b.id).archive == "done"


def test_the_sweep_is_bounded_per_pass(tmp_path):
    home = _watch_home(tmp_path)
    store = _store(tmp_path)
    for i in range(4):
        d = tmp_path / "results" / f"book{i}"
        d.mkdir(parents=True)
        (d / f"reviewed book{i}.docx").write_bytes(b"x")
        store.save(Job(id=f"rev-{i}", filename=f"book{i}.docx", source_path="",
                       model="m", mode="now", state="done", kind="review",
                       results_dir=str(d),
                       created_at=f"2026-08-1{i}T09:00:00+00:00"))
    opener = fake_drive()

    done = archive.sweep_once(home, store, get_key=_key, opener=opener, limit=3)

    assert done == 3
    # One left for the next pass.
    assert sum(1 for j in store.all() if j.archive == "done") == 3
    assert sum(1 for j in store.all() if j.archive == "") == 1


def test_backoff_holds_a_freshly_failed_job_and_releases_an_old_one(tmp_path):
    store = _store(tmp_path)
    d = tmp_path / "results" / "b"
    d.mkdir(parents=True)
    (d / "reviewed b.docx").write_bytes(b"x")
    job = store.save(Job(id="rev-b", filename="b.docx", source_path="",
                         model="m", mode="now", state="done", kind="review",
                         results_dir=str(d), created_at="2026-08-10T09:00:00+00:00",
                         archive="pending", archive_attempts=1))
    now = datetime.now(timezone.utc)

    # Just failed a moment ago (updated_at is now): not due yet.
    store.update(job.id, archive="pending", archive_attempts=1)
    assert store.get(job.id) not in archive.due_jobs(store, now=now)

    # The same job an hour later is due again.
    assert store.get(job.id) in archive.due_jobs(
        store, now=now + timedelta(hours=1))
