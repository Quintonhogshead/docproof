"""The judge gates in the app: each panel switch, model picker and editable
instruction field, from the submitted job down to the run config.

The switch itself is an ordinary feature toggle; what needs holding is that the
picker overrides the house default only when a pick was made, that an untouched
panel changes nothing, and that a job record written before this feature existed
still loads and runs. See app/features.py, app/jobs.py config_for, and
docproof/judges.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.features import FEATURES_BY_ID, feature_catalog
from app.jobs import Job, JobRunner, JobStore
from app.main import create_app
from app.settings import Paths, Settings
from docproof.config import Config

from .conftest import FIXTURES

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


@pytest.fixture
def runner(tmp_path):
    store = JobStore(Paths(tmp_path).ensure())
    return store, JobRunner(store, Settings(), config_path=CONFIG)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app with a key on file and the runner off: jobs are created and
    recorded, and nothing calls a vendor."""
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    app = create_app(tmp_path, start_runner=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def uploaded(client) -> str:
    with (FIXTURES / "simple.docx").open("rb") as fh:
        r = client.post("/api/files", files={"files": ("simple.docx", fh)})
    assert r.status_code == 200
    return r.json()["files"][0]["id"]


def _job(store, **over) -> Job:
    fields = {"id": "j1", "filename": "simple.docx",
              "source_path": str(FIXTURES / "simple.docx"),
              "model": "claude-sonnet-5", "mode": "now", "glossary_model": "off"}
    fields.update(over)
    return store.save(Job(**fields))


# --- the switch --------------------------------------------------------------

def test_the_switch_flips_the_config_flag():
    cfg = Config()
    spec = FEATURES_BY_ID["meaning_check"]
    assert spec.read(cfg) is False
    spec.write(cfg, True)
    assert cfg.meaning_check.enabled is True
    spec.write(cfg, False)
    assert cfg.meaning_check.enabled is False


def test_the_catalog_prices_it_off_its_own_model():
    entry = next(f for f in feature_catalog(Config()) if f["id"] == "meaning_check")
    assert entry["heavy"] is True
    assert entry["group"] == "pass"
    assert entry["default"] is False
    assert entry["cost"] == {"kind": "judge", "model": "claude-fable-5"}


def test_the_run_config_reaches_the_gate_through_the_feature_map(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True}))
    assert cfg.meaning_check.enabled is True


# --- the picker and the prompt ----------------------------------------------

def test_a_picked_model_overrides_the_house_default(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True},
                            meaning_model="claude-opus-5"))
    assert cfg.meaning_check.model == "claude-opus-5"


def test_an_untouched_picker_keeps_the_house_default(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True}))
    assert cfg.meaning_check.model == "claude-fable-5"


def test_edited_instructions_reach_the_run_config(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True},
                            meaning_prompt="Ask only about meaning."))
    assert cfg.meaning_check.prompt == "Ask only about meaning."


def test_blank_instructions_stay_the_engine_sentinel(runner):
    """Empty is the engine's "use the built-in default" sentinel, so it must
    pass through verbatim rather than being filled in here."""
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True}))
    assert cfg.meaning_check.prompt == ""


def test_a_record_from_before_the_feature_still_runs(runner):
    store, r = runner
    cfg = r.config_for(_job(store))                    # no meaning fields at all
    assert cfg.meaning_check.enabled is False
    assert cfg.meaning_check.model == "claude-fable-5"


def test_continuity_only_strips_the_gate_with_the_other_passes(runner):
    """A continuity-only run makes no text changes, so there is nothing for the
    gate to read — it comes off with everything else rather than being billed
    for a pass with no work in it."""
    store, r = runner
    cfg = r.config_for(_job(store, continuity_only=True,
                            features={"meaning_check": True}))
    assert cfg.meaning_check.enabled is False


# --- submission --------------------------------------------------------------

