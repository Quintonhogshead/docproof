"""Two users, one server, and the guarantee that neither can see or touch the
other's work. These are the checks that make the web build safe to hand to
more than one person — uploads, job lists, reads and downloads are all private
to the owner, and a job id from someone else's account is a 404, not a door."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.jobs import Job, JobStore
from app.main import create_app
from app.settings import Paths
from app.spending import LedgerEntry, SpendingLedger
from .conftest import FIXTURES

SECRET = "test-session-secret"


@pytest.fixture
def app(tmp_path, monkeypatch):
    # A saved key lets jobs be created without a vendor; the runner is off, so
    # nothing actually calls out — jobs simply sit queued, which is all these
    # ownership checks need.
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("a@press.com", "password1")
    accounts.create_user("b@press.com", "password1")
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret=SECRET, https_only=False)


def _as(app, email):
    c = TestClient(app)
    assert c.post("/api/login",
                  json={"email": email, "password": "password1"}).status_code == 200
    return c


def _upload(client, name="simple.docx"):
    with (FIXTURES / name).open("rb") as fh:
        r = client.post("/api/files", files={"files": (name, fh)})
    assert r.status_code == 200
    return r.json()["files"][0]["id"]


def _seed_job(app, owner_email, job_id, **extra):
    """Put a finished-looking job straight in the store, owned by the user with
    `owner_email`. Ownership is by user id (a uuid), so we resolve the email to
    the id the routes actually compare against."""
    owner_id = app.state.accounts.get_by_email(owner_email).id
    extra.setdefault("state", "done")
    app.state.store.save(Job(
        id=job_id, filename="book.docx", source_path="/tmp/book.docx",
        model="claude-sonnet-5", mode="now", owner_id=owner_id, **extra))


def test_uploads_are_private_to_the_uploader(app):
    a, b = _as(app, "a@press.com"), _as(app, "b@press.com")
    file_id = _upload(a)
    # B cannot turn A's upload id into a job — it does not resolve under B.
    r = b.post("/api/jobs", json={"file_ids": [file_id],
                                  "model": "claude-sonnet-5", "mode": "batch"})
    assert r.status_code == 404
    # A can.
    r = a.post("/api/jobs", json={"file_ids": [file_id],
                                  "model": "claude-sonnet-5", "mode": "batch"})
    assert r.status_code == 200


def test_job_list_shows_only_your_own(app):
    _seed_job(app, "a@press.com", "job-a")
    _seed_job(app, "b@press.com", "job-b")
    a, b = _as(app, "a@press.com"), _as(app, "b@press.com")
    assert [j["id"] for j in a.get("/api/jobs").json()["jobs"]] == ["job-a"]
    assert [j["id"] for j in b.get("/api/jobs").json()["jobs"]] == ["job-b"]


def test_cannot_read_another_users_job(app):
    _seed_job(app, "b@press.com", "job-b")
    a = _as(app, "a@press.com")
    # 404, not 403 — A is not even told the job exists.
    assert a.get("/api/jobs/job-b").status_code == 404


def test_cannot_download_another_users_result(app):
    _seed_job(app, "b@press.com", "job-b", results_dir="/tmp/whatever")
    a = _as(app, "a@press.com")
    assert a.get("/api/jobs/job-b/file/document").status_code == 404


def test_cannot_retry_or_cancel_another_users_job(app):
    _seed_job(app, "b@press.com", "job-b", state="failed")
    a = _as(app, "a@press.com")
    assert a.post("/api/jobs/job-b/retry").status_code == 404
    assert a.post("/api/jobs/job-b/cancel").status_code == 404


def test_usage_counts_only_your_own_jobs(app):
    _seed_job(app, "a@press.com", "job-a", cost=3.0, api_calls=1)
    _seed_job(app, "b@press.com", "job-b", cost=99.0, api_calls=1)
    a = _as(app, "a@press.com")
    total = a.get("/api/usage").json()
    # A's bill reflects A's $3 job, never B's $99 one.
    assert total["totals"]["cost"] == pytest.approx(3.0)


def _seed_watch(app, *, live_cost=0.0, cleared_cost=0.0):
    """Give the watcher its own store — a live job and a cleared (ledger-only)
    one — so a test can check whether the watcher's spend is counted."""
    store = JobStore(Paths(app.state.watch.home))
    if live_cost:
        store.save(Job(id="w-live", filename="w.docx", source_path="/tmp/w.docx",
                       model="claude-sonnet-5", mode="now", kind="promo",
                       source="watch", state="done", cost=live_cost, api_calls=1,
                       created_at="2026-08-10T00:00:00+00:00"))
    if cleared_cost:
        SpendingLedger(store.paths.spending_db).record(LedgerEntry(
            id="w-cleared", filename="w2.docx", kind="promo",
            model="claude-sonnet-5", mode="now", source="watch", owner_id="",
            created_at="2026-08-10T00:00:00+00:00", words=0, input_tokens=0,
            output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
            api_calls=1, cost=cleared_cost))


