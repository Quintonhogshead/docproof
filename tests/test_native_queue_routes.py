from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.watch.drive import DriveFile, FOLDER_MIME


def _app(tmp_path):
    return create_app(tmp_path, start_runner=False)


def _book():
    return {
        "project_id": "project-1",
        "title": "The Book",
        "author": "A Writer",
        "surname": "Writer",
        "folder_id": "folder-1",
        "source_id": "source-2",
        "source_version": 2,
        "title_aliases": [],
        "author_aliases": [],
    }


def test_queue_status_is_local_and_uses_configured_quiet_period(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        answer = client.get("/api/watch/native/queue")
    assert answer.status_code == 200
    assert answer.json()["quiet_seconds"] == 10800
    assert answer.json()["events"] == []


def test_book_registration_requires_drive_connection(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda _home: None)
    with TestClient(app) as client:
        answer = client.post("/api/watch/native/books", json=_book())
    assert answer.status_code == 503
    assert "Drive" in answer.json()["detail"]


def test_delivery_release_refuses_when_uploads_are_disabled(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        answer = client.post('/api/watch/native/batches/test-batch/resume-delivery')
    assert answer.status_code == 409
    assert 'disabled' in answer.json()['detail']


def test_book_registration_reads_folder_and_unique_highest_source(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda _home: "access-token")
    monkeypatch.setattr("app.watch.drive._json_call", lambda *args, **kwargs: {
        "id": "folder-1", "name": "Interior Design", "mimeType": FOLDER_MIME,
        "trashed": False,
    })
    monkeypatch.setattr("app.watch.drive.list_folder", lambda *args, **kwargs: [
        DriveFile("source-1", "Writer - Book 1.indd", "application/octet-stream"),
        DriveFile("source-2", "Writer - Book 2.indd", "application/octet-stream"),
    ])
    with TestClient(app) as client:
        answer = client.post("/api/watch/native/books", json=_book())
    assert answer.status_code == 200
    assert answer.json()["books"][0]["source_id"] == "source-2"
    assert answer.json()["books"][0]["source_version"] == 2


def test_book_registration_rejects_a_stale_source_mapping(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda _home: "access-token")
    monkeypatch.setattr("app.watch.drive._json_call", lambda *args, **kwargs: {
        "id": "folder-1", "name": "Interior Design", "mimeType": FOLDER_MIME,
        "trashed": False,
    })
    monkeypatch.setattr("app.watch.drive.list_folder", lambda *args, **kwargs: [
        DriveFile("source-2", "Writer - Book 2.indd", "application/octet-stream"),
    ])
    payload = {**_book(), "source_id": "old-source"}
    with TestClient(app) as client:
        answer = client.post("/api/watch/native/books", json=payload)
    assert answer.status_code == 409
    assert "highest export" in answer.json()["detail"]


def test_book_registration_rejects_non_folder_metadata(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda _home: "access-token")
    monkeypatch.setattr("app.watch.drive._json_call", lambda *args, **kwargs: {
        "id": "folder-1", "name": "Other", "mimeType": "text/plain", "trashed": False,
    })
    with TestClient(app) as client:
        answer = client.post("/api/watch/native/books", json=_book())
    assert answer.status_code == 400
    assert "Interior Design" in answer.json()["detail"]
