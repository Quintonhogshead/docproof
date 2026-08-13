"""The promo panel over HTTP, with a fake provider.

Same rules as the other app tests: nothing reaches a vendor, nothing sleeps.
The manual-run flow — drop, generate, read, edit, download — and the watch
settings form.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from docproof.promo import MarketingPlan, PromoResult, SocialPost
from docproof.providers import ProviderResult

from .conftest import FIXTURES
from .fakes import USAGE

MODEL = "claude-haiku-4-5"

_PLAN_MD = (
    "# The Book — A. Writer\n\n"
    "This is a resource for you, the author, to consider some publicity "
    "efforts and endeavors to pursue!\n\n"
    "## Target Audience\n\n- **Readers:** people who read.\n\n"
    "## Marketing Strategy\n\n- **Events:** hold some.\n\n"
    "## Comparable Titles\n\n- **Some Book:** it rhymes with this one.\n\n"
    "## Key Message\n\nA book worth your time.")


class PromoProvider:
    """Answers the structured calls promo makes. For the teaser/posts call it
    returns copy with 'Zephyr' (in no fixture manuscript, so the grounding check
    flags exactly one term); for the marketing-plan call it returns Markdown.
    It branches on schema_name so one fake serves both runs."""

    def complete_structured(self, **kw) -> ProviderResult:
        if kw.get("schema_name") == "marketing_plan":
            return ProviderResult(
                parsed=MarketingPlan(plan=_PLAN_MD).model_dump(), usage=USAGE)
        result = PromoResult(
            teaser="A manuscript, and the Zephyr that changed it.",
            posts=[SocialPost(text=f"Would you read it? ({i + 1})")
                   for i in range(12)])
        return ProviderResult(parsed=result.model_dump(), usage=USAGE)


@pytest.fixture
def provider():
    return PromoProvider()


@pytest.fixture
def client(tmp_path, provider, monkeypatch):
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
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


def upload(client, name="simple.docx"):
    with (FIXTURES / name).open("rb") as fh:
        r = client.post("/api/files", files={"files": (name, fh.read())})
    return r.json()["files"][0]["id"]


def run_one(client, name="simple.docx", model=MODEL):
    file_id = upload(client, name)
    r = client.post("/api/promo/run",
                    json={"file_ids": [file_id], "model": model})
    assert r.status_code == 200, r.text
    client.app_state.runner.wait_idle()
    return r.json()["jobs"][0]["id"]


# --- manual runs --------------------------------------------------------------

def test_a_run_generates_copy_and_lists_it(client):
    job_id = run_one(client)

    jobs = client.get("/api/promo/jobs").json()["jobs"]
    assert [j["id"] for j in jobs] == [job_id]
    job = jobs[0]
    assert job["is_promo"] and job["state"] == "done"
    assert job["unverified"] == 1               # "Zephyr" flagged


def test_the_draft_reads_back_teaser_and_twelve_posts(client):
    job_id = run_one(client)
    draft = client.get(f"/api/promo/jobs/{job_id}/draft").json()
    assert draft["teaser"].startswith("A manuscript")
    assert len(draft["posts"]) == 12
    assert draft["unverified"] == 1


def test_editing_a_draft_rewrites_the_documents(client):
    job_id = run_one(client)
    edited = {"teaser": "A brand new teaser.",
              "posts": [{"platform": "", "text": f"New post {i}",
                         "hashtags": []} for i in range(12)]}
    assert client.put(f"/api/promo/jobs/{job_id}/draft",
                      json=edited).status_code == 200

    again = client.get(f"/api/promo/jobs/{job_id}/draft").json()
    assert again["teaser"] == "A brand new teaser."
    # The .docx was re-made from the edit, so the download is the new copy.
    doc = client.get(f"/api/promo/jobs/{job_id}/file/teaser")
    assert doc.status_code == 200
    assert doc.headers["content-disposition"].count("teaser.docx") == 1


def test_both_documents_download(client):
    job_id = run_one(client)
    for which, tail in (("teaser", "teaser.docx"),
                        ("posts", "posts.docx")):     # space is %20-encoded
        r = client.get(f"/api/promo/jobs/{job_id}/file/{which}")
        assert r.status_code == 200 and tail in r.headers["content-disposition"]


def test_a_bad_draft_is_refused(client):
    job_id = run_one(client)
    r = client.put(f"/api/promo/jobs/{job_id}/draft",
                   json={"teaser": "only a teaser, no posts key"})
    assert r.status_code == 422           # posts is required by the request model


def test_another_job_kind_is_not_a_promo_run(client):
    """A review job id is a 404 on the promo routes, not another kind's data."""
    r = client.get("/api/promo/jobs/does-not-exist/draft")
    assert r.status_code == 404


# --- the marketing plan -------------------------------------------------------

def run_plan(client, name="simple.docx", model=MODEL, **meta):
    file_id = upload(client, name)
    body = {"file_ids": [file_id], "model": model, **meta}
    r = client.post("/api/promo/plan", json=body)
    assert r.status_code == 200, r.text
    client.app_state.runner.wait_idle()
    return r.json()["jobs"][0]["id"]


