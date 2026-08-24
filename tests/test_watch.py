"""The watcher's decisions, made without a network.

Three questions live here and nowhere else: what a file in the folder is, what
the watcher already did to it, and what it was told to watch. Everything
expensive downstream — a download, a model, an upload — happens only because
`classify` said so, which is why these are the tests that get to be pedantic.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.settings import ENV_VARS, PROVIDERS
from app.watch.drive import DOCX_MIME, FOLDER_MIME, GOOGLE_DOC_MIME, DriveFile
from app.watch.settings import WatchSettings, default_watch_home, folder_id_from
from app.watch.stages import (FAILED, FORMATTED, JOB_PROP, OUTPUT_PROP,
                              STATE_PROP, Stage, classify)
from app.watch.state import FileRecord, WatchState
from docproof.prep.convert import CONVERTIBLE


def drive_file(name: str = "Book.docx", *, mime: str = DOCX_MIME,
               props: dict | None = None, **kw) -> DriveFile:
    return DriveFile(id=kw.pop("id", "f-1"), name=name, mime_type=mime,
                     app_properties=props or {}, **kw)


# --- what a file is -----------------------------------------------------------

def test_a_docx_with_no_marker_is_a_new_manuscript():
    assert classify(drive_file("Wolves of the Yard.docx")) is Stage.NEW_MANUSCRIPT


def test_a_native_google_doc_is_a_new_manuscript():
    """Authors write in Docs. It has no extension at all, so the mime type is
    the only thing that answers."""
    doc = drive_file("Wolves of the Yard", mime=GOOGLE_DOC_MIME)
    assert classify(doc) is Stage.NEW_MANUSCRIPT


@pytest.mark.parametrize("suffix", CONVERTIBLE)
def test_every_format_prep_can_convert_is_a_manuscript(suffix):
    """Read from prep's own list, so a format added there is watched for here
    without anybody remembering to."""
    assert classify(drive_file(f"Book{suffix}")) is Stage.NEW_MANUSCRIPT


def test_things_that_are_not_manuscripts_are_left_alone():
    assert classify(drive_file("cover.pdf")) is Stage.SKIP
    assert classify(drive_file("art.png")) is Stage.SKIP
    assert classify(drive_file("Contracts", mime=FOLDER_MIME)) is Stage.SKIP


def test_an_extensionless_book_original_is_a_new_manuscript():
    """A Word or Google doc renamed to "<surname> - Book Original" with the
    ".docx" dropped still carries the house intake label, so it is the book to
    prepare — not skipped until a person re-adds the extension by hand."""
    assert classify(drive_file("Johnson - Book Original")) is Stage.NEW_MANUSCRIPT
    # Case and dash drift is forgiven here as everywhere else.
    assert classify(drive_file("Johnson — book original")) is Stage.NEW_MANUSCRIPT


def test_an_extensionless_file_without_the_label_is_left_alone():
    """The label is what earns the exception: a bare name without it is not a
    manuscript, so a stray note or a cover with no extension is still skipped."""
    assert classify(drive_file("cover")) is Stage.SKIP
    assert classify(drive_file("Johnson - Draft Two")) is Stage.SKIP


def test_a_folder_named_like_a_book_original_is_still_skipped():
    """A book folder that happens to be named like the intake is a folder, not
    the book — the folder check wins before the label ever gets a look."""
    folder = drive_file("Johnson - Book Original", mime=FOLDER_MIME)
    assert classify(folder) is Stage.SKIP


def test_a_folder_is_skipped_even_when_it_is_named_like_a_manuscript():
    folder = drive_file("Draft.docx", mime=FOLDER_MIME)
    assert classify(folder) is Stage.SKIP


def test_a_prepared_manuscript_is_done():
    marked = drive_file(props={STATE_PROP: FORMATTED, JOB_PROP: "j-1"})
    assert classify(marked) is Stage.DONE


def test_a_failed_manuscript_is_not_tried_again():
    """The failure needed a person the first time. Trying nightly until
    somebody notices is how a broken file becomes a bill."""
    marked = drive_file(props={STATE_PROP: FAILED})
    assert classify(marked) is Stage.FAILED


def test_something_docproof_wrote_is_never_an_input():
    output = drive_file("tagged_Book.docx", props={OUTPUT_PROP: "1"})
    assert classify(output) is Stage.OUTPUT


@pytest.mark.parametrize("name", ["tagged_Book.docx", "tracked_Book.docx",
                                  "reviewed_Book.docx", "prep_notes_Book.md",
                                  "PREP_NOTES_Book.md", "Grest - book 0.docx",
                                  "Grest - book 0 - notes.md"])
def test_an_output_is_recognised_by_name_when_its_marker_is_gone(name):
    """Somebody duplicates a file, or re-uploads one out of Downloads, and the
    properties do not come with it. Preparing an already-prepared manuscript is
    the one mistake in this folder that costs money."""
    assert classify(drive_file(name)) is Stage.OUTPUT


def test_a_marker_beats_a_name():
    """A manuscript an author happened to call `tracked_changes.docx` is still
    theirs — but only until DocProof says otherwise about it."""
    assert classify(drive_file("Book.docx", props={OUTPUT_PROP: "1"})) is \
        Stage.OUTPUT
    assert classify(drive_file("Book.docx",
                               props={STATE_PROP: FORMATTED})) is Stage.DONE


def test_size_is_zero_for_a_doc_that_does_not_report_one():
    """Drive omits `size` for native Docs. Reading it as an int must not be
    the thing that breaks a tick."""
    parsed = DriveFile.from_api({"id": "f-1", "name": "Book",
                                 "mimeType": GOOGLE_DOC_MIME})
    assert parsed.size == 0
    assert parsed.app_properties == {}
    assert parsed.is_google_doc


# --- what the watcher already did ---------------------------------------------

def test_state_round_trips(tmp_path):
    state = WatchState(tmp_path / "state.json")
    state.record(FileRecord(file_id="f-1", name="Book.docx", job_id="j-1",
                            uploaded={"tagged_Book.docx": "up-1"},
                            marked=FORMATTED, attempts=2))

    again = WatchState.load(tmp_path / "state.json")
    rec = again.get("f-1")
    assert rec.job_id == "j-1"
    assert rec.uploaded == {"tagged_Book.docx": "up-1"}
    assert rec.marked == FORMATTED
    assert rec.attempts == 2
    assert rec.updated_at            # stamped on the way in


def test_an_unknown_file_gets_an_empty_record_that_is_not_saved(tmp_path):
    state = WatchState(tmp_path / "state.json")
    assert state.get("f-9").job_id == ""
    assert not (tmp_path / "state.json").exists()


def test_a_corrupt_state_file_starts_clean(tmp_path, caplog):
    """The markers in Drive still prevent the expensive mistake. Refusing to
    run because a cache file is corrupt would be the worse failure."""
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    state = WatchState.load(path)

    assert state.files == {}
    assert "unreadable" in caplog.text.lower()


def test_a_malformed_entry_is_skipped_and_the_rest_survive(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "files": {
        "f-1": {"name": "Good.docx", "job_id": "j-1"},
        "f-2": "not a record",
    }}), encoding="utf-8")

    state = WatchState.load(path)

    assert state.get("f-1").job_id == "j-1"
    assert "f-2" not in state.files
    assert "malformed" in caplog.text.lower()


def test_state_written_by_a_newer_version_keeps_the_fields_it_knows(tmp_path):
    """Forward compatible for the same reason Settings is: a field added later
    must not make an older watcher refuse to read its own state."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "files": {
        "f-1": {"name": "Book.docx", "job_id": "j-1", "hubspot_deal": "42"},
    }}), encoding="utf-8")

    assert WatchState.load(path).get("f-1").job_id == "j-1"


