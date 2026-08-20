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
    # Join the worker rather than only signalling it, so no job outlives the
    # stubs this fixture installed (see tests/test_app.py).
    app.state.runner.stop(join=30)


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
    # Gates off, so this exercises the free, deterministic path the assertions
    # below describe; the model passes are on by default and have their own tests.
    job = run_corrections(
        client, upload(client, "layout.idml")["id"],
        [{"find": "Their were", "replace": "There were"},
         {"find": "nowhere in the book", "replace": "x"}],
        corrections_sanity=False, corrections_second_look=False,
        corrections_escalate=False)
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


def test_the_run_records_the_step_it_is_on(client, monkeypatch):
    """The steps the applier reports reach the job record, so the card can name
    them — and the finished job is left with no stage at all."""
    seen = []
    store = client.app_state.runner.store
    real = store.update

    def spy(job_id, **fields):
        if "stage" in fields:
            seen.append(fields["stage"])
        return real(job_id, **fields)

    monkeypatch.setattr(store, "update", spy)
    # Gates off: the deterministic path reports exactly these steps. With the model
    # passes on it reports theirs too, which their own tests cover.
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          json.dumps([{"find": "Their were",
                                       "replace": "There were"}]),
                          corrections_sanity=False, corrections_second_look=False,
                          corrections_escalate=False)
    assert job["state"] == "done"
    assert ["reading", "applying", "verifying", "writing"] == \
        [s for s in seen if s]
    assert seen[-1] == ""                  # cleared when it finished
    assert job["stage"] == ""


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


def test_a_word_file_that_is_a_typed_list_goes_to_the_model(client, tmp_path,
                                                            monkeypatch):
    """No redline in it, but plenty of corrections: an editor's typed list. It is
    read like a pasted one rather than refused."""
    provider = FakeProvider([ProviderResult(
        parsed={"edits": [
            {"find": "Their were", "replace": "There were", "instruction": "",
             "kind": "mechanical", "occurrence": 0}]},
        usage=NormalizedUsage(input_tokens=300, output_tokens=50))])
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    doc = make_tracked_docx(tmp_path / "list.docx", [
        [("", "p. 12 — change 'Their were' to 'There were'")]])
    with doc.open("rb") as fh:
        r = client.post("/api/corrections/extract-docx",
                        files={"file": ("list.docx", fh.read())})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1 and provider.calls
    # The model was shown the file's own words, and the JSON it produced is what
    # the applicator takes.
    assert "Their were" in provider.calls[0]["user"]
    assert json.loads(body["json"])[0]["find"] == "Their were"


def test_an_empty_word_file_says_there_is_nothing_to_read(client, tmp_path):
    doc = make_tracked_docx(tmp_path / "blank.docx", [[("", "   ")]])
    with doc.open("rb") as fh:
        r = client.post("/api/corrections/extract-docx",
                        files={"file": ("blank.docx", fh.read())})
    assert r.status_code == 400 and "no text" in r.json()["detail"]


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


# --- reading a proof against the book it will be applied to -------------------

