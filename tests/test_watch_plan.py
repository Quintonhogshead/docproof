"""The marketing-plan stage of a tick, end to end, with a fake Drive and HubSpot.

The properties worth holding this to: the plan gates on its *own* HubSpot
property (the "Marketing Plan" field, Needed -> Uploaded) and never on the status
dropdown the format and copy stages share; it reads the author's blurbs and
questionnaire from the folder and grounds the plan in them; a hold-mode run
generates and waits without shipping; delivery — auto, or once approved — puts
exactly one plan document in the folder and moves the property on; and none of it
runs the model twice, even when a pass dies between generating and delivering.
Everything is `mock=True`, so no model and no network.
"""
from __future__ import annotations

import pytest

from app.jobs import JobStore
from app.settings import Paths
from app.watch import plan as planlib
from app.watch import tick as ticklib
from app.watch.drive import DOCX_MIME, DriveFile
from app.watch.settings import WatchSettings
from app.watch.stages import (OUTPUT_PROP, PLAN_DONE, PLAN_FAILED, PLAN_PENDING,
                              PLAN_PROP, SOURCE_PROP, STATE_PROP)
from app.watch.state import WatchState

from .conftest import FIXTURES
from .fakes import drive_entry, fake_drive, http_error

FOLDER = "1AbCdEfGhIjKlMnOp"
MANUSCRIPT = (FIXTURES / "googledoc.docx").read_bytes()
PLAN_DOC = "Wolves - marketing plan.docx"


def plan_ws(**over) -> WatchSettings:
    fields = dict(folder_id=FOLDER, model="claude-haiku-4-5",
                  client_id="client-1", client_secret="secret-1",
                  hubspot_enabled=True, hubspot_object="0-970",
                  hubspot_key_property="author_last_name",
                  hubspot_status_property="docproof",
                  hubspot_format_ready_value="Ready for Formatting",
                  hubspot_format_done_value="Formatting Complete",
                  plan_enabled=True,
                  hubspot_plan_property="marketing_plan",
                  hubspot_plan_needed_value="Needed",
                  hubspot_plan_done_value="Uploaded",
                  hubspot_pen_property="pen_name")
    fields.update(over)
    return WatchSettings(**fields)


def plan_needed(last_name, *, pen="", **extra):
    props = {"marketing_plan": "Needed", "author_last_name": last_name, **extra}
    if pen:
        props["pen_name"] = pen
    return props


def folder(**files) -> dict:
    return {name.replace("_", "-", 1): entry for name, entry in files.items()}


def run(home, ws, opener, **kw):
    return ticklib.tick(home, ws, opener=opener,
                        get_key=lambda name: "refresh-1", mock=True, **kw)


def uploads_in(opener) -> dict:
    return {entry["name"]: entry for entry in opener.files.values()
            if entry.get("appProperties", {}).get(OUTPUT_PROP)}


def plan_docs(opener, name=PLAN_DOC) -> list:
    """Every uploaded copy of one plan document — a list, not a dict, so a
    duplicate upload is visible rather than deduped by name."""
    return [e for e in opener.files.values()
            if e.get("appProperties", {}).get(OUTPUT_PROP) and e["name"] == name]


def hs_props(opener, key="Wolves"):
    return opener.hubspot[f"hs-{key}"]["properties"]


def plan_job(home):
    rec = WatchState.load(home / "state.json").get("f-1")
    return JobStore(Paths(home)).get(rec.plan_job_id), rec


# --- gathering the inputs (pure, no network) ----------------------------------

def _df(name, fid, props=None):
    return DriveFile(id=fid, name=name, mime_type=DOCX_MIME,
                     app_properties=props or {})


def test_find_input_files_picks_blurb_and_questionnaire_by_pattern():
    ws = plan_ws()
    manuscript = _df("Wolves.docx", "f-1")
    listing = [manuscript,
               _df("Wolves - back cover.docx", "b-1"),
               _df("Wolves - PNQ answers.docx", "q-1"),
               _df("unrelated cover art.png", "x-1")]
    blurb, form = planlib.find_input_files(listing, manuscript, ws)
    assert blurb.id == "b-1"        # matched "back.?cover"
    assert form.id == "q-1"         # matched "pnq"


def test_find_input_files_skips_the_manuscript_and_docproof_outputs():
    ws = plan_ws(plan_blurb_pattern="Wolves", plan_form_pattern="Wolves")
    manuscript = _df("Wolves.docx", "f-1")
    listing = [manuscript,
               _df("Wolves - marketing plan.docx", "o-1", {OUTPUT_PROP: "1"}),
               _df("Wolves - blurbs.docx", "b-1")]
    blurb, form = planlib.find_input_files(listing, manuscript, ws)
    # Never the manuscript itself, never a file DocProof wrote — only the sibling.
    assert blurb.id == "b-1" and form.id == "b-1"


