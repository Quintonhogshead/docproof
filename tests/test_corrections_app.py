"""The corrections job kind, driven through HTTP.

Same rules as the other app tests: nothing reaches a vendor and nothing sleeps.
Corrections runs no model at all, so there is no provider to fake — the whole
run is deterministic Python over the exported IDML.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from docproof.corrections.idml import parse_story
from docproof.providers import NormalizedUsage, ProviderResult

from .conftest import FIXTURES
from .fakes import FakeProvider
from .test_corrections_extract import _proof, make_tracked_docx

LAYOUT = FIXTURES / "layout.idml"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # No provider is built for a corrections job, but the app has other routes
    # that ask after keys; keep them satisfied so the fixture is reusable.
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


def upload(client, name):
    with (FIXTURES / name).open("rb") as fh:
        return client.post("/api/files",
                           files={"files": (name, fh.read())}).json()["files"][0]


def start_corrections(client, file_id, corrections, **extra):
    body = {"file_ids": [file_id], "kind": "corrections",
            "corrections": json.dumps(corrections) if not isinstance(
                corrections, str) else corrections, **extra}
    resp = client.post("/api/jobs", json=body)
    return resp


def run_corrections(client, file_id, corrections, **extra):
    job = start_corrections(client, file_id, corrections, **extra).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    return client.get(f"/api/jobs/{job['id']}").json()


def story_text(idml: Path, story_id: str = "ue0") -> list[str]:
    with zipfile.ZipFile(idml) as z:
        s = parse_story(z.read(f"Stories/Story_{story_id}.xml"), story_id)
    return [p.text for p in s.paragraphs]


# --- staging ------------------------------------------------------------------

def test_an_idml_can_be_corrected(client):
    staged = upload(client, "layout.idml")
    assert staged["ok"] and staged["can_correct"] is True
    assert staged["correct_error"] is None


def test_a_word_document_cannot_be_corrected(client):
    staged = upload(client, "simple.docx")
    assert staged["can_correct"] is False
    assert "InDesign" in staged["correct_error"]


# --- running ------------------------------------------------------------------

def test_corrections_produce_a_corrected_file_and_a_report(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    assert job["state"] == "done", job.get("error")
    assert job["is_corrections"] and job["kind"] == "corrections"
    assert job["applied"] == 1
    assert job["flags"] == 0 and job["discrepancies"] == 0
    assert job["verified"] is True
    assert job["cost"] == 0.0

    results = Path(job["results_dir"])
    assert (results / "layout_corrected.idml").is_file()
    assert (results / "corrections_notes.md").is_file()
    assert (results / "corrections.json").is_file()
    # The corrected file really carries the change.
    assert story_text(results / "layout_corrected.idml")[4] == \
        "There were several mistakes here to find."


def test_the_corrected_file_and_report_are_downloadable(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    for which, expected in (("corrected", "layout_corrected.idml"),
                            ("corrections-notes", "corrections_notes.md"),
                            ("document", "layout_corrected.idml")):
        r = client.get(f"/api/jobs/{job['id']}/file/{which}")
        assert r.status_code == 200, which
        assert expected in r.headers["content-disposition"]


def test_the_corrections_report_reads_back_for_the_screen(client):
    job = run_corrections(
        client, upload(client, "layout.idml")["id"],
        [{"find": "Their were", "replace": "There were"},
         {"find": "nowhere in the book", "replace": "x"}])
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert report["mode"] == "apply"
    assert report["deterministic"] is True
    assert report["apply"]["applied"] == 1
    assert len(report["apply"]["flagged"]) == 1
    # The unanchored edit shows in the job counters too.
    assert job["applied"] == 1 and job["flags"] == 1 and job["verified"] is False


def test_a_partly_unreadable_list_still_applies_the_good_edits(client):
    """A single bad entry is reported, not fatal — the rest apply."""
    job = run_corrections(
        client, upload(client, "layout.idml")["id"],
        [{"find": "Their were", "replace": "There were"},
         {"replace": "no find to anchor"}])          # a parse issue
    assert job["state"] == "done"
    assert job["applied"] == 1
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert len(report["parse"]["issues"]) == 1


# --- refusing bad input at submit ---------------------------------------------

def test_a_non_idml_file_is_refused(client):
    staged = upload(client, "simple.docx")
    r = start_corrections(client, staged["id"],
                          [{"find": "a", "replace": "b"}])
    assert r.status_code == 400
    assert "InDesign" in r.json()["detail"]


def test_an_empty_corrections_list_is_refused(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], "")
    assert r.status_code == 400


def test_malformed_json_is_refused_with_a_readable_message(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], "{not json")
    assert r.status_code == 400
    assert "could not be read" in r.json()["detail"]


def test_a_list_with_no_usable_corrections_is_refused(client):
    staged = upload(client, "layout.idml")
    r = start_corrections(client, staged["id"], [{"replace": "no find"}])
    assert r.status_code == 400
    assert "No usable corrections" in r.json()["detail"]


def test_more_than_one_file_is_refused(client):
    a = upload(client, "layout.idml")["id"]
    b = upload(client, "layout.idml")["id"]
    r = client.post("/api/jobs", json={
        "file_ids": [a, b], "kind": "corrections",
        "corrections": json.dumps([{"find": "a", "replace": "b"}])})
    assert r.status_code == 400
    assert "one InDesign file" in r.json()["detail"]


# --- it coexists with the other kinds -----------------------------------------

def test_a_corrections_job_appears_in_the_results_list(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    listed = client.get("/api/jobs").json()["jobs"]
    assert any(j["id"] == job["id"] and j["is_corrections"] for j in listed)
    assert job["plain_state"] == "Ready"


def test_corrections_never_asks_for_a_model_or_a_key(client, monkeypatch):
    """The deterministic path builds no provider and needs no key — so a run
    still succeeds with every key removed."""
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "")
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}])
    assert job["state"] == "done", job.get("error")
    assert job["applied"] == 1


# --- extracting a list from a Word file or prose ------------------------------

def test_extract_from_a_redlined_word_file(client, tmp_path):
    doc = make_tracked_docx(tmp_path / "corr.docx", [
        [("del", "Their"), ("ins", "There"),
         ("", " were several mistakes here to find.")]])
    with doc.open("rb") as fh:
        r = client.post("/api/corrections/extract-docx",
                        files={"file": ("corr.docx", fh.read())})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    # The returned JSON is exactly what the corrections textarea takes — so it
    # round-trips straight into a corrections run.
    parsed = json.loads(body["json"])
    assert "There" in parsed[0]["replace"]
    job = run_corrections(client, upload(client, "layout.idml")["id"], body["json"])
    assert job["state"] == "done" and job["applied"] == 1


def test_a_word_file_with_no_tracked_changes_says_so(client, tmp_path):
    doc = make_tracked_docx(tmp_path / "plain.docx",
                            [[("", "Nothing tracked in here.")]])
    with doc.open("rb") as fh:
        r = client.post("/api/corrections/extract-docx",
                        files={"file": ("plain.docx", fh.read())})
    assert r.status_code == 400 and "No tracked changes" in r.json()["detail"]


def test_a_non_docx_upload_to_extract_is_refused(client):
    r = client.post("/api/corrections/extract-docx",
                    files={"file": ("notes.txt", b"change x to y")})
    assert r.status_code == 400 and "Word" in r.json()["detail"]


def test_extract_from_a_prose_list_uses_the_model(client, monkeypatch):
    provider = FakeProvider([ProviderResult(
        parsed={"edits": [
            {"find": "Their were", "replace": "There were", "instruction": "",
             "kind": "mechanical", "occurrence": 0}]},
        usage=NormalizedUsage(input_tokens=300, output_tokens=50))])
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    r = client.post("/api/corrections/extract-list",
                    json={"text": "change 'Their were' to 'There were'"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1 and provider.calls
    parsed = json.loads(body["json"])
    assert parsed[0]["find"] == "Their were"
    # The (small) extraction spend is recorded to the dashboard under corrections.
    usage = client.get("/api/usage").json()
    assert usage["totals"]["output_tokens"] >= 50


def test_extract_from_a_list_without_a_key_is_refused(client, monkeypatch):
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "")
    r = client.post("/api/corrections/extract-list", json={"text": "change x to y"})
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_extract_from_a_commented_pdf(client, monkeypatch, tmp_path):
    """A PDF proof's comments are read deterministically, then the (fake) model
    turns them into a draft edit list."""
    provider = FakeProvider([ProviderResult(
        parsed={"edits": [
            {"find": "fish oil", "replace": "petroleum jelly", "instruction": "",
             "kind": "mechanical", "occurrence": 0},
            {"find": "tobacco", "replace": "candlestick", "instruction": "",
             "kind": "mechanical", "occurrence": 0}]},
        usage=NormalizedUsage(input_tokens=400, output_tokens=80))])
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    pdf = _proof(tmp_path / "proof.pdf")
    with pdf.open("rb") as fh:
        r = client.post("/api/corrections/extract-pdf",
                        files={"file": ("proof.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    # The reader really pulled the two comments out and handed them to the model.
    assert "petroleum jelly" in provider.calls[0]["user"]
    assert "tobacco" in provider.calls[0]["user"]


def test_a_pdf_with_no_comments_is_refused(client, tmp_path):
    from .test_corrections_extract import make_commented_pdf
    plain = make_commented_pdf(tmp_path / "plain.pdf",
                               lines=[(72, 700, "No comments in this proof.")],
                               annots=[])
    with plain.open("rb") as fh:
        r = client.post("/api/corrections/extract-pdf",
                        files={"file": ("plain.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 400 and "No comments" in r.json()["detail"]


def test_a_non_pdf_upload_to_extract_pdf_is_refused(client):
    r = client.post("/api/corrections/extract-pdf",
                    files={"file": ("notes.txt", b"not a pdf")})
    assert r.status_code == 400 and "PDF" in r.json()["detail"]


def test_read_pdf_hands_back_batches_and_costs_nothing(client, monkeypatch,
                                                       tmp_path):
    """The first, deterministic half of the panel's read: the comments are
    pulled and split into bounded batches, with no model call at all — a big
    proof splits so the panel can read it a batch at a time."""
    # One comment per batch, so the two-comment proof yields two batches.
    monkeypatch.setattr("app.routes.jobs.CORRECTIONS_PDF_BATCH_SIZE", 1)
    # A provider would raise if anything tried to call the model here.
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda *a, **k: pytest.fail("read-pdf must not call a model"))
    pdf = _proof(tmp_path / "proof.pdf")
    with pdf.open("rb") as fh:
        r = client.post("/api/corrections/read-pdf",
                        files={"file": ("proof.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert len(body["batches"]) == 2
    assert all("comment:" in b for b in body["batches"])


def test_extract_pdf_reads_a_big_proof_in_bounded_batches(client, monkeypatch,
                                                          tmp_path):
    """The truncation fix: a proof whose comments exceed one batch is read in
    several bounded model calls, and the edits accumulate across them — so a
    large mark-up can't overrun the output ceiling and silently lose everything.
    Here batch size 1 forces one call per comment; both edits come back."""
    monkeypatch.setattr("app.routes.jobs.CORRECTIONS_PDF_BATCH_SIZE", 1)
    provider = FakeProvider([
        ProviderResult(parsed={"edits": [
            {"find": "fish oil", "replace": "petroleum jelly", "instruction": "",
             "kind": "mechanical", "occurrence": 0}]},
            usage=NormalizedUsage(input_tokens=200, output_tokens=40)),
        ProviderResult(parsed={"edits": [
            {"find": "tobacco", "replace": "candlestick", "instruction": "",
             "kind": "mechanical", "occurrence": 0}]},
            usage=NormalizedUsage(input_tokens=200, output_tokens=40))])
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    pdf = _proof(tmp_path / "proof.pdf")
    with pdf.open("rb") as fh:
        r = client.post("/api/corrections/extract-pdf",
                        files={"file": ("proof.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2                       # accumulated across two calls
    non_batch_calls = [c for c in provider.calls if not c.get("batch")]
    assert len(non_batch_calls) == 2                # one model call per batch