def test_read_pdf_resolves_the_mechanical_marks_without_a_model(client,
                                                                monkeypatch,
                                                                tmp_path):
    """A note that is a function of the span it sits on never reaches the model.
    The proof fixture's highlight says "Slick with petroleum jelly" (prose, for the
    model); a "Lowercase" mark on the same word is resolved here and for nothing."""
    from tests.test_corrections_extract import make_commented_pdf
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda *a, **k: pytest.fail("read-pdf must not call a model"))
    pdf = make_commented_pdf(
        tmp_path / "marks.pdf",
        lines=[(72, 700, "were slick with Fish oil.")],
        annots=[{"subtype": "/Highlight",
                 # over "Fish" — Helvetica 12pt puts it at x 150-176
                 "rect": [150, 698, 176, 712],
                 "quad": [150, 712, 176, 712, 150, 698, 176, 698],
                 "contents": "Lowercase"}])
    with pdf.open("rb") as fh:
        r = client.post("/api/corrections/read-pdf",
                        files={"file": ("marks.pdf", fh.read(), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["resolved_count"] == 1
    assert body["resolved"][0]["find"] == "Fish"
    assert body["resolved"][0]["replace"] == "fish"
    assert body["resolved"][0]["source"] == "p1-1"
    assert body["batches"] == []              # nothing left for the model to read


def test_read_pdf_hands_back_the_page_texts_for_the_page_map(client, tmp_path):
    """The page texts are what turn "marked on page 49" into a run of book text, so
    they ride back with the comments and into the job."""
    with _proof(tmp_path / "proof.pdf").open("rb") as fh:
        r = client.post("/api/corrections/read-pdf",
                        files={"file": ("proof.pdf", fh.read(), "application/pdf")})
    body = r.json()
    assert len(body["pages"]) == 1
    assert "fish oil" in body["pages"][0]


def test_read_pdf_shows_the_model_the_book_when_the_idml_is_staged(client,
                                                                  monkeypatch,
                                                                  tmp_path):
    """The fix for the anchors that could never match: given the staged IDML, the
    batch the model reads carries the *book's* text for each marked page, not just
    the PDF's rendering of it. The model then copies its anchor instead of recalling
    one for a document it has never seen."""
    from tests.test_corrections_extract import make_commented_pdf
    monkeypatch.setattr("app.routes.jobs.build_provider",
                        lambda *a, **k: pytest.fail("read-pdf must not call a model"))
    staged = upload(client, "layout.idml")
    # A page of "proof" whose text is the book's own opening paragraph, so the map
    # can place it; the comment is prose, so it goes to the model and carries the
    # book text with it.
    book = story_text(LAYOUT, "ue0")
    page = " ".join(book[:3])
    pdf = make_commented_pdf(
        tmp_path / "aligned.pdf",
        lines=[(72, 700 - 14 * i, chunk) for i, chunk in enumerate(
            [page[i:i + 90] for i in range(0, min(len(page), 540), 90)])],
        annots=[{"subtype": "/Text", "rect": [72, 697, 90, 712],
                 "contents": "Make this read better somehow, your call"}])
    with pdf.open("rb") as fh:
        r = client.post("/api/corrections/read-pdf",
                        data={"file_id": staged["id"]},
                        files={"file": ("aligned.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["batches"]) == 1
    assert "THE BOOK'S OWN TEXT" in body["batches"][0]
    assert "[book text, page 1]" in body["batches"][0]


def test_read_pdf_without_the_idml_still_reads_the_proof(client, tmp_path):
    """The book text is a nicety. No staged file (or an unreadable one) means the
    anchors are quoted from the proof, exactly as before — never a failed read."""
    with _proof(tmp_path / "proof.pdf").open("rb") as fh:
        r = client.post("/api/corrections/read-pdf",
                        data={"file_id": "not-a-real-file"},
                        files={"file": ("proof.pdf", fh.read(),
                                        "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


def test_a_corrections_job_carries_the_page_texts_into_the_run(client):
    """End to end: the pages sent with the job reach the page map, and the report
    says how many of them were placed."""
    src = upload(client, "layout.idml")
    book = story_text(LAYOUT, "ue0")
    job = run_corrections(
        client, src["id"],
        json.dumps([{"find": "several", "replace": "many", "source": "p1-1"}]),
        corrections_pages=json.dumps([book[4]]))
    assert job["state"] == "done", job.get("error")
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert report["pages"] == {"placed": 1, "total": 1, "labeled": 1}


# --- resolving flags from the review screen ------------------------------------

def _flagged_job(client):
    """A finished run with one ambiguous flag ("was" appears twice), and the
    queue its report left for the review screen."""
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "was", "replace": "is"}],
                          corrections_sanity=False,
                          corrections_second_look=False,
                          corrections_escalate=False)
    assert job["state"] == "done", job.get("error")
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    return job, report["queue"][0]


def test_clicking_an_option_resolves_the_flag_over_http(client):
    job, item = _flagged_job(client)
    assert job["flags"] == 1
    option = next(o for o in item["options"] if "room" in o["before"])
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "option_id": option["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["flags"] == 0
    assert body["item"]["resolved"]["kind"] == "option"
    # The corrected file the download serves carries the change.
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room is empty." \
        in story_text(corrected, "ue0")
    # The card's counters moved with it.
    card = client.get(f"/api/jobs/{job['id']}").json()
    assert card["flags"] == 0 and card["applied"] == 1
    # And it cannot land twice.
    again = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                        json={"item_id": item["id"],
                              "option_id": option["id"]})
    assert again.status_code == 409


def test_a_typed_answer_resolves_the_flag_over_http(client, monkeypatch):
    job, item = _flagged_job(client)
    provider = FakeProvider([ProviderResult(
        parsed={"decision": "apply", "find": "the room was empty",
                "replace": "the room stood empty", "context": "",
                "format": "", "note": "as asked"},
        usage=NormalizedUsage(input_tokens=10, output_tokens=5))])
    monkeypatch.setattr("app.routes.jobs._build_resolve_provider",
                        lambda: (provider, "fake-model"))
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"],
                          "text": "make it 'stood empty'"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["resolved"]["kind"] == "typed"
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room stood empty." \
        in story_text(corrected, "ue0")


def test_a_declined_answer_reports_the_reason_and_writes_nothing(client,
                                                                 monkeypatch):
    job, item = _flagged_job(client)
    provider = FakeProvider([ProviderResult(
        parsed={"decision": "decline", "find": "", "replace": "",
                "context": "", "format": "", "note": "that is layout work"},
        usage=NormalizedUsage(input_tokens=10, output_tokens=5))])
    monkeypatch.setattr("app.routes.jobs._build_resolve_provider",
                        lambda: (provider, "fake-model"))
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "text": "move it up a line"})
    assert r.status_code == 422
    assert "layout work" in r.json()["detail"]
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room was empty." \
        in story_text(corrected, "ue0")
    # Still resolvable: the flag was not consumed.
    assert client.get(f"/api/jobs/{job['id']}").json()["flags"] == 1


def test_resolve_requires_exactly_one_of_option_and_text(client):
    job, item = _flagged_job(client)
    for body in ({"item_id": item["id"]},
                 {"item_id": item["id"], "option_id": "x", "text": "y"}):
        r = client.post(f"/api/jobs/{job['id']}/corrections/resolve", json=body)
        assert r.status_code == 400, body


def test_resolving_an_unknown_flag_is_a_404(client):
    job, _ = _flagged_job(client)
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": "nope", "option_id": "x"})
    assert r.status_code == 404


def test_ignore_sets_a_flag_aside_and_reopen_puts_it_back(client):
    job, item = _flagged_job(client)
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "dismiss": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["resolved"]["kind"] == "dismissed"
    assert body["counts"]["flags"] == 0
    assert body["counts"]["applied"] == 0          # nothing was applied
    # The file was never touched.
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room was empty." \
        in story_text(corrected, "ue0")
    # Put it back: the flag returns to the awaiting pile.
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "reopen": True})
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["flags"] == 1
    assert r.json()["item"]["resolved"] is None


def test_the_models_suggestion_applies_in_one_click(client, monkeypatch):
    job, item = _flagged_job(client)
    # A run whose escalate tier advised would have stamped this; inject it the
    # way that run would have, then apply it with one click.
    json_path = Path(job["results_dir"]) / "corrections.json"
    payload = json.loads(json_path.read_text("utf-8"))
    payload["queue"][0]["advice"] = ("the mark is on the room sentence — "
                                     "change that copy")
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    provider = FakeProvider([ProviderResult(
        parsed={"decision": "apply", "find": "the room was empty",
                "replace": "the room is empty", "context": "",
                "format": "", "note": "as the advice says"},
        usage=NormalizedUsage(input_tokens=10, output_tokens=5))])
    monkeypatch.setattr("app.routes.jobs._build_resolve_provider",
                        lambda: (provider, "fake-model"))
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "suggestion": True})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["resolved"]["kind"] == "suggestion"
    # The adjudicator was handed the advice as the instruction to carry out.
    assert "change that copy" in provider.calls[0]["user"]
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room is empty." \
        in story_text(corrected, "ue0")


