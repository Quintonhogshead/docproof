"""The interior-corrections stage, end to end, with a fake Drive, a fake HubSpot
and no model at all.

What it is held to: the file it corrects is the highest integer
"<surname> - Book N.idml" in the author's "Interior Design" folder and nothing
else; what comes back is "<surname> - Book N.5.idml" with the two-sheet
spreadsheet beside it, in the same folder; every correction the author sent
lands on exactly one sheet; the CRM moves on exactly once; a second pass
neither re-applies nor re-uploads; and nothing is ever guessed — a rival Book
N.5, a missing folder, an empty form each stop the book and tell a person.

The submission is a tracked-changes Word file, which the intake reads without a
model, and the model passes are off, so the whole round trip is deterministic.
"""
from __future__ import annotations

import openpyxl
import pytest

from app.jobs import JobStore
from app.settings import Paths
from app.watch import corrections as corrlib
from app.watch import naming
from app.watch import tick as ticklib
from app.watch.drive import DriveFile, FOLDER_MIME
from app.watch.settings import WatchSettings
from app.watch.stages import (CORRECTIONS_DONE, CORRECTIONS_FAILED,
                              CORRECTIONS_PROP, OUTPUT_PROP, SOURCE_PROP)
from app.watch.state import WatchState

from .conftest import FIXTURES
from .fakes import drive_entry, fake_drive
from .test_corrections_extract import make_tracked_docx

FOLDER = "1AbCdEfGhIjKlMnOp"
AUTHOR = "sf-johnson"
INTERIOR = "sf-johnson-interior"
IDML = (FIXTURES / "layout.idml").read_bytes()
FILE_URL = "https://api.hubapi.com/files/sub-1?filename=Johnson%20corrections.docx"

SOURCE = "Johnson - Book 3.idml"
HAND_OFF = {"Johnson - Book 3.5.idml", "Johnson - Book 3.5 - corrections.xlsx",
            "Johnson - Book 3.5 - notes.md"}


def ws(**over) -> WatchSettings:
    fields = dict(folder_id=FOLDER, model="claude-haiku-4-5",
                  client_id="client-1", client_secret="secret-1",
                  hubspot_enabled=True, hubspot_object="0-970",
                  hubspot_key_property="author_last_name",
                  hubspot_status_property="docproof",
                  hubspot_format_ready_value="Ready for Formatting",
                  hubspot_format_done_value="Formatting Complete",
                  subfolders_enabled=True, require_source_label=True,
                  hubspot_first_property="firstname",
                  hubspot_last_property="lastname",
                  corrections_enabled=True,
                  hubspot_corrections_file_property="corr_file",
                  hubspot_corrections_text_property="corr_text",
                  corrections_model_passes=False)
    fields.update(over)
    return WatchSettings(**fields)


def ready(**extra) -> dict:
    return {"docproof": "Ready for Corrections", "firstname": "Quinton",
            "lastname": "Johnson", "corr_file": FILE_URL, **extra}


def folder_entry(name, parent) -> dict:
    entry = drive_entry(name, mime=FOLDER_MIME)
    entry["parents"] = [parent]
    return entry


def in_folder(name, parent=INTERIOR, **kw) -> dict:
    entry = drive_entry(name, **kw)
    entry["parents"] = [parent]
    return entry


def tree(*extra_interior: tuple[str, dict]) -> dict:
    files = {AUTHOR: folder_entry("Quinton Johnson", FOLDER),
             INTERIOR: folder_entry("Interior Design", AUTHOR),
             "idml-3": in_folder(SOURCE)}
    for fid, entry in extra_interior:
        files[fid] = entry
    return files


def submission(tmp_path) -> bytes:
    """A tracked-changes list: one change the file carries, one it does not."""
    path = make_tracked_docx(tmp_path / "corrections.docx", [
        [("", "She opened the door, the room was "), ("del", "empty"),
         ("ins", "bare"), ("", ".")],
        [("", "The moon was "), ("del", "blue"), ("ins", "red"), ("", ".")],
    ])
    return path.read_bytes()


