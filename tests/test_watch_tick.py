"""One pass over the folder, end to end, with a fake Drive and a fake model.

The properties worth holding this to are all about money and the author's
words: a manuscript is prepared once and only once, a tick that dies partway
does not pay twice for what it already bought, and a file that fails its
word-for-word check reaches nobody.

Nothing here touches a network or sleeps.
"""
from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from app.jobs import JobRunner, JobStore
from app.settings import Paths
from app.watch import tick as ticklib
from app.watch.drive import FOLDER_MIME, GOOGLE_DOC_MIME
from app.watch.hubspot import HubSpotAuthError
from app.watch.settings import WatchSettings
from app.watch.stages import (FAILED, FORMATTED, JOB_PROP, OUTPUT_PROP,
                              REASON_PROP, SOURCE_PROP, STATE_PROP)
from app.watch.state import WatchState

from .conftest import FIXTURES
from .fakes import TaggingProvider, drive_entry, fake_drive, http_error

FOLDER = "1AbCdEfGhIjKlMnOp"
MANUSCRIPT = (FIXTURES / "googledoc.docx").read_bytes()


@pytest.fixture
def ws():
    return WatchSettings(folder_id=FOLDER, model="claude-haiku-4-5",
                         client_id="client-1", client_secret="secret-1")


@pytest.fixture
def provider(monkeypatch):
    """The one model call prep makes, answered without a vendor."""
    tagger = TaggingProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: tagger)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    return tagger


def folder(**files) -> dict:
    """A watched folder holding one entry per keyword — `f_1=drive_entry(...)`
    reads as `f-1` on the Drive side."""
    return {name.replace("_", "-", 1): entry for name, entry in files.items()}


def run(home, ws, opener, **kw):
    return ticklib.tick(home, ws, opener=opener, get_key=lambda name: "refresh-1",
                        **kw)


def names_in(opener) -> set[str]:
    return {entry["name"] for entry in opener.files.values()}


def uploads_in(opener) -> dict:
    return {entry["name"]: entry for entry in opener.files.values()
            if entry.get("appProperties", {}).get(OUTPUT_PROP)}


# --- the ordinary case --------------------------------------------------------

def test_a_new_manuscript_is_prepared_uploaded_and_marked(tmp_path, ws,
                                                          provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener)

    assert report.ok and report.prepped == ["Wolves.docx"]
    placed = uploads_in(opener)
    assert set(placed) == {"Wolves - book 0.docx", "Wolves - book 0 - notes.md"}
    assert placed["Wolves - book 0.docx"]["parents"] == [FOLDER]
    assert placed["Wolves - book 0.docx"]["appProperties"][SOURCE_PROP] == "f-1"
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED
    assert opener.files["f-1"]["appProperties"][JOB_PROP]


def test_what_lands_in_the_folder_is_what_prep_wrote(tmp_path, ws, provider):
    """The bytes uploaded are the bytes on disk, not a re-run of anything."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    job = JobStore(Paths(tmp_path)).all()[0]
    local = Path(job.results_dir) / "tagged_Wolves.docx"   # prep's internal name
    uploaded = [fid for fid, entry in opener.files.items()
                if entry["name"] == "Wolves - book 0.docx"][0]
    assert opener.content[uploaded] == local.read_bytes()


def test_the_run_is_recorded_where_spending_is_added_up(tmp_path, ws, provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    job = JobStore(Paths(tmp_path)).all()[0]
    assert job.state == "done" and job.verified is True
    assert job.api_calls and job.input_tokens
    assert job.kind == "prep"


def test_asking_for_both_files_uploads_both(tmp_path, ws, provider):
    ws.prep_output = "both"
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    assert "Wolves - book 0.docx" in uploads_in(opener)
    assert "Wolves - book 0 - tracked changes.docx" in uploads_in(opener)


def test_the_notes_can_be_left_out_of_the_folder(tmp_path, ws, provider):
    ws.upload_notes = False
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    assert set(uploads_in(opener)) == {"Wolves - book 0.docx"}


def test_a_native_google_doc_is_exported_and_named_after_its_title(
        tmp_path, ws, provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves of the Yard",
                                               mime=GOOGLE_DOC_MIME)),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    assert any("/f-1/export?" in c.full_url for c in opener.calls)
    assert not any(c.full_url.endswith("alt=media") for c in opener.calls)
    assert "Wolves of the Yard - book 0.docx" in uploads_in(opener)


def test_a_doc_titled_with_a_slash_still_becomes_a_file(tmp_path, ws, provider):
    """A Doc has a title, not a filename, and a title may hold the one
    character a filename may not."""
    opener = fake_drive(folder(f_1=drive_entry("Draft 3/4",
                                               mime=GOOGLE_DOC_MIME)),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener)

    assert report.ok
    assert "Draft 3-4 - book 0.docx" in uploads_in(opener)


def test_the_house_naming_scheme_end_to_end(tmp_path, ws, provider):
    """An author manuscript "<surname> - Book Original" comes back as
    "<surname> - book 0", and the next tick knows that output for its own — it
    is never handed back to be formatted again."""
    opener = fake_drive(
        folder(f_1=drive_entry("Grest - Book Original.docx")), docx=MANUSCRIPT)

    first = run(tmp_path, ws, opener)

    assert first.prepped == ["Grest - Book Original.docx"]
    assert "Grest - book 0.docx" in uploads_in(opener)

    second = run(tmp_path, ws, opener)          # the folder now holds the output

    assert second.new == 0                      # the output is not a manuscript
    assert dict(second.plan)["Grest - book 0.docx"] == "output"


# --- doing it once ------------------------------------------------------------

def test_a_second_tick_finds_nothing_to_do(tmp_path, ws, provider):
    """The marker written last time is what makes the manuscript invisible
    now, and the outputs carry their own so they are never mistaken for
    manuscripts."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    run(tmp_path, ws, opener)
    before = len(provider.calls)
    placed = len(uploads_in(opener))

    second = run(tmp_path, ws, opener)

    assert second.new == 0 and second.prepped == []
    assert len(provider.calls) == before
    assert len(uploads_in(opener)) == placed


