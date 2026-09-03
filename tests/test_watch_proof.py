"""The proofing stage, end to end, with a fake Drive, a fake HubSpot and no
model at all.

The properties worth holding this to are the ones that cost money or mislead a
person: a book is read once and only once; the CRM moves on exactly once, and
only for a verdict of `done`; a `needs_human` verdict writes NOTHING and reaches
a person instead; and what proofing writes into the folder is never mistaken for
a manuscript to work on again.

Nothing here touches a network, a model, or a clock.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path

import pytest

from app.jobs import JobStore
from app.settings import Paths
from app.watch import proof as prooflib
from app.watch import tick as ticklib
from app.watch.drive import FOLDER_MIME
from app.watch.settings import WatchSettings
from app.watch.stages import (OUTPUT_PROP, PROOF_AWAITING, PROOF_DONE,
                              PROOF_HUMAN, PROOF_PROP, STATE_PROP,
                              Stage, classify, is_proof_candidate)
from app.watch.state import WatchState

from .conftest import FIXTURES
from .fakes import drive_entry, fake_drive

FOLDER = "1AbCdEfGhIjKlMnOp"
SUB = "sf-johnson"
MANUSCRIPT = (FIXTURES / "googledoc.docx").read_bytes()

BOOK = "Johnson - Book Original.docx"
DELIVERABLE = "Johnson - book 1.docx"
LETTER = "Johnson - book 1 - letter.md"
STYLE_SHEET = "Johnson - book 1 - style-sheet.md"
OUTCOME = "Johnson - book 1 - outcome.json"


# --- the watcher --------------------------------------------------------------

def proof_ws(**over) -> WatchSettings:
    """A watcher with the HubSpot gate on and proofing switched on, flat-folder.
    The filename stem is the author key, so "Johnson - Book Original.docx" has
    to be matched by a pattern that pulls the surname out of it."""
    fields = dict(folder_id=FOLDER, model="claude-haiku-4-5",
                  client_id="client-1", client_secret="secret-1",
                  hubspot_enabled=True, hubspot_object="0-970",
                  hubspot_key_property="author_last_name",
                  hubspot_status_property="docproof",
                  hubspot_format_ready_value="Ready for Formatting",
                  hubspot_format_done_value="Formatting Complete",
                  proofing_enabled=True,
                  hubspot_key_pattern=r"^([^-]+?)\s*-")
    fields.update(over)
    return WatchSettings(**fields)


def sub_proof_ws(**over) -> WatchSettings:
    """The same, in the mode production runs in: per-author subfolders, and only
    the "<surname> - Book Original" is ever the book."""
    fields = dict(subfolders_enabled=True, require_source_label=True,
                  hubspot_first_property="firstname",
                  hubspot_last_property="lastname")
    fields.update(over)
    return proof_ws(**fields)


def ready_to_proof(last="Johnson", first="Quinton", **extra) -> dict:
    return {"docproof": "Ready for Proofing", "author_last_name": last,
            "firstname": first, "lastname": last, **extra}


def folder(**files) -> dict:
    return {name.replace("_", "-", 1): entry for name, entry in files.items()}


def author_folder(name, parent=FOLDER) -> dict:
    entry = drive_entry(name, mime=FOLDER_MIME)
    entry["parents"] = [parent]
    return entry


def in_sub(name, sub=SUB, **kw) -> dict:
    entry = drive_entry(name, **kw)
    entry["parents"] = [sub]
    return entry


def run(home, ws, opener, **kw):
    return ticklib.tick(home, ws, opener=opener,
                        get_key=lambda name: "refresh-1", **kw)


def uploads_in(opener) -> dict:
    return {entry["name"]: entry for entry in opener.files.values()
            if entry.get("appProperties", {}).get(OUTPUT_PROP)}


def patches(opener) -> list:
    return [c for c in opener.calls if "api.hubapi.com" in c.full_url
            and c.get_method() == "PATCH"]


def hs_props(opener, key="Johnson") -> dict:
    return opener.hubspot[f"hs-{key}"]["properties"]


# --- a galley run that costs nothing ------------------------------------------

@pytest.fixture
def galley(monkeypatch):
    """A galley job that finishes without a model.

    It writes exactly what a real run leaves in its results folder — the
    tracked-changes manuscript under DocProof's own suffix, the change log
    beside it, the letter and the style sheet — and marks the job done. The
    verdict is NOT faked: `proof.assess` runs for real over the folder, so the
    outcome.json these tests assert on is the one the shipping code writes.

    The list it returns is every job it ran, which is how "the book was read
    once and only once" is asked."""
    ran = []

    def fake_run_job(runner, store, job):
        out = Path(runner.results_dir(job))
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(job.filename).stem
        (out / f"{stem} - Atmosphere Press Proofreader.docx").write_bytes(b"dx")
        (out / f"{stem} - Atmosphere Press Proofreader Change Log.docx"
         ).write_bytes(b"log")
        (out / "letter.md").write_text("# Editorial letter\n", encoding="utf-8")
        (out / "style-sheet.md").write_text("# Style sheet\n", encoding="utf-8")
        store.save(job)
        store.update(job.id, state="done", results_dir=str(out), cost=1.25)
        ran.append(job)
        return store.get(job.id)

    monkeypatch.setattr(prooflib, "run_job", fake_run_job)
    return ran


