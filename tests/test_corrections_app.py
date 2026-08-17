"""The corrections job kind, driven through HTTP.

Same rules as the other app tests: nothing reaches a vendor and nothing sleeps.
Corrections runs no model at all, so there is no provider to fake — the whole
run is deterministic Python over the exported IDML.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from docproof.corrections.idml import parse_story

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # No provider is built for a corrections job, but the app has other routes
    # that ask after keys; keep them satisfied so the fixture is reusable.
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"}})
    app = create_app(tmp_path, start_runner=False)
    app.state.settings.output_dir = str(tmp_path / "out")
    app.state.runner.start()
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
    app.state.runner.stop()


def upload(client, name):
    with (FIXTURES / name).open("rb") as fh:
        return client.post("/api/files",
                           files={"files": (name, fh.read())}).json()["files"][0]


def start_corrections(client, file_id, corrections, **extra):
    body = {"file_ids": [file_id], "kind": "corrections",
            "corrections": json.dumps(corrections) if not isinstance(
                corrections, str) else corrections, **extra}
    resp = client.post("/api/jobs", json=body)
    return resp


def run_corrections(client, file_id, corrections, **extra):
    job = start_corrections(client, file_id, corrections, **extra).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    return client.get(f"/api/jobs/{job['id']}").json()


def story_text(idml: Path, story_id: str = "ue0") -> list[str]:
    with zipfile.ZipFile(idml) as z:
        s = parse_story(z.read(f"Stories/Story_{story_id}.xml"), story_id)
    return [p.text for p in s.paragraphs]


# --- staging ------------------------------------------------------------------

def test_an_idml_can_be_corrected(client):
    staged = upload(client, "layout.idml")
    assert staged["ok"] and staged["can_correct"] is True
    assert staged["correct_error"] is None


def test_a_word_document_cannot_be_corrected(client):
    staged = upload(client, "simple.docx")
    assert staged["can_correct"] is False
    assert "InDesign" in staged["correct_error"]


# --- running ------------------------------------------------------------------

def test_corrections_produce_a_corrected_file_and_a_report(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    assert job["state"] == "done", job.get("error")
    assert job["is_corrections"] and job["kind"] == "corrections"
    assert job["applied"] == 1
    assert job["flags"] == 0 and job["discrepancies"] == 0
    assert job["verified"] is True
    assert job["cost"] == 0.0

    results = Path(job["results_dir"])
    assert (results / "layout_corrected.idml").is_file()
    assert (results / "corrections_notes.md").is_file()
    assert (results / "corrections.json").is_file()
    # The corrected file really carries the change.
    assert story_text(results / "layout_corrected.idml")[4] == \
        "There were several mistakes here to find."


def test_the_corrected_file_and_report_are_downloadable(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    for which, expected in (("corrected", "layout_corrected.idml"),
                            ("corrections-notes", "corrections_notes.md"),
                            ("document", "layout_corrected.idml")):
        r = client.get(f"/api/jobs/{job['id']}/file/{which}")
        assert r.status_code == 200, which
        assert expected in r.headers["content-disposition"]


def test_the_corrections_report_reads_back_for_the_screen(client):
    job = run_corrections(
        client, upload(client, "layout.idml")["id"],
        [{"find": "Their were", "replace": "There were"},
         {"find": "nowhere in the book", "replace": "x"}])
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert report["mode"] == "apply"
    assert report["deterministic"] is True
    assert report["apply"]["applied"] == 1
    assert len(report["apply"]["flagged"]) == 1
    # The unanchored edit shows in the job counters too.
    assert job["applied"] == 1 and job["flags"] == 1 and job["verified"] is False


def test_a_partly_unreadable_list_still_applies_the_good_edits(client):
    """A single bad entry is reported, not fatal — the rest apply."""
    job = run_corrections(
        client, upload(client, "layout.idml")["id"],
        [{"find": "Their were", "replace": "There were"},
         {"replace": "no find to anchor"}])          # a parse issue
    assert job["state"] == "done"
    assert job["applied"] == 1
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert len(report["parse"]["issues"]) == 1


# --- refusing bad input at submit ---------------------------------------------

def test_a_non_idml_file_is_refused(client):
    staged = upload(client, "simple.docx")
    r = start_corrections(client, staged["id"],
                          [{"find": "a", "replace": "b"}])
    assert r.status_code == 400
    assert "InDesign" in r.json()["detail"]


def test_an_empty_corrections_list_is_refused(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], "")
    assert r.status_code == 400


def test_malformed_json_is_refused_with_a_readable_message(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], "{not json")
    assert r.status_code == 400
    assert "could not be read" in r.json()["detail"]


def test_a_list_with_no_usable_corrections_is_refused(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], [{"replace": "no find"}])
    assert r.status_code == 400
    assert "No usable corrections" in r.json()["detail"]


def test_more_than_one_file_is_refused(client):
    a = upload(client, "layout.idml")["id"]
    b = upload(client, "layout.idml")["id"]
    r = client.post("/api/jobs", json={
        "file_ids": [a, b], "kind": "corrections",
        "corrections": json.dumps([{"find": "a", "replace": "b"}])})
    assert r.status_code == 400
    assert "one InDesign file" in r.json()["detail"]


# --- it coexists with the other kinds -----------------------------------------

def test_a_corrections_job_appears_in_the_results_list(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    listed = client.get("/api/jobs").json()["jobs"]
    assert any(j["id"] == job["id"] and j["is_corrections"] for j in listed)
    assert job["plain_state"] == "Ready"


def test_corrections_never_asks_for_a_model_or_a_key(client, monkeypatch):
    """The deterministic path builds no provider and needs no key — so a run
    still succeeds with every key removed."""
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "")
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    assert job["state"] == "done", job.get("error")
    assert job["applied"] == 1
