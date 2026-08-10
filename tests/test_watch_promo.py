"""The promo stage of a tick, end to end, with a fake Drive and HubSpot.

The properties worth holding this to: promo gates on its own HubSpot value and
never on formatting's; a hold-mode run generates and waits without shipping;
delivery — auto, or once approved — puts exactly the two documents in the folder
and moves the book on; and none of it runs twice. Everything is `mock=True`, so
no model and no network.
"""
from __future__ import annotations

import pytest

from app.jobs import JobStore
from app.settings import Paths
from app.watch import tick as ticklib
from app.watch.settings import WatchSettings
from app.watch.stages import (OUTPUT_PROP, PROMO_DONE, PROMO_PENDING, PROMO_PROP,
                              SOURCE_PROP, STATE_PROP)
from app.watch.state import WatchState

from .conftest import FIXTURES
from .fakes import drive_entry, fake_drive

FOLDER = "1AbCdEfGhIjKlMnOp"
MANUSCRIPT = (FIXTURES / "googledoc.docx").read_bytes()


def promo_ws(**over) -> WatchSettings:
    fields = dict(folder_id=FOLDER, model="claude-haiku-4-5",
                  client_id="client-1", client_secret="secret-1",
                  hubspot_enabled=True, hubspot_object="0-970",
                  hubspot_key_property="author_last_name",
                  hubspot_status_property="docproof",
                  hubspot_format_ready_value="Ready for Formatting",
                  hubspot_format_done_value="Formatting Complete",
                  promo_enabled=True,
                  hubspot_promo_ready_value="Ready for Promo text",
                  hubspot_promo_done_value="Promo text finished")
    fields.update(over)
    return WatchSettings(**fields)


def promo_ready(last_name, **extra):
    return {"docproof": "Ready for Promo text",
            "author_last_name": last_name, **extra}


def folder(**files) -> dict:
    return {name.replace("_", "-", 1): entry for name, entry in files.items()}


def run(home, ws, opener, **kw):
    return ticklib.tick(home, ws, opener=opener,
                        get_key=lambda name: "refresh-1", mock=True, **kw)


def uploads_in(opener) -> dict:
    return {entry["name"]: entry for entry in opener.files.values()
            if entry.get("appProperties", {}).get(OUTPUT_PROP)}


def hs_props(opener, key="Wolves"):
    return opener.hubspot[f"hs-{key}"]["properties"]


# --- auto mode ----------------------------------------------------------------

def test_auto_mode_writes_uploads_and_moves_the_book_on(tmp_path):
    ws = promo_ws(promo_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.promoted == ["Wolves.docx"]
    placed = uploads_in(opener)
    assert set(placed) == {"Wolves - teaser.docx", "Wolves - social posts.docx"}
    assert placed["Wolves - teaser.docx"]["parents"] == [FOLDER]
    assert placed["Wolves - teaser.docx"]["appProperties"][SOURCE_PROP] == "f-1"
    # Its own marker, and the format marker is never touched.
    assert opener.files["f-1"]["appProperties"][PROMO_PROP] == PROMO_DONE
    assert STATE_PROP not in opener.files["f-1"]["appProperties"]
    # HubSpot moved to the promo done value.
    assert hs_props(opener)["docproof"] == "Promo text finished"
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.promo_hubspot_id == "hs-Wolves" and rec.promo_hubspot_done


def test_auto_mode_does_not_regenerate_on_a_second_pass(tmp_path):
    ws = promo_ws(promo_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves")})

    run(tmp_path, ws, opener)
    before = WatchState.load(tmp_path / "state.json").get("f-1").promo_job_id
    report2 = run(tmp_path, ws, opener)

    assert report2.promoted == []          # the marker keeps it out
    after = WatchState.load(tmp_path / "state.json").get("f-1").promo_job_id
    assert before == after                 # same job, never re-run


# --- hold mode ----------------------------------------------------------------

def test_hold_mode_generates_and_waits_without_shipping(tmp_path):
    ws = promo_ws(promo_auto_upload=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.promoted == ["Wolves.docx"]
    assert uploads_in(opener) == {}        # nothing shipped
    assert opener.files["f-1"]["appProperties"][PROMO_PROP] == PROMO_PENDING
    assert hs_props(opener)["docproof"] == "Ready for Promo text"   # unmoved
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.promo_marked == "pending"
    job = JobStore(Paths(tmp_path)).get(rec.promo_job_id)
    assert job.state == "done" and job.approval == "pending"


def test_hold_then_approved_delivers_on_the_next_tick(tmp_path):
    ws = promo_ws(promo_auto_upload=False)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves")})

    run(tmp_path, ws, opener)              # generate, hold
    store = JobStore(Paths(tmp_path))
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    store.update(rec.promo_job_id, approval="approved")   # what the panel does

    report = run(tmp_path, ws, opener)     # the next tick delivers

    assert set(uploads_in(opener)) == {"Wolves - teaser.docx",
                                       "Wolves - social posts.docx"}
    assert opener.files["f-1"]["appProperties"][PROMO_PROP] == PROMO_DONE
    assert hs_props(opener)["docproof"] == "Promo text finished"
    assert report.promoted == []           # delivered, not regenerated


# --- coexistence & guards -----------------------------------------------------

def test_promo_and_format_each_handle_their_own_ready_book(tmp_path):
    """Two books in one folder: one flagged for formatting, one for promo. Each
    stage takes its own and leaves the other's marker alone. Both run mocked, so
    the point under test is the gating, not either model."""
    ws = promo_ws(promo_auto_upload=True)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx"),
                               f_2=drive_entry("Bears.docx")),
                        docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves"),
                                 "Bears": {"docproof": "Ready for Formatting",
                                           "author_last_name": "Bears"}})

    report = run(tmp_path, ws, opener)     # mock=True for both stages

    assert report.promoted == ["Wolves.docx"]
    assert report.prepped == ["Bears.docx"]
    assert opener.files["f-1"]["appProperties"][PROMO_PROP] == PROMO_DONE
    assert opener.files["f-2"]["appProperties"][STATE_PROP] == "formatted"


def test_promo_on_without_its_values_refuses_the_pass(tmp_path):
    ws = promo_ws(hubspot_promo_ready_value="")     # a gate with nothing to ask
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": promo_ready("Wolves")})
    with pytest.raises(ticklib.NotConfigured):
        run(tmp_path, ws, opener)


def test_promo_stands_aside_under_subfolder_mode(tmp_path):
    ws = promo_ws(promo_auto_upload=True, subfolders_enabled=True,
                  hubspot_first_property="first", hubspot_last_property="last")
    opener = fake_drive(hubspot={})        # subfolder discovery finds nothing
    report = run(tmp_path, ws, opener)
    assert report.promoted == []           # skipped, no error