@pytest.fixture
def needs_human(monkeypatch):
    """The other verdict, from the assessor rather than from the folder: the
    thresholds themselves are `galley.outcome`'s to test, so here the sentence
    is stubbed and what is under test is what the watcher does with it."""
    from galley.outcome import Outcome

    def fake_assess(run_dir, **kw):
        return Outcome(outcome="needs_human",
                       reason="most sentences must be rewritten",
                       evidence={"rewrite_share": 0.9})

    monkeypatch.setattr("galley.outcome.assess", fake_assess)


# --- what proofing writes is never a manuscript -------------------------------

def test_the_book_1_files_are_recognised_as_output():
    """The name belt: every file the proofread hands back carries the "- book 1"
    stage token, so a folder full of them is never read as four new manuscripts
    — marker or no marker, which is the case that matters for the external
    runner, whose files DocProof did not upload."""
    for name in (DELIVERABLE, LETTER, STYLE_SHEET, OUTCOME):
        bare = drive_entry(name)
        assert classify(_file(bare, "x")) is Stage.OUTPUT
        assert not is_proof_candidate(_file(bare, "x"))


def test_the_book_original_is_still_a_proof_candidate_once_formatted():
    """Proofing is the second pass over the same book, so the formatting marker
    must not hide it — but proofing's own terminal marker must."""
    formatted = _file(drive_entry(BOOK, props={STATE_PROP: "formatted"}), "m-1")
    assert is_proof_candidate(formatted)
    assert classify(formatted) is Stage.DONE      # formatting is done with it

    for done in (PROOF_DONE, PROOF_HUMAN, "failed"):
        marked = _file(drive_entry(BOOK, props={PROOF_PROP: done}), "m-1")
        assert not is_proof_candidate(marked)
    # "awaiting" is deliberately not terminal: a book out with a practitioner
    # has to stay visible or its verdict would never be picked up.
    waiting = _file(drive_entry(BOOK, props={PROOF_PROP: PROOF_AWAITING}), "m-1")
    assert is_proof_candidate(waiting)


def _file(entry: dict, file_id: str):
    from app.watch.drive import DriveFile
    return DriveFile.from_api({**entry, "id": file_id})


# --- discovery ----------------------------------------------------------------

def test_a_book_ready_for_proofing_is_read_and_flipped(tmp_path, galley):
    """The whole stage in one pass: discovered at "Ready for Proofing", read,
    the four files delivered, and the record moved to "Proofing Complete"."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.proofed == [BOOK]
    assert report.prepped == []                    # formatting did not run
    assert set(uploads_in(opener)) == {DELIVERABLE, LETTER, STYLE_SHEET, OUTCOME}
    assert hs_props(opener)["docproof"] == "Proofing Complete"
    assert len(patches(opener)) == 1               # exactly one write
    assert opener.files["f-1"]["appProperties"][PROOF_PROP] == PROOF_DONE

    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.proof_hubspot_id == "hs-Johnson"
    assert rec.proof_hubspot_done is True
    assert rec.proof_outcome == "done"
    assert len(galley) == 1


def test_the_outcome_that_ships_is_the_one_the_verdict_was_read_from(tmp_path,
                                                                     galley):
    """outcome.json goes to the folder whole, so whoever reads it next — a
    person, the archive, a later tick — sees the numbers the flip was made on."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)

    uploaded = [fid for fid, e in opener.files.items() if e["name"] == OUTCOME][0]
    payload = json.loads(opener.content[uploaded].decode())
    assert payload["outcome"] == "done"
    assert payload["hubspot"]["value"] == "Proofing Complete"
    local = Path(JobStore(Paths(tmp_path)).all()[0].results_dir) / "outcome.json"
    assert json.loads(local.read_text("utf-8")) == payload