def test_download_anyway_replays_the_original_held_back_changes(runner,
                                                                monkeypatch,
                                                                tmp_path):
    """The rebuild must reproduce the gate's verdicts, not discard them. The
    verdicts are not in the checkpoint (that holds raw detector output written
    before finish() ever runs), so they come from what the run recorded."""
    import json

    from docproof.pipeline import MEANING_HELD_FILE

    store, r = runner
    out = tmp_path / "out"
    out.mkdir()
    held = [{"para_id": "body-0000", "original_text": "A sentence.",
             "occurrence": 1, "corrected_text": "A sentance.",
             "explanation": "Not applied: changes the word."}]
    (out / MEANING_HELD_FILE).write_text(json.dumps(held), encoding="utf-8")
    job = _job(store, features={"meaning_check": True}, state="failed",
               results_dir=str(out))
    seen = {}

    def fake_finish(prepared, findings, usage, cfg, **kw):
        seen["held"] = kw.get("judge_held")
        raise RuntimeError("stop here — the arguments are what this test is after")

    monkeypatch.setattr("app.jobs.finish", fake_finish)
    monkeypatch.setattr("app.jobs.prepare", lambda *a, **k: object())
    monkeypatch.setattr(r, "_checkpoint", lambda *a, **k: object())
    monkeypatch.setattr("app.jobs.run_sync", lambda *a, **k: ([], object()))
    with pytest.raises(RuntimeError):
        r.download_anyway(job.id)
    assert seen["held"] == held


def test_download_anyway_turns_the_gate_off(runner, monkeypatch, tmp_path):
    """`download_anyway` promises to rebuild the deliverable from what the run
    already paid for, without touching a provider. The gate lives inside
    finish() and builds its own provider, so it has to be switched off there or
    that promise becomes a bill."""
    store, r = runner
    job = _job(store, features={"meaning_check": True}, state="failed",
               results_dir=str(tmp_path / "out"))
    seen = {}

    def fake_finish(prepared, findings, usage, cfg, **kw):
        seen["enabled"] = cfg.meaning_check.enabled
        raise RuntimeError("stop here — the config is what this test is after")

    monkeypatch.setattr("app.jobs.finish", fake_finish)
    monkeypatch.setattr("app.jobs.prepare",
                        lambda *a, **k: object())
    monkeypatch.setattr(r, "_checkpoint", lambda *a, **k: object())
    monkeypatch.setattr("app.jobs.run_sync",
                        lambda *a, **k: ([], object()))
    with pytest.raises(RuntimeError):
        r.download_anyway(job.id)
    assert seen["enabled"] is False


def test_the_panel_offers_the_gate_and_its_prompt_default(client):
    body = client.get("/api/features").json()
    ids = [f["id"] for f in body["features"]]
    assert "meaning_check" in ids
    assert body["meaning"]["prompt_default"].strip()


def test_an_unknown_meaning_model_is_refused(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"meaning_check": True}, "meaning_model": "not-a-model"})
    assert r.status_code == 400
    assert "meaning" in r.json()["detail"].lower()


def test_an_unknown_meaning_model_is_refused_even_with_the_gate_off(client,
                                                                    uploaded):
    """The pick lands on the run config whether or not the gate runs, so an id
    that is not in the catalog is refused either way."""
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"meaning_check": False}, "meaning_model": "not-a-model"})
    assert r.status_code == 400


def test_a_keyless_meaning_model_only_blocks_when_the_gate_will_run(
        client, uploaded, monkeypatch):
    """The picker always sends its value, so a vendor with no key on file must
    not block a run that never switches the gate on — but must block one that
    does."""
    monkeypatch.setattr("app.settings.get_api_key",
                        lambda p: "" if p == "openai" else "test-key")
    body = {"file_ids": [uploaded], "model": "claude-sonnet-5",
            "meaning_model": "gpt-5.6-sol"}
    off = client.post("/api/jobs", json={**body,
                                         "features": {"meaning_check": False}})
    assert off.status_code == 200
    on = client.post("/api/jobs", json={**body,
                                        "features": {"meaning_check": True}})
    assert on.status_code == 400
    assert "key" in on.json()["detail"].lower()


def test_the_pick_is_recorded_on_the_job(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"meaning_check": True},
        "meaning_model": "claude-opus-5",
        "meaning_prompt": "Meaning only."})
    assert r.status_code == 200
    job_id = r.json()["jobs"][0]["id"]
    saved = client.get(f"/api/jobs/{job_id}").json()
    assert saved["meaning_model"] == "claude-opus-5"
    assert saved["meaning_prompt"] == "Meaning only."


# --- the fix gate's own wiring -----------------------------------------------