def test_forgetting_a_file_survives_a_reload(tmp_path):
    state = WatchState(tmp_path / "state.json")
    state.record(FileRecord(file_id="f-1"))
    state.forget("f-1")

    assert WatchState.load(tmp_path / "state.json").files == {}


# --- what it was told to watch ------------------------------------------------

def test_watch_settings_round_trip(tmp_path):
    WatchSettings(folder_id="abc123xyz", model="claude-opus-5",
                  max_files_per_tick=2).save(tmp_path)

    loaded = WatchSettings.load(tmp_path)

    assert loaded.folder_id == "abc123xyz"
    assert loaded.model == "claude-opus-5"
    assert loaded.max_files_per_tick == 2


def test_missing_watch_settings_are_the_defaults(tmp_path):
    assert WatchSettings.load(tmp_path) == WatchSettings()


def test_unreadable_watch_settings_are_the_defaults(tmp_path, caplog):
    (tmp_path / "watch.json").write_text("{{{", encoding="utf-8")

    assert WatchSettings.load(tmp_path) == WatchSettings()
    assert "unreadable" in caplog.text.lower()


def test_a_setting_from_a_newer_version_is_dropped_not_fatal(tmp_path):
    (tmp_path / "watch.json").write_text(
        json.dumps({"folder_id": "abc123xyz", "hubspot_pipeline": "x"}),
        encoding="utf-8")

    assert WatchSettings.load(tmp_path).folder_id == "abc123xyz"


