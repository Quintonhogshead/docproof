"""The app backend, driven through HTTP with a fake provider underneath.
No test here reaches a vendor, and none sleeps on a timer — the ticker's body
is called directly."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobRunner, JobStore  # noqa: F401 - JobRunner patched
from app.main import create_app
from app.settings import CONFIG_PATH, Paths, Settings
from docproof import __version__ as docproof_version
from docproof.config import load_config
from .conftest import FIXTURES
from .fakes import ScriptedBatchProvider, finding_result

SPLICE = "The manuscript was finished, nobody wanted to read it."

# Read from the config the app itself loads, so enabling an error type is a
# one-file change rather than a test edit. What these tests care about is that
# the app prices and renders EVERY shipped pass, not how many there are today.
_SHIPPED = load_config(CONFIG_PATH)
PASSES = len(_SHIPPED.error_type_groups)
TYPES = len(_SHIPPED.error_type_keys)


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
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"},
        "openai": {"configured": True, "source": "environment"},
        "gemini": {"configured": True, "source": "environment"}})

    # Run a single error-type pass so a scripted finding is unambiguous about
    # which pass it belongs to. Uploads still preflight against the real config.
    base = JobRunner.config_for

    def one_pass(self, job):
        cfg = base(self, job)
        cfg.error_types = ["comma_splice"]
        # The whole-book glossary pass is its own model call; these end-to-end
        # tests are about the review/job machinery, not that pass.
        cfg.glossary.enabled = False
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


# --- multi-round review (stage F) --------------------------------------------

def test_features_endpoint_exposes_the_rounds_default(client):
    body = client.get("/api/features").json()
    assert body["rounds"]["default"] == 1
    assert body["rounds"]["max"] == 4
    assert body["rounds"]["judge_prompt_default"]      # the built-in prompt, for prefill


def test_rounds_out_of_range_is_refused(client):
    fid = _upload(client)["id"]
    resp = client.post("/api/jobs", json={
        "file_ids": [fid], "model": "claude-sonnet-5", "mode": "now", "rounds": 9})
    assert resp.status_code == 400
    assert "rounds" in resp.text


def test_features_endpoint_lists_the_category_knobs(client):
    cats = client.get("/api/features").json()["categories"]
    assert cats, "expected at least one tunable category"
    one = cats[0]
    assert set(one) >= {"id", "keys", "names", "passes",
                        "token_budget", "default_token_budget"}
    assert one["passes"] == 1 and one["token_budget"] is None   # shipped baseline
    assert one["default_token_budget"] > 0                       # the global fallback
    assert len(one["names"]) == len(one["keys"])                 # a label per key


def test_category_knobs_reach_the_job(client):
    fid = _upload(client)["id"]
    cid = client.get("/api/features").json()["categories"][0]["id"]
    knob = {cid: {"passes": 2, "token_budget": 1800}}
    job = _run(client, fid, category_knobs=knob)
    assert job["category_knobs"] == knob


def test_category_knobs_reject_an_unknown_category(client):
    fid = _upload(client)["id"]
    resp = client.post("/api/jobs", json={
        "file_ids": [fid], "model": "claude-sonnet-5", "mode": "now",
        "category_knobs": {"no_such_category": {"passes": 2}}})
    assert resp.status_code == 400 and "category" in resp.text.lower()


def test_category_knobs_reject_a_sub_one_value(client):
    fid = _upload(client)["id"]
    cid = client.get("/api/features").json()["categories"][0]["id"]
    resp = client.post("/api/jobs", json={
        "file_ids": [fid], "model": "claude-sonnet-5", "mode": "now",
        "category_knobs": {cid: {"passes": 0}}})
    assert resp.status_code == 400 and "passes" in resp.text


def test_a_review_runs_multiple_rounds(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    fid = _upload(client)["id"]
    job = _run(client, fid, rounds=2, judge_prompt="Keep only sure fixes.")
    assert job["rounds"] == 2 and job["judge_prompt"] == "Keep only sure fixes."
    client.app_state.runner.wait_idle()
    final = client.get(f"/api/jobs/{job['id']}").json()
    assert final["state"] == "done", final.get("error")     # ran the multi-round path
    assert final["applied"] >= 1                            # round 1's fix survived


# --- upload -------------------------------------------------------------------

def test_upload_preflights_at_drop_time(client):
    staged = _upload(client)
    assert staged["ok"] is True
    assert staged["sections"] >= 1 and staged["paragraphs"] == 3
    assert staged["requests"] == staged["sections"] * PASSES


def test_upload_rejects_an_unreadable_kind_of_file(client):
    resp = client.post("/api/files",
                       files={"files": ("notes.pdf", b"hello")})
    entry = resp.json()["files"][0]
    assert entry["ok"] is False
    # The error names every format docproof reads, not just the first one.
    assert ".docx" in entry["error"] and ".idml" in entry["error"]


def test_upload_of_a_manuscript_format_asks_for_the_converter(client):
    """.txt, .doc, .rtf and .odt are prep inputs rather than DocProof formats:
    they are converted at drop time, so the failure to name is the missing
    converter, not the file."""
    import shutil

    if shutil.which("soffice") or Path(
            "/Applications/LibreOffice.app/Contents/MacOS/soffice").exists():
        pytest.skip("LibreOffice is installed here, so the conversion "
                    "succeeds instead of asking for it")
    resp = client.post("/api/files",
                       files={"files": ("notes.txt", b"hello")})
    entry = resp.json()["files"][0]
    assert entry["ok"] is False
    assert "LibreOffice" in entry["error"]


def test_upload_accepts_an_indesign_layout(client):
    staged = _upload(client, "layout.idml")
    assert staged["ok"] is True
    assert staged["format"]["name"] == "InDesign"
    assert staged["format"]["app"] == "InDesign"
    assert staged["paragraphs"] >= 1


def test_staged_file_says_which_application_it_came_from(client):
    assert _upload(client)["format"]["name"] == "Word"


# --- compare (tracked-changes diff) -------------------------------------------

def test_compare_two_tracked_change_docs(client):
    # tracked.docx carries one tracked insertion; comparing it with itself is a
    # perfect match. It also proves /api/compare accepts tracked-changes docs,
    # which /api/files deliberately rejects.
    with (FIXTURES / "tracked.docx").open("rb") as a, \
         (FIXTURES / "tracked.docx").open("rb") as b:
        resp = client.post("/api/compare",
                           files={"doc_a": ("tracked.docx", a),
                                  "doc_b": ("tracked.docx", b)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["totals"]["agree"] >= 1
    assert data["totals"]["only_a"] == 0 and data["totals"]["only_b"] == 0
    assert data["score_a_as_truth"]["located_recall"] == 1.0


def test_compare_rejects_a_non_docx(client):
    with (FIXTURES / "tracked.docx").open("rb") as a:
        resp = client.post("/api/compare",
                           files={"doc_a": ("tracked.docx", a),
                                  "doc_b": ("notes.txt", b"not a zip")})
    assert resp.status_code == 400
    assert ".docx" in resp.json()["detail"]


def test_the_app_can_say_which_build_it_is(client):
    body = client.get("/api/version").json()
    assert body["version"] == docproof_version
    assert set(body) >= {"version", "built", "commit", "source", "frozen"}


def test_checking_for_a_newer_build_answers_in_a_sentence(client, monkeypatch):
    """However it goes — up to date, behind, or the source folder gone — the
    button has something to print. Nothing here reaches a network."""
    monkeypatch.setattr("app.version.check_for_update",
                        lambda: {"ok": True, "current": False,
                                 "message": "There are 3 changes …"})
    body = client.get("/api/version/check").json()
    assert body["current"] is False and body["message"].startswith("There are")


def test_a_new_version_is_downloaded_and_shown_never_installed(client,
                                                               monkeypatch,
                                                               tmp_path):
    """DocProof is unsigned. Replacing itself with code pulled off the network
    is the exact thing macOS is right to refuse, so the disk image lands in
    Downloads and the person installs it."""
    dmg = tmp_path / "DocProof-0.2.0.dmg"
    dmg.write_bytes(b"disk image")
    revealed = []
    monkeypatch.setattr("app.version.download_release", lambda: {
        "ok": True, "path": str(dmg), "filename": dmg.name,
        "message": f"{dmg.name} is in your Downloads folder."})
    monkeypatch.setattr("app.routes.common.open_path",
                        lambda path, *, reveal=False: revealed.append((path, reveal)))
    monkeypatch.setattr("sys.platform", "darwin")

    body = client.post("/api/version/download").json()
    assert body["ok"] and body["filename"] == dmg.name
    assert revealed == [(dmg, True)]        # shown in the Finder, not opened


def test_a_download_that_could_not_happen_says_why(client, monkeypatch):
    monkeypatch.setattr("app.version.download_release", lambda: {
        "ok": False, "message": "GitHub would not accept that token."})
    resp = client.post("/api/version/download")
    assert resp.status_code == 400
    assert "would not accept that token" in resp.json()["detail"]


def test_update_route_installs_and_reports(client, monkeypatch):
    monkeypatch.setattr("app.update.perform_update", lambda runner: {
        "ok": True, "message": "DocProof 0.2.0 is installed — reopening now."})
    body = client.post("/api/version/update").json()
    assert body["ok"] and "reopening" in body["message"]


def test_update_route_passes_refusals_through_verbatim(client, monkeypatch):
    from app.update import UpdateError

    def refuse(runner):
        raise UpdateError("A document is being worked on right now.")

    monkeypatch.setattr("app.update.perform_update", refuse)
    resp = client.post("/api/version/update")
    assert resp.status_code == 400
    assert "being worked on" in resp.json()["detail"]


def test_rebuild_route_starts_and_reports_its_progress(client, monkeypatch):
    """A minute of work, so the route answers at once and the page asks how it
    is going."""
    monkeypatch.setattr("app.update.refuse_reason", lambda runner: None)
    monkeypatch.setattr("app.update.rebuild_from_checkout",
                        lambda runner, progress=None, **kw: "0.3.0")

    started = client.post("/api/version/rebuild").json()
    assert started["state"] == "pulling"

    for _ in range(500):
        body = client.get("/api/version/rebuild").json()
        if body["state"] in ("done", "failed"):
            break
        time.sleep(0.01)
    assert body["state"] == "done" and "0.3.0" in body["message"]


def test_rebuild_route_refuses_before_it_starts_anything(client, monkeypatch):
    """Somebody who clicks this mid-review is told now, not a second later in
    a progress line."""
    monkeypatch.setattr("app.update.refuse_reason",
                        lambda runner: "A document is being worked on right now.")
    started = []
    monkeypatch.setattr("app.update.rebuild_from_checkout",
                        lambda *a, **kw: started.append(1))

    resp = client.post("/api/version/rebuild")

    assert resp.status_code == 400
    assert "being worked on" in resp.json()["detail"]
    assert started == []


def test_a_rebuild_that_fails_is_reported_through_the_progress_route(
        client, monkeypatch):
    from app.update import UpdateError

    monkeypatch.setattr("app.update.refuse_reason", lambda runner: None)

    def explode(runner, progress=None, **kw):
        raise UpdateError("The tests do not pass, so nothing was installed.")

    monkeypatch.setattr("app.update.rebuild_from_checkout", explode)
    client.post("/api/version/rebuild")

    for _ in range(500):
        body = client.get("/api/version/rebuild").json()
        if body["state"] in ("done", "failed"):
            break
        time.sleep(0.01)
    assert body["state"] == "failed" and "tests do not pass" in body["message"]


def test_the_github_token_is_stored_like_every_other_secret(client,
                                                            monkeypatch):
    """Not an AI key and nothing bills for it, but it is still a secret: the
    Keychain, never a file, never returned to the page."""
    saved = {}
    monkeypatch.setattr("app.settings.set_api_key",
                        lambda provider, key: saved.update({provider: key}))
    body = client.put("/api/settings",
                      json={"github_token": "github_pat_abc"}).json()
    assert saved == {"github": "github_pat_abc"}
    assert "github_pat_abc" not in json.dumps(body)


def test_formats_endpoint_lists_what_can_be_read(client):
    body = client.get("/api/formats").json()
    assert body["suffixes"] == [".docx", ".idml"]
    assert {f["name"] for f in body["formats"]} == {"Word", "InDesign"}
    # The picker offers these alongside Word, so the page must not be keeping
    # its own copy of the list — this one grew by three without it noticing.
    assert ".rtf" in body["prep_extra_suffixes"]
    assert ".docx" not in body["prep_extra_suffixes"]


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


def test_a_finished_job_is_pushed_to_the_drive_archive(client, provider,
                                                       monkeypatch, tmp_path):
    """The whole glue, end to end: a job finishing through the runner archives
    itself to Drive when the archive is on, and the card links to the folder."""
    from app.watch.settings import WatchSettings
    from .fakes import fake_drive
    # The runner's notify_home is <root>/watch; put archive settings there and a
    # sign-in, and route every Drive call the archive makes to the fake.
    WatchSettings(archive_enabled=True, archive_folder_id="root-archive",
                  client_id="cid", client_secret="sec").save(tmp_path / "watch")
    opener = fake_drive()
    monkeypatch.setattr("app.watch.drive._open_url", opener)

    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    job = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()

    final = client.get(f"/api/jobs/{job['id']}").json()
    assert final["state"] == "done", final.get("error")
    assert final["archive"] == "done"
    assert final["drive_link"].endswith(final["drive_folder_id"])
    # The reviewed document and the manifest are both in the fake's folder.
    names = {e.get("name", "") for e in opener.files.values()}
    assert "docproof.json" in names
    assert any(n.startswith("Reviews") for n in names)


def test_a_wiped_result_is_served_back_from_the_archive(client, provider,
                                                        monkeypatch, tmp_path):
    """Phase 2: a redeploy wipes the local results folder, but the download still
    works — the file is fetched straight back from Drive and re-cached."""
    import shutil
    from app.watch.settings import WatchSettings
    from .fakes import fake_drive
    WatchSettings(archive_enabled=True, archive_folder_id="root-archive",
                  client_id="cid", client_secret="sec").save(tmp_path / "watch")
    monkeypatch.setattr("app.watch.drive._open_url", fake_drive())

    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    job = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["archive"] == "done"

    # The redeploy: the whole results folder vanishes.
    results_dir = client.app_state.store.get(job["id"]).results_dir
    shutil.rmtree(results_dir)
    assert not Path(results_dir).exists()

    # The download still hands over the real document, and re-caches it.
    docx = client.get(f"/api/jobs/{job['id']}/file/docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    assert Path(results_dir).is_dir()          # fetched back onto the volume


def test_download_anyway_writes_the_file_after_an_audit_failure(
        client, provider, monkeypatch):
    from docproof.audit import AuditError
    import app.jobs as jobs_module

    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]

    # Make the first finish() raise the reject-all audit — as if the reassembler
    # had left an untracked change — while the review itself succeeds and
    # checkpoints. download_anyway calls the real finish (2nd call) with the
    # audit downgraded.
    real_finish = jobs_module.finish
    calls = {"n": 0}

    def flaky_finish(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuditError("body-0000 at character 5: reject-all mismatch")
        return real_finish(*a, **k)

    monkeypatch.setattr("app.jobs.finish", flaky_finish)

    staged = _upload(client)
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()

    failed = client.get(f"/api/jobs/{job['id']}").json()
    assert failed["state"] == "failed"
    assert failed["audit_failed"] is True
    assert "reject-all mismatch" in failed["error"]

    # The escape hatch: no new model calls (the scripted provider has no results
    # left), the file is written, and the override is recorded.
    resp = client.post(f"/api/jobs/{job['id']}/download-anyway")
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["state"] == "done" and done["audit_overridden"] is True

    docx = client.get(f"/api/jobs/{job['id']}/file/docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"


def test_download_anyway_does_not_rebill_the_paid_passes(
        client, provider, monkeypatch):
    """A job that ran with the paid finish()-time passes on — Sapling, the
    low-confidence valve, an ensemble with an overseer-verifier — must not pay
    any of them again on download-anyway. The rebuild replays the checkpoint
    and disarms every pass that spends inside finish(); this drives the whole
    path and treats any provider call or Sapling contact as the failure."""
    from docproof.audit import AuditError
    from docproof.config import DetectorSpec
    import app.jobs as jobs_module

    # The job's config has every paid pass on — applied through config_for so
    # the run and the rebuild agree, which the checkpoint fingerprint requires.
    inner = JobRunner.config_for

    def paid(self, job):
        cfg = inner(self, job)
        cfg.sapling.enabled = True
        cfg.low_confidence.confirm = True
        cfg.ensemble.detectors = [DetectorSpec(model="claude-sonnet-5")]
        cfg.ensemble.verifier_model = "claude-fable-5"
        return cfg

    monkeypatch.setattr(JobRunner, "config_for", paid)
    # The ensemble path ignores the injected provider and builds one client per
    # detector itself; route that through the fake for the original run.
    monkeypatch.setattr("docproof.providers.build_provider",
                        lambda cfg, api_key=None: provider)

    # A below-gate finding, so the valve would have real work to re-bill.
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1), confidence="low")]

    real_finish = jobs_module.finish
    calls = {"n": 0}

    def flaky_finish(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuditError("body-0000 at character 5: reject-all mismatch")
        return real_finish(*a, **k)

    monkeypatch.setattr("app.jobs.finish", flaky_finish)

    job = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "failed"

    # From here on, reaching Sapling or building any real provider IS the bug.
    monkeypatch.setenv("SAPLING_API_KEY", "key-on-file")

    def no_sapling(*a, **k):
        raise AssertionError("download-anyway sent the book to Sapling")

    def no_provider(*a, **k):
        raise AssertionError("download-anyway built a provider inside finish()")

    monkeypatch.setattr("docproof.sapling.check_paragraphs", no_sapling)
    monkeypatch.setattr("docproof.providers.build_provider", no_provider)
    paid_calls = len(provider.calls)

    resp = client.post(f"/api/jobs/{job['id']}/download-anyway")
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["state"] == "done" and done["audit_overridden"] is True
    assert len(provider.calls) == paid_calls       # not one more model call
    assert client.app_state.store.get(job["id"]).sapling_cost == 0.0

    docx = client.get(f"/api/jobs/{job['id']}/file/docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"


def test_download_anyway_refused_on_a_healthy_job(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    staged = _upload(client)
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"
    # A review that passed the audit has nothing to override.
    resp = client.post(f"/api/jobs/{job['id']}/download-anyway")
    assert resp.status_code == 409


def test_job_is_refused_without_a_key(client, monkeypatch):
    staged = _upload(client)
    monkeypatch.setattr("app.settings.get_api_key", lambda p: None)
    resp = client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                          "model": "claude-sonnet-5",
                                          "mode": "now"})
    assert resp.status_code == 400
    assert "Settings" in resp.json()["detail"]


def test_features_endpoint_lists_switches_at_their_current_defaults(client):
    body = client.get("/api/features").json()
    by_id = {f["id"]: f for f in body["features"]}
    # Off-by-default recall levers read off, default-on passes read on.
    assert by_id["storysheet"]["default"] is False
    assert by_id["rewrite"]["default"] is False
    assert by_id["adjudicate"]["default"] is True
    # ensemble is not a switch, so it must not appear.
    assert "ensemble" not in by_id
    # Heavy passes carry a pricing hint so the estimate can move with them; the
    # rest carry none.
    assert by_id["storysheet"]["cost"]["kind"] == "read"
    assert by_id["rewrite"]["cost"]["kind"] == "retype"
    assert by_id["adjudicate"]["cost"] is None


def test_a_job_with_an_unknown_feature_is_refused(client):
    staged = _upload(client)
    resp = client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                          "model": "claude-sonnet-5",
                                          "mode": "now",
                                          "features": {"bogus": True}})
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_a_features_map_is_stored_on_the_job(client):
    staged = _upload(client)
    job = _run(client, staged["id"], features={"storysheet": True})
    assert job["features"] == {"storysheet": True}


def test_a_job_with_an_unknown_variant_is_refused(client):
    """Refused at submit, not left to raise mid-run — on the batch path that
    would surface days after the operator walked away."""
    staged = _upload(client)
    resp = client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                          "model": "claude-sonnet-5",
                                          "mode": "now",
                                          "variant": "british"})
    assert resp.status_code == 400
    assert "variant" in resp.json()["detail"]


def test_a_picked_variant_is_stored_on_the_job(client):
    job = _run(client, _upload(client)["id"], variant="uk")
    assert job["variant"] == "uk"


def test_a_page_that_sends_no_variant_leaves_it_to_the_config(client):
    """The watcher never sends one, and older pages don't either."""
    job = _run(client, _upload(client)["id"])
    assert job["variant"] == ""