def test_a_suggestion_click_without_advice_is_refused(client):
    job, item = _flagged_job(client)
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"], "suggestion": True})
    assert r.status_code == 422
    assert "no model suggestion" in r.json()["detail"]


def test_the_manual_editor_reads_and_saves_a_line_over_http(client):
    job, item = _flagged_job(client)
    option = next(o for o in item["options"] if "room" in o["before"])
    loc = f"story_id={option['story_id']}&paragraph={option['paragraph']}"
    state = client.get(
        f"/api/jobs/{job['id']}/corrections/paragraph?{loc}").json()
    assert state["text"] == "She opened the door, the room was empty."
    assert state["runs"] and state["prev_break"] is False
    new = "She opened the door; the room was bare."
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve", json={
        "item_id": item["id"],
        "manual": {
            "story_id": option["story_id"],
            "paragraph": option["paragraph"],
            "expected": state["text"], "text": new,
            "runs": [{"start": new.index("bare"),
                      "end": new.index("bare") + 4,
                      "bold": False, "italic": True}],
            "insert_break_after": True,
        }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["resolved"]["kind"] == "manual"
    assert body["item"]["resolved"]["breaks"]["added_after"] is True
    assert body["counts"]["flags"] == 0 and body["counts"]["applied"] == 1
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    texts = story_text(corrected, "ue0")
    at = texts.index(new)
    assert texts[at + 1] == ""              # the section break landed
    # The italics read back through the same editor endpoint.
    state = client.get(f"/api/jobs/{job['id']}/corrections/paragraph"
                       f"?story_id={option['story_id']}"
                       f"&paragraph={option['paragraph']}").json()
    assert any(r["italic"] for r in state["runs"])
    # And the printable report says what happened.
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert report["resolutions"][0]["kind"] == "manual"


