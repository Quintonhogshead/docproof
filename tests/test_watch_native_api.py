"""The native corrections settings and file boundary exposed by DocWatch."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.watch.settings import WatchSettings


def test_native_form_settings_round_trip_through_watch_api(tmp_path):
    app = create_app(tmp_path, start_runner=False)
    fields = {
        "corrections_engine": "native",
        "corrections_native_form_poll": True,
        "corrections_native_quiet_seconds": 10800,
        "corrections_native_form_project_property": "docproof_project_id",
        "corrections_native_form_file_count_property": "correction_file_count",
        "corrections_native_start_after": "2026-01-01T00:00:00Z",
        "corrections_native_form_first_property": "firstname",
        "corrections_native_form_last_property": "lastname",
        "corrections_native_form_book_property": "which_book_are_these_corrections_for_",
        "corrections_native_form_file_property": "interior_design_corrections_documents",
        "corrections_native_form_notes_property": "anything_else_",
        "corrections_native_project_id_property": "hs_object_id",
        "corrections_native_project_book_property": "book_title",
    }
    with TestClient(app) as client:
        response = client.put("/api/watch", json=fields)
        assert response.status_code == 200
        watch = response.json()["watch"]
        for name, value in fields.items():
            assert watch[name] == value

    saved = WatchSettings.load(app.state.watch.home)
    for name, value in fields.items():
        assert getattr(saved, name) == value


def test_native_file_route_serves_job_output_but_not_paths_outside_job(tmp_path):
    app = create_app(tmp_path, start_runner=False)
    jobs = app.state.watch.home / "native_jobs"
    job = jobs / "job-1"
    job.mkdir(parents=True)
    output = job / "output.pdf"
    output.write_bytes(b"native result")
    spreadsheet = job / "corrections.xlsx"
    spreadsheet.write_bytes(b"audit result")
    (job / "job.json").write_text(
        json.dumps({"result": {"output_pdf": str(output),
                                "audit_spreadsheet": str(spreadsheet)}}), encoding="utf-8")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    bad = jobs / "job-2"
    bad.mkdir()
    (bad / "job.json").write_text(
        json.dumps({"result": {"output_pdf": str(outside),
                                "audit_spreadsheet": str(outside)}}), encoding="utf-8")

    with TestClient(app) as client:
        answer = client.get("/api/watch/native/jobs/job-1/file/pdf")
        assert answer.status_code == 200
        assert answer.content == b"native result"
        spreadsheet_answer = client.get("/api/watch/native/jobs/job-1/file/spreadsheet")
        assert spreadsheet_answer.status_code == 200
        assert spreadsheet_answer.content == b"audit result"
        assert client.get("/api/watch/native/jobs/job-2/file/pdf").status_code == 404
        assert client.get("/api/watch/native/jobs/job-2/file/spreadsheet").status_code == 404
        assert client.get("/api/watch/native/jobs/job-1/file/../../outside").status_code in {404, 422}


def test_hubspot_credential_response_never_echoes_token(tmp_path, monkeypatch):
    token = "pat-secret-that-must-not-be-returned"
    monkeypatch.setattr("app.routes.watch.settingslib.set_api_key", lambda *_: None)
    app = create_app(tmp_path, start_runner=False)
    with TestClient(app) as client:
        response = client.post("/api/watch/hubspot-connection", json={"token": token})
    assert response.status_code == 200
    assert token not in response.text
    assert response.json() == {"configured": True}


def test_manual_file_is_bound_to_the_exact_frozen_submission(tmp_path):
    from app.watch import native_files
    app = create_app(tmp_path, start_runner=False)
    home = app.state.watch.home
    folder = home / "native_jobs" / "awaiting-file"
    folder.mkdir(parents=True)
    url = "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/12345?filename=corrections.txt"
    (folder / "job.json").write_text(json.dumps({"submission_urls": [url]}))
    with TestClient(app) as client:
        bad = client.post("/api/watch/native/jobs/awaiting-file/attachment/99999",
                          files={"attachment": ("corrections.txt", b"Fix one word")})
        assert bad.status_code == 400
        good = client.post("/api/watch/native/jobs/awaiting-file/attachment/12345",
                           files={"attachment": ("corrections.txt", b"Fix one word")})
        assert good.status_code == 200
    cached = native_files.cached_file(url, home / "manual-attachments")
    assert cached.read_bytes() == b"Fix one word"


def test_native_ui_names_the_form_properties_and_poll_mode():
    from pathlib import Path

    root = Path(__file__).parents[1]
    html = (root / "app/static/index.html").read_text(encoding="utf-8")
    js = (root / "app/static/app.js").read_text(encoding="utf-8")
    for field in (
        "corrections-native-form-poll",
        "corrections-native-start-after",
        "corrections-native-form-first",
        "corrections-native-form-last",
        "corrections-native-form-book",
        "corrections-native-project-id",
        "corrections-native-project-book",
    ):
        assert field in html
        assert field in js
    for name in (
        "firstname", "lastname", "which_book_are_these_corrections_for_",
        "interior_design_corrections_documents", "anything_else_",
    ):
        assert name in html


def test_watch_status_exposes_worker_heartbeat_and_unmatched_intake(tmp_path):
    from app.watch import status as watch_status

    home = tmp_path / "watch"
    home.mkdir()
    (home / "native-worker.json").write_text(json.dumps({
        "state": "attention",
        "started_at": "2026-09-09T12:00:00Z",
        "finished_at": "2026-09-09T12:01:00Z",
        "error": "",
        "report": {"corrected": ["Book 1"], "needs_human": [["Book 2", "ambiguous"]],
                   "failed": [], "secret": "must not escape"},
    }), encoding="utf-8")
    (home / "native-intake.json").write_text(json.dumps({
        "checked_at": "2026-09-09T12:01:00Z",
        "unmatched": [{"submission_id": "sub-1", "first_name": "Terry",
                        "last_name": "Ardell", "book": "Book 3",
                        "reason": "No matching book", "private_url": "secret"}],
    }), encoding="utf-8")

    body = watch_status.status(home, get_key=lambda _name: None)
    assert body["native_worker"] == {
        "state": "attention", "started_at": "2026-09-09T12:00:00Z",
        "local_only": False, "drive_uploads_enabled": False,
        "finished_at": "2026-09-09T12:01:00Z", "error": None,
        "report": {"listed": 0, "waiting": 0, "corrected": 1,
                    "uploaded": 0, "failed": 0, "needs_human": 1},
    }
    assert body["native_intake"]["count"] == 1
    assert "identity" not in body["native_intake"]["unmatched"][0]
    assert body["native_intake"]["unmatched"][0]["first_name"] == "Terry"
    assert "private_url" not in body["native_intake"]["unmatched"][0]