def test_the_files_it_wrote_are_never_read_back_as_manuscripts(tmp_path, ws,
                                                               provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    run(tmp_path, ws, opener)

    report = run(tmp_path, ws, opener)

    assert all(stage == "output" for name, stage in report.plan
               if " - book 0" in name)


def test_a_manuscript_already_marked_done_is_left_alone(tmp_path, ws, provider):
    opener = fake_drive(folder(f_1=drive_entry(
        "Wolves.docx", props={STATE_PROP: FORMATTED})), docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener)

    assert report.new == 0
    assert provider.calls == []


# --- picking up where a dead tick stopped -------------------------------------

def test_a_crash_before_the_upload_does_not_pay_for_the_book_again(
        tmp_path, ws, provider):
    """The expensive step is the model. A tick that died between preparing a
    manuscript and uploading it left something already paid for on disk, and
    the next tick has to find it rather than buy it twice."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT,
                        fail={"upload": urllib.error.URLError("no route")})

    first = run(tmp_path, ws, opener)
    assert not first.ok
    assert uploads_in(opener) == {}
    paid = len(provider.calls)

    second = run(tmp_path, ws, opener)

    assert second.ok and second.prepped == ["Wolves.docx"]
    assert len(provider.calls) == paid          # not one call more
    assert set(uploads_in(opener)) == {"Wolves - book 0.docx",
                                       "Wolves - book 0 - notes.md"}
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


def test_a_crash_between_two_uploads_only_sends_the_missing_ones(
        tmp_path, ws, provider, monkeypatch):
    ws.prep_output = "both"
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    real_upload = ticklib.drive.upload
    sent = []

    def one_then_die(*a, **kw):
        if len(sent) >= 1:
            raise urllib.error.URLError("no route")
        sent.append(kw.get("name"))
        return real_upload(*a, **kw)

    monkeypatch.setattr("app.watch.drive.upload", one_then_die)
    run(tmp_path, ws, opener)
    landed = set(uploads_in(opener))
    monkeypatch.setattr("app.watch.drive.upload", real_upload)

    run(tmp_path, ws, opener)

    assert len(landed) == 1
    assert set(uploads_in(opener)) == {"Wolves - book 0.docx",
                                       "Wolves - book 0 - tracked changes.docx",
                                       "Wolves - book 0 - notes.md"}


def test_an_upload_the_state_file_lost_is_adopted_not_repeated(tmp_path, ws,
                                                               provider):
    """Died after an upload and before writing it down. The output is in the
    folder carrying the id of the manuscript it came from, which is a fact
    about the folder rather than a guess from the name."""
    opener = fake_drive(folder(
        f_1=drive_entry("Wolves.docx"),
        f_2=drive_entry("Wolves - book 0.docx",
                        props={OUTPUT_PROP: "1", SOURCE_PROP: "f-1"}),
    ), docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    tagged = [e for e in opener.files.values()
              if e["name"] == "Wolves - book 0.docx"]
    assert len(tagged) == 1                  # adopted, not uploaded beside
    assert "Wolves - book 0 - notes.md" in uploads_in(opener)


def test_a_crash_before_the_marker_only_writes_the_marker(tmp_path, ws,
                                                          provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT, fail={"patch": http_error(500, "nope")})

    first = run(tmp_path, ws, opener)
    assert not first.ok
    paid, placed = len(provider.calls), len(uploads_in(opener))

    second = run(tmp_path, ws, opener)

    assert second.ok
    assert len(provider.calls) == paid
    assert len(uploads_in(opener)) == placed
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


# --- when the words did not survive -------------------------------------------

def eats_a_word(monkeypatch):
    """Corrupt the file prep writes, the way tests/test_prep.py does, so the
    word-for-word check fails on something real."""
    from docproof.prep.ingest import BODY_PART
    from docproof.prep.writers import clean as clean_writer
    from docproof.utils.xml_helpers import qn

    real = clean_writer.write_clean

    def writer(pkg, structure, plan, sheet, **kw):
        stats = real(pkg, structure, plan, sheet, **kw)
        for node in pkg.tree(BODY_PART).iter(qn("w:t")):
            if "remembered" in (node.text or ""):
                node.text = node.text.replace("remembered", "")
        return stats

    monkeypatch.setattr(clean_writer, "write_clean", writer)
    monkeypatch.setattr("docproof.prep.pipeline.write_clean", writer)


def test_a_file_whose_words_drifted_reaches_nobody(tmp_path, ws, provider,
                                                   monkeypatch):
    """The whole promise of prep is that it does not change the author's
    words. A file that failed that check must not be in the folder an author
    and a designer are looking at."""
    eats_a_word(monkeypatch)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener)

    assert not report.ok
    assert uploads_in(opener) == {}
    props = opener.files["f-1"]["appProperties"]
    assert props[STATE_PROP] == FAILED
    assert props[REASON_PROP]


def test_a_failed_manuscript_is_not_tried_again_the_next_night(
        tmp_path, ws, provider, monkeypatch):
    eats_a_word(monkeypatch)
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    run(tmp_path, ws, opener)
    paid = len(provider.calls)

    second = run(tmp_path, ws, opener)

    assert second.new == 0
    assert len(provider.calls) == paid


def test_a_failure_note_can_be_asked_for(tmp_path, ws, provider, monkeypatch):
    eats_a_word(monkeypatch)
    ws.upload_failure_note = True
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    placed = uploads_in(opener)
    assert set(placed) == {"prep_failed_Wolves.md"}
    note = opener.content[[fid for fid, e in opener.files.items()
                           if e["name"] == "prep_failed_Wolves.md"][0]]
    assert b"Nothing about the original was changed" in note


def test_a_failure_note_is_not_stacked_when_the_marker_lags(tmp_path, ws,
                                                            provider,
                                                            monkeypatch):
    """The note landed; the marker patch then failed, so the next tick came
    back to the same file. It used to upload a second note beside the first —
    the same crash window upload_outputs already guards."""
    eats_a_word(monkeypatch)
    ws.upload_failure_note = True
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT,
                        fail={"patch": http_error(500, "nope")})

    first = run(tmp_path, ws, opener)
    assert not first.ok                      # note up, marker not

    run(tmp_path, ws, opener)                # Drive is healthy again

    notes = [e for e in opener.files.values()
             if e["name"] == "prep_failed_Wolves.md"]
    assert len(notes) == 1                   # adopted, not stacked
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FAILED


# --- when something is merely broken ------------------------------------------

def test_a_transient_failure_is_tried_again_and_then_given_up_on(
        tmp_path, ws, provider, monkeypatch):
    """Three runs failing the same way is a fact about the file, not about the
    weather. It stops being retried and starts being visible."""
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: (_ for _ in ()).throw(
                            RuntimeError("the model would not answer")))
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    for attempt in range(3):
        report = run(tmp_path, ws, opener)
        assert not report.ok
        assert STATE_PROP not in opener.files["f-1"].get("appProperties", {})

    report = run(tmp_path, ws, opener)

    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FAILED
    assert "Gave up" in opener.files["f-1"]["appProperties"][REASON_PROP]
    assert report.failed


def test_a_finished_manuscript_is_never_given_up_on(tmp_path, ws, provider,
                                                    monkeypatch):
    """Prep finished on the first tick; Drive then failed the upload three
    ticks running. The model has been paid and the files exist — the next
    healthy tick delivers them instead of marking the book failed."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    real_upload = ticklib.drive.upload
    drive_is = {"down": True}

    def flaky_upload(*a, **kw):
        if drive_is["down"]:
            raise urllib.error.URLError("drive is down")
        return real_upload(*a, **kw)

    monkeypatch.setattr("app.watch.drive.upload", flaky_upload)

    for _ in range(3):
        assert not run(tmp_path, ws, opener).ok
    paid = len(provider.calls)
    assert WatchState.load(tmp_path / "state.json").get("f-1").attempts == 3

    drive_is["down"] = False
    report = run(tmp_path, ws, opener)

    assert report.ok
    assert "Wolves - book 0.docx" in uploads_in(opener)
    assert len(provider.calls) == paid           # delivered, not re-run
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


def test_one_bad_manuscript_does_not_stop_the_others(tmp_path, ws, provider,
                                                     monkeypatch):
    opener = fake_drive(folder(f_1=drive_entry("Broken.docx", modified="1"),
                               f_2=drive_entry("Wolves.docx", modified="2")),
                        docx=MANUSCRIPT)
    real_download = ticklib.drive.download

    def refuse_the_first(token, file_id, dest, **kw):
        if file_id == "f-1":
            raise urllib.error.URLError("no route")
        return real_download(token, file_id, dest, **kw)

    monkeypatch.setattr("app.watch.drive.download", refuse_the_first)

    report = run(tmp_path, ws, opener)

    assert report.prepped == ["Wolves.docx"]
    assert [name for name, _ in report.failed] == ["Broken.docx"]
    assert "Wolves - book 0.docx" in uploads_in(opener)


def test_a_bad_run_counts_against_the_file_that_caused_it(tmp_path, ws,
                                                          provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT,
                        fail={"download": urllib.error.URLError("no route")})

    run(tmp_path, ws, opener)

    assert WatchState.load(tmp_path / "state.json").get("f-1").attempts == 1


# --- how much one pass may do -------------------------------------------------

def test_the_cap_holds_and_says_what_it_left(tmp_path, ws, provider, caplog):
    """Ten manuscripts appearing at once is somebody reorganising a folder
    more often than it is ten new books."""
    ws.max_files_per_tick = 2
    opener = fake_drive(folder(f_1=drive_entry("A.docx", modified="1"),
                               f_2=drive_entry("B.docx", modified="2"),
                               f_3=drive_entry("C.docx", modified="3")),
                        docx=MANUSCRIPT)

    with caplog.at_level(logging.INFO, logger="docproof.app.watch.tick"):
        report = run(tmp_path, ws, opener)

    assert report.prepped == ["A.docx", "B.docx"]
    assert report.deferred == 1
    assert "leaving 1 for the next" in caplog.text
    assert STATE_PROP not in opener.files["f-3"].get("appProperties", {})


def test_the_oldest_manuscript_goes_first(tmp_path, ws, provider):
    ws.max_files_per_tick = 1
    opener = fake_drive(folder(f_1=drive_entry("Newer.docx", modified="2026-02"),
                               f_2=drive_entry("Older.docx", modified="2026-01")),
                        docx=MANUSCRIPT)

    assert run(tmp_path, ws, opener).prepped == ["Older.docx"]


# --- rehearsals ---------------------------------------------------------------

def test_a_dry_run_only_looks(tmp_path, ws, provider):
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener, dry_run=True)

    assert report.dry_run and report.new == 1
    assert report.plan == [("Wolves.docx", "new")]
    assert provider.calls == []
    assert uploads_in(opener) == {}
    assert not (tmp_path / "jobs").exists()
    assert not (tmp_path / "state.json").exists()