def make_opener(tmp_path, files=None, hubspot=None):
    opener = fake_drive(files if files is not None else tree(),
                        hubspot=hubspot if hubspot is not None
                        else {"Johnson": ready()})
    opener.content["idml-3"] = IDML
    opener.content["sub-1"] = submission(tmp_path)
    return opener


def run(home, settings, opener, **kw):
    return ticklib.tick(home, settings, opener=opener,
                        get_key=lambda name: "refresh-1", **kw)


def uploads_in(opener) -> dict:
    return {e["name"]: e for e in opener.files.values()
            if e.get("appProperties", {}).get(OUTPUT_PROP)}


def patches(opener) -> list:
    return [c for c in opener.calls if "api.hubapi.com" in c.full_url
            and c.get_method() == "PATCH"]


# --- names (pure) --------------------------------------------------------------

@pytest.mark.parametrize("name,version", [
    ("Johnson - Book 3.idml", 3.0), ("Johnson - Book3.idml", 3.0),
    ("Johnson — Book 10.idml", 10.0), ("Johnson - Book 3.5.idml", 3.5),
])
def test_idml_version_reads_the_designers_series(name, version):
    assert naming.idml_version(name) == ("johnson", version)


@pytest.mark.parametrize("name", [
    "Johnson - Book 3.docx", "Johnson - Book Original.idml", "notes.idml",
])
def test_idml_version_ignores_what_is_not_an_export(name):
    assert naming.idml_version(name) is None


def test_source_is_an_integer_export_with_the_records_surname():
    assert naming.is_idml_source_name("Johnson - Book 3.idml", "JOHNSON")
    assert not naming.is_idml_source_name("Johnson - Book 3.5.idml", "Johnson")
    assert not naming.is_idml_source_name("Smith - Book 3.idml", "Johnson")


def test_hand_off_names_step_by_a_half_and_mirror_the_spacing():
    names = naming.corrections_hand_off_names("Johnson - Book 3.idml")
    assert names["idml"] == "Johnson - Book 3.5.idml"
    assert names["sheet"] == "Johnson - Book 3.5 - corrections.xlsx"
    assert naming.corrections_base("Johnson - Book3.idml") == "Johnson - Book3.5"
    with pytest.raises(ValueError):
        naming.corrections_base("Johnson - Book 3.5.idml")


def test_the_hand_off_is_recognised_as_output():
    assert naming.is_idml_output_name("Johnson - Book 3.5.idml")
    assert naming.is_idml_output_name("Johnson - Book 3.5 - corrections.xlsx")
    assert not naming.is_idml_output_name("Johnson - Book 3.idml")


def _df(name, fid, props=None, mime="application/octet-stream"):
    return DriveFile(id=fid, name=name, mime_type=mime,
                     app_properties=props or {})


def test_pick_source_takes_the_highest_integer_export():
    listing = [_df("Johnson - Book 3.idml", "a"), _df("Johnson - Book 4.idml", "b"),
               _df("Johnson - Book 3.5.idml", "c"), _df("Johnson - Book 4.pdf", "d")]
    chosen, why = corrlib.pick_source(listing, "Johnson")
    assert chosen.id == "b" and why == ""


def test_pick_source_refuses_a_tie_and_reports_a_finished_latest():
    tie = [_df("Johnson - Book 4.idml", "a"), _df("Johnson - Book 4.idml", "b")]
    assert corrlib.pick_source(tie, "Johnson") == (None, "tie")
    done = [_df("Johnson - Book 4.idml", "a", {CORRECTIONS_PROP: CORRECTIONS_DONE})]
    assert corrlib.pick_source(done, "Johnson") == (None, "done")
    failed = [_df("Johnson - Book 4.idml", "a", {CORRECTIONS_PROP: CORRECTIONS_FAILED})]
    assert corrlib.pick_source(failed, "Johnson") == (None, "failed")
    assert corrlib.pick_source([_df("Johnson - Book 3.5.idml", "c")],
                               "Johnson") == (None, "none")