def test_static_assets_are_served_no_cache(client):
    """The page and its script/style carry no content hash, so they ship
    no-cache — a deploy is picked up on the next load, not left stale behind a
    version number that already reads new."""
    for path in ("/", "/app.js", "/styles.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-cache", path


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


# --- opening what came out ----------------------------------------------------

@pytest.fixture
def opened(monkeypatch):
    """Every file the app hands to the desktop, without handing it anywhere."""
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr("app.routes.common.open_path",
                        lambda path, *, reveal=False: calls.append((path, reveal)))
    monkeypatch.setattr("sys.platform", "darwin")
    return calls


def _finished_review(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    job = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"
    return job


def test_open_hands_the_reviewed_document_to_its_application(
        client, provider, opened):
    """The window this app lives in cannot display a .docx and will not
    download one, so the button has to open the file on the Mac itself."""
    job = _finished_review(client, provider)

    resp = client.post(f"/api/jobs/{job['id']}/open/document")
    assert resp.status_code == 200, resp.text
    assert resp.json()["opened"] == "simple - Atmosphere Press Proofreader.docx"

    path, reveal = opened[0]
    assert path.name == "simple - Atmosphere Press Proofreader.docx" and path.is_file()
    assert reveal is False


def test_show_in_finder_reveals_the_file_instead_of_opening_it(
        client, provider, opened):
    job = _finished_review(client, provider)
    assert client.post(
        f"/api/jobs/{job['id']}/open/document?reveal=true").status_code == 200
    assert opened[0][1] is True


def test_opening_a_result_that_was_moved_says_which_file_is_gone(
        client, provider, opened):
    job = _finished_review(client, provider)
    results = Path(client.get(f"/api/jobs/{job['id']}").json()["results_dir"])
    (results / "simple - Atmosphere Press Proofreader.docx").unlink()

    resp = client.post(f"/api/jobs/{job['id']}/open/document")
    assert resp.status_code == 404
    assert "simple - Atmosphere Press Proofreader.docx" in resp.json()["detail"]
    assert not opened


def test_opening_a_name_the_app_does_not_write_is_refused(
        client, provider, opened):
    job = _finished_review(client, provider)
    resp = client.post(f"/api/jobs/{job['id']}/open/passwords")
    assert resp.status_code == 404 and not opened


def test_a_plain_browser_is_told_to_download_instead(client, provider,
                                                     monkeypatch):
    """Run outside the desktop window there is nothing to hand a file to, and
    the page falls back to the download route on this exact status."""
    job = _finished_review(client, provider)
    monkeypatch.setattr("sys.platform", "linux")
    resp = client.post(f"/api/jobs/{job['id']}/open/document")
    assert resp.status_code == 501
    assert client.get(f"/api/jobs/{job['id']}/file/document").status_code == 200


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

    # The tick polls the batch ready and hands it to the worker; the worker
    # writes the document, so wait for it to drain before reading the result.
    client.post("/api/tick")
    client.app_state.runner.wait_idle()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done", done.get("error")
    assert client.get(f"/api/jobs/{job['id']}/file/docx").status_code == 200


def test_an_indesign_layout_can_go_overnight_too(client, provider):
    """Nothing between submit and collect knows what format it is carrying, and
    the cheap overnight run is worth as much to a layout as to a manuscript.
    This pins that: an .idml goes through the same three stages and comes back
    as a reviewed .idml."""
    idml_splice = "It was late, we were tired and the road went on forever."
    provider.results = [finding_result(
        para_id="story-ue0-p0001", error_type="comma_splice",
        original=idml_splice, corrected=idml_splice.replace("late,", "late;"))]
    staged = _upload(client, "layout.idml")
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "waiting"

    client.post("/api/tick")
    client.app_state.runner.wait_idle()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done", done.get("error")
    assert done["applied"] == 1

    reviewed = client.get(f"/api/jobs/{job['id']}/file/document")
    assert reviewed.status_code == 200 and reviewed.content[:2] == b"PK"
    # The house filename has spaces in it, so the header carries it RFC 5987
    # encoded. Browsers decode it; the test has to as well.
    from urllib.parse import unquote
    assert "layout - Atmosphere Press Proofreader.idml" in unquote(
        reviewed.headers["content-disposition"])


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


# --- cancelling -----------------------------------------------------------

def test_a_scheduled_job_can_be_cancelled_before_its_time_comes(client):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch", schedule_at="23:59")

    resp = client.post(f"/api/jobs/{job['id']}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "cancelled"

    # A tick that would otherwise have promoted it must not un-cancel it.
    client.post("/api/tick")
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"


def test_a_job_that_has_already_started_cannot_be_cancelled(client, provider):
    job = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"

    resp = client.post(f"/api/jobs/{job['id']}/cancel")
    assert resp.status_code == 400
    assert "nothing left to cancel" in resp.json()["detail"]
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"


def test_cancelling_an_unknown_job_says_so(client):
    resp = client.post("/api/jobs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_a_cancelled_job_is_not_still_running_on_the_dashboard(client):
    """Cancelled is over, the same as failed. It used to sit in the
    `unfinished` count forever — a machine with nothing running whose
    Requests tile said otherwise."""
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch", schedule_at="23:59")
    client.post(f"/api/jobs/{job['id']}/cancel")

    assert client.get("/api/usage").json()["unfinished"] == 0


def test_a_missing_upload_creates_no_jobs_at_all(client):
    """Two files, the second's upload gone: the old loop had already enqueued
    the first before 404ing, so the page said failure while half the batch
    ran — and the natural retry ran (and billed) it twice."""
    good = _upload(client)

    resp = client.post("/api/jobs", json={
        "file_ids": [good["id"], "u-gone"], "model": "claude-sonnet-5",
        "mode": "now"})

    assert resp.status_code == 404
    assert client.get("/api/jobs").json()["jobs"] == []


def test_a_cancelled_job_reports_ready_false(client):
    """Cancelled is a dead end, not a result — nothing should offer to open
    files for it the way a finished job does."""
    job = _run(client, _upload(client)["id"], mode="batch", schedule_at="23:59")
    client.post(f"/api/jobs/{job['id']}/cancel")
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["state"] == "cancelled" and body["ready"] is False


def test_a_crash_midway_through_collection_is_picked_up_again(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()

    # The app died after the results landed but before the document was
    # written. They are still at the vendor and already paid for, so the job
    # has to finish rather than sit saying "almost done". A restart is what
    # re-queues an interrupted collect now: resume_interrupted flips it back to
    # "waiting", the tick re-polls and hands it to the worker, and the worker
    # writes it.
    client.app_state.store.update(job["id"], state="collecting")
    client.app_state.runner.resume_interrupted()

    client.post("/api/tick")
    client.app_state.runner.wait_idle()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done", done.get("error")
    assert client.get(f"/api/jobs/{job['id']}/file/docx").status_code == 200


def test_collection_that_keeps_failing_asks_for_attention(client, monkeypatch):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()

    def boom(*a, **k):
        raise RuntimeError("disk is full")

    monkeypatch.setattr("app.jobs.batchlib.collect", boom)
    # The tick hands the ready batch to the worker; the worker runs the
    # (failing) collect, so wait for it to drain before reading the outcome.
    client.post("/api/tick")
    client.app_state.runner.wait_idle()

    failed = client.get(f"/api/jobs/{job['id']}").json()
    # Not left in `collecting`, which nothing would pick up again.
    assert failed["state"] == "failed"
    assert "disk is full" in failed["error"]
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200


def test_a_schedule_names_a_time_on_a_day_not_just_a_clock_face(tmp_path):
    runner = JobRunner(JobStore(Paths(tmp_path)), Settings(),
                       config_path=tmp_path / "config.yaml")
    created = datetime.now().astimezone().replace(hour=12, minute=0, second=0,
                                                  microsecond=0)

    def job(at):
        return Job(id="j", filename="f.docx", source_path="/f.docx", model="m",
                   mode="batch", state="scheduled", schedule_at=at,
                   created_at=created.isoformat())

    # 02:00 has already gone by for a job made at midday: it belongs to
    # tomorrow morning, not to right now.
    assert runner._scheduled_for(job("02:00")) == (
        created + timedelta(days=1)).replace(hour=2)
    assert runner._due(job("02:00")) is False

    # A time still ahead on the same day is left where it is.
    assert runner._scheduled_for(job("23:00")) == created.replace(hour=23)

    # Nothing unparseable is allowed to strand a job forever.
    assert runner._scheduled_for(job("half past nine")) is None
    assert runner._due(job("half past nine")) is True


def test_a_restarted_app_resumes_an_in_flight_batch(client, provider, tmp_path):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "waiting"

    # A brand new runner over the same directory — nothing carried in memory,
    # and no worker thread (this runner is never start()ed). The tick finds the
    # ready batch and hands it off; with no worker draining the queue, the job
    # sits at "collecting" until run_one is driven by hand.
    settings = Settings(output_dir=str(tmp_path / "out"))
    fresh = JobRunner(JobStore(Paths(tmp_path)), settings,
                      config_path=client.app_state.runner.config_path)
    fresh.tick_once()
    assert fresh.store.get(job["id"]).state == "collecting"
    fresh.run_one(job["id"])
    assert fresh.store.get(job["id"]).state == "done"


def test_building_the_app_arms_the_stack_dumper(tmp_path):
    """faulthandler is on after create_app, so a crash writes a Python
    traceback and `kill -USR1` dumps every thread's stack to the logs — the one
    way to pin a future deadlock on the frozen production binary."""
    import faulthandler

    create_app(tmp_path, start_runner=False)
    assert faulthandler.is_enabled()


# --- results folders ----------------------------------------------------------

def _runner(tmp_path):
    return JobRunner(JobStore(Paths(tmp_path)),
                     Settings(output_dir=str(tmp_path / "out")),
                     config_path=tmp_path / "config.yaml")


def _job(**over):
    fields = dict(id="j1", filename="Novel.docx", source_path="/Novel.docx",
                  model="m", mode="now", created_at="2026-08-04T10:00:00+00:00")
    fields.update(over)
    return Job(**fields)


def test_reviewing_one_document_twice_keeps_both_results(tmp_path):
    runner = _runner(tmp_path)
    first = runner._claim_results_dir(runner.store.save(_job(id="j1")))
    second = runner._claim_results_dir(runner.store.save(_job(id="j2")))

    assert first.name == "Novel"
    assert second.name == "Novel (2)"
    assert first != second and first.is_dir() and second.is_dir()

    third = runner._claim_results_dir(runner.store.save(_job(id="j3")))
    assert third.name == "Novel (3)"


def test_a_folder_already_on_disk_is_not_written_into(tmp_path):
    """Something the user put there by hand counts as taken, too."""
    runner = _runner(tmp_path)
    (tmp_path / "out" / "Novel").mkdir(parents=True)
    (tmp_path / "out" / "Novel" / "mine.txt").write_text("keep me")

    claimed = runner._claim_results_dir(runner.store.save(_job()))
    assert claimed.name == "Novel (2)"
    assert (tmp_path / "out" / "Novel" / "mine.txt").read_text() == "keep me"


def test_a_rerun_returns_to_its_own_folder(tmp_path):
    """A job that is retried must not claim a second folder and strand the
    first — the ticker re-runs collection after a crash."""
    runner = _runner(tmp_path)
    job = runner.store.save(_job())
    first = runner._claim_results_dir(job)
    again = runner._claim_results_dir(runner.store.get(job.id))

    assert first == again
    assert runner.store.get(job.id).results_dir == str(first)


def test_a_failed_run_gives_its_folder_back(tmp_path):
    runner = _runner(tmp_path)
    job = runner.store.save(_job())
    claimed = runner._claim_results_dir(job)
    runner._release_results_dir(job.id)

    assert not claimed.exists()
    assert runner.store.get(job.id).results_dir is None
    # The name is free again, so the next review is not pushed to (2).
    assert runner._claim_results_dir(runner.store.save(_job(id="j2"))).name \
        == "Novel"


def test_a_folder_with_results_in_it_is_never_released(tmp_path):
    runner = _runner(tmp_path)
    job = runner.store.save(_job())
    claimed = runner._claim_results_dir(job)
    (claimed / "findings.json").write_text("{}")

    runner._release_results_dir(job.id)
    assert claimed.is_dir()
    assert runner.store.get(job.id).results_dir == str(claimed)


def test_the_same_document_reviewed_twice_downloads_twice(client, provider):
    """End to end: the earlier review's download must not start serving the
    later review's document."""
    jobs = []
    for _ in range(2):
        staged = _upload(client)                  # same filename each time
        jobs.append(_run(client, staged["id"]))
        client.app_state.runner.wait_idle()

    assert jobs[0]["id"] != jobs[1]["id"]      # same document, same second
    dirs = [client.app_state.store.get(j["id"]).results_dir for j in jobs]
    assert dirs[0] != dirs[1]
    assert Path(dirs[1]).name == "simple (2)"

    # Two entries that read alike need something that tells them apart.
    named = [client.get(f"/api/jobs/{j['id']}").json()["results_name"]
             for j in jobs]
    assert named == ["simple", "simple (2)"]
    for job in jobs:
        assert client.get(f"/api/jobs/{job['id']}/file/docx").status_code == 200


# --- picking sections ---------------------------------------------------------

def test_upload_lists_the_sections_a_user_can_pick(client):
    staged = _upload(client)
    assert len(staged["chunks"]) == staged["sections"]
    section = staged["chunks"][0]
    # The picker is built from the user's own prose, not from chunk ids.
    assert SPLICE in section["preview"]
    assert section["paragraphs"] >= 1 and section["est_tokens"] > 0
    # Uploads preflight against the real config, so the picker prices a
    # section at every shipped pass even though this test runs one.
    assert staged["passes"] == PASSES
    assert staged["requests"] == staged["sections"] * staged["passes"]


def test_a_review_can_be_limited_to_chosen_sections(client, provider):
    staged = _upload(client)
    picked = [staged["chunks"][0]["chunk_id"]]
    job = _run(client, staged["id"], mode="batch",
               selections={staged["id"]: picked})
    client.app_state.runner.wait_idle()

    assert client.app_state.store.get(job["id"]).selection == picked
    # The batch that went out covers only the picked section.
    assert len(provider.calls[0]["requests"]) == len(picked)

    client.post("/api/tick")
    client.app_state.runner.wait_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"


def test_a_null_selection_means_the_whole_document(client):
    staged = _upload(client)
    job = _run(client, staged["id"], selections={staged["id"]: None})
    assert client.app_state.store.get(job["id"]).selection is None


# --- prompts ------------------------------------------------------------------

def test_prompts_screen_shows_what_will_be_sent(client):
    body = client.get("/api/prompts").json()
    assert len(body["types"]) == TYPES
    assert len(body["passes"]) == PASSES
    assert all(t["detection_prompt"] for t in body["types"])
    assert not any(t["edited"] for t in body["types"])
    # The assembled message is the real one, not a description of it.
    joined = " ".join(p["system_prompt"] for p in body["passes"])
    assert "ERROR TYPE: comma_splice" in joined
    assert body["sample_user_turn"].startswith('<paragraph id=')


def test_an_edited_prompt_is_used_by_the_next_review(client, provider):
    saved = client.put("/api/prompts/comma_splice",
                       json={"detection_prompt": "ZZZ-SENTINEL-ZZZ"})
    assert saved.status_code == 200
    edited = [t for t in saved.json()["types"]
              if t["key"] == "comma_splice"][0]
    assert edited["edited"] is True

    staged = _upload(client)
    _run(client, staged["id"])
    client.app_state.runner.wait_idle()
    assert "ZZZ-SENTINEL-ZZZ" in provider.calls[0]["system"]

    reset = client.delete("/api/prompts/comma_splice").json()
    assert not any(t["edited"] for t in reset["types"])


def test_an_empty_prompt_is_refused(client):
    assert client.put("/api/prompts/comma_splice",
                      json={"detection_prompt": "   "}).status_code == 400
    assert client.put("/api/prompts/not_a_type",
                      json={"detection_prompt": "x"}).status_code == 400


# --- the report ---------------------------------------------------------------

def test_report_reads_back_the_finished_review(client, provider):
    provider.results = [finding_result(
        para_id="body-0000", error_type="comma_splice", original=SPLICE,
        corrected=SPLICE.replace(",", ";", 1))]
    staged = _upload(client)
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()

    report = client.get(f"/api/jobs/{job['id']}/report").json()
    assert report["headline"]["applied"] == 1
    group = report["groups"][0]
    # Error types are named, not keyed, and the fix is shown as a diff.
    assert group["error_name"] == "Comma splice"
    finding = group["findings"][0]
    assert [s["text"] for s in finding["after"] if s["changed"]] == ["finished;"]
    assert finding["confidence_word"] == "Confident"


def test_report_is_absent_until_there_are_results(client):
    staged = _upload(client)
    job = _run(client, staged["id"], mode="batch")
    assert client.get(f"/api/jobs/{job['id']}/report").status_code == 404


# --- models and settings ------------------------------------------------------

def test_models_endpoint_prices_the_staged_files(client):
    staged = _upload(client)
    body = client.get(f"/api/models?file_ids={staged['id']}").json()
    by_id = {m["id"]: m for m in body["models"]}

    assert {m["provider"] for m in body["models"]} == {"anthropic", "openai",
                                                       "gemini"}
    opus = by_id["claude-opus-5"]
    assert opus["cost_now"] > opus["cost_batch"] > 0
    # Batch is exactly half, at any hour — the UI copy depends on this.
    assert opus["cost_batch"] == pytest.approx(opus["cost_now"] / 2)
    assert by_id["claude-haiku-4-5"]["cost_now"] < opus["cost_now"]
    # The between-round judge picker opens on the house default (default.yaml).
    assert body["default_judge_model"] == "gpt-5.6-sol"
    # The reviewer picker opens on the shipped default (gpt-5.6-luna), and the
    # catalog default is exposed independently so a stale persisted value can't
    # surface Sonnet.
    assert body["default_model"] == "gpt-5.6-luna"
    assert body["catalog_default_model"] == "gpt-5.6-luna"


def test_settings_round_trip_never_returns_a_key(client, monkeypatch):
    saved = {}
    monkeypatch.setattr("app.settings.set_api_key",
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


def test_boot_migrates_a_legacy_sonnet_default_to_luna(tmp_path):
    from app.main import create_app
    paths = Paths(tmp_path).ensure()
    # A volume file frozen by an older build: model + no settings_version.
    paths.settings_file.write_text(
        '{"model": "claude-sonnet-5", "effort": "high"}', "utf-8")
    app = create_app(tmp_path, start_runner=False)
    assert app.state.settings.model == "gpt-5.6-luna"   # in memory
    assert app.state.settings.effort == "high"          # untouched
    reloaded = Settings.load(paths)
    assert reloaded.model == "gpt-5.6-luna"             # persisted
    assert reloaded.settings_version == 1               # one-shot stamp


def test_boot_leaves_a_non_sonnet_default_alone(tmp_path):
    from app.main import create_app
    paths = Paths(tmp_path).ensure()
    paths.settings_file.write_text('{"model": "claude-opus-5"}', "utf-8")
    create_app(tmp_path, start_runner=False)
    assert Settings.load(paths).model == "claude-opus-5"


def test_boot_leaves_a_deliberate_stamped_sonnet_alone(tmp_path):
    """A Sonnet chosen after the migration ran (settings_version already 1) is a
    deliberate choice the one-shot guard must never revert."""
    from app.main import create_app
    paths = Paths(tmp_path).ensure()
    paths.settings_file.write_text(
        '{"model": "claude-sonnet-5", "settings_version": 1}', "utf-8")
    create_app(tmp_path, start_runner=False)
    assert Settings.load(paths).model == "claude-sonnet-5"


def test_an_explicit_model_choice_is_stamped_and_survives_a_restart(client, tmp_path):
    """An admin picking a model (even Sonnet) on a fresh, never-migrated install
    stamps settings_version via the PUT, so the one-shot legacy migration never
    silently reverts it on the next boot."""
    from app.main import create_app
    resp = client.put("/api/settings", json={"model": "claude-sonnet-5"})
    assert resp.status_code == 200
    assert Settings.load(Paths(tmp_path)).settings_version == 1   # stamped by the PUT
    app = create_app(tmp_path, start_runner=False)                # a later boot
    assert app.state.settings.model == "claude-sonnet-5"          # left alone


def test_an_effort_only_save_does_not_stamp_the_version(client, tmp_path):
    """An effort-only PUT must NOT stamp — so a legacy volume still frozen on the
    old Sonnet default is still repaired by the boot migration, not sealed in."""
    resp = client.put("/api/settings", json={"effort": "high"})
    assert resp.status_code == 200
    assert Settings.load(Paths(tmp_path)).settings_version == 0


# --- reasoning effort ---------------------------------------------------------

def test_the_effort_default_persists_and_round_trips(client, tmp_path):
    resp = client.put("/api/settings", json={"effort": "high"})
    assert resp.status_code == 200
    assert resp.json()["settings"]["effort"] == "high"
    assert client.get("/api/settings").json()["settings"]["effort"] == "high"
    assert Settings.load(Paths(tmp_path)).effort == "high"


def test_an_unknown_effort_default_is_refused(client):
    assert client.put("/api/settings", json={"effort": "turbo"}).status_code == 400


def test_a_review_falls_back_to_the_saved_effort(client):
    """A page that sends no effort inherits whatever the default was set to."""
    client.put("/api/settings", json={"effort": "medium"})
    job = _run(client, _upload(client)["id"])
    assert job["effort"] == "medium"


def test_a_review_can_override_the_saved_effort_for_one_run(client):
    client.put("/api/settings", json={"effort": "low"})
    job = _run(client, _upload(client)["id"], effort="max")
    assert job["effort"] == "max"


def test_a_job_asking_for_an_unknown_effort_is_refused(client):
    staged = _upload(client)
    resp = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "model": "claude-sonnet-5",
        "mode": "now", "effort": "turbo"})
    assert resp.status_code == 400


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
                  "done", "failed", "cancelled"):
        text = Job(id="j", filename="f.docx", source_path="/f.docx",
                   model="m", mode="batch", state=state,
                   schedule_at="23:00").plain_state()
        assert not any(w.lower() in text.lower() for w in words), text


# --- aborting and clearing ----------------------------------------------------

def test_aborting_a_running_job_signals_the_worker(client):
    """A running job's cancel doesn't fail the way it used to — it flags the
    run for the worker to stop, and answers 200 rather than '400, already
    started'."""
    store: JobStore = client.app_state.store
    runner: JobRunner = client.app_state.runner
    store.save(Job(id="run1", filename="simple.docx",
                   source_path=str(FIXTURES / "simple.docx"),
                   model="claude-sonnet-5", mode="now", state="running"))

    resp = client.post("/api/jobs/run1/cancel")
    assert resp.status_code == 200, resp.text
    assert runner._cancel_pending("run1"), "the worker was asked to stop"


def test_an_overnight_batch_still_cannot_be_recalled(client):
    """A submitted batch is at the vendor and will bill regardless; cancel says
    so rather than pretending to stop it."""
    store: JobStore = client.app_state.store
    store.save(Job(id="wait1", filename="simple.docx", source_path="x",
                   model="claude-sonnet-5", mode="batch", state="waiting"))
    resp = client.post("/api/jobs/wait1/cancel")
    assert resp.status_code == 400
    assert store.get("wait1").state == "waiting"


def test_clearing_removes_finished_jobs_and_their_documents(client):
    staged = _upload(client)
    job = _run(client, staged["id"])
    client.app_state.runner.wait_idle()
    jid = job["id"]
    record = client.app_state.store.get(jid)
    assert record.state == "done"
    assert record.results_dir and Path(record.results_dir).is_dir()

    resp = client.post("/api/jobs/clear")
    assert resp.status_code == 200
    assert jid in resp.json()["removed"]
    assert client.get("/api/jobs").json()["jobs"] == []
    assert not Path(record.results_dir).exists(), "documents cleared too"


def test_deleting_one_finished_job_leaves_the_others(client):
    first = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()
    second = _run(client, _upload(client)["id"])
    client.app_state.runner.wait_idle()

    resp = client.delete(f"/api/jobs/{first['id']}")
    assert resp.status_code == 200
    ids = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert first["id"] not in ids
    assert second["id"] in ids


def test_deleting_an_active_job_is_refused(client):
    store: JobStore = client.app_state.store
    store.save(Job(id="act1", filename="simple.docx", source_path="x",
                   model="claude-sonnet-5", mode="batch", state="waiting"))
    resp = client.delete("/api/jobs/act1")
    assert resp.status_code == 400
    assert store.get("act1") is not None, "an active job is left in place"