def test_admin_usage_shows_the_full_bill_including_the_watcher(app):
    """An admin's Spending is every source on the one card: every user's app
    jobs, plus the watcher's spend, live and cleared."""
    app.state.accounts.create_user("admin@press.com", "password1",
                                    is_admin=True)
    _seed_job(app, "a@press.com", "job-a", cost=3.0, api_calls=1)
    _seed_job(app, "b@press.com", "job-b", cost=99.0, api_calls=1)
    _seed_watch(app, live_cost=5.0, cleared_cost=7.0)

    total = _as(app, "admin@press.com").get("/api/usage").json()
    assert total["totals"]["cost"] == pytest.approx(3.0 + 99.0 + 5.0 + 7.0)


def test_regular_user_usage_still_excludes_the_watcher(app):
    """The watcher is the organisation's, not a user's — a regular user's bill
    stays their own app jobs, never the watcher's."""
    _seed_job(app, "a@press.com", "job-a", cost=3.0, api_calls=1)
    _seed_watch(app, live_cost=5.0, cleared_cost=7.0)

    total = _as(app, "a@press.com").get("/api/usage").json()
    assert total["totals"]["cost"] == pytest.approx(3.0)


def test_your_own_job_is_reachable(app):
    _seed_job(app, "a@press.com", "job-a")
    a = _as(app, "a@press.com")
    assert a.get("/api/jobs/job-a").json()["id"] == "job-a"


def test_web_accepts_a_prep_job(app):
    # docx in, formatted docx out — prep runs server-side; only the later .indd
    # placement needs a Mac. Creating the job is all the web path has to allow.
    a = _as(app, "a@press.com")
    file_id = _upload(a)
    r = a.post("/api/jobs", json={"file_ids": [file_id],
                                  "model": "claude-sonnet-5",
                                  "kind": "prep", "prep_output": "indesign"})
    assert r.status_code == 200
    assert r.json()["jobs"][0]["is_prep"] is True


def test_independent_examination_is_hidden_and_refused_for_regular_users(app):
    user = _as(app, "a@press.com")
    feature_ids = {f["id"] for f in user.get("/api/features").json()["features"]}
    assert "examination_judgment" not in feature_ids

    file_id = _upload(user)
    r = user.post("/api/jobs", json={
        "file_ids": [file_id], "model": "claude-sonnet-5", "mode": "now",
        "features": {"examination_judgment": True}})
    assert r.status_code == 403
    assert "administrator-only" in r.json()["detail"]


def test_admin_examination_is_visible_but_run_now_and_one_round_only(
        app, monkeypatch):
    app.state.accounts.create_user("admin@press.com", "password1", is_admin=True)
    admin = _as(app, "admin@press.com")
    feature_ids = {f["id"] for f in admin.get("/api/features").json()["features"]}
    assert "examination_judgment" in feature_ids
    file_id = _upload(admin)
    body = {"file_ids": [file_id], "model": "claude-sonnet-5",
            "features": {"examination_judgment": True}}

    batch = admin.post("/api/jobs", json={**body, "mode": "batch"})
    assert batch.status_code == 400
    assert "run right now" in batch.json()["detail"]
    rounds = admin.post("/api/jobs", json={**body, "mode": "now", "rounds": 2})
    assert rounds.status_code == 400
    no_ledger = admin.post("/api/jobs", json={
        **body, "mode": "now",
        "features": {"examination_judgment": True,
                     "examination_graph": False}})
    assert no_ledger.status_code == 400
    assert "coverage ledger" in no_ledger.json()["detail"]

    accepted = admin.post("/api/jobs", json={**body, "mode": "now", "rounds": 1})
    assert accepted.status_code == 200
    assert accepted.json()["jobs"][0]["features"]["examination_judgment"] is True

    monkeypatch.setenv("DOCPROOF_EXAMINATION_JUDGMENT", "0")
    killed = admin.post("/api/jobs", json={**body, "mode": "now", "rounds": 1})
    assert killed.status_code == 409
    assert "kill switch" in killed.json()["detail"]


def test_healthz_reports_version(app):
    body = _as(app, "a@press.com").get("/healthz").json()
    assert body["ok"] and body["version"]