def test_proof_pdf_is_the_one_under_the_same_stem():
    idml = _df("Johnson - Book 3.idml", "a")
    listing = [idml, _df("Johnson - Book 3.pdf", "p"), _df("Johnson - Book 2.pdf", "q")]
    assert corrlib.proof_pdf_for(listing, idml).id == "p"
    assert corrlib.proof_pdf_for([idml], idml) is None


# --- the round trip --------------------------------------------------------------

def test_a_ready_form_is_applied_and_the_book_3_5_goes_back_beside_the_export(
        tmp_path):
    opener = make_opener(tmp_path)
    report = run(tmp_path, ws(), opener)

    assert not report.failed, report.failed
    assert report.corrected and report.corrected[0].startswith(SOURCE)
    placed = uploads_in(opener)
    assert HAND_OFF <= set(placed)
    for name in HAND_OFF:
        # In the Interior Design folder, pointing at the export it came from.
        assert placed[name]["parents"] == [INTERIOR]
        assert placed[name]["appProperties"][SOURCE_PROP] == "idml-3"
    # Nothing was written into the author folder or the parent.
    assert all(e["parents"] == [INTERIOR] for e in placed.values())

    # The CRM moved on, once, to the designer's cue.
    assert opener.hubspot["hs-Johnson"]["properties"]["docproof"] == \
        "Corrections Applied"
    assert len(patches(opener)) == 1
    # The export carries the stage's marker, written last.
    assert opener.files["idml-3"]["appProperties"][CORRECTIONS_PROP] == \
        CORRECTIONS_DONE


def test_the_spreadsheet_accounts_for_every_correction_on_one_sheet_each(
        tmp_path):
    opener = make_opener(tmp_path)
    run(tmp_path, ws(), opener)
    sheet_id = uploads_in(opener)["Johnson - Book 3.5 - corrections.xlsx"]["id"]
    path = tmp_path / "sheet.xlsx"
    path.write_bytes(opener.content[sheet_id])
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Applied", "Not applied"]

    def rows(sheet):
        cells = list(sheet.iter_rows(values_only=True))
        header_at = next(i for i, r in enumerate(cells) if r and r[0] == "#")
        header = list(cells[header_at])
        return [dict(zip(header, r)) for r in cells[header_at + 1:]
                if r and r[0] is not None]

    applied = rows(wb["Applied"])
    not_applied = rows(wb["Not applied"])
    # Two tracked changes went in: one the file carries, one it does not.
    assert len(applied) == 1 and len(not_applied) == 1
    assert "empty" in applied[0]["Find"] and "bare" in applied[0]["Replace"]
    assert "blue" in not_applied[0]["Find"]
    assert "not found" in not_applied[0]["Status"].lower()
    # The corrected IDML really carries the change.
    from docproof.corrections.idml import read_stories
    corrected = tmp_path / "book.idml"
    corrected.write_bytes(
        opener.content[uploads_in(opener)["Johnson - Book 3.5.idml"]["id"]])
    text = "\n".join(p.text for s in read_stories(corrected) for p in s.paragraphs)
    assert "the room was bare." in text and "the room was empty." not in text


def test_a_second_pass_neither_re_applies_nor_re_uploads(tmp_path):
    opener = make_opener(tmp_path)
    run(tmp_path, ws(), opener)
    before = dict(uploads_in(opener))
    # The status has moved on, so HubSpot no longer lists the record as ready.
    report = run(tmp_path, ws(), opener)
    assert report.corrected == []
    assert uploads_in(opener) == before
    assert len(patches(opener)) == 1
    store = JobStore(Paths(tmp_path))
    assert len([j for j in store.all() if j.kind == "corrections"]) == 1