def test_find_input_files_empty_pattern_disables_that_input():
    ws = plan_ws(plan_blurb_pattern="", plan_form_pattern="questionnaire")
    manuscript = _df("Wolves.docx", "f-1")
    listing = [manuscript, _df("Wolves - questionnaire.md", "q-1")]
    blurb, form = planlib.find_input_files(listing, manuscript, ws)
    assert blurb is None and form.id == "q-1"


# --- auto mode ----------------------------------------------------------------

def test_auto_mode_writes_uploads_and_moves_the_book_on(tmp_path):
    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves", pen="Q. Penn")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.planned == ["Wolves.docx"]
    placed = uploads_in(opener)
    assert set(placed) == {PLAN_DOC}
    assert placed[PLAN_DOC]["parents"] == [FOLDER]
    assert placed[PLAN_DOC]["appProperties"][SOURCE_PROP] == "f-1"
    # Its own marker, and the format marker is never touched.
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE
    assert STATE_PROP not in opener.files["f-1"]["appProperties"]
    # HubSpot moved to the plan's Uploaded value, on the plan's own property.
    assert hs_props(opener)["marketing_plan"] == "Uploaded"
    job, rec = plan_job(tmp_path)
    assert rec.plan_hubspot_id == "hs-Wolves" and rec.plan_hubspot_done
    assert job.is_plan and job.plan_author == "Q. Penn"   # pen captured off the record


def test_reads_blurbs_and_questionnaire_from_the_folder(tmp_path):
    """The blurb and questionnaire siblings are found by pattern, read, and baked
    into the plan job — grounding the plan in what the author told the press."""
    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(
        folder(f_1=drive_entry("Wolves.docx"),
               fb_1=drive_entry("Wolves - blurb.md"),
               fq_1=drive_entry("Wolves - questionnaire.md")),
        docx=MANUSCRIPT, hubspot={"Wolves": plan_needed("Wolves")})
    # `.md` siblings hit the plain-text read path, so their exact text lands.
    opener.content["fb-1"] = b"Praise for Wolves: a quiet triumph."
    opener.content["fq-1"] = b"Q: Where do you live? A: Missoula, Montana."

    run(tmp_path, ws, opener)

    job, _ = plan_job(tmp_path)
    assert job.plan_blurbs == "Praise for Wolves: a quiet triumph."
    assert job.plan_questionnaire == "Q: Where do you live? A: Missoula, Montana."


def test_auto_mode_does_not_regenerate_on_a_second_pass(tmp_path):
    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})

    run(tmp_path, ws, opener)
    before = WatchState.load(tmp_path / "state.json").get("f-1").plan_job_id
    report2 = run(tmp_path, ws, opener)

    assert report2.planned == []           # the marker keeps it out
    after = WatchState.load(tmp_path / "state.json").get("f-1").plan_job_id
    assert before == after                 # same job, never re-run


# --- hold mode ----------------------------------------------------------------

def test_hold_mode_generates_and_waits_without_shipping(tmp_path):
    ws = plan_ws(plan_auto_upload=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.planned == ["Wolves.docx"]
    assert uploads_in(opener) == {}        # nothing shipped
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_PENDING
    assert hs_props(opener)["marketing_plan"] == "Needed"   # unmoved
    job, rec = plan_job(tmp_path)
    assert rec.plan_marked == "pending"
    assert job.state == "done" and job.approval == "pending"


def test_hold_then_approved_delivers_on_the_next_tick(tmp_path):
    ws = plan_ws(plan_auto_upload=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})

    run(tmp_path, ws, opener)              # generate, hold
    store = JobStore(Paths(tmp_path))
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    store.update(rec.plan_job_id, approval="approved")   # what the panel does

    report = run(tmp_path, ws, opener)     # the next tick delivers

    assert set(uploads_in(opener)) == {PLAN_DOC}
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE
    assert hs_props(opener)["marketing_plan"] == "Uploaded"
    assert report.planned == []            # delivered, not regenerated


# --- coexistence & guards -----------------------------------------------------

def test_plan_gates_on_its_own_property_beside_formatting(tmp_path):
    """Two books: one flagged for a plan, one for formatting. Each stage takes
    its own on its own property and leaves the other's marker alone."""
    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(
        folder(f_1=drive_entry("Wolves.docx"), f_2=drive_entry("Bears.docx")),
        docx=MANUSCRIPT,
        hubspot={"Wolves": plan_needed("Wolves"),
                 "Bears": {"docproof": "Ready for Formatting",
                           "author_last_name": "Bears"}})

    report = run(tmp_path, ws, opener)

    assert report.planned == ["Wolves.docx"]
    assert report.prepped == ["Bears.docx"]
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE
    assert STATE_PROP not in opener.files["f-1"]["appProperties"]
    assert opener.files["f-2"]["appProperties"][STATE_PROP] == "formatted"
    assert PLAN_PROP not in opener.files["f-2"]["appProperties"]


