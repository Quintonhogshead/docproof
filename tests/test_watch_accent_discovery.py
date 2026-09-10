"""Accented HubSpot names discover an unaccented Drive book end to end."""
import pytest

from app.watch import tick as ticklib
from app.watch.state import WatchState
from app.watch.tick import TickReport
from tests.fakes import fake_drive
from tests.test_watch_tick import (FOLDER, SUB, author_folder, in_sub,
                                   ready_author, sub_ws, _queries)


@pytest.mark.parametrize("stage_name,source,ready", [
    ("format", "Book Original", "Ready for Formatting"),
    ("proof", "Book 1", "Ready for Proofing"),
])
def test_accented_hubspot_name_finds_folder_and_its_manuscript(
        tmp_path, stage_name, source, ready):
    ws = sub_ws(require_source_label=True,
                hubspot_proof_ready_value="Ready for Proofing")
    opener = fake_drive({
        SUB: author_folder("Jose Aragon"),
        "m-1": in_sub(f"Aragon - {source}.docx"),
        "m-2": in_sub(f"Another - {source}.docx"),
    }, hubspot={"Aragón": ready_author("José", "Aragón", docproof=ready)})
    state = WatchState(tmp_path / "state.json")
    report = TickReport()
    stage = getattr(ticklib, stage_name + "_stage")(ws)

    listing, routes = ticklib._discover(
        "drive-token", "hs-token", ws, state, stage=stage, opener=opener,
        report=report, dry_run=False)

    assert not report.needs_human and not report.missing_source
    assert routes["m-1"] == SUB
    assert "m-2" not in routes
    assert any(f.id == "m-1" for f in listing)
    rec = state.get("m-1")
    assert stage.id_get(rec) == "hs-Aragón"
    assert rec.author_first == "José" and rec.author_last == "Aragón"
    assert f"'{FOLDER}' in parents and trashed = false" not in _queries(opener)
