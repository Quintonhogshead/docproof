"""A reset clears one workflow and cannot reuse its previous paid-for result."""
import pytest
from fastapi.testclient import TestClient

from app.lock import FolderLock
from app.watch import flags
from app.watch.drive import DriveError, DriveFile
from app.watch.state import FileRecord, WatchState, STATE_FILE
from tests.fakes import drive_entry, fake_drive
from tests.test_watch_agent_api import make_app, seed, _boss, clean_env


@pytest.mark.parametrize("stage", flags.STAGES)
def test_reset_clears_only_selected_workflow_and_keeps_history(tmp_path, stage):
    _, field, prop, terminal = flags.STAGES[stage]
    job_field = "job_id" if stage == "format" else stage + "_job_id"
    hubspot_field = "hubspot_id" if stage == "format" else stage + "_hubspot_id"
    upload_field = "uploaded" if stage == "format" else stage + "_uploaded"
    rec = FileRecord(file_id="source", name="Aragon - Book 1.docx",
                     author_first="José", author_last="Aragón", subfolder_id="author")
    setattr(rec, field, terminal[0])
    setattr(rec, job_field, "old-job")
    setattr(rec, hubspot_field, "old-hubspot")
    setattr(rec, upload_field, {"old.docx": "old-output"})
    other = "proof" if stage == "format" else "format"
    _, other_field, other_prop, other_terminal = flags.STAGES[other]
    setattr(rec, other_field, other_terminal[0])
    state = WatchState(tmp_path / STATE_FILE)
    state.record(rec)
    opener = fake_drive({"source": drive_entry(rec.name, props={
        prop: terminal[0], other_prop: other_terminal[0], "unrelated": "keep"})})

    flags.reset("token", rec, state, stage, who="boss", opener=opener)

    back = WatchState.load(state.path).get("source")
    assert getattr(back, field) == ""
    assert getattr(back, job_field) == ""
    assert getattr(back, hubspot_field) == ""  # ready status must be checked again
    assert getattr(back, upload_field) == {}
    assert getattr(back, other_field) == other_terminal[0]
    assert back.author_last == "Aragón" and back.subfolder_id == "author"
    assert back.flag_reset_history[0]["previous"][job_field] == "old-job"
    assert back.flag_reset_history[0]["by"] == "boss"
    props = opener.files["source"]["appProperties"]
    assert prop not in props and props[other_prop] == other_terminal[0]
    assert props["unrelated"] == "keep"
    assert props[f"docproof.reset.{stage}"] == back.flag_resets[stage]


def test_drive_failure_preserves_the_local_flag_and_job(tmp_path):
    rec = FileRecord(file_id="source", proof_marked="done", proof_job_id="paid")
    state = WatchState(tmp_path / STATE_FILE)
    state.record(rec)
    before = state.path.read_bytes()
    def refuse(*args, **kwargs):
        raise DriveError("offline")
    with pytest.raises(DriveError):
        flags.reset("token", rec, state, "proof", who="boss", opener=refuse)
    assert state.path.read_bytes() == before and rec.proof_marked == "done"


def test_reset_excludes_old_and_undated_outputs_but_keeps_new_delivery():
    rec = FileRecord(file_id="source", flag_resets={"proof": "2026-09-10T12:00:00+00:00"})
    source = DriveFile("source", "Book 1.docx", "")
    old = DriveFile("old", "Book 2.docx", "", modified_time="2026-09-10T11:00:00Z")
    new = DriveFile("new", "Book 2.docx", "", modified_time="2026-09-10T12:00:01Z")
    undated = DriveFile("unknown", "Book 2.docx", "")
    assert flags.current_outputs([old, new, undated], rec, "proof", source) == [new]
    assert flags.current_outputs([old], rec, "format", source) == [old]


@pytest.mark.parametrize("stage,module_name", [("format", "prep"), ("proof", "proof"),
    ("promo", "promo"), ("plan", "plan"), ("corrections", "corrections")])
def test_new_run_uploads_new_results_instead_of_adopting_old_files(
        tmp_path, monkeypatch, stage, module_name):
    from importlib import import_module
    from types import SimpleNamespace
    from app.watch.settings import WatchSettings
    module = import_module("app.watch." + module_name)
    result = tmp_path / "result.docx"
    result.write_bytes(b"new result")
    artifact = SimpleNamespace(name="result.docx", path=result,
                               mime="application/octet-stream")
    monkeypatch.setattr(module, "artifacts", lambda *args: [artifact])
    rec = FileRecord(file_id="source", flag_resets={stage: "2026-09-10T12:00:00Z"})
    state = WatchState(tmp_path / STATE_FILE)
    state.record(rec)
    source = DriveFile("source", "Book 1.docx", "")
    old = DriveFile("old", "result.docx", "", modified_time="2026-09-01T00:00:00Z",
                    app_properties={"docproof.src": "source"})
    opener = fake_drive({})
    placed = module.upload_outputs("token", source, SimpleNamespace(id="new-job"),
        WatchSettings(folder_id="folder"), rec, state, [old],
        dest_folder_id="folder", opener=opener)
    assert placed == ["result.docx"]
    upload_field = "uploaded" if stage == "format" else stage + "_uploaded"
    uploaded_id = getattr(rec, upload_field)["result.docx"]
    assert uploaded_id != "old"
    assert opener.files[uploaded_id]["appProperties"]["docproof.job"] == "new-job"


