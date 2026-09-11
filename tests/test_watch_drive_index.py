"""Incremental discovery: dormant folders, moves, recovery and read-only previews."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.watch import drive, drive_index, formatting
from .fakes import fake_drive, http_error
from .test_watch_drive_formatting import entry, manuscript, run, settings, provider


def synchronize(tmp_path, opener, *, persist=True, ws=None):
    return drive_index.sync(tmp_path, ws or settings(), "access", "refresh",
                            opener=opener, persist=persist)


def listings(opener):
    return [parse_qs(urlparse(r.full_url).query)["q"][0] for r in opener.calls
            if "q" in parse_qs(urlparse(r.full_url).query)]


def add(opener, fid, metadata):
    opener.files[fid] = {"id": fid, **metadata}


def test_thousand_dormant_folders_need_no_repeat_listings(tmp_path):
    opener = fake_drive({f"f{i}": entry(f"Author {i}", mime=drive.FOLDER_MIME)
                         for i in range(1000)})
    synchronize(tmp_path, opener)
    assert len(listings(opener)) == 1001
    opener.calls.clear()
    again = synchronize(tmp_path, opener)
    assert len(again.files) == 1001
    assert listings(opener) == []
    assert len(opener.calls) == 2  # root access check plus one change-feed page


def test_dormant_author_reactivates_and_preview_does_not_consume_change(tmp_path):
    opener = fake_drive({"author": entry("Old author", mime=drive.FOLDER_MIME)})
    synchronize(tmp_path, opener)
    path = tmp_path / drive_index.FILENAME
    before = path.read_bytes()
    add(opener, "src", entry("Book Original.docx", "author"))
    opener.calls.clear()
    preview, _ = run(tmp_path, opener=opener, dry_run=True)
    assert preview.new == 1 and path.read_bytes() == before
    assert listings(opener) == []
    current = synchronize(tmp_path, opener)
    assert current.files["src"].parents == ("author",)
    assert path.read_bytes() != before


def test_folder_moved_in_discovers_unchanged_descendants_and_excludes_archive(tmp_path):
    opener = fake_drive({"outside": entry("Elsewhere", "my-drive", mime=drive.FOLDER_MIME),
                         "new": entry("Author", "outside", mime=drive.FOLDER_MIME),
                         "nested": entry("Manuscript", "new", mime=drive.FOLDER_MIME),
                         "src": entry("Book Original.docx", "nested"),
                         "archive": entry("Archive", "new", mime=drive.FOLDER_MIME),
                         "backup": entry("Book Original.docx", "archive")})
    ws = settings(archive_folder_id="archive")
    synchronize(tmp_path, opener, ws=ws)
    opener.files["new"]["parents"] = ["rootfolder"]
    opener.calls.clear()
    current = synchronize(tmp_path, opener, ws=ws)
    assert "src" in current.files and "backup" not in current.files
    assert set(listings(opener)) == {"'new' in parents and trashed = false",
                                     "'nested' in parents and trashed = false"}


@pytest.mark.parametrize("event", ["move", "trash", "remove"])
def test_folder_leaving_scope_removes_all_unchanged_descendants(tmp_path, event):
    opener = fake_drive({"author": entry("Author", mime=drive.FOLDER_MIME),
                         "nested": entry("Draft", "author", mime=drive.FOLDER_MIME),
                         "src": entry("Book Original.docx", "nested")})
    synchronize(tmp_path, opener)
    if event == "move":
        opener.files["author"]["parents"] = ["elsewhere"]
    elif event == "trash":
        opener.files["author"]["trashed"] = True
    else:
        del opener.files["author"]
    current = synchronize(tmp_path, opener)
    assert "author" not in current.files and "src" not in current.files


def test_changed_name_and_move_keep_evidence_in_its_actual_folder(tmp_path):
    opener = fake_drive({"a": entry("First", mime=drive.FOLDER_MIME),
                         "b": entry("Second", mime=drive.FOLDER_MIME),
                         "src": entry("Draft.docx", "a"),
                         "old": entry("Book One.docx", "a")}, page_size=1)
    synchronize(tmp_path, opener)
    opener.files["src"].update(name="Book Original.docx", parents=["b"])
    opener.files["old"]["name"] = "Archived Book One.docx"
    opener.calls.clear()
    report, _ = run(tmp_path, opener=opener, dry_run=True)
    assert report.new == 1 and report.left_alone == 0
    assert listings(opener) == []
    assert sum(urlparse(r.full_url).path.endswith("/changes") for r in opener.calls) == 2


def test_change_during_initial_walk_is_replayed(tmp_path, monkeypatch):
    opener = fake_drive({"author": entry("Author", mime=drive.FOLDER_MIME)})
    original = drive.list_folder
    def concurrent_upload(token, folder, **kw):
        listing = original(token, folder, **kw)
        if folder == "author":
            add(opener, "src", entry("Book Original.docx", "author"))
        return listing
    monkeypatch.setattr(drive, "list_folder", concurrent_upload)
    current = synchronize(tmp_path, opener)
    assert "src" in current.files


def test_child_before_parent_and_unreported_ancestor_are_resolved(tmp_path, monkeypatch):
    opener = fake_drive({})
    synchronize(tmp_path, opener)
    add(opener, "parent", entry("New author", mime=drive.FOLDER_MIME))
    add(opener, "src", entry("Book Original.docx", "parent"))
    monkeypatch.setattr(drive, "list_changes", lambda *a, **k: (
        [{"fileId": "src", "file": opener.files["src"]}], "next"))
    current = synchronize(tmp_path, opener)
    assert "src" in current.files and "parent" in current.files


def test_failed_change_page_keeps_checkpoint_and_blocks_stale_work(tmp_path):
    opener = fake_drive({}, page_size=1)
    synchronize(tmp_path, opener)
    path = tmp_path / drive_index.FILENAME
    before = path.read_bytes()
    add(opener, "a", entry("A", mime=drive.FOLDER_MIME))
    add(opener, "b", entry("B", mime=drive.FOLDER_MIME))
    def failing(request, **kw):
        if "-page-1" in request.full_url:
            raise http_error(500)
        return opener(request, **kw)
    report, _ = run(tmp_path, opener=failing)
    assert report.failed and not report.uploaded and path.read_bytes() == before
    current = synchronize(tmp_path, opener)
    assert "a" in current.files and "b" in current.files


def test_incomplete_subtree_is_never_committed(tmp_path, monkeypatch):
    opener = fake_drive({})
    synchronize(tmp_path, opener)
    path = tmp_path / drive_index.FILENAME
    before = path.read_bytes()
    add(opener, "a", entry("Author", mime=drive.FOLDER_MIME))
    original = drive.list_folder
    def fail(*a, **kw):
        raise drive.DriveError("temporary failure")
    monkeypatch.setattr(drive, "list_folder", fail)
    with pytest.raises(drive.DriveError):
        synchronize(tmp_path, opener)
    assert path.read_bytes() == before
    monkeypatch.setattr(drive, "list_folder", original)
    assert "a" in synchronize(tmp_path, opener).files


@pytest.mark.parametrize("status", [401, 403, 500])
def test_access_or_service_error_does_not_reset_the_inventory(tmp_path, status):
    opener = fake_drive({})
    synchronize(tmp_path, opener)
    path = tmp_path / drive_index.FILENAME
    before = path.read_bytes()
    opener.calls.clear()
    def failing(request, **kw):
        if urlparse(request.full_url).path.endswith("/changes"):
            raise http_error(status)
        return opener(request, **kw)
    with pytest.raises(drive.DriveError):
        synchronize(tmp_path, failing)
    assert path.read_bytes() == before and not listings(opener)
    assert not any("startPageToken" in r.full_url for r in opener.calls)


def test_restored_folder_access_refreshes_existing_nested_folders(tmp_path, monkeypatch):
    opener = fake_drive({"a": entry("Author", mime=drive.FOLDER_MIME),
                         "b": entry("Drafts", "a", mime=drive.FOLDER_MIME)})
    synchronize(tmp_path, opener)
    add(opener, "src", entry("Book Original.docx", "b"))
    monkeypatch.setattr(drive, "list_changes", lambda *a, **k: (
        [{"fileId": "a", "removed": True}, {"fileId": "a", "file": opener.files["a"]}], "next"))
    assert "src" in synchronize(tmp_path, opener).files


@pytest.mark.parametrize("reset", ["corrupt", "missing", "cursor", "connection", "archive", "root"])
def test_recovery_rebuilds_inventory(tmp_path, reset):
    opener = fake_drive({"a": entry("Author", mime=drive.FOLDER_MIME)})
    synchronize(tmp_path, opener)
    path = tmp_path / drive_index.FILENAME
    ws = settings()
    if reset == "corrupt":
        path.write_text("{broken")
    elif reset == "missing":
        path.unlink()
    elif reset == "cursor":
        data = json.loads(path.read_text())
        data["cursor"] = "invalid"
        path.write_text(json.dumps(data))
    elif reset == "connection":
        ws.client_id = "reconnected"
    elif reset == "archive":
        ws.archive_folder_id = "a"
    elif reset == "root":
        ws.folder_id = "a"
    opener.calls.clear()
    synchronize(tmp_path, opener, ws=ws)
    assert any("startPageToken" in r.full_url for r in opener.calls)
    assert listings(opener)


def test_shared_drive_changes_keep_drive_scope_and_finish_pagination(tmp_path):
    root = entry("Root", "drive", mime=drive.FOLDER_MIME)
    root["driveId"] = "shared-drive"
    opener = fake_drive({"rootfolder": root}, page_size=1)
    synchronize(tmp_path, opener)
    for i in range(3):
        add(opener, f"src{i}", entry(f"Notes {i}.docx"))
    current = synchronize(tmp_path, opener)
    assert all(f"src{i}" in current.files for i in range(3))
    for req in opener.calls:
        if "/changes" in req.full_url:
            query = parse_qs(urlparse(req.full_url).query)
            assert query["driveId"] == ["shared-drive"]
            assert query["supportsAllDrives"] == ["true"]


def test_invalid_page_token_400_rebuilds_instead_of_stalling(tmp_path):
    opener = fake_drive({})
    synchronize(tmp_path, opener)
    rejected = False
    def once(request, **kw):
        nonlocal rejected
        if not rejected and urlparse(request.full_url).path.endswith("/changes"):
            rejected = True
            raise http_error(400, "Invalid page token")
        return opener(request, **kw)
    opener.calls.clear()
    synchronize(tmp_path, once)
    assert any("startPageToken" in r.full_url for r in opener.calls)


def test_cap_keeps_unchanged_books_queued_across_restart(tmp_path, provider):
    files = {}
    for i in range(2):
        files[f"a{i}"] = entry(f"Author {i}", mime=drive.FOLDER_MIME)
        files[f"s{i}"] = entry("Book Original.docx", f"a{i}")
    opener = fake_drive(files, docx=manuscript())
    ws = settings(max_files_per_tick=1)
    first, _ = run(tmp_path, opener=opener, ws=ws)
    assert first.deferred == 1 and len(first.uploaded) == 1
    second, _ = run(tmp_path, opener=opener, ws=ws)
    assert second.ok and len(second.uploaded) == 1
    opener.calls.clear()
    third, _ = run(tmp_path, opener=opener, ws=ws)
    assert third.ok and not third.uploaded and not listings(opener)


@pytest.mark.parametrize("event", ["rename", "move", "ambiguous", "delivery"])
def test_cached_candidate_revalidated_before_any_formatting(tmp_path, monkeypatch, provider, event):
    opener = fake_drive({"a": entry("Author", mime=drive.FOLDER_MIME),
                         "src": entry("Book Original.docx", "a")}, docx=manuscript())
    synchronize(tmp_path, opener)
    original = drive_index.sync
    def changed_after_sync(*a, **kw):
        inventory = original(*a, **kw)
        if event == "rename":
            opener.files["src"]["name"] = "Withdrawn.docx"
        elif event == "move":
            opener.files["a"]["parents"] = ["elsewhere"]
        elif event == "ambiguous":
            add(opener, "copy", entry("Book Original copy.docx", "a"))
        else:
            add(opener, "output", entry("Book One.docx", "a"))
        return inventory
    monkeypatch.setattr(drive_index, "sync", changed_after_sync)
    report, _ = run(tmp_path, opener=opener)
    assert not report.uploaded and not provider.calls
    if event == "delivery":
        assert opener.files["src"]["name"] == "Book Original_done.docx"