def test_a_rehearsal_never_calls_a_model_but_still_fills_the_folder(
        tmp_path, ws, monkeypatch):
    """The point of `--mock-tags`: prove the round trip against a real Drive
    folder — sign in, list, download, upload, mark — without spending
    anything."""
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: (_ for _ in ()).throw(
                            AssertionError("a rehearsal called a model")))
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener, mock=True)

    assert report.ok and report.prepped == ["Wolves.docx"]
    assert set(uploads_in(opener)) == {"Wolves - book 0.docx",
                                       "Wolves - book 0 - notes.md"}
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


def test_a_rehearsal_still_checks_the_authors_words(tmp_path, ws, monkeypatch):
    """A rehearsal that skipped the one gate protecting the author's words
    would be rehearsing something else."""
    eats_a_word(monkeypatch)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener, mock=True)

    assert not report.ok
    assert uploads_in(opener) == {}
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FAILED


def test_a_rehearsal_leaves_interrupted_real_work_alone(tmp_path, ws, provider):
    """`--mock-tags` is documented as costing nothing. A job a dead real pass
    left mid-flight must wait for a real pass — not be finished, at full
    price, by a rehearsal draining the queue."""
    from app.jobs import Job

    store = JobStore(Paths(tmp_path).ensure())
    store.save(Job(id="j1", filename="Wolves.docx",
                   source_path=str(tmp_path / "gone.docx"),
                   model="claude-haiku-4-5", mode="now", kind="prep",
                   state="running"))
    opener = fake_drive(folder())

    report = run(tmp_path, ws, opener, mock=True)

    assert report.ok
    assert provider.calls == []
    assert JobStore(Paths(tmp_path)).get("j1").state == "running"  # untouched