def test_a_second_pass_neither_re_reads_nor_re_writes(tmp_path, galley):
    """The marker and the state file together: nothing is paid for twice, and
    the CRM is written exactly once however many passes run."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)
    second = run(tmp_path, ws, opener)

    assert second.proofed == []
    assert len(galley) == 1                        # the book was read once
    assert len(patches(opener)) == 1               # and moved on once


def test_a_book_not_flagged_for_proofing_is_left_alone(tmp_path, galley):
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": {"docproof": "Formatting Complete",
                                             "author_last_name": "Johnson"}})

    report = run(tmp_path, ws, opener)

    assert report.proofed == [] and galley == []
    assert uploads_in(opener) == {}
    assert PROOF_PROP not in opener.files["f-1"].get("appProperties", {})


def test_proofing_off_changes_nothing(tmp_path, galley):
    """The regression guard the prod config depends on: an unchanged install —
    proofing never switched on — behaves exactly as it did before this existed,
    even with books sitting at "Ready for Proofing"."""
    ws = proof_ws(proofing_enabled=False)
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener)

    assert report.proofed == [] and galley == []
    assert uploads_in(opener) == {} and patches(opener) == []


def test_a_shared_surname_flagged_twice_needs_a_person(tmp_path, galley):
    """Two Projects ready to proofread under one surname: DocProof will not
    guess which book the file is. Nothing is read, nothing is written, and a
    person is told."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry("Smith - Book Original.docx")),
                        docx=MANUSCRIPT,
                        hubspot={"a": ready_to_proof(last="Smith"),
                                 "b": ready_to_proof(last="Smith")})

    report = run(tmp_path, ws, opener)

    assert report.proofed == [] and galley == []
    assert [n for n, _ in report.needs_human] == ["Smith - Book Original.docx"]
    assert "cannot tell which book" in report.needs_human[0][1]
    assert patches(opener) == []
    assert PROOF_PROP not in opener.files["f-1"].get("appProperties", {})


def test_a_ready_book_in_its_authors_subfolder_is_found_and_delivered_there(
        tmp_path, galley):
    """Production's mode. Proofing discovers its own authors at its own ready
    value — the parent Author Folder is never listed — and everything it writes
    lands in the book's own subfolder."""
    ws = sub_proof_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "m-1": in_sub(BOOK),
                         # a decoy author nobody flagged
                         "sf-2": author_folder("Jane Smith"),
                         "d-1": in_sub("Smith - Book Original.docx", sub="sf-2")},
                        docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener)

    assert report.ok and report.proofed == [BOOK]
    placed = uploads_in(opener)
    assert set(placed) == {DELIVERABLE, LETTER, STYLE_SHEET, OUTCOME}
    for entry in placed.values():
        assert entry["parents"] == [SUB]           # never the parent, never sf-2
    assert hs_props(opener)["docproof"] == "Proofing Complete"

    rec = WatchState.load(tmp_path / "state.json").get("m-1")
    assert rec.proof_hubspot_id == "hs-Johnson" and rec.subfolder_id == SUB

    queries = [urllib.parse.parse_qs(
        urllib.parse.urlparse(c.full_url).query).get("q", [""])[0]
        for c in opener.calls]
    assert f"'{FOLDER}' in parents and trashed = false" not in queries


def test_a_book_ready_for_proofing_is_never_formatted_as_well(tmp_path, galley,
                                                              monkeypatch):
    """The one way the two stages could collide in subfolder mode: `run_prep`
    prepares every candidate in ITS listing without gating again, so proofing
    must never put its books there. Nothing is prepped, and no format job runs."""
    ws = sub_proof_ws()
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "m-1": in_sub(BOOK)},
                        docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    def no_prep(*a, **kw):                 # pragma: no cover - must not be hit
        raise AssertionError("the formatting stage read a proofing book")

    monkeypatch.setattr(ticklib.prep, "run_job", no_prep)

    report = run(tmp_path, ws, opener)

    assert report.proofed == [BOOK] and report.prepped == []
    assert STATE_PROP not in opener.files["m-1"].get("appProperties", {})


