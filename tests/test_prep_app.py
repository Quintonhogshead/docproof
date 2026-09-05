"""Prep and the spending dashboard, driven through HTTP with a fake provider.

Same rules as test_app.py: nothing here reaches a vendor and nothing sleeps.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobRunner, JobStore
from app.main import create_app
from app.settings import Paths, Settings

from .conftest import FIXTURES
from .fakes import LABELS, USAGE, TaggingProvider

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


@pytest.fixture
def provider():
    return TaggingProvider()


@pytest.fixture
def client(tmp_path, provider, monkeypatch):
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.key_status", lambda: {
        "anthropic": {"configured": True, "source": "environment"},
        "openai": {"configured": True, "source": "environment"},
        "gemini": {"configured": True, "source": "environment"}})

    app = create_app(tmp_path, start_runner=False)
    app.state.settings.output_dir = str(tmp_path / "out")
    app.state.runner.start()
    with TestClient(app) as c:
        c.app_state = app.state
        yield c
    # Join the worker rather than only signalling it, so no job outlives the
    # stubs this fixture installed (see tests/test_app.py).
    app.state.runner.stop(join=30)


def upload(client, name="googledoc.docx"):
    with (FIXTURES / name).open("rb") as fh:
        response = client.post("/api/files", files={"files": (name, fh.read())})
    return response.json()["files"][0]


def start_prep(client, file_id, output="indesign", **extra):
    body = {"file_ids": [file_id], "model": "claude-haiku-4-5", "kind": "prep",
            "prep_output": output, **extra}
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


def test_a_manuscript_with_tracked_changes_is_refused_by_prep(client):
    staged = upload(client, "tracked.docx")
    # Review and prep both refuse a manuscript with unresolved tracked changes.
    assert staged["can_review"] is False and staged["can_prep"] is False
    assert "tracked changes" in staged["prep_error"]
    # Promo only reads, so it takes the file as it stands — which means the file
    # is usable, but for promo alone.
    assert staged["can_promo"] is True and staged["ok"] is True


# --- running ------------------------------------------------------------------

def test_prep_produces_a_tagged_file_and_its_notes(client):
    job = start_prep(client, upload(client)["id"])
    assert job["state"] == "done", job.get("error")
    assert job["is_prep"] and job["verified"] is True
    assert job["tagged"] == 10
    assert job["flags"] >= 1                      # the byline the model raised

    results = Path(job["results_dir"])
    assert (results / "tagged_googledoc.idml").is_file()
    assert (results / "prep_notes.md").is_file()
    assert not (results / "tracked_googledoc.docx").exists()


def test_the_output_toggle_decides_which_files_are_written(client):
    both = start_prep(client, upload(client)["id"], output="both")
    results = Path(both["results_dir"])
    assert (results / "tagged_googledoc.idml").is_file()
    assert (results / "tracked_googledoc.docx").is_file()

    tracked = start_prep(client, upload(client)["id"], output="tracked")
    results = Path(tracked["results_dir"])
    assert (results / "tracked_googledoc.docx").is_file()
    assert not (results / "tagged_googledoc.idml").exists()


def test_the_book_output_writes_the_reading_copy(client):
    """The book output is a plain reading copy — written, verified, downloadable.
    It is the plain manuscript by default (Times New Roman 12pt), which reads no
    subject or title, so none is detected; the subject-driven paperback design is
    exercised in test_prep_book."""
    job = start_prep(client, upload(client)["id"], output="book")
    assert job["state"] == "done", job.get("error")
    assert job["verified"] is True
    results = Path(job["results_dir"])
    assert (results / "book_googledoc.docx").is_file()
    assert not (results / "tagged_googledoc.idml").exists()
    # The plain design needs no subject/title/author, so none is detected.
    assert job["prep_book"] == {}

    r = client.get(f"/api/jobs/{job['id']}/file/book")
    assert r.status_code == 200
    assert "book_googledoc.docx" in r.headers["content-disposition"]
    # The generic "open it" button resolves to the book copy too.
    assert client.get(f"/api/jobs/{job['id']}/file/document").status_code == 200
    notes = client.get(f"/api/jobs/{job['id']}/prep").json()
    assert notes["files"]["book"] is True


def test_an_unknown_prep_output_is_refused(client):
    staged = upload(client)
    r = client.post("/api/jobs", json={
        "file_ids": [staged["id"]], "model": "claude-haiku-4-5",
        "kind": "prep", "prep_output": "sculpture"})
    assert r.status_code == 400


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
    for which, expected in (("indesign", "tagged_googledoc.idml"),
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
    assert notes["files"] == {"book": False, "indesign": True,
                              "tracked": False}


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


# --- opening the InDesign (IDML) file ------------------------------------------

def test_opening_the_indesign_file_opens_the_idml(client, monkeypatch):
    """The InDesign deliverable is an IDML that IS the placed document — InDesign
    turns it into an INDD on open — so there is no Place step. 'place' just opens
    the .idml; no template, no InDesign automation."""
    opened = []
    monkeypatch.setattr("app.routes.common.open_path",
                        lambda path, *, reveal=False: opened.append(Path(path)))
    job = start_prep(client, upload(client)["id"])
    resp = client.post(f"/api/jobs/{job['id']}/place")
    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "tagged_googledoc.idml"
    assert opened and opened[0].name == "tagged_googledoc.idml"
    # It opens the file this job wrote, in place.
    assert opened[0].parent == Path(job["results_dir"])


def test_a_review_job_has_no_indesign_file(client):
    """A reviewed document was never prepared for layout, so there is nothing
    to open in InDesign."""
    staged = upload(client)
    job = client.post("/api/jobs", json={"file_ids": [staged["id"]],
                                         "model": "claude-haiku-4-5",
                                         "mode": "now"}).json()["jobs"][0]
    client.app_state.runner.wait_idle()
    resp = client.post(f"/api/jobs/{job['id']}/place")
    assert resp.status_code == 400 and "prepared" in resp.json()["detail"]


def test_the_reflow_script_is_downloadable(client):
    """The one-time reflow script the designer installs to flow the book across
    pages after opening the IDML."""
    resp = client.get("/api/prep/reflow-script")
    assert resp.status_code == 200
    assert "DocProof-reflow.jsx" in resp.headers["content-disposition"]
    assert "nextTextFrame" in resp.text and "Reflow" in resp.text


def _idml_bytes() -> bytes:
    return (FIXTURES / "layout.idml").read_bytes()


def test_the_house_template_can_be_uploaded_and_reverted(client):
    """The house InDesign template is data, like the style guide: replace it in
    the app and prep flows into it from the next document on."""
    body = client.get("/api/prep/template").json()
    assert body["ok"] and body["using_override"] is False

    r = client.post("/api/prep/template",
                    files={"file": ("House.idml", _idml_bytes())})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["using_override"] is True
    assert (client.app_state.paths.prep / "house_template.idml").is_file()

    reverted = client.delete("/api/prep/template").json()
    assert reverted["using_override"] is False
    assert not (client.app_state.paths.prep / "house_template.idml").exists()


def test_a_file_that_is_not_an_idml_is_refused_as_a_template(client):
    wrong_ext = client.post("/api/prep/template",
                            files={"file": ("notes.txt", b"not an idml")})
    assert wrong_ext.status_code == 400
    not_a_zip = client.post("/api/prep/template",
                            files={"file": ("x.idml", b"still not a zip")})
    assert not_a_zip.status_code == 400 and "IDML" in not_a_zip.json()["detail"]
    # A refused upload leaves nothing half-written under the name prep reads.
    assert not list(client.app_state.paths.prep.glob("*.uploading"))
    assert client.get("/api/prep/template").json()["using_override"] is False


def test_an_uploaded_template_is_what_prep_flows_into(client):
    client.post("/api/prep/template",
                files={"file": ("House.idml", _idml_bytes())})
    job = start_prep(client, upload(client)["id"], output="indesign")
    assert job["state"] == "done", job.get("error")
    assert job["verified"] is True
    assert (Path(job["results_dir"]) / "tagged_googledoc.idml").is_file()


def test_the_template_is_remembered_between_launches(client, tmp_path):
    template = tmp_path / "House prose.indd"
    template.write_bytes(b"template")
    client.put("/api/settings", json={"indesign_template": str(template)})
    assert client.get("/api/settings").json()["settings"]["indesign_template"] \
        == str(template)
    saved = json.loads((client.app_state.paths.settings_file).read_text("utf-8"))
    assert saved["indesign_template"] == str(template)


# --- submitting a style guide from the app -------------------------------------

def _shipped_text(client) -> str:
    return Path(client.get("/api/prep/styles").json()["shipped_path"]).read_text(
        "utf-8")


def _submit_sheet(client, text, name="mine.yaml"):
    return client.post("/api/prep/styles/sheet",
                       files={"file": (name, text.encode("utf-8"),
                                       "application/x-yaml")})


def test_a_style_guide_can_be_handed_over_in_the_app(client):
    """Same effect as dropping the file into the folder, without asking anyone
    to find the folder."""
    resp = _submit_sheet(client, _shipped_text(client).replace(
        "Atmosphere Press prose template", "Riverbend Books"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Riverbend Books"
    assert resp.json()["using_override"] is True

    # Whatever it was called on the way in, it lands under the name prep looks
    # for — and the next manuscript is tagged with it.
    paths = client.app_state.paths
    assert (paths.prep / "house_styles.yaml").is_file()
    job = start_prep(client, upload(client)["id"])
    notes = client.get(f"/api/jobs/{job['id']}/prep").json()
    assert notes["style_sheet"]["name"] == "Riverbend Books"


def test_a_broken_style_guide_is_refused_and_changes_nothing(client):
    """It has to fail here, in front of the person who chose the file, rather
    than at 11pm in the middle of a run."""
    _submit_sheet(client, _shipped_text(client).replace(
        "Atmosphere Press prose template", "Riverbend Books"))

    doubled = _shipped_text(client).replace('id: Copyright', 'id: TitlePage')
    resp = _submit_sheet(client, doubled)
    assert resp.status_code == 400
    assert "TitlePage" in resp.json()["detail"]
    # The sheet that was working a moment ago is still the one in force, and
    # no half-written file was left behind under a name prep might read.
    assert client.get("/api/prep/styles").json()["name"] == "Riverbend Books"
    assert not list(client.app_state.paths.prep.glob("*.uploading"))


def test_a_file_that_is_not_a_style_guide_at_all_is_refused(client):
    resp = _submit_sheet(client, "just: some\n  broken: yaml\n")
    assert resp.status_code == 400
    assert client.get("/api/prep/styles").json()["using_override"] is False


def test_going_back_to_the_shipped_style_guide(client):
    _submit_sheet(client, _shipped_text(client).replace(
        "Atmosphere Press prose template", "Riverbend Books"))
    body = client.delete("/api/prep/styles/sheet").json()
    assert body["name"] == "Atmosphere Press prose template"
    assert body["using_override"] is False
    assert not (client.app_state.paths.prep / "house_styles.yaml").exists()


# --- adjusting how the styles look ---------------------------------------------

def _formats(client) -> dict:
    return {s["name"]: s["format"]
            for s in client.get("/api/prep/styles").json()["styles"]}


def test_the_styles_come_back_with_the_formatting_to_adjust(client):
    chapter = _formats(client)["chapter # / title"]
    assert chapter["size"] == 18 and chapter["page_break_before"] is True


def test_adjusting_a_style_reaches_the_tagged_document(client):
    """The point of the sliders: a change made in Settings is in the IDML the
    designer opens, not just in a preference file."""
    resp = client.put("/api/prep/styles/format", json={
        "styles": {"chapter # / title": {"size": 24, "space_before": 36}},
        "scene_break_glyph": "# # #"})
    assert resp.status_code == 200, resp.text
    assert _formats(client)["chapter # / title"]["size"] == 24
    assert client.get("/api/prep/styles").json()["glyph"] == "# # #"

    job = start_prep(client, upload(client)["id"])
    tagged = Path(job["results_dir"]) / "tagged_googledoc.idml"
    with zipfile.ZipFile(tagged) as z:
        styles_xml = z.read("Resources/Styles.xml").decode("utf-8")
    # The chapter style, by its house name, carries the adjusted 24pt size.
    assert 'Name="chapter # / title"' in styles_xml
    assert 'PointSize="24"' in styles_xml


def test_adjusting_styles_never_touches_what_indesign_matches_on(client):
    """Names and ids are the contract with the template and with the model, so
    the editor cannot reach them however the request is phrased."""
    before = client.get("/api/prep/styles").json()["styles"]
    client.put("/api/prep/styles/format",
               json={"styles": {"body para": {"indent": 24}},
                     "trim": "6 x 9"})
    after = client.get("/api/prep/styles").json()["styles"]

    assert [(s["name"], s["id"], s["assign"], s["describe"]) for s in before] \
        == [(s["name"], s["id"], s["assign"], s["describe"]) for s in after]
    assert _formats(client)["body para"]["indent"] == 24
    assert client.get("/api/prep/styles").json()["trim"] == "6 x 9"


def test_clearing_a_format_value_is_not_the_same_as_zeroing_it(client):
    client.put("/api/prep/styles/format",
               json={"styles": {"body para": {"clear": ["indent"]}}})
    assert "indent" not in _formats(client)["body para"]


def test_adjusting_a_style_that_is_not_in_the_sheet_says_so(client):
    resp = client.put("/api/prep/styles/format",
                      json={"styles": {"drop cap": {"size": 30}}})
    assert resp.status_code == 400
    assert "drop cap" in resp.json()["detail"]


def test_a_size_no_template_would_want_is_refused(client):
    resp = client.put("/api/prep/styles/format",
                      json={"styles": {"body para": {"size": 400}}})
    assert resp.status_code == 422


def test_adjustments_apply_to_whichever_sheet_is_in_force(client):
    """Edits land on the style set the user actually submitted, and reverting
    to the shipped one throws the edits away with it — one file, one undo."""
    _submit_sheet(client, _shipped_text(client).replace(
        "Atmosphere Press prose template", "Riverbend Books"))
    client.put("/api/prep/styles/format",
               json={"styles": {"body para": {"size": 11}}})

    body = client.get("/api/prep/styles").json()
    assert body["name"] == "Riverbend Books"
    assert _formats(client)["body para"]["size"] == 11

    client.delete("/api/prep/styles/sheet")
    assert "size" not in _formats(client)["body para"]


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


# --- which interior a prep run uses -------------------------------------------

def test_prep_book_output_defaults_to_the_plain_manuscript(tmp_path):
    """The book output is the plain manuscript by default — a manual run and the
    watched folder alike. The watched folder keeps its own design field so it
    stays plain even if the app's default is later pointed at the paperback."""
    store = JobStore(Paths(tmp_path).ensure())
    runner = JobRunner(store, Settings(), config_path=CONFIG)
    base = dict(filename="simple.docx",
                source_path=str(FIXTURES / "simple.docx"),
                model="claude-sonnet-5", mode="now", kind="prep",
                prep_output="book")
    watched = store.save(Job(id="w1", source="watch", **base))
    manual = store.save(Job(id="a1", source="app", **base))
    assert (runner.config_for(watched).prep.book_design
            == "prep/book_manuscript.yaml")
    assert (runner.config_for(manual).prep.book_design
            == "prep/book_manuscript.yaml")