def test_a_pass_that_dies_at_the_door_still_counts_as_a_look(tmp_path, ws,
                                                             provider):
    """The in-app clock retries whenever there is no stamp. A pass that failed
    refreshing its token must leave one anyway, or a revoked sign-in means a
    fresh full attempt every minute instead of every tick_every_minutes."""
    from app.watch.state import last_tick

    opener = fake_drive(fail={"token": http_error(400, "invalid_grant")})

    with pytest.raises(ticklib.DriveError):
        run(tmp_path, ws, opener)

    assert last_tick(tmp_path) is not None


def test_a_dry_run_leaves_no_stamp(tmp_path, ws, provider):
    """Read-only means the stamp too: a look that did no work must not push
    back the clock that decides when real work happens."""
    from app.watch.state import last_tick

    run(tmp_path, ws, fake_drive(folder()), dry_run=True)

    assert last_tick(tmp_path) is None


# --- before anything can happen -----------------------------------------------

def test_no_folder_yet_says_which_command_sets_one(tmp_path, provider):
    with pytest.raises(ticklib.NotConfigured, match="docproof-watch init"):
        run(tmp_path, WatchSettings(), fake_drive())


def test_no_sign_in_yet_says_which_command_does_it(tmp_path, ws, provider):
    with pytest.raises(ticklib.NotConfigured, match="docproof-watch auth"):
        ticklib.tick(tmp_path, ws, opener=fake_drive(),
                     get_key=lambda name: None)