# --- needs_human --------------------------------------------------------------

def test_needs_human_writes_nothing_to_hubspot_and_tells_a_person(
        tmp_path, galley, needs_human):
    """The verdict that must never move a record. The toggle stays at "Ready
    for Proofing" — which is what tells a human proofreader to pick the book up
    — and the reason reaches the owner by email."""
    ws = proof_ws(notify_email="quinton@atmospherepress.com")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener)

    assert report.proofed == [BOOK]                # it was read
    assert patches(opener) == []                   # and nothing was written
    assert hs_props(opener)["docproof"] == "Ready for Proofing"

    assert [n for n, _ in report.needs_human] == [BOOK]
    assert "most sentences must be rewritten" in report.needs_human[0][1]
    assert len(opener.emails) == 1
    raw = base64.urlsafe_b64decode(opener.emails[0]["raw"]).decode()
    assert "most sentences must be rewritten" in raw
    assert "To: quinton@atmospherepress.com" in raw

    rec = WatchState.load(tmp_path / "state.json").get("f-1")
    assert rec.proof_outcome == "needs_human" and rec.proof_hubspot_done is False
    assert opener.files["f-1"]["appProperties"][PROOF_PROP] == PROOF_HUMAN


def test_a_book_left_for_a_human_is_not_read_again(tmp_path, galley,
                                                   needs_human):
    """`needs_human` is terminal for DocProof: reading the book a second time
    would buy the same answer at the same price."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)
    second = run(tmp_path, ws, opener)

    assert second.proofed == [] and len(galley) == 1
    assert second.needs_human == []                # said once, not every morning


# --- the external runner ------------------------------------------------------

def test_external_mode_waits_and_says_so(tmp_path, galley):
    """DocWatch runs nothing: it notices the book, marks it, and tells the owner
    where to find it. The Mac-side practitioner does the reading."""
    ws = proof_ws(proof_runner="external",
                  notify_email="quinton@atmospherepress.com")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener)

    assert galley == [] and report.proofed == []
    assert uploads_in(opener) == {} and patches(opener) == []
    assert [n for n, _ in report.awaiting_proof] == [BOOK]
    assert OUTCOME in report.awaiting_proof[0][1]
    assert opener.files["f-1"]["appProperties"][PROOF_PROP] == PROOF_AWAITING

    raw = base64.urlsafe_b64decode(opener.emails[0]["raw"]).decode()
    assert BOOK in raw and FOLDER in raw

    # A second pass says nothing new: the book is still waiting, and a book that
    # waits a week is not news every morning.
    again = run(tmp_path, ws, opener)
    assert again.awaiting_proof == [] and len(opener.emails) == 1


def test_external_mode_picks_the_verdict_up_when_it_lands(tmp_path, galley):
    """The hand-off: the practitioner drops the four files in the author's
    folder, and the next pass reads the outcome and moves the record on."""
    ws = proof_ws(proof_runner="external")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    first = run(tmp_path, ws, opener)
    assert [n for n, _ in first.awaiting_proof] == [BOOK]

    _hand_off(opener, {"outcome": "done", "reason": "no open items",
                       "hubspot": {"object": "0-970", "property": "docproof",
                                   "value": "Proofing Complete"}})

    second = run(tmp_path, ws, opener)

    assert second.proofed == [BOOK] and galley == []
    assert hs_props(opener)["docproof"] == "Proofing Complete"
    assert len(patches(opener)) == 1
    assert opener.files["f-1"]["appProperties"][PROOF_PROP] == PROOF_DONE


def test_an_external_needs_human_verdict_writes_nothing(tmp_path, galley):
    ws = proof_ws(proof_runner="external")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)
    _hand_off(opener, {"outcome": "needs_human",
                       "reason": "the book needs a human", "hubspot": {}})
    report = run(tmp_path, ws, opener)

    assert patches(opener) == []
    assert hs_props(opener)["docproof"] == "Ready for Proofing"
    assert [n for n, _ in report.needs_human] == [BOOK]
    assert "the book needs a human" in report.needs_human[0][1]


def test_a_handed_off_outcome_cannot_name_the_property_to_write(tmp_path,
                                                                galley):
    """The file was placed in Drive by something outside DocProof, so its
    `hubspot` block is read as decoration, never as an instruction: the property
    and the value both come from the watcher's own settings."""
    ws = proof_ws(proof_runner="external", hubspot_output_property="output_file")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)
    _hand_off(opener, {"outcome": "done", "reason": "clean",
                       "hubspot": {"object": "0-970",
                                   "property": "hs_pipeline_stage",
                                   "value": "Closed Won"}})
    run(tmp_path, ws, opener)

    props = hs_props(opener)
    assert props["docproof"] == "Proofing Complete"
    assert "hs_pipeline_stage" not in props
    assert json.loads(patches(opener)[0].data)["properties"] == {
        "docproof": "Proofing Complete"}


