"""The judge gates in the app: each panel switch, model picker and editable
instruction field, from the submitted job down to the run config.

The switch itself is an ordinary feature toggle; what needs holding is that the
picker overrides the house default only when a pick was made, that an untouched
panel changes nothing, and that a job record written before this feature existed
still loads and runs. See app/features.py, app/jobs.py config_for, and
docproof/judges.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
    assert entry["cost"] == {"kind": "judge", "model": "gpt-5.6-luna"}


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
    assert cfg.meaning_check.model == "gpt-5.6-luna"


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
    assert cfg.meaning_check.model == "gpt-5.6-luna"


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
    assert entries["fix_check"]["cost"] == {"kind": "judge", "model": "gpt-5.6-luna"}
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
    assert cfg.meaning_check.model == "gpt-5.6-luna"


def test_the_gates_are_independently_switchable(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"meaning_check": True,
                                             "fix_check": False}))
    assert cfg.meaning_check.enabled is True and cfg.fix_check.enabled is False


def test_a_record_from_before_the_fix_gate_still_runs(runner):
    store, r = runner
    cfg = r.config_for(_job(store))            # no fix fields at all
    assert cfg.fix_check.enabled is False
    assert cfg.fix_check.model == "gpt-5.6-luna"
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
    assert body["default_meaning_model"] == "gpt-5.6-luna"
    assert body["default_fix_model"] == "gpt-5.6-luna"


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
                                      "fix_check": True,
                                      "examination_judgment": True}))
    assert cfg.meaning_check.enabled is False
    assert cfg.fix_check.enabled is False
    assert cfg.examination_graph.judgment.enabled is False


# --- the whole-book continuity model picker ----------------------------------

def test_the_continuity_pick_reaches_the_run_config(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"continuity": True},
                           continuity_model="claude-opus-5"))
    assert cfg.continuity.model == "claude-opus-5"


def test_an_untouched_continuity_picker_keeps_the_house_default(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"continuity": True}))
    assert cfg.continuity.model == "gpt-5.6-luna"


def test_a_record_from_before_the_continuity_picker_still_runs(runner):
    store, r = runner
    cfg = r.config_for(_job(store))                  # no continuity fields at all
    assert cfg.continuity.model == "gpt-5.6-luna"


def test_the_models_route_offers_the_continuity_default(client):
    body = client.get("/api/models?file_ids=").json()
    assert body["default_continuity_model"] == "gpt-5.6-luna"


def test_an_unknown_continuity_model_is_refused(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"continuity": True}, "continuity_model": "not-a-model"})
    assert r.status_code == 400
    assert "continuity" in r.json()["detail"].lower()


def test_a_keyless_continuity_model_only_blocks_when_the_read_will_run(
        client, uploaded, monkeypatch):
    """The picker always sends its value, so a keyless vendor must not block a run
    that never switches continuity on — but must block one that does, and one
    that runs it as continuity-only."""
    monkeypatch.setattr("app.settings.get_api_key",
                        lambda p: "" if p == "openai" else "test-key")
    body = {"file_ids": [uploaded], "model": "claude-sonnet-5",
            "continuity_model": "gpt-5.6-sol"}
    off = client.post("/api/jobs", json={**body,
                                         "features": {"continuity": False}})
    assert off.status_code == 200
    on = client.post("/api/jobs", json={**body,
                                        "features": {"continuity": True}})
    assert on.status_code == 400
    assert "key" in on.json()["detail"].lower()
    only = client.post("/api/jobs", json={**body, "continuity_only": True,
                                          "features": {"continuity": False}})
    assert only.status_code == 400


def test_the_continuity_pick_is_recorded_on_the_job(client, uploaded):
    r = client.post("/api/jobs", json={
        "file_ids": [uploaded], "model": "claude-sonnet-5",
        "features": {"continuity": True},
        "continuity_model": "claude-opus-5"})
    assert r.status_code == 200
    saved = client.get(f"/api/jobs/{r.json()['jobs'][0]['id']}").json()
    assert saved["continuity_model"] == "claude-opus-5"


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


def _started_rejudge(store, r, job, tmp_path, monkeypatch) -> Job:
    """Kick off a re-judge and hand back the record it created, caught mid-run:
    the gates are stubbed to raise, so the new job is exactly as the card reads
    it while the judges work."""
    r.settings.output_dir = str(tmp_path / "out")

    def fake_rejudge(cfg, results_dir, **kw):
        raise RuntimeError("stop — the new record is what this test is after")

    monkeypatch.setattr("docproof.rejudge.rejudge", fake_rejudge)
    with pytest.raises(RuntimeError):
        r.rejudge(job.id, gates={"meaning_check": True, "fix_check": False})
    fresh = [j for j in store.all() if j.id != job.id]
    assert len(fresh) == 1, "a re-judge writes exactly one new record"
    return fresh[0]


def test_a_rejudge_starts_its_own_clock(runner, tmp_path, monkeypatch):
    """The new record is a copy of the finished review, and `save` — unlike
    `update` — does not stamp the stage clock. Left inherited, the card read the
    original's section count against a clock last set when that review finished,
    so a re-judge started now announced itself as "Reviewing (412 of 412
    sections) · 33h 0m"."""
    store, r = runner
    job = _finished(store, tmp_path, stage="", done=412, total=412,
                    review_round=3, total_rounds=3,
                    stage_since="2026-08-13T00:00:00+00:00")
    fresh = _started_rejudge(store, r, job, tmp_path, monkeypatch)

    began = datetime.fromisoformat(fresh.stage_since)
    assert (datetime.now(timezone.utc) - began).total_seconds() < 60
    assert (fresh.done, fresh.total) == (0, 0)
    assert (fresh.review_round, fresh.total_rounds) == (0, 0)
    # And it says what it is doing, rather than borrowing the detector loop's
    # line and its section count.
    assert fresh.stage == "judging"
    assert fresh.plain_state() == "Putting the corrections to the judges"