def test_no_oauth_client_yet_points_at_the_walkthrough(tmp_path, provider):
    ws = WatchSettings(folder_id=FOLDER)
    with pytest.raises(ticklib.NotConfigured, match="docs/watch.md"):
        run(tmp_path, ws, fake_drive())


def test_a_folder_that_cannot_be_read_stops_the_run_before_any_work(
        tmp_path, ws, provider):
    opener = fake_drive(fail={"list": http_error(404, "File not found")})

    with pytest.raises(ticklib.DriveError, match="docproof-watch init"):
        run(tmp_path, ws, opener)

    assert not (tmp_path / "jobs").exists()


# --- the seams ----------------------------------------------------------------

def test_the_copy_edit_slots_are_run_in_the_order_they_will_need(
        tmp_path, ws, provider, monkeypatch):
    """Collect before submit before prepare: work paid for last night lands in
    the folder before anything new is started, and an overnight batch is with
    the vendor while the synchronous work runs rather than an hour behind it.
    Wiring them now is cheaper than remembering the order later."""
    order = []
    for slot in ("collect_finished", "submit_ready", "run_prep"):
        real = getattr(ticklib, slot)
        monkeypatch.setattr(ticklib, slot,
                            lambda *a, _s=slot, _r=real, **kw: (
                                order.append(_s), _r(*a, **kw))[1])
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    assert order == ["collect_finished", "submit_ready", "run_prep"]


def test_an_interrupted_job_is_finished_before_new_work_is_started(
        tmp_path, ws, provider):
    """`resume_interrupted` hands ids to a queue expecting a worker thread.
    There isn't one — this process does the work itself — so the queue has to
    be emptied by hand, and a job left `running` by a killed tick has to come
    back rather than sit there forever."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    run(tmp_path, ws, opener)

    store = JobStore(Paths(tmp_path))
    job = store.all()[0]
    store.update(job.id, state="running")

    runner = JobRunner(store, ws.app_settings(tmp_path),
                       config_path=ticklib.config_path())
    state = WatchState.load(tmp_path / "state.json")
    listing = [ticklib.DriveFile(id="f-1", name="Wolves.docx",
                                 mime_type=GOOGLE_DOC_MIME)]
    ticklib._drain(runner, state, listing)

    assert store.get(job.id).state == "done"


def test_a_withdrawn_manuscripts_job_is_parked_not_paid_for(tmp_path, ws,
                                                            provider):
    """The author pulled the file back mid-run. Finishing its job pays the
    model for work that can never be delivered — parked instead, with the
    checkpoint kept, so a returning manuscript resumes what was bought."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")),
                        docx=MANUSCRIPT)
    run(tmp_path, ws, opener)

    store = JobStore(Paths(tmp_path))
    job = store.all()[0]
    store.update(job.id, state="running")
    paid = len(provider.calls)

    runner = JobRunner(store, ws.app_settings(tmp_path),
                       config_path=ticklib.config_path())
    state = WatchState.load(tmp_path / "state.json")
    ticklib._drain(runner, state, listing=[])      # the folder no longer has it

    parked = store.get(job.id)
    assert parked.state == "failed"
    assert "left the folder" in parked.error
    assert len(provider.calls) == paid             # not a token spent on it


# --- the HubSpot gate ---------------------------------------------------------
#
# A shared folder, so a file has to say which book it is. When HubSpot is the
# gate, a manuscript is prepared only when its record is marked ready and not
# already done, and its record is marked done once the file is back. Everything
# else waits, untouched, for a later tick — the same "stood aside, not failed"
# posture the folder lock keeps.

def hs_ws(**over):
    """A watcher with the HubSpot gate switched on. The filename stem is the
    author key (no pattern), so "Wolves.docx" is matched to the ready Project
    whose author_last_name is "Wolves"."""
    fields = dict(folder_id=FOLDER, model="claude-haiku-4-5",
                  client_id="client-1", client_secret="secret-1",
                  hubspot_enabled=True, hubspot_object="0-970",
                  hubspot_key_property="author_last_name",
                  hubspot_status_property="docproof",
                  hubspot_format_ready_value="Ready for Formatting",
                  hubspot_format_done_value="Formatting Complete")
    fields.update(over)
    return WatchSettings(**fields)


def ready(last_name, **extra):
    """A Project record flagged ready, keyed by its author last name."""
    return {"docproof": "Ready for Formatting",
            "author_last_name": last_name, **extra}


def hs_props(opener, key="Wolves"):
    return opener.hubspot[f"hs-{key}"]["properties"]


