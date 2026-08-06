"""Two users, one server, and the guarantee that neither can see or touch the
other's work. These are the checks that make the web build safe to hand to
more than one person — uploads, job lists, reads and downloads are all private
to the owner, and a job id from someone else's account is a 404, not a door."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.jobs import Job
from app.main import create_app
from app.settings import Paths
from .conftest import FIXTURES

SECRET = "test-session-secret"


@pytest.fixture
def app(tmp_path, monkeypatch):
    # A saved key lets jobs be created without a vendor; the runner is off, so
    # nothing actually calls out — jobs simply sit queued, which is all these
    # ownership checks need.
    monkeypatch.setattr("app.main.get_api_key", lambda p: "test-key")
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


def test_healthz_reports_version(app):
    body = _as(app, "a@press.com").get("/healthz").json()
    assert body["ok"] and body["version"]