def test_both_gates_are_switches_in_the_catalog():
    cfg = Config()
    for fid, path in (("meaning_check", "meaning_check"),
                      ("fix_check", "fix_check")):
        spec = FEATURES_BY_ID[fid]
        assert spec.read(cfg) is False
        spec.write(cfg, True)
        assert getattr(cfg, path).enabled is True
    entries = {f["id"]: f for f in feature_catalog(Config())}
    assert entries["fix_check"]["cost"] == {"kind": "judge",
                                            "model": "claude-fable-5"}
    assert entries["fix_check"]["heavy"] is True


def test_the_fix_gate_pick_and_prompt_reach_the_run_config(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"fix_check": True},
                            fix_model="claude-opus-5",
                            fix_prompt="Only ask whether the fix is right."))
    assert cfg.fix_check.enabled is True
    assert cfg.fix_check.model == "claude-opus-5"
    assert cfg.fix_check.prompt == "Only ask whether the fix is right."
    # ...and the meaning gate is untouched by it.
    assert cfg.meaning_check.enabled is False
    assert cfg.meaning_check.model == "claude-fable-5"


def test_the_gates_are_independently_switchable(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True,
                                             "fix_check": False}))
    assert cfg.meaning_check.enabled is True and cfg.fix_check.enabled is False


def test_a_record_from_before_the_fix_gate_still_runs(runner):
    store, r = runner
    cfg = r.config_for(_job(store))            # no fix fields at all
    assert cfg.fix_check.enabled is False
    assert cfg.fix_check.model == "claude-fable-5"
    assert cfg.fix_check.prompt == ""


def test_the_panel_offers_both_prompt_defaults(client):
    body = client.get("/api/features").json()
    ids = [f["id"] for f in body["features"]]
    assert "meaning_check" in ids and "fix_check" in ids
    assert body["meaning"]["prompt_default"].strip()
    assert body["fix"]["prompt_default"].strip()
    assert body["fix"]["prompt_default"] != body["meaning"]["prompt_default"]


def test_the_models_route_offers_both_defaults(client):
    body = client.get("/api/models?file_ids=").json()
    assert body["default_meaning_model"] == "claude-fable-5"
    assert body["default_fix_model"] == "claude-fable-5"


def test_an_unknown_fix_model_is_refused(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"fix_check": True}, "fix_model": "not-a-model"})
    assert r.status_code == 400
    assert "fix-check" in r.json()["detail"]


def test_the_fix_pick_is_recorded_on_the_job(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"fix_check": True},
        "fix_model": "claude-opus-5", "fix_prompt": "Fixes only."})
    assert r.status_code == 200
    saved = client.get(f"/api/jobs/{r.json()['jobs'][0]['id']}").json()
    assert saved["fix_model"] == "claude-opus-5"
    assert saved["fix_prompt"] == "Fixes only."


def test_continuity_only_strips_both_gates(runner):
    store, r = runner
    cfg = r.config_for(_job(store, continuity_only=True,
                            features={"meaning_check": True,
                                      "fix_check": True}))
    assert cfg.meaning_check.enabled is False
    assert cfg.fix_check.enabled is False


# --- re-judging a finished review --------------------------------------------

def _finished(store, tmp_path, **over):
    """A finished review with the record and the manuscript a re-judge needs."""
    from .conftest import FIXTURES
    out = tmp_path / "results"
    out.mkdir(exist_ok=True)
    (out / "findings.json").write_text('{"source": "x", "findings": []}',
                                       encoding="utf-8")
    fields = {"state": "done", "results_dir": str(out),
              "source_path": str(FIXTURES / "simple.docx")}
    fields.update(over)
    return _job(store, **fields)


def test_a_finished_review_offers_a_rejudge(runner, tmp_path):
    store, r = runner
    assert r.can_rejudge(_finished(store, tmp_path)) is True


def test_a_review_without_its_record_cannot_be_rejudged(runner, tmp_path):
    store, r = runner
    out = tmp_path / "empty"
    out.mkdir()
    assert r.can_rejudge(_finished(store, tmp_path, results_dir=str(out))) is False


def test_a_review_whose_manuscript_is_gone_cannot_be_rejudged(runner, tmp_path):
    store, r = runner
    job = _finished(store, tmp_path, source_path=str(tmp_path / "gone.docx"))
    assert r.can_rejudge(job) is False