def test_a_ready_book_is_prepared_and_marked_done_in_hubspot(tmp_path, provider):
    ws = hs_ws(hubspot_output_property="output_file")
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.prepped == ["Wolves.docx"]
    assert "Wolves - book 0.docx" in uploads_in(opener)
    props = hs_props(opener)
    assert props["docproof"] == "Formatting Complete"   # the value DocProof sets
    assert props["output_file"] == "Wolves - book 0.docx"
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.hubspot_id == "hs-Wolves" and rec.hubspot_done is True


def test_read_only_hubspot_prepares_but_never_writes_back(tmp_path, provider):
    """The gate still reads — a book runs only when its record says ready — but
    the CRM is left exactly as it was, so a real run can be watched without
    changing a record. The Drive marker is still written, so it is not run twice."""
    ws = hs_ws(hubspot_write_back=False, hubspot_output_property="output_file")
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.prepped == ["Wolves.docx"]
    assert "Wolves - book 0.docx" in uploads_in(opener)     # Drive still written
    props = hs_props(opener)
    assert props["docproof"] == "Ready for Formatting"      # CRM unchanged
    assert "output_file" not in props                       # nothing written
    assert not any("api.hubapi.com" in c.full_url and c.get_method() == "PATCH"
                   for c in opener.calls)                    # no write at all
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.hubspot_done is False
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


def test_a_book_that_is_not_ready_waits_untouched(tmp_path, provider):
    """Not-ready is a reason to wait, and waiting spends nothing, writes no
    Drive marker, and does not touch HubSpot — so setting the value later is
    all it takes."""
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": {"docproof": "",
                                            "author_last_name": "Wolves"}})

    report = run(tmp_path, ws, opener)

    assert report.waiting == 1 and report.prepped == [] and report.ok
    assert provider.calls == []                    # no model was called
    assert uploads_in(opener) == {}                # nothing put back
    assert STATE_PROP not in opener.files["f-1"].get("appProperties", {})
    assert hs_props(opener)["docproof"] == ""      # HubSpot untouched


def test_a_book_with_no_hubspot_record_waits(tmp_path, provider):
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={})

    report = run(tmp_path, ws, opener)

    assert report.waiting == 1 and report.prepped == [] and provider.calls == []


def test_a_file_whose_name_carries_no_key_waits(tmp_path, provider):
    ws = hs_ws(hubspot_key_pattern=r"ISBN (\d{13})")     # nothing to match
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")})

    report = run(tmp_path, ws, opener)

    assert report.waiting == 1 and report.prepped == [] and provider.calls == []


def test_a_book_already_done_elsewhere_is_left_alone(tmp_path, provider):
    """Marked complete in HubSpot but never prepared by us — an editor set it by
    hand. Skipped rather than re-run: not ready means not a candidate."""
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": {"docproof": "Formatting Complete",
                                            "author_last_name": "Wolves"}})

    report = run(tmp_path, ws, opener)

    assert report.waiting == 1 and report.prepped == [] and provider.calls == []


def test_a_shared_surname_flagged_twice_needs_a_person(tmp_path, provider):
    """Two ready Projects under one surname: DocProof will not guess. The file
    waits and the pass records it for a person — the case worth a notification."""
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Smith.docx")), docx=MANUSCRIPT,
                        hubspot={"a": ready("Smith"), "b": ready("Smith")})

    report = run(tmp_path, ws, opener)

    assert report.prepped == [] and provider.calls == []
    assert [n for n, _ in report.needs_human] == ["Smith.docx"]
    assert report.waiting == 1
    assert STATE_PROP not in opener.files["f-1"].get("appProperties", {})
    assert opener.emails == []                      # no address set, so no email


def test_a_needs_person_pass_emails_the_owner_when_an_address_is_set(
        tmp_path, provider):
    """The whole point of `notify_email`: an ambiguous match reaches a person
    without anyone watching the log."""
    ws = hs_ws(notify_email="quinton@atmospherepress.com")
    opener = fake_drive(folder(f_1=drive_entry("Smith.docx")), docx=MANUSCRIPT,
                        hubspot={"a": ready("Smith"), "b": ready("Smith")})

    report = run(tmp_path, ws, opener)

    assert [n for n, _ in report.needs_human] == ["Smith.docx"]
    assert len(opener.emails) == 1
    raw = base64.urlsafe_b64decode(opener.emails[0]["raw"]).decode()
    assert "Smith.docx" in raw
    assert "To: quinton@atmospherepress.com" in raw


