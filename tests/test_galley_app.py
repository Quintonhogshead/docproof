"""E1 — the galley job kind, driven through HTTP with fake detectors.

Same rules as the other app tests: nothing reaches a vendor and nothing sleeps.
The detector adapters are faked through the runner's `_galley_adapters` seam, so
no provider is built and no model is called.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FIXTURES
from tests.galley.fakes import FakeDetector, gfinding

TINY_NOVEL = FIXTURES / "tiny_novel.docx"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"}})
    app = create_app(tmp_path, start_runner=False)
    app.state.settings.output_dir = str(tmp_path / "out")

    # Inject fake detectors: wave one is a ladder that reports one finding and a
    # small cost, so the run reaches a model-free `done`.
    def fake_adapters(job, cfg):
        return {
            "docproof_ladder": FakeDetector(
                name="docproof_ladder",
                scripted=[gfinding("g-1", "body-0001", "teh", "the")],
                cost_usd=0.5,
            )
        }

    app.state.runner._galley_adapters = fake_adapters
    app.state.runner.start()
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
    app.state.runner.stop(join=30)


def upload(client, path):
    with path.open("rb") as fh:
        return client.post(
            "/api/files", files={"files": (path.name, fh.read())}
        ).json()["files"][0]


def _run_galley(client, file_id, **extra):
    body = {"file_ids": [file_id], "kind": "galley", "tier": "T0",
            "budget_usd": 10.0, **extra}
    job = client.post("/api/jobs", json=body).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    return client.get(f"/api/jobs/{job['id']}").json()


@pytest.mark.skipif(not TINY_NOVEL.exists(), reason="fixture book missing")
def test_a_galley_job_runs_to_done(client):
    staged = upload(client, TINY_NOVEL)
    assert staged["ok"]
    job = _run_galley(client, staged["id"])

    # GALLEY-004: the app runs the certify gate; with no verify/settle
    # evidence the job is needs_human, its deliverables kept and exposed.
    assert job["state"] == "needs_human"
    assert job["is_galley"] is True
    # to_api carries wave and spend.
    assert job["galley_wave"] == 1
    assert job["galley_waves_total"] == 1
    assert job["cost"] == pytest.approx(0.5)
    assert job["budget_usd"] == pytest.approx(10.0)
    # A case file was written into the results dir.
    from pathlib import Path
    assert job["results_dir"]
    assert (Path(job["results_dir"]) / "casefile.json").is_file()


@pytest.mark.skipif(not TINY_NOVEL.exists(), reason="fixture book missing")
def test_galley_rejects_bad_tier(client):
    staged = upload(client, TINY_NOVEL)
    resp = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "kind": "galley", "tier": "T9",
        "budget_usd": 10.0})
    assert resp.status_code == 400


@pytest.mark.skipif(not TINY_NOVEL.exists(), reason="fixture book missing")
def test_galley_defaults_budget_from_tier(client):
    staged = upload(client, TINY_NOVEL)
    job = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "kind": "galley", "tier": "T2"}
    ).json()["jobs"][0]
    assert job["tier"] == "T2"
    assert job["budget_usd"] == pytest.approx(60.0)  # T2 default