def test_read_only_hubspot_delivers_but_leaves_the_property(tmp_path):
    ws = plan_ws(plan_auto_upload=True, hubspot_write_back=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.planned == ["Wolves.docx"]
    assert set(uploads_in(opener)) == {PLAN_DOC}      # plan still delivered
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE
    assert hs_props(opener)["marketing_plan"] == "Needed"   # CRM untouched


def test_plan_on_without_hubspot_refuses_the_pass(tmp_path):
    ws = plan_ws(hubspot_enabled=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT)
    with pytest.raises(ticklib.NotConfigured):
        run(tmp_path, ws, opener)


def test_plan_on_without_its_values_refuses_the_pass(tmp_path):
    ws = plan_ws(hubspot_plan_needed_value="")     # a gate with nothing to ask
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})
    with pytest.raises(ticklib.NotConfigured):
        run(tmp_path, ws, opener)


def test_oversize_book_emails_a_person_and_stops_retrying(tmp_path, monkeypatch):
    """A book too big for a one-pass plan is not retried three times: the tick
    emails a person the size and how to run it by hand, marks the book failed so
    the next tick leaves it be, and does not count it as a blind failure."""
    from app.watch import notify
    from docproof.promo import PromoTooLarge

    def too_big(*a, **k):
        raise PromoTooLarge("way over", tokens=250_000, limit=180_000,
                            words=190_000)
    monkeypatch.setattr("docproof.promo.prepare_plan", too_big)

    calls = []
    monkeypatch.setattr(notify, "plan_too_large",
                        lambda token, ws, file, job, **kw: calls.append(
                            (file.name, job.error_kind, job.words,
                             job.input_tokens)) or True)

    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})

    report = run(tmp_path, ws, opener)

    assert calls == [("Wolves.docx", "oversize", 190_000, 250_000)]
    assert report.planned == []
    assert report.failed == []             # emailed directly, not a blind fail
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_FAILED

    report2 = run(tmp_path, ws, opener)    # the marker keeps it out
    assert report2.planned == [] and len(calls) == 1


def test_plan_stands_aside_under_subfolder_mode(tmp_path):
    ws = plan_ws(plan_auto_upload=True, subfolders_enabled=True,
                 hubspot_first_property="first", hubspot_last_property="last")
    opener = fake_drive(hubspot={})        # subfolder discovery finds nothing
    report = run(tmp_path, ws, opener)
    assert report.planned == []            # skipped, no error


def test_disabled_is_a_no_op(tmp_path):
    """The default posture: plan off, and a folder full of ready books is left
    exactly as it was found."""
    ws = plan_ws(plan_enabled=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": plan_needed("Wolves")})
    report = run(tmp_path, ws, opener)
    assert report.planned == []
    assert uploads_in(opener) == {}
    assert PLAN_PROP not in opener.files["f-1"]["appProperties"]


# --- crashes: the model is never paid for twice -------------------------------

def test_crash_after_generate_before_upload_never_repays(tmp_path, monkeypatch):
    """A pass that dies after writing the plan but before it lands re-delivers on
    the next tick without asking the model anything — the plan is on disk and the
    state file points at it."""
    runs = {"n": 0}
    real = planlib.run_job

    def counting(*a, **k):
        runs["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(planlib, "run_job", counting)

    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        fail={"upload": http_error(503, "busy")},
                        hubspot={"Wolves": plan_needed("Wolves")})

    r1 = run(tmp_path, ws, opener)
    assert r1.failed and not uploads_in(opener)          # generated, upload died
    before = WatchState.load(tmp_path / "state.json").get("f-1").plan_job_id
    assert before and runs["n"] == 1

    r2 = run(tmp_path, ws, opener)                        # upload works now
    assert set(uploads_in(opener)) == {PLAN_DOC}
    after = WatchState.load(tmp_path / "state.json").get("f-1").plan_job_id
    assert after == before and runs["n"] == 1            # never re-generated
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE


def test_crash_after_upload_before_mark_adopts_no_duplicate(tmp_path):
    """A pass that uploaded the plan and moved HubSpot on, then died before the
    Drive marker, does not upload a second copy on the next tick — it adopts the
    one already there and writes only the marker."""
    ws = plan_ws(plan_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        fail={"patch": http_error(503, "busy")},
                        hubspot={"Wolves": plan_needed("Wolves")})

    r1 = run(tmp_path, ws, opener)
    assert r1.failed
    assert len(plan_docs(opener)) == 1                   # uploaded once
    assert PLAN_PROP not in opener.files["f-1"]["appProperties"]   # marker died
    assert hs_props(opener)["marketing_plan"] == "Uploaded"       # writeback landed

    run(tmp_path, ws, opener)                            # marker PATCH works now
    assert len(plan_docs(opener)) == 1                   # adopted, not duplicated
    assert opener.files["f-1"]["appProperties"][PLAN_PROP] == PLAN_DONE