def test_a_co_author_surname_still_matches(tmp_path, provider):
    """The record stores "Lichtenstein (and Dolores DelBello)"; a file named for
    "Lichtenstein" still finds it."""
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Lichtenstein.docx")),
                        docx=MANUSCRIPT,
                        hubspot={"lich": ready("Lichtenstein (and Dolores "
                                               "DelBello)")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.prepped == ["Lichtenstein.docx"]
    assert hs_props(opener, "lich")["docproof"] == "Formatting Complete"


def test_a_transient_ready_lookup_defers_the_whole_pass(tmp_path, provider):
    """The ready list is fetched once a pass; if HubSpot cannot be reached, every
    new book waits and the next tick tries again — nothing marked, nothing spent."""
    ws = hs_ws()
    opener = fake_drive(folder(
        f_1=drive_entry("Aardvark.docx", modified="2026-01-01T00:00:00.000Z"),
        f_2=drive_entry("Wolves.docx", modified="2026-01-02T00:00:00.000Z")),
        docx=MANUSCRIPT,
        hubspot={"Aardvark": ready("Aardvark"), "Wolves": ready("Wolves")},
        fail={"hubspot_search": http_error(503)})   # the one ready lookup dies

    report = run(tmp_path, ws, opener)

    assert report.prepped == [] and report.waiting == 2 and report.ok
    assert provider.calls == []                     # no model was called


def test_a_rejected_token_stops_the_whole_pass(tmp_path, provider):
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")},
                        fail={"hubspot_search": http_error(401)})

    with pytest.raises(HubSpotAuthError):
        run(tmp_path, ws, opener)


def test_enabling_hubspot_without_a_required_field_refuses_the_pass(
        tmp_path, provider):
    ws = hs_ws(hubspot_format_ready_value="")       # a gate that cannot ask
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")})

    with pytest.raises(ticklib.NotConfigured, match="format_ready_value"):
        run(tmp_path, ws, opener)


def test_with_hubspot_off_the_crm_is_never_asked(tmp_path, ws, provider):
    """The regression guard: an install that never heard of HubSpot behaves
    exactly as it did before, status property and all."""
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": {"docproof": ""}})

    report = run(tmp_path, ws, opener)              # the default, off ws

    assert report.prepped == ["Wolves.docx"]        # prepped despite blank status
    assert not any("api.hubapi.com" in c.full_url for c in opener.calls)


def test_a_crash_before_the_drive_marker_is_finished_next_tick(tmp_path,
                                                               provider):
    """The reachable gap (#3): HubSpot is told done, then the process dies
    before the Drive marker lands. The next tick recognises the book as ours
    and finishes the tail — no re-prep, no redundant HubSpot write."""
    ws = hs_ws()
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT,
                        hubspot={"Wolves": ready("Wolves")},
                        fail={"patch": http_error(500)})   # the marker dies

    first = run(tmp_path, ws, opener)

    assert not first.ok                             # the marker failed
    # but the status was moved on first
    assert hs_props(opener)["docproof"] == "Formatting Complete"
    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.hubspot_done is True and rec.hubspot_id == "hs-Wolves"
    assert STATE_PROP not in opener.files["f-1"].get("appProperties", {})
    spent = len(provider.calls)

    second = run(tmp_path, ws, opener)              # the marker now succeeds

    assert second.prepped == ["Wolves.docx"]        # finished, not gated away
    assert len(provider.calls) == spent             # zero re-spend
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED
    patches = [c for c in opener.calls if "api.hubapi.com" in c.full_url
               and c.get_method() == "PATCH"]
    assert len(patches) == 1                        # done was written once


# --- per-author subfolders ----------------------------------------------------
#
# `subfolders_enabled` inverts the pass: instead of listing the watched folder,
# `_discover` asks HubSpot who is ready and looks only in those authors' own
# subfolders, routing each book's outputs back into the subfolder it came from.
# The properties worth holding this to: outputs never land in the parent, an
# author who is not ready is never looked at, and a folder DocProof cannot name
# with confidence is left for a person rather than guessed.

SUB = "sf-johnson"


def sub_ws(**over):
    fields = dict(subfolders_enabled=True, hubspot_first_property="firstname",
                  hubspot_last_property="lastname")
    fields.update(over)
    return hs_ws(**fields)


def author_folder(name, parent=FOLDER):
    entry = drive_entry(name, mime=FOLDER_MIME)
    entry["parents"] = [parent]
    return entry


def in_sub(name, sub=SUB, **kw):
    entry = drive_entry(name, **kw)
    entry["parents"] = [sub]
    return entry


def ready_author(first, last, **extra):
    return {"docproof": "Ready for Formatting", "author_last_name": last,
            "firstname": first, "lastname": last, **extra}


def _queries(opener):
    out = []
    for c in opener.calls:
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(c.full_url).query).get("q")
        if q:
            out.append(q[0])
    return out