def test_a_rejudge_does_not_inherit_the_original_run(runner, tmp_path,
                                                     monkeypatch):
    """Worse than cosmetic. `_archive_job` takes the folder off the record and
    skips every file already in `drive_files`, so an inherited pair marks the
    re-judged deliverable archived — at the original's folder, without uploading
    a byte of it. The token counts would likewise bill the original's review a
    second time until the gates' own usage overwrote them."""
    store, r = runner
    job = _finished(store, tmp_path, archive="done", archive_attempts=2,
                    drive_folder_id="folder-1",
                    drive_files={"simple - reviewed.docx": "file-1"},
                    cost=12.5, input_tokens=900, api_calls=40, mode="batch",
                    schedule_at="23:00")
    fresh = _started_rejudge(store, r, job, tmp_path, monkeypatch)

    assert (fresh.drive_folder_id, fresh.drive_files) == ("", {})
    assert (fresh.archive, fresh.archive_attempts) == ("", 0)
    assert (fresh.cost, fresh.input_tokens, fresh.api_calls) == (None, 0, 0)
    # It runs inline, now — not overnight at the hour the original was booked.
    assert (fresh.mode, fresh.schedule_at) == ("now", None)


def test_an_interrupted_rejudge_is_not_re_run_as_a_review(runner, tmp_path):
    """A re-judge runs inline in the request thread with no checkpoint and no
    queue entry. Handing its id to the worker on restart would read an ordinary
    review record and run the whole detector pipeline — a full paid re-review
    nobody asked for."""
    store, r = runner
    _job(store, id="rj", state="running", stage="judging", mode="now")
    r.resume_interrupted()

    assert r.queue.empty()
    after = store.get("rj")
    assert after.state == "failed"
    assert "again" in after.error


def test_an_interrupted_review_is_still_re_queued(runner, tmp_path):
    """The other side of that guard: an ordinary sync review interrupted by a
    restart still resumes off its checkpoint."""
    store, r = runner
    _job(store, id="rv", state="running", stage="reviewing", mode="now")
    r.resume_interrupted()

    assert r.queue.get_nowait() == "rv"
    assert store.get("rv").state == "queued"


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