def setup_api(tmp_path, monkeypatch, *, marked="done"):
    app = make_app(tmp_path)
    state = seed(app.state.watch.home, [FileRecord(
        file_id="source", name="Aragon - Book 1.docx", proof_marked=marked,
        proof_job_id="paid", author_first="José", author_last="Aragón")])
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda home: "token")
    opener = fake_drive({"source": drive_entry("Aragon - Book 1.docx",
                                              props={"docproof.proof": marked})})
    monkeypatch.setattr("app.watch.drive._open_url", opener)
    body = {"file_id": "source", "stage": "proof",
            "updated_at": state.get("source").updated_at}
    return app, state, body, opener


def test_admin_can_find_and_reset_a_read_book(tmp_path, monkeypatch):
    app, state, body, opener = setup_api(tmp_path, monkeypatch)
    boss = _boss(app)
    row = boss.get("/api/watch").json()["watch"]["files"][0]
    assert row["author"] == "José Aragón"
    assert row["flags"] == [{"stage": "proof", "label": "Proofreading",
                             "value": "done", "completed": True}]
    response = boss.post("/api/watch/flags/reset", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["watch"]["files"][0]["flags"] == []
    assert boss.post("/api/watch/flags/reset", json=body).status_code == 409
    back = WatchState.load(state.path).get("source")
    assert back.flag_reset_history[0]["by"] == "boss@press.com"


@pytest.mark.parametrize("block,code", [("busy", 409), ("lock", 409),
                                      ("stale", 409), ("auth", 400),
                                      ("anonymous", 401), ("active", 409)])
def test_reset_refusals_do_not_change_the_flag(tmp_path, monkeypatch, block, code):
    app, state, body, opener = setup_api(tmp_path, monkeypatch,
                                       marked="awaiting" if block == "active" else "done")
    before = state.path.read_bytes()
    lock = FolderLock(app.state.watch.home)
    if block == "busy":
        app.state.watch._running = True
    elif block == "lock":
        lock.acquire()
    elif block == "stale":
        body["updated_at"] = "an old page"
    elif block == "auth":
        monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda home: None)
    client = TestClient(app) if block == "anonymous" else _boss(app)
    try:
        assert client.post("/api/watch/flags/reset", json=body).status_code == code
        assert state.path.read_bytes() == before
        assert not opener.calls
    finally:
        lock.release()


def test_observed_drive_marker_is_available_for_history_after_state_restore(tmp_path):
    state = WatchState(tmp_path / STATE_FILE)
    file = DriveFile("source", "Aragon - Book 1.docx", "",
                     app_properties={"docproof.proof": "done"})
    flags.remember(file, state, author_first="José", author_last="Aragón")
    rec = state.get("source")
    assert flags.for_record(rec)[0]["completed"] is True
    assert rec.author_last == "Aragón"


def test_reset_that_reached_drive_recovers_after_a_failed_local_save(tmp_path):
    state = WatchState(tmp_path / STATE_FILE)
    state.record(FileRecord(file_id="source", proof_marked="done",
                            proof_job_id="old-job", proof_hubspot_id="old-id"))
    at = "2026-09-10T12:00:00+00:00"
    file = DriveFile("source", "Aragon - Book 1.docx", "",
                     app_properties={"docproof.reset.proof": at})
    flags.remember(file, state)
    back = WatchState.load(state.path).get("source")
    assert back.proof_marked == back.proof_job_id == back.proof_hubspot_id == ""
    assert back.flag_resets["proof"] == at
    assert back.flag_reset_history[0]["previous"]["proof_job_id"] == "old-job"


def test_reset_proof_waits_for_a_new_verdict_instead_of_reapplying_the_old_one(tmp_path):
    from app.watch.status import awaiting
    from tests.test_watch_proof import (BOOK, OUTCOME, MANUSCRIPT, proof_ws,
        ready_to_proof, folder, run, _hand_off, hs_props)

    ws = proof_ws(proof_runner="external")
    ws.save(tmp_path)
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})
    run(tmp_path, ws, opener)
    _hand_off(opener, {"outcome": "done", "reason": "previous read"})
    run(tmp_path, ws, opener)
    state = WatchState.load(tmp_path / STATE_FILE)
    rec = state.get("f-1")
    assert rec.proof_marked == "done"
    flags.reset("token", rec, state, "proof", who="boss", opener=opener)

    # Resetting does not silently move the CRM back to ready.
    run(tmp_path, ws, opener)
    assert awaiting(tmp_path) == []
    hs_props(opener)["docproof"] = "Ready for Proofing"
    again = run(tmp_path, ws, opener)
    assert again.proofed == []
    assert awaiting(tmp_path)[0]["request_id"] == rec.flag_resets["proof"]
    assert hs_props(opener)["docproof"] == "Ready for Proofing"

    # A newly delivered result, even with the same filename, completes it.
    _hand_off(opener, {"outcome": "done", "reason": "new read"})
    for entry in opener.files.values():
        if entry["name"] == OUTCOME:
            entry["modifiedTime"] = "2099-01-01T00:00:00Z"
    assert run(tmp_path, ws, opener).proofed == [BOOK]
    assert WatchState.load(tmp_path / STATE_FILE).get("f-1").proof_outcome_reason == "new read"