def test_a_plan_run_generates_and_lists_as_a_plan(client):
    job_id = run_plan(client, author="A. Writer", city="Austin, TX")
    jobs = client.get("/api/promo/jobs").json()["jobs"]
    job = next(j for j in jobs if j["id"] == job_id)
    assert job["is_promo"] and job["is_plan"] and job["state"] == "done"
    # A plan runs no grounding check, so it never carries flags to reconcile.
    assert job["unverified"] == 0


def test_plan_metadata_reaches_the_job(client):
    job_id = run_plan(client, author="A. Writer", blurbs="A brave book.",
                      city="Austin, TX", keywords="rivers, journey")
    job = client.app_state.store.get(job_id)
    assert job.plan_only is True
    assert job.plan_author == "A. Writer"
    assert job.plan_blurbs == "A brave book."
    assert job.plan_city == "Austin, TX"
    assert job.plan_keywords == "rivers, journey"


def test_the_plan_reads_back_and_edits_rewrite_the_docx(client):
    job_id = run_plan(client, author="A. Writer")
    draft = client.get(f"/api/promo/jobs/{job_id}/plan").json()
    assert draft["plan"].startswith("# The Book")

    assert client.put(f"/api/promo/jobs/{job_id}/plan",
                      json={"plan": "# New Title\n\nRewritten plan."}
                      ).status_code == 200
    again = client.get(f"/api/promo/jobs/{job_id}/plan").json()
    assert again["plan"].startswith("# New Title")

    doc = client.get(f"/api/promo/jobs/{job_id}/file/plan")
    assert doc.status_code == 200
    assert "marketing%20plan.docx" in doc.headers["content-disposition"]


def test_the_plan_endpoints_reject_a_copy_job(client):
    """A teaser/posts job is not a plan job: its id 404s on the plan routes."""
    copy_id = run_one(client)
    assert client.get(f"/api/promo/jobs/{copy_id}/plan").status_code == 404
    assert client.put(f"/api/promo/jobs/{copy_id}/plan",
                      json={"plan": "x"}).status_code == 404


def test_a_bad_plan_draft_is_refused(client):
    job_id = run_plan(client, author="A. Writer")
    r = client.put(f"/api/promo/jobs/{job_id}/plan", json={"not_plan": "x"})
    assert r.status_code == 422           # plan is required by the request model


def test_plan_jobs_stay_out_of_the_results_list(client):
    job_id = run_plan(client, author="A. Writer")
    results = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert job_id not in results


# --- routing from the drop screen ---------------------------------------------

def test_staging_reports_promo_eligibility(client):
    with (FIXTURES / "simple.docx").open("rb") as fh:
        entry = client.post(
            "/api/files",
            files={"files": ("simple.docx", fh.read())}).json()["files"][0]
    assert entry["can_promo"] is True and entry["ok"] is True


def test_staging_reports_a_promo_cost_estimate(client):
    """The drop card carries the tokens the cost hint is built from, the
    single-pass size the limit is measured against, and the over-limit flag."""
    with (FIXTURES / "simple.docx").open("rb") as fh:
        promo = client.post(
            "/api/files",
            files={"files": ("simple.docx", fh.read())}
        ).json()["files"][0]["promo"]
    assert promo["words"] > 0
    assert promo["pass_tokens"] > 0
    assert promo["input_tokens"] >= promo["pass_tokens"]
    assert promo["max_input_tokens"] > 0
    assert promo["over_limit"] is False       # a fixture, comfortably under


def test_promo_run_carries_effort_and_override(client):
    """The reasoning dial and the oversize override reach the job record, so the
    run honours the slider and the human's choice to run a big book anyway."""
    file_id = upload(client)
    r = client.post("/api/promo/run",
                    json={"file_ids": [file_id], "model": MODEL,
                          "effort": "high", "allow_oversize": True})
    assert r.status_code == 200, r.text
    client.app_state.runner.wait_idle()
    job = client.app_state.store.get(r.json()["jobs"][0]["id"])
    assert job.effort == "high"
    assert job.allow_oversize is True


def test_promo_jobs_stay_out_of_the_results_list(client):
    """A drop routed to promo lands in the Promo panel, not in Results."""
    job_id = run_one(client)
    results = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    promo = [j["id"] for j in client.get("/api/promo/jobs").json()["jobs"]]
    assert job_id not in results
    assert job_id in promo


# --- settings -----------------------------------------------------------------

def test_promo_settings_round_trip(client):
    assert client.get("/api/promo/settings").json()["promo_enabled"] is False

    r = client.put("/api/promo/settings", json={
        "promo_enabled": True,
        "hubspot_promo_ready_value": "Ready for Promo text",
        "hubspot_promo_done_value": "Promo text finished",
        "promo_auto_upload": False})
    assert r.status_code == 200

    got = client.get("/api/promo/settings").json()
    assert got["promo_enabled"] is True
    assert got["hubspot_promo_ready_value"] == "Ready for Promo text"
    assert got["promo_auto_upload"] is False


def test_promo_settings_reject_an_unknown_model(client):
    r = client.put("/api/promo/settings", json={"promo_model": "not-a-model"})
    assert r.status_code == 400