def test_an_unreadable_outcome_waits_rather_than_guessing(tmp_path, galley):
    """Half a file, or an outcome nobody has heard of, is not a verdict. The
    book stays where it is and the next pass looks again."""
    ws = proof_ws(proof_runner="external")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    run(tmp_path, ws, opener)
    _hand_off(opener, {"outcome": "probably fine"})
    report = run(tmp_path, ws, opener)

    assert report.proofed == [] and patches(opener) == []
    assert opener.files["f-1"]["appProperties"][PROOF_PROP] == PROOF_AWAITING


def test_a_verdict_is_read_from_the_books_own_folder(tmp_path, galley):
    """Two authors share a surname, each in their own folder, each with a
    "Smith - Book Original" — so each hand-off is called "Smith - book 1 -
    outcome.json". Scanning the pass's whole listing by name would let one
    author's verdict move the other author's record on. The lookup is scoped to
    the folder the book was found in, so only the one that really landed counts."""
    ws = sub_proof_ws(proof_runner="external")
    opener = fake_drive({
        "sf-a": author_folder("John Smith"),
        "m-a": in_sub("Smith - Book Original.docx", sub="sf-a"),
        "sf-b": author_folder("Jane Smith"),
        "m-b": in_sub("Smith - Book Original.docx", sub="sf-b"),
    }, docx=MANUSCRIPT,
        hubspot={"john": ready_to_proof(last="Smith", first="John"),
                 "jane": ready_to_proof(last="Smith", first="Jane")})

    run(tmp_path, ws, opener)
    # Only John's practitioner has answered.
    _hand_off(opener, {"outcome": "done", "reason": "clean"},
              name="Smith - book 1 - outcome.json", parent="sf-a")

    report = run(tmp_path, ws, opener)

    assert len(report.proofed) == 1
    assert hs_props(opener, "john")["docproof"] == "Proofing Complete"
    assert hs_props(opener, "jane")["docproof"] == "Ready for Proofing"
    assert opener.files["m-a"]["appProperties"][PROOF_PROP] == PROOF_DONE
    assert opener.files["m-b"]["appProperties"][PROOF_PROP] == PROOF_AWAITING


def _hand_off(opener, payload: dict, name: str = OUTCOME,
              parent: str = FOLDER) -> str:
    """An external practitioner's outcome file, placed the way a practitioner
    places one: no appProperties, no marker, just the agreed name."""
    file_id = f"ext-{name}"
    opener.files[file_id] = {"id": file_id, "name": name,
                             "mimeType": "application/json",
                             "appProperties": {}, "parents": [parent],
                             "modifiedTime": "2026-02-01T00:00:00.000Z",
                             "size": "512"}
    opener.content[file_id] = json.dumps(payload).encode()
    return file_id


# --- the settings contract ----------------------------------------------------

def test_the_defaults_are_no_new_behaviour():
    """An unchanged prod config must keep behaving exactly as it does today. The
    ready/done values carry the press's real vocabulary, but nothing reads them
    until the switch is on."""
    ws = WatchSettings()
    assert ws.proofing_enabled is False
    assert ws.proof_runner == "app"
    assert ws.hubspot_proof_ready_value == "Ready for Proofing"
    assert ws.hubspot_proof_done_value == "Proofing Complete"
    assert ws.proof_tier == "T2" and ws.proof_budget_usd == 0.0


def test_the_settings_survive_a_save_and_load(tmp_path):
    ws = WatchSettings(folder_id=FOLDER, proofing_enabled=True,
                       proof_runner="external", proof_tier="T3",
                       proof_budget_usd=42.5,
                       hubspot_proof_ready_value="Ready for Proofing",
                       hubspot_proof_done_value="Proofing Complete")
    ws.save(tmp_path)

    back = WatchSettings.load(tmp_path)

    assert back.proofing_enabled is True and back.proof_runner == "external"
    assert back.proof_tier == "T3" and back.proof_budget_usd == 42.5
    assert back.hubspot_proof_done_value == "Proofing Complete"