def test_the_stage_reads_only_the_highest_export_and_never_a_half_step(tmp_path):
    files = tree(("idml-2", in_folder("Johnson - Book 2.idml")),
                 ("idml-25", in_folder("Johnson - Book 2.5.idml",
                                       props={OUTPUT_PROP: "1"})))
    opener = make_opener(tmp_path, files=files)
    opener.content["idml-2"] = IDML
    run(tmp_path, ws(), opener)
    placed = uploads_in(opener)
    assert "Johnson - Book 3.5.idml" in placed
    assert "Johnson - Book 2.5.idml" not in {
        n for n, e in placed.items() if e["id"].startswith("up-")}
    assert CORRECTIONS_PROP not in opener.files["idml-2"]["appProperties"]


def test_a_rival_book_3_5_is_never_overwritten(tmp_path):
    files = tree(("rival", in_folder("Johnson - Book 3.5.idml")))
    opener = make_opener(tmp_path, files=files)
    report = run(tmp_path, ws(), opener)
    assert report.corrected == []
    assert uploads_in(opener) == {}
    assert any("will not overwrite" in reason
               for _, reason in report.needs_human)
    assert opener.hubspot["hs-Johnson"]["properties"]["docproof"] == \
        "Ready for Corrections"


def test_no_interior_design_folder_is_a_missing_source(tmp_path):
    files = {AUTHOR: folder_entry("Quinton Johnson", FOLDER)}
    opener = make_opener(tmp_path, files=files)
    report = run(tmp_path, ws(), opener)
    assert report.missing_source and "Interior Design" in report.missing_source[0][1]
    assert uploads_in(opener) == {}


def test_a_form_with_nothing_attached_needs_a_person(tmp_path):
    record = ready()
    record["corr_file"] = ""
    opener = make_opener(tmp_path, hubspot={"Johnson": record})
    report = run(tmp_path, ws(), opener)
    assert any("no corrections" in reason for _, reason in report.needs_human)
    assert uploads_in(opener) == {}
    assert len(patches(opener)) == 0


def test_typed_text_alone_needs_a_model_and_says_so(tmp_path):
    """No key for the reader here, so a typed list cannot be read: the export is
    marked failed with the reason and a person is told, rather than three silent
    retries."""
    record = ready()
    record["corr_file"] = ""
    record["corr_text"] = "Page 3: change 'empty' to 'bare'."
    opener = make_opener(tmp_path, hubspot={"Johnson": record})
    report = run(tmp_path, ws(), opener)
    assert any("could not be read" in reason for _, reason in report.needs_human)
    assert opener.files["idml-3"]["appProperties"][CORRECTIONS_PROP] == \
        CORRECTIONS_FAILED
    assert uploads_in(opener) == {}


def test_the_stage_stands_aside_when_off_or_half_configured(tmp_path):
    opener = make_opener(tmp_path)
    report = run(tmp_path, ws(corrections_enabled=False), opener)
    assert report.corrected == [] and uploads_in(opener) == {}
    with pytest.raises(ticklib.NotConfigured):
        run(tmp_path, ws(subfolders_enabled=False), opener)
    with pytest.raises(ticklib.NotConfigured):
        run(tmp_path, ws(hubspot_corrections_file_property="",
                         hubspot_corrections_text_property=""), opener)


def test_read_only_hubspot_still_delivers_but_writes_nothing(tmp_path):
    opener = make_opener(tmp_path)
    run(tmp_path, ws(hubspot_write_back=False), opener)
    assert "Johnson - Book 3.5.idml" in uploads_in(opener)
    assert len(patches(opener)) == 0
    assert opener.files["idml-3"]["appProperties"][CORRECTIONS_PROP] == \
        CORRECTIONS_DONE


def test_state_records_the_job_before_the_run_and_the_input_kind(tmp_path):
    opener = make_opener(tmp_path)
    run(tmp_path, ws(), opener)
    rec = WatchState.load(tmp_path / "state.json").get("idml-3")
    assert rec.corrections_job_id and rec.corrections_hubspot_done
    assert rec.corrections_marked == CORRECTIONS_DONE
    assert rec.corrections_input_kind == "docx"
    assert rec.corrections_input_name == "Johnson corrections.docx"
    assert rec.subfolder_id == INTERIOR