def test_no_secret_is_written_to_the_settings_file(tmp_path):
    """The refresh token goes to the Keychain. Nothing here may hold it."""
    WatchSettings(folder_id="abc123xyz", client_secret="not-really-secret",
                  ).save(tmp_path)

    written = json.loads((tmp_path / "watch.json").read_text("utf-8"))

    assert "refresh_token" not in written
    assert not any("refresh" in key for key in written)


def test_results_land_under_the_watch_home_by_default(tmp_path):
    assert WatchSettings().results_dir(tmp_path) == tmp_path / "results"
    assert WatchSettings(output_dir="/elsewhere").results_dir(tmp_path) == \
        Path("/elsewhere")


def test_the_watchers_choices_reach_the_job_runner(tmp_path):
    """`JobRunner` reads a `Settings`; the watcher has its own. This is the
    only place the two shapes meet."""
    settings = WatchSettings(model="claude-opus-5",
                             prep_output="both").app_settings(tmp_path)

    assert settings.model == "claude-opus-5"
    assert settings.prep_output == "both"
    assert settings.output_dir == str(tmp_path / "results")


def test_the_watch_home_is_relocatable(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCPROOF_WATCH_HOME", str(tmp_path / "elsewhere"))
    assert default_watch_home() == tmp_path / "elsewhere"


def test_the_watch_home_defaults_beside_the_apps(monkeypatch):
    monkeypatch.delenv("DOCPROOF_WATCH_HOME", raising=False)
    assert default_watch_home().name == "watch"
    assert default_watch_home().parent.name == "DocProof"


# --- the folder somebody pasted -----------------------------------------------

@pytest.mark.parametrize("pasted", [
    "1AbCdEfGhIjKlMnOp",
    "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp",
    "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing",
    "https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOp",
    "https://drive.google.com/drive/u/2/folders/1AbCdEfGhIjKlMnOp/edit",
    "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp",
    "  https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp  ",
])
def test_a_folder_id_is_found_in_whatever_was_pasted(pasted):
    """People copy the address bar, not the id."""
    assert folder_id_from(pasted) == "1AbCdEfGhIjKlMnOp"


@pytest.mark.parametrize("junk", ["", "   ", "https://drive.google.com/",
                                  "my folder", "short"])
def test_something_that_is_not_a_folder_says_so(junk):
    with pytest.raises(ValueError, match="Google Drive folder"):
        folder_id_from(junk)


# --- the token's home ---------------------------------------------------------

def test_the_google_token_rides_the_same_rails_as_every_other_secret():
    """In ENV_VARS, so environment beats Keychain; out of PROVIDERS, so
    nothing ever offers to review a manuscript with it."""
    assert ENV_VARS["google"] == "GOOGLE_REFRESH_TOKEN"
    assert "google" not in PROVIDERS