def test_prep_and_unfinished_jobs_are_not_rejudgeable(runner, tmp_path):
    store, r = runner
    assert r.can_rejudge(_finished(store, tmp_path, kind="prep")) is False
    assert r.can_rejudge(_finished(store, tmp_path, state="running")) is False


def test_the_rejudge_route_needs_at_least_one_gate(client, uploaded):
    r = client.post("/api/jobs/nope/rejudge",
                    json={"meaning_check": False, "fix_check": False})
    assert r.status_code == 404          # unknown job is checked first


def test_the_rejudge_route_rejects_an_unknown_model(client, uploaded,
                                                    monkeypatch):
    created = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5"}).json()
    job_id = created["jobs"][0]["id"]
    monkeypatch.setattr("app.jobs.JobRunner.can_rejudge", lambda self, j: True)
    r = client.post(f"/api/jobs/{job_id}/rejudge",
                    json={"meaning_check": True, "meaning_model": "not-a-model"})
    assert r.status_code == 400
    assert "Unknown model" in r.json()["detail"]


def test_the_rejudge_route_refuses_an_empty_request(client, uploaded,
                                                    monkeypatch):
    created = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5"}).json()
    job_id = created["jobs"][0]["id"]
    monkeypatch.setattr("app.jobs.JobRunner.can_rejudge", lambda self, j: True)
    r = client.post(f"/api/jobs/{job_id}/rejudge",
                    json={"meaning_check": False, "fix_check": False})
    assert r.status_code == 400
    assert "Switch on" in r.json()["detail"]


def test_the_card_reports_whether_a_rejudge_is_possible(client, uploaded):
    created = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5"}).json()
    card = client.get(f"/api/jobs/{created['jobs'][0]['id']}").json()
    assert "rejudgeable" in card       # the panel reads this to show the button


def test_the_picked_models_reach_the_rejudge(runner, tmp_path, monkeypatch):
    """The point of the picker: each gate runs on the model the user chose for
    it, not on the house default."""
    store, r = runner
    job = _finished(store, tmp_path)
    seen = {}

    def fake_rejudge(cfg, results_dir, **kw):
        seen["meaning"] = (cfg.meaning_check.enabled, cfg.meaning_check.model)
        seen["fix"] = (cfg.fix_check.enabled, cfg.fix_check.model)
        raise RuntimeError("stop — the config is what this test is after")

    monkeypatch.setattr("docproof.rejudge.rejudge", fake_rejudge)
    with pytest.raises(RuntimeError):
        r.rejudge(job.id, gates={"meaning_check": True, "fix_check": True},
                  models={"meaning_check": "claude-opus-5",
                          "fix_check": "claude-haiku-4-5"})
    assert seen["meaning"] == (True, "claude-opus-5")
    assert seen["fix"] == (True, "claude-haiku-4-5")


def test_a_gate_left_off_stays_off_in_the_rejudge(runner, tmp_path, monkeypatch):
    store, r = runner
    job = _finished(store, tmp_path)
    seen = {}

    def fake_rejudge(cfg, results_dir, **kw):
        seen["meaning"] = cfg.meaning_check.enabled
        seen["fix"] = cfg.fix_check.enabled
        raise RuntimeError("stop")

    monkeypatch.setattr("docproof.rejudge.rejudge", fake_rejudge)
    with pytest.raises(RuntimeError):
        r.rejudge(job.id, gates={"meaning_check": False, "fix_check": True},
                  models={"fix_check": "claude-opus-5"})
    assert seen == {"meaning": False, "fix": True}


def test_the_tick_endpoint_carries_the_card_flags(client, uploaded):
    """The Results screen opens by ticking, so /api/tick renders the list just
    as often as /api/jobs. A flag on one and not the other makes a button appear
    and vanish depending on how the screen was reached — which is exactly how
    the Re-judge button shipped unreachable."""
    created = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5"}).json()
    job_id = created["jobs"][0]["id"]

    listed = client.get("/api/jobs").json()["jobs"]
    ticked = client.post("/api/tick").json()["jobs"]
    a = next(j for j in listed if j["id"] == job_id)
    b = next(j for j in ticked if j["id"] == job_id)
    for flag in ("rejudgeable", "recoverable"):
        assert flag in a, f"/api/jobs dropped {flag}"
        assert flag in b, f"/api/tick dropped {flag}"
        assert a[flag] == b[flag]