def test_a_subfolder_book_is_routed_into_its_own_subfolder(tmp_path, provider):
    ws = sub_ws(hubspot_output_property="output_file")
    opener = fake_drive({
        SUB: author_folder("Quinton Johnson"),
        "m-1": in_sub("Johnson - Book Original.docx"),
        # A decoy author, not flagged ready: proves the parent is never swept.
        "sf-2": author_folder("Jane Smith"),
        "d-1": in_sub("Smith - Book Original.docx", sub="sf-2"),
    }, docx=MANUSCRIPT, hubspot={"Johnson": ready_author("Quinton", "Johnson")})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.prepped == ["Johnson - Book Original.docx"]
    placed = uploads_in(opener)
    assert placed                                   # something was written
    for entry in placed.values():
        assert entry["parents"] == [SUB]            # never FOLDER, never sf-2
    assert not any("Smith" in name for name in placed)   # decoy untouched

    rec = WatchState.load(tmp_path / "state.json").get("m-1")
    assert rec.hubspot_id == "hs-Johnson"
    assert rec.subfolder_id == SUB
    assert rec.subfolder_name == "Quinton Johnson"
    assert rec.author_first == "Quinton" and rec.author_last == "Johnson"
    assert hs_props(opener, "Johnson")["docproof"] == "Formatting Complete"

    # A scoped name query was used; the flat parent listing never was.
    assert any("name = 'Quinton Johnson'" in q for q in _queries(opener))
    assert f"'{FOLDER}' in parents and trashed = false" not in _queries(opener)


def test_a_record_missing_a_name_is_left_for_a_person(tmp_path, provider):
    ws = sub_ws()
    opener = fake_drive(
        {SUB: author_folder("Quinton Johnson"),
         "m-1": in_sub("Johnson - Book Original.docx")},
        docx=MANUSCRIPT,
        hubspot={"Johnson": {"docproof": "Ready for Formatting",
                             "author_last_name": "Johnson",
                             "lastname": "Johnson"}})   # no firstname

    report = run(tmp_path, ws, opener)

    assert report.prepped == [] and not uploads_in(opener)
    assert report.needs_human and report.waiting >= 1


def test_a_ready_author_with_no_folder_is_left_for_a_person(tmp_path, provider):
    ws = sub_ws()
    opener = fake_drive({}, docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_author("Quinton", "Johnson")})

    report = run(tmp_path, ws, opener)

    assert report.prepped == []
    assert report.needs_human
    assert "Quinton Johnson" in report.needs_human[0][0]


def test_two_folders_for_one_author_is_left_for_a_person(tmp_path, provider):
    ws = sub_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "sf-x": author_folder("Quinton Johnson"),
                         "m-1": in_sub("Johnson - Book Original.docx")},
                        docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_author("Quinton", "Johnson")})

    report = run(tmp_path, ws, opener)

    assert report.prepped == [] and not uploads_in(opener)
    assert report.needs_human


def test_two_manuscripts_in_a_subfolder_is_left_for_a_person(tmp_path, provider):
    ws = sub_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "m-1": in_sub("Johnson - Book Original.docx"),
                         "m-2": in_sub("Johnson - Draft Two.docx")},
                        docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_author("Quinton", "Johnson")})

    report = run(tmp_path, ws, opener)

    assert report.prepped == [] and not uploads_in(opener)
    assert report.needs_human


def test_a_dry_run_in_subfolder_mode_writes_no_state(tmp_path, provider):
    ws = sub_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "m-1": in_sub("Johnson - Book Original.docx")},
                        docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_author("Quinton", "Johnson")})

    report = run(tmp_path, ws, opener, dry_run=True)

    assert report.new == 1                          # it found the book
    assert not uploads_in(opener)
    rec = WatchState.load(tmp_path / "state.json").get("m-1")
    assert rec.hubspot_id == "" and rec.subfolder_id == ""


def test_an_in_flight_book_is_readopted_even_when_not_ready(tmp_path, provider):
    """A book a previous tick started, whose status has since moved off ready,
    is re-listed from the subfolder it recorded — so `_drain` and resume still
    find its manuscript instead of parking it as gone from the folder."""
    ws = sub_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "m-1": in_sub("Johnson - Book Original.docx")},
                        docx=MANUSCRIPT, hubspot={})   # nothing ready now

    state = WatchState(tmp_path / "state.json")
    rec = state.get("m-1")
    rec.hubspot_id, rec.subfolder_id, rec.marked = "hs-Johnson", SUB, ""
    state.record(rec)

    listing, routes = ticklib._discover("tok", "hs", ws, state, opener=opener,
                                        report=ticklib.TickReport(),
                                        dry_run=False)

    assert any(f.id == "m-1" for f in listing)
    assert routes.get("m-1") == SUB


# --- the completion email is gated on being asked for -------------------------

def test_a_finished_book_emails_the_full_log_when_asked(tmp_path, ws, provider):
    ws.notify_email = "quinton@atmospherepress.com"
    ws.notify_on_complete = True
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT)

    report = run(tmp_path, ws, opener)

    assert report.ok and len(opener.emails) == 1
    decoded = base64.urlsafe_b64decode(opener.emails[0]["raw"]).decode()
    assert "Wolves" in decoded and "estimated" in decoded.lower()


def test_no_completion_email_unless_the_flag_is_set(tmp_path, ws, provider):
    ws.notify_email = "quinton@atmospherepress.com"   # address set, flag off
    opener = fake_drive(folder(f_1=drive_entry("Wolves.docx")), docx=MANUSCRIPT)

    run(tmp_path, ws, opener)

    assert opener.emails == []