def test_proofing_without_hubspot_is_refused(tmp_path, galley):
    ws = proof_ws(hubspot_enabled=False)
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT)

    with pytest.raises(ticklib.NotConfigured, match="HubSpot"):
        run(tmp_path, ws, opener)


def test_a_blank_proof_done_value_is_refused_before_the_pass(tmp_path, galley):
    """Half-configured is worse than off: a patch of an empty value would blank
    the status property rather than move it on."""
    ws = proof_ws(hubspot_proof_done_value="")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT)

    with pytest.raises(ticklib.NotConfigured, match="proof_done_value"):
        run(tmp_path, ws, opener)


def test_an_unknown_runner_is_refused(tmp_path, galley):
    ws = proof_ws(proof_runner="magic")
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT)

    with pytest.raises(ticklib.NotConfigured, match="proof_runner"):
        run(tmp_path, ws, opener)


def test_a_mock_pass_never_reads_a_book(tmp_path, galley):
    """`--mock-tags` is documented as costing nothing, and a galley wave loop
    over a novel has no free rehearsal, so proofing stands aside."""
    ws = proof_ws()
    opener = fake_drive(folder(f_1=drive_entry(BOOK)), docx=MANUSCRIPT,
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, ws, opener, mock=True)

    assert galley == [] and report.proofed == []
    assert patches(opener) == []


def test_the_budget_falls_back_to_the_tier(tmp_path):
    assert prooflib.budget_for(WatchSettings(proof_tier="T1")) == 30.0
    assert prooflib.budget_for(
        WatchSettings(proof_tier="T1", proof_budget_usd=7.5)) == 7.5


def test_the_hand_off_names_are_the_agreed_four():
    assert prooflib.hand_off_names(BOOK) == {
        "manuscript": DELIVERABLE, "letter": LETTER,
        "style_sheet": STYLE_SHEET, "outcome": OUTCOME}


def test_the_job_it_makes_is_a_galley_job(tmp_path):
    job = prooflib.make_job(tmp_path / "Johnson - Book Original.docx",
                            proof_ws(proof_tier="T3"))
    assert job.kind == "galley" and job.tier == "T3"
    assert job.budget_usd == 150.0 and job.source == "watch"


# --- the CLI ------------------------------------------------------------------

def test_the_cli_turns_proofing_on_and_off(tmp_path, capsys):
    from app.watch import cli

    # UNUSABLE, because this home has no Google sign-in — the settings are
    # still written, which is what is under test here.
    assert cli.main(["--home", str(tmp_path), "init", "--folder", FOLDER,
                     "--enable-hubspot", "--hubspot-object", "0-970",
                     "--hubspot-key-property", "author_last_name",
                     "--hubspot-status-property", "docproof",
                     "--hubspot-format-ready-value", "Ready for Formatting",
                     "--hubspot-format-done-value", "Formatting Complete",
                     "--enable-proofing", "--proof-runner", "external",
                     "--proof-tier", "T3", "--proof-budget", "12.5"]
                    ) == cli.UNUSABLE

    ws = WatchSettings.load(tmp_path)
    assert ws.proofing_enabled is True and ws.proof_runner == "external"
    assert ws.proof_tier == "T3" and ws.proof_budget_usd == 12.5
    assert ws.hubspot_proof_ready_value == "Ready for Proofing"
    printed = capsys.readouterr().out
    assert "Proofing on" in printed and "Ready for Proofing" in printed

    cli.main(["--home", str(tmp_path), "init", "--disable-proofing"])
    assert WatchSettings.load(tmp_path).proofing_enabled is False
    # The values are kept, so turning it back on does not re-ask for them.
    assert WatchSettings.load(tmp_path).hubspot_proof_done_value == \
        "Proofing Complete"


def test_the_cli_can_set_the_proof_values(tmp_path):
    from app.watch import cli

    cli.main(["--home", str(tmp_path), "init", "--folder", FOLDER,
              "--hubspot-proof-ready-value", "Ready for Proofing",
              "--hubspot-proof-done-value", "Formatting/Proofing Complete"])

    ws = WatchSettings.load(tmp_path)
    assert ws.hubspot_proof_done_value == "Formatting/Proofing Complete"
