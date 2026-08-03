"""The app backend, driven through HTTP with a fake provider underneath.
No test here reaches a vendor, and none sleeps on a timer — the ticker's body
is called directly."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobRunner, JobStore  # noqa: F401 - JobRunner patched
from app.main import create_app
from app.settings import Paths, Settings
from .conftest import FIXTURES
from .fakes import ScriptedBatchProvider, finding_result

SPLICE = "The manuscript was finished, nobody wanted to read it."


@pytest.fixture
def provider():
    return ScriptedBatchProvider(pending_polls=0)


@pytest.fixture
def client(tmp_path, provider, monkeypatch):
    """An app whose only vendor contact is the fake, and whose keys look
    configured without anything being stored."""
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.main.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.main.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"},
        "openai": {"configured": True, "source": "environment"}})

    # Run a single error-type pass so a scripted finding is unambiguous about
    # which pass it belongs to. Uploads still preflight against the real config.
    base = JobRunner.config_for

    def one_pass(self, job):
        cfg = base(self, job)
        cfg.error_types = ["comma_splice"]
        return cfg

    monkeypatch.setattr(JobRunner, "config_for", one_pass)

    app = create_app(tmp_path, start_runner=False)
    app.state.settings.output_dir = str(tmp_path / "out")
    app.state.runner.start()
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
    app.state.runner.stop()


def _upload(client, name="simple.docx"):
    with (FIXTURES / name).open("rb") as fh:
        resp = client.post("/api/files", files={"files": (name, fh)})
    assert resp.status_code == 200
    return resp.json()["files"][0]


def _run(client, file_id, **body):
    payload = {"file_ids": [file_id], "model": "claude-sonnet-5",
               "mode": "now", **body}
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["jobs"][0]


# --- upload -------------------------------------------------------------------

def test_upload_preflights_at_drop_time(client):
    staged = _upload(client)
    assert staged["ok"] is True
    assert staged["sections"] >= 1 and staged["paragraphs"] == 3
    assert staged["requests"] == staged["sections"] * 3   # three passes


def test_upload_rejects_non_docx(client):
    resp = client.post("/api/files",
                       files={"files": ("notes.txt", b"hello")})
    entry = resp.json()["files"][0]
    assert entry["ok"] is False and ".docx" in entry["error"]


# --- run now ------------------------------------------------------------------

def test_run_now_produces_downloadable_results(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    staged = _upload(client)
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()

    final = client.get(f"/api/jobs/{job['id']}").json()
    assert final["state"] == "done", final.get("error")
    assert final["ready"] is True
    assert final["plain_state"] == "Ready"

    docx = client.get(f"/api/jobs/{job['id']}/file/docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    assert "docproof review" in client.get(
        f"/api/jobs/{job['id']}/file/summary").text
    assert json.loads(
        client.get(f"/api/jobs/{job['id']}/file/findings").text)["findings"]


def test_job_is_refused_without_a_key(client, monkeypatch):
    staged = _upload(client)
    monkeypatch.setattr("app.main.get_api_key", lambda p: None)
    resp = client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                          "model": "claude-sonnet-5",
                                          "mode": "now"})
    assert resp.status_code == 400
    assert "Settings" in resp.json()["detail"]


def test_failed_job_reports_plainly_and_can_be_retried(client, monkeypatch):
    staged = _upload(client)
    from docproof.providers import ProviderError
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: (_ for _ in ()).throw(
                            ProviderError("No Anthropic API key.")))
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()

    failed = client.get(f"/api/jobs/{job['id']}").json()
    assert failed["state"] == "failed"
    assert failed["plain_state"] == "Needs attention"
    assert "API key" in failed["error"]
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200


# --- batch --------------------------------------------------------------------

def test_batch_job_waits_then_collects_on_a_tick(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()

    waiting = client.get(f"/api/jobs/{job['id']}").json()
    assert waiting["state"] == "waiting"
    assert "overnight" in waiting["plain_state"]

    client.post("/api/tick")
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done", done.get("error")
    assert client.get(f"/api/jobs/{job['id']}/file/docx").status_code == 200


def test_scheduled_job_holds_until_its_time(client, monkeypatch):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch", schedule_at="23:59")
    scheduled = client.get(f"/api/jobs/{job['id']}").json()
    assert scheduled["state"] == "scheduled"
    assert scheduled["plain_state"] == "Waiting until 23:59"

    client.post("/api/tick")                     # not due yet
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "scheduled"

    monkeypatch.setattr(JobRunner, "_due", lambda self, job: True)
    client.post("/api/tick")
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "waiting"


def test_a_restarted_app_resumes_an_in_flight_batch(client, provider, tmp_path):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "waiting"

    # A brand new runner over the same directory — nothing carried in memory.
    settings = Settings(output_dir=str(tmp_path / "out"))
    fresh = JobRunner(JobStore(Paths(tmp_path)), settings,
                      config_path=client.app_state.runner.config_path)
    fresh.tick_once()
    assert fresh.store.get(job["id"]).state == "done"


# --- models and settings ------------------------------------------------------

def test_models_endpoint_prices_the_staged_files(client):
    staged = _upload(client)
    body = client.get(f"/api/models?file_ids={staged['id']}").json()
    by_id = {m["id"]: m for m in body["models"]}

    assert {m["provider"] for m in body["models"]} == {"anthropic", "openai"}
    opus = by_id["claude-opus-5"]
    assert opus["cost_now"] > opus["cost_batch"] > 0
    # Batch is exactly half, at any hour — the UI copy depends on this.
    assert opus["cost_batch"] == pytest.approx(opus["cost_now"] / 2)
    assert by_id["claude-haiku-4-5"]["cost_now"] < opus["cost_now"]


def test_settings_round_trip_never_returns_a_key(client, monkeypatch):
    saved = {}
    monkeypatch.setattr("app.main.set_api_key",
                        lambda p, k: saved.__setitem__(p, k))
    resp = client.put("/api/settings", json={
        "model": "claude-haiku-4-5", "min_confidence": "high",
        "anthropic_key": "sk-ant-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["model"] == "claude-haiku-4-5"
    assert saved["anthropic"] == "sk-ant-secret"
    assert "sk-ant-secret" not in json.dumps(body)

    again = client.get("/api/settings").json()
    assert again["settings"]["min_confidence"] == "high"


def test_settings_survive_a_restart(client, tmp_path):
    client.put("/api/settings", json={"model": "claude-opus-5"})
    assert Settings.load(Paths(tmp_path)).model == "claude-opus-5"


def test_staged_file_id_cannot_escape_the_uploads_folder(client, tmp_path):
    outside = tmp_path.parent / "secret.docx"
    outside.write_bytes(b"PK")
    resp = client.post("/api/jobs", json={
        "file_ids": ["../../secret.docx"], "model": "claude-sonnet-5",
        "mode": "now"})
    assert resp.status_code == 404


def test_plain_state_never_leaks_jargon():
    words = ("batch", "API", "chunk", "token", "provider")
    for state in ("scheduled", "queued", "running", "waiting", "collecting",
                  "done", "failed"):
        text = Job(id="j", filename="f.docx", source_path="/f.docx",
                   model="m", mode="batch", state=state,
                   schedule_at="23:00").plain_state()
        assert not any(w.lower() in text.lower() for w in words), text