def test_a_stale_manual_save_is_refused_over_http(client):
    job, item = _flagged_job(client)
    option = item["options"][0]
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve", json={
        "item_id": item["id"],
        "manual": {"story_id": option["story_id"],
                   "paragraph": option["paragraph"],
                   "expected": "not what the line says", "text": "x",
                   "runs": []}})
    assert r.status_code == 422
    assert "changed since" in r.json()["detail"]
    assert client.get(f"/api/jobs/{job['id']}").json()["flags"] == 1


def test_the_paragraph_endpoint_relocates_by_expected_text(client):
    job, item = _flagged_job(client)
    room = "She opened the door, the room was empty."
    # POST with the wrong index but the right text: relocated, not misread.
    r = client.post(f"/api/jobs/{job['id']}/corrections/paragraph",
                    json={"story_id": "ue0", "paragraph": 0, "expect": room})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == room and r.json()["paragraph"] == 2
    # Text the story does not carry: refused, not guessed.
    r = client.post(f"/api/jobs/{job['id']}/corrections/paragraph",
                    json={"story_id": "ue0", "paragraph": 0,
                          "expect": "never in the book"})
    assert r.status_code == 409


def test_a_touchup_edits_an_applied_change_without_moving_counts(client):
    job = run_corrections(client, upload(client, "layout.idml")["id"],
                          [{"find": "Their were", "replace": "There were"}],
                          corrections_sanity=False,
                          corrections_second_look=False,
                          corrections_escalate=False)
    assert job["applied"] == 1 and job["flags"] == 0
    line = "There were several mistakes here to find."
    r = client.post(f"/api/jobs/{job['id']}/corrections/edit", json={
        "manual": {"story_id": "ue0", "paragraph": 4, "expected": line,
                   "text": line.replace("mistakes", "errors"), "runs": []}})
    assert r.status_code == 200, r.text
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "There were several errors here to find." \
        in story_text(corrected, "ue0")
    # Counts unmoved; the report carries the touch-up.
    card = client.get(f"/api/jobs/{job['id']}").json()
    assert card["applied"] == 1 and card["flags"] == 0
    report = client.get(f"/api/jobs/{job['id']}/corrections").json()
    assert report["resolutions"][0]["touchup"] is True
    assert report["changes"][-1]["instruction"] == "edited by hand in review"


def test_suggest_lands_a_placement_the_designer_accepts(client, monkeypatch):
    job, item = _flagged_job(client)
    provider = FakeProvider([ProviderResult(
        parsed={"decision": "apply", "find": "the room was empty",
                "replace": "the room is empty", "context": "",
                "format": "", "note": "the mark reads as the room copy"},
        usage=NormalizedUsage(input_tokens=10, output_tokens=5))])
    monkeypatch.setattr("app.routes.jobs._build_resolve_provider",
                        lambda: (provider, "fake-model"))
    r = client.post(f"/api/jobs/{job['id']}/corrections/suggest",
                    json={"item_id": item["id"]})
    assert r.status_code == 200, r.text
    options = r.json()["item"]["options"]
    suggested = [o for o in options if o.get("suggested")]
    assert len(suggested) == 1
    assert suggested[0]["id"] == f"{item['id']}-s1"
    # Nothing applied yet.
    corrected = Path(job["results_dir"]) / "layout_corrected.idml"
    assert "She opened the door, the room was empty." \
        in story_text(corrected, "ue0")
    # Accepting it is the ordinary option click — recorded as a suggestion.
    r = client.post(f"/api/jobs/{job['id']}/corrections/resolve",
                    json={"item_id": item["id"],
                          "option_id": suggested[0]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["resolved"]["kind"] == "suggestion"
    assert "She opened the door, the room is empty." \
        in story_text(corrected, "ue0")


def test_a_declined_suggest_returns_the_reason(client, monkeypatch):
    job, item = _flagged_job(client)
    provider = FakeProvider([ProviderResult(
        parsed={"decision": "decline", "find": "", "replace": "",
                "context": "", "format": "", "note": "nothing settles it"},
        usage=NormalizedUsage(input_tokens=10, output_tokens=5))])
    monkeypatch.setattr("app.routes.jobs._build_resolve_provider",
                        lambda: (provider, "fake-model"))
    r = client.post(f"/api/jobs/{job['id']}/corrections/suggest",
                    json={"item_id": item["id"]})
    assert r.status_code == 422
    assert "nothing settles it" in r.json()["detail"]
