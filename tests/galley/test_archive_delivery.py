"""The fixed hand-off is split: the redline to the author folder, the record to
DocWatch's Drive archive. The publisher routes by each artifact's destination,
delivers the redline first, files the record only when there is an archive to
file it in, and uploads the verdict last."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from galley import driver as gd


def _package(tmp_path):
    files = []
    for name, destination in (("Writer - Book One - Pre-Proofread.docx", "handoff"),
                              ("Writer - Book One - outcome.json", "archive"),
                              ("Writer - Book One - proofreading report.md", "archive")):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files.append({"path": str(path), "name": name, "destination": destination,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"packet_sha256": "packet", "archive_name": "Writer - Book One", "artifacts": files}


def test_redline_goes_to_the_author_folder_and_the_record_to_the_archive(tmp_path):
    package = _package(tmp_path)
    ledger = tmp_path / "delivery.json"
    calls = []

    def upload(files, folder):
        calls.append((files[0].name, folder))
        return ["id-" + files[0].name]

    ids = gd.publish_verified_handoff(package, "author-folder", ledger, source_id="book-1",
                                      archive_folder_id="archive-root", upload=upload, verify=lambda *a: True)
    assert len(ids) == 3
    assert calls == [("Writer - Book One - Pre-Proofread.docx", "author-folder"),
                     ("Writer - Book One - proofreading report.md", "archive-root"),
                     ("Writer - Book One - outcome.json", "archive-root")]
    saved = json.loads(ledger.read_text())
    assert saved["status"] == "delivered"
    assert saved["artifacts"]["Writer - Book One - Pre-Proofread.docx"]["destination"] == "handoff"
    assert saved["artifacts"]["Writer - Book One - outcome.json"]["folder_id"] == "archive-root"


def test_without_an_archive_the_redline_is_delivered_and_the_record_waits(tmp_path):
    package = _package(tmp_path)
    ledger = tmp_path / "delivery.json"
    calls = []

    def upload(files, folder):
        calls.append((files[0].name, folder))
        return ["id-" + files[0].name]

    with pytest.raises(gd.DriverError, match="no Drive archive folder"):
        gd.publish_verified_handoff(package, "author-folder", ledger, source_id="book-1",
                                    upload=upload, verify=lambda *a: True)
    assert calls == [("Writer - Book One - Pre-Proofread.docx", "author-folder")]
    saved = json.loads(ledger.read_text())
    assert saved["status"] == "pending" and "archive" in saved["archive_error"]
    assert saved["artifacts"]["Writer - Book One - Pre-Proofread.docx"]["verified"] is True
    # Once DocWatch names an archive, the retry files the record and finishes
    # without uploading the redline again.
    gd.publish_verified_handoff(package, "author-folder", ledger, source_id="book-1",
                                archive_folder_id="archive-root", upload=upload, verify=lambda *a: True)
    assert [name for name, _ in calls].count("Writer - Book One - Pre-Proofread.docx") == 1
    assert calls[-1] == ("Writer - Book One - outcome.json", "archive-root")
    saved = json.loads(ledger.read_text())
    assert saved["status"] == "delivered" and "archive_error" not in saved


def test_a_legacy_package_without_destinations_is_delivered_beside_the_book(tmp_path):
    package = _package(tmp_path)
    for row in package["artifacts"]:
        row.pop("destination")
    calls = []

    def upload(files, folder):
        calls.append(folder)
        return ["id-" + files[0].name]

    gd.publish_verified_handoff(package, "author-folder", tmp_path / "delivery.json", source_id="book-1",
                                upload=upload, verify=lambda *a: True)
    assert calls == ["author-folder"] * 3


def test_with_drive_credentials_the_record_is_filed_in_a_run_folder_under_the_archive(tmp_path, monkeypatch):
    from app.watch import archive, drive

    package = _package(tmp_path)
    contents = {row["name"]: open(row["path"], "rb").read() for row in package["artifacts"]}
    uploads, made, remote = [], [], {}
    monkeypatch.setattr(gd, "drive_token", lambda: "token")
    monkeypatch.setattr(drive, "list_folder", lambda token, folder: remote.get(folder, []))

    def upload(token, folder, path, **kw):
        uploads.append((path.name, folder, kw["app_properties"]))
        remote.setdefault(folder, []).append(SimpleNamespace(id=f"id-{path.name}", name=path.name,
                                                             app_properties=kw["app_properties"]))
        return f"id-{path.name}"
    monkeypatch.setattr(drive, "upload", upload)
    monkeypatch.setattr(drive, "download_bytes", lambda token, file_id, **kw: contents[file_id[3:]])

    def run_folder(token, root, *, kind, name, source_id, month, opener=None):
        made.append((root, kind, name, source_id))
        return "run-folder"
    monkeypatch.setattr(archive, "external_run_folder", run_folder)

    ledger = tmp_path / "delivery.json"
    gd.publish_verified_handoff(package, "author-folder", ledger, source_id="book-1",
                                archive_folder_id="archive-root")
    assert made == [("archive-root", "galley", "Writer - Book One", "book-1")]
    assert [(name, folder) for name, folder, _ in uploads] == [
        ("Writer - Book One - Pre-Proofread.docx", "author-folder"),
        ("Writer - Book One - proofreading report.md", "run-folder"),
        ("Writer - Book One - outcome.json", "run-folder")]
    props = uploads[-1][2]
    assert props["galley_source"] == "book-1" and props["galley_destination"] == "archive"
    saved = json.loads(ledger.read_text())
    assert saved["archive"] == {"root": "archive-root", "folder_id": "run-folder",
                                "path": saved["archive"]["path"]}
    assert saved["archive"]["path"].startswith("Proofing/") and saved["archive"]["path"].endswith("/Writer - Book One")
    # A retry reuses the remembered run folder rather than resolving it again.
    made.clear()
    gd.publish_verified_handoff(package, "author-folder", ledger, source_id="book-1",
                                archive_folder_id="archive-root")
    assert made == []


def test_external_run_folder_is_found_by_the_source_tag_or_made(monkeypatch):
    from app.watch import archive, drive

    created, queries = [], []

    def find_children(token, parent, *, name=None, app_property=None, folders_only=False, opener=None):
        queries.append((parent, name, app_property))
        if name == "Proofing":
            return [SimpleNamespace(id="proofing")]
        if name == "2026-09":
            return [SimpleNamespace(id="month")]
        if app_property == ("galley_source", "known"):
            return [SimpleNamespace(id="existing-run")]
        return []

    def create_folder(token, parent, name, *, app_properties=None, opener=None):
        created.append((parent, name, app_properties))
        return "new-run"
    monkeypatch.setattr(drive, "find_children", find_children)
    monkeypatch.setattr(drive, "create_folder", create_folder)
    assert archive.external_run_folder("t", "root", kind="galley", name="Writer - Book One",
                                       source_id="known", month="2026-09", opener=object()) == "existing-run"
    assert created == []
    assert archive.external_run_folder("t", "root", kind="galley", name="Writer - Book One",
                                       source_id="fresh", month="2026-09", opener=object()) == "new-run"
    assert created == [("month", "Writer - Book One",
                        {archive.ARCHIVE_PROP: "1", archive.SOURCE_PROP: "fresh",
                         archive.KIND_PROP: "galley", archive.JOBSTATE_PROP: "external"})]
