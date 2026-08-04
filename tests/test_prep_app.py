"""Prep and the spending dashboard, driven through HTTP with a fake provider.

Same rules as test_app.py: nothing here reaches a vendor and nothing sleeps.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobRunner
from app.main import create_app
from docproof.providers import ProviderResult

from .conftest import FIXTURES
from .fakes import USAGE

# Enough labels to make the fixture a real manuscript: a title, a copyright
# line, a chapter, a scene break and a back-matter title.
LABELS = {"body-0000": "title page", "body-0002": "copyright",
          "body-0004": "chapter # / title", "body-0008": "scene break",
          "body-0012": "front/backmatter title"}


class TaggingProvider:
    """Answers the one question prep asks, for whatever ids it was sent."""

    name = "fake-tagger"

    def __init__(self):
        self.calls = []

    def complete_structured(self, *, user, **kwargs):
        import re
        ids = re.findall(r'id="([^"]+)"', user)
        blanks = set(re.findall(r'<blank id="([^"]+)"/>', user))
        self.calls.append({"user": user, **kwargs})
        rows = [{"para_id": pid,
                 "role": LABELS.get(pid, "spacing" if pid in blanks else "body"),
                 "flag": "Byline — the house title page comes from the cover."
                         if pid == "body-0000" else ""}
                for pid in ids]
        return ProviderResult(parsed={"paragraphs": rows}, usage=USAGE)


@pytest.fixture
def provider():
    return TaggingProvider()


@pytest.fixture
def client(tmp_path, provider, monkeypatch):
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.main.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.main.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"},
        "openai": {"configured": True, "source": "environment"},
        "gemini": {"configured": True, "source": "environment"}})

    app = create_app(tmp_path, start_runner=False)
    app.state.settings.output_dir = str(tmp_path / "out")
    app.state.runner.start()
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
    app.state.runner.stop()


def upload(client, name="googledoc.docx"):
    with (FIXTURES / name).open("rb") as fh:
        response = client.post("/api/files", files={"files": (name, fh.read())})
    return response.json()["files"][0]


def start_prep(client, file_id, output="indesign"):
    body = {"file_ids": [file_id], "model": "claude-haiku-4-5", "kind": "prep",
            "prep_output": output}
    job = client.post("/api/jobs", json=body).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    return client.get(f"/api/jobs/{job['id']}").json()


# --- staging ------------------------------------------------------------------

def test_a_dropped_file_is_preflighted_for_both_jobs(client):
    """The user has not chosen yet, so both questions are answered at drop
    time — while they are still looking at the screen."""
    staged = upload(client)
    assert staged["ok"] and staged["can_review"] and staged["can_prep"]
    assert staged["prep"]["paragraphs"] == 14
    assert staged["prep"]["blank_lines"] == 5
    assert staged["prep"]["style_sheet"] == "Atmosphere Press prose template"


def test_a_layout_can_be_reviewed_but_never_prepped(client):
    staged = upload(client, "layout.idml")
    assert staged["can_review"] and not staged["can_prep"]
    assert "INTO InDesign" in staged["prep_error"]


def test_a_manuscript_with_tracked_changes_is_refused(client):
    staged = upload(client, "tracked.docx")
    assert not staged["ok"]
    assert "tracked changes" in staged["error"]


# --- running ------------------------------------------------------------------

def test_prep_produces_a_tagged_file_and_its_notes(client):
    job = start_prep(client, upload(client)["id"])
    assert job["state"] == "done", job.get("error")
    assert job["is_prep"] and job["verified"] is True
    assert job["tagged"] == 10
    assert job["flags"] >= 1                      # the byline the model raised

    results = Path(job["results_dir"])
    assert (results / "tagged_googledoc.docx").is_file()
    assert (results / "prep_notes.md").is_file()
    assert not (results / "tracked_googledoc.docx").exists()


def test_the_output_toggle_decides_which_files_are_written(client):
    both = start_prep(client, upload(client)["id"], output="both")
    results = Path(both["results_dir"])
    assert (results / "tagged_googledoc.docx").is_file()
    assert (results / "tracked_googledoc.docx").is_file()

    tracked = start_prep(client, upload(client)["id"], output="tracked")
    results = Path(tracked["results_dir"])
    assert (results / "tracked_googledoc.docx").is_file()
    assert not (results / "tagged_googledoc.docx").exists()


def test_prep_is_never_queued_overnight(client):
    """Windows are read in order — a paragraph's meaning depends on the ones
    before it — so there is no batch form of prep to fall into by accident."""
    staged = upload(client)
    job = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "model": "claude-haiku-4-5", "kind": "prep",
        "mode": "batch", "schedule_at": "23:00"}).json()["jobs"][0]
    assert job["mode"] == "now" and job["schedule_at"] is None


def test_both_files_are_downloadable_by_name(client):
    job = start_prep(client, upload(client)["id"], output="both")
    for which, expected in (("indesign", "tagged_googledoc.docx"),
                            ("tracked", "tracked_googledoc.docx"),
                            ("notes", "prep_notes.md")):
        r = client.get(f"/api/jobs/{job['id']}/file/{which}")
        assert r.status_code == 200, which
        assert expected in r.headers["content-disposition"]
    # The generic button works too, whichever output the job actually wrote.
    assert client.get(f"/api/jobs/{job['id']}/file/document").status_code == 200


def test_the_prep_notes_read_back_for_the_screen(client):
    job = start_prep(client, upload(client)["id"])
    notes = client.get(f"/api/jobs/{job['id']}/prep").json()
    assert notes["verified"] is True
    assert notes["counts"]["scene_breaks_inserted"] == 1
    assert notes["styles"]["body para"] >= 1
    assert any(f["kind"] == "model" for f in notes["flags"])
    assert notes["files"] == {"indesign": True, "tracked": False}


def test_a_prep_job_interrupted_mid_write_is_started_again(client):
    """Prep is never at a vendor, so a job that died between labelling and
    writing has nothing to poll for — it has to be re-run, and the ticker must
    not go looking for a batch that never existed."""
    job = start_prep(client, upload(client)["id"])
    store, runner = client.app_state.store, client.app_state.runner
    store.update(job["id"], state="collecting")

    runner.tick_once()                       # must not fail it
    assert store.get(job["id"]).state == "collecting"

    runner.resume_interrupted()
    runner.wait_idle()
    again = store.get(job["id"])
    assert again.state == "done"
    assert again.results_dir == job["results_dir"]     # same folder, not a (2)


def test_a_review_still_works_alongside_prep(client, monkeypatch):
    """The two pipelines share a queue, a results folder scheme and a job
    list. Adding prep must not have moved review."""
    staged = upload(client, "simple.docx")
    job = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "model": "claude-haiku-4-5",
        "mode": "now"}).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "done" and done["kind"] == "review"


# --- the style guide ----------------------------------------------------------

def test_the_style_guide_says_which_file_is_in_force(client):
    body = client.get("/api/prep/styles").json()
    assert body["ok"] and body["name"] == "Atmosphere Press prose template"
    assert not body["using_override"]
    names = [s["name"] for s in body["styles"]]
    assert "chapter # / title" in names and "body first" in names
    assert body["override_path"].endswith("prep/house_styles.yaml")


def test_dropping_in_your_own_style_guide_replaces_the_shipped_one(client):
    """The whole point of the style set being a file. No new build, no code."""
    paths = client.app_state.paths
    shipped = Path(client.get("/api/prep/styles").json()["shipped_path"])
    (paths.prep / "house_styles.yaml").write_text(
        shipped.read_text("utf-8").replace("Atmosphere Press prose template",
                                           "Riverbend Books"),
        encoding="utf-8")

    body = client.get("/api/prep/styles").json()
    assert body["name"] == "Riverbend Books" and body["using_override"]

    job = start_prep(client, upload(client)["id"])
    notes = client.get(f"/api/jobs/{job['id']}/prep").json()
    assert notes["style_sheet"]["name"] == "Riverbend Books"


# --- spending -----------------------------------------------------------------

def test_the_dashboard_adds_up_what_has_been_spent(client):
    start_prep(client, upload(client)["id"])
    usage = client.get("/api/usage").json()

    assert usage["totals"]["jobs"] == 1
    assert usage["totals"]["input_tokens"] == USAGE.input_tokens
    assert usage["totals"]["output_tokens"] == USAGE.output_tokens
    assert usage["totals"]["api_calls"] == 1
    assert usage["totals"]["words"] == 67
    assert usage["totals"]["cost"] > 0

    by_model = usage["by_model"][0]
    assert by_model["model"] == "claude-haiku-4-5"
    assert by_model["display"]                     # a name, not an id
    assert usage["by_kind"][0]["kind"] == "prep"
    assert usage["recent"][0]["filename"] == "googledoc.docx"
    assert len(usage["by_month"]) == 1


def test_the_dashboard_counts_reviews_and_prep_separately(client):
    start_prep(client, upload(client)["id"])
    staged = upload(client, "simple.docx")
    client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                   "model": "claude-haiku-4-5", "mode": "now"})
    client.app_state.runner.wait_idle()

    usage = client.get("/api/usage").json()
    kinds = {row["kind"]: row for row in usage["by_kind"]}
    assert set(kinds) == {"prep", "review"}
    assert usage["totals"]["jobs"] == 2


def test_a_job_that_predates_usage_records_is_filled_in_from_its_results(client):
    """Someone upgrading mid-week should not see a dashboard full of blanks."""
    job = start_prep(client, upload(client)["id"])
    store = client.app_state.store
    store.update(job["id"], input_tokens=0, output_tokens=0, api_calls=0,
                 cache_read_tokens=0, cache_write_tokens=0, cost=None)

    usage = client.get("/api/usage").json()
    assert usage["totals"]["api_calls"] == 1
    assert usage["totals"]["input_tokens"] == USAGE.input_tokens


def test_an_empty_dashboard_is_zeros_not_an_error(client):
    usage = client.get("/api/usage").json()
    assert usage["totals"] == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "api_calls": 0, "cost": 0.0, "jobs": 0,
        "words": 0}
    assert usage["recent"] == [] and usage["by_month"] == []


# --- old records --------------------------------------------------------------

def test_a_job_record_from_before_prep_existed_still_loads(client):
    """Job records are files on disk; an upgrade must not orphan them."""
    store = client.app_state.store
    (store.paths.jobs / "old-job").mkdir(parents=True)
    (store.paths.jobs / "old-job" / "app.json").write_text(json.dumps({
        "id": "old-job", "filename": "novel.docx", "source_path": "/gone.docx",
        "model": "claude-haiku-4-5", "mode": "batch", "state": "done",
        "created_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")

    job = store.get("old-job")
    assert job.kind == "review" and not job.is_prep
    assert job.prep_output == "indesign" and job.cost is None
    assert client.get("/api/jobs/old-job").json()["plain_state"] == "Ready"
