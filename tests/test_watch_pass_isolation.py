"""A HubSpot selection belongs only to its pass, including previews/resumes."""
import pytest

from app.watch import tick as ticklib
from app.watch.stages import PROOF_DONE, PROOF_FAILED, PROOF_PROP
from app.watch.state import WatchState

from .fakes import fake_drive
from .test_watch_proof import (BOOK, FOLDER, ORIGINAL, SUB, author_folder, in_sub,
                               ready_to_proof, run, sub_proof_ws)


@pytest.mark.parametrize("ready,expected", [
    ("Ready for Proofing", [(BOOK, "proof")]),
    ("Ready for Formatting", [(ORIGINAL, "new")]),
    ("Ready for Formatting/Proofing", []),
])
def test_preview_shows_only_the_selected_pass(tmp_path, ready, expected):
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "original": in_sub(ORIGINAL),
                         "proof": in_sub(BOOK)},
                        hubspot={"Johnson": ready_to_proof(docproof=ready)})

    report = run(tmp_path, sub_proof_ws(), opener, dry_run=True)

    assert report.plan == expected
    assert report.new == sum(stage == "new" for _, stage in expected)
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("subfolders", [False, True])
@pytest.mark.parametrize("ready,expected", [
    ("Ready for Proofing", [("proof", "proof")]),
    ("Ready for Formatting", [("format", "original")]),
])
def test_pickup_runs_only_the_selected_pass_with_both_sources_present(
        tmp_path, monkeypatch, subfolders, ready, expected):
    parent = SUB if subfolders else FOLDER
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "original": in_sub(ORIGINAL, sub=parent),
                         "proof": in_sub(BOOK, sub=parent)},
                        hubspot={"Johnson": ready_to_proof(docproof=ready)})
    picked = []
    monkeypatch.setattr(ticklib, "_one", lambda *args, **kwargs:
                        picked.append(("format", args[3].id)))
    monkeypatch.setattr(ticklib, "_one_proof", lambda *args, **kwargs:
                        picked.append(("proof", args[3].id)))

    report = run(tmp_path, sub_proof_ws(subfolders_enabled=subfolders),
                 opener, mock=True)

    assert report.ok
    assert picked == expected


@pytest.mark.parametrize("enabled,props", [
    (False, {}),
    (True, {PROOF_PROP: PROOF_DONE}),
])
def test_flat_preview_does_not_offer_disabled_or_completed_proofing(
        tmp_path, enabled, props):
    opener = fake_drive({"proof": in_sub(BOOK, sub=FOLDER, props=props)},
                        hubspot={"Johnson": ready_to_proof()})
    ws = sub_proof_ws(subfolders_enabled=False, proofing_enabled=enabled)

    report = run(tmp_path, ws, opener, dry_run=True)

    assert report.plan == []


def test_failed_proof_metadata_is_reported_as_failed_not_completed(tmp_path):
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "proof": in_sub(BOOK, props={PROOF_PROP: PROOF_FAILED})},
                        hubspot={"Johnson": ready_to_proof()})

    report = run(tmp_path, sub_proof_ws(), opener, dry_run=True)

    assert report.plan == []
    assert report.stuck_ready == []
    assert len(report.needs_human) == 1
    assert "marked failed" in report.needs_human[0][1]


@pytest.mark.parametrize("source,extra,id_field,runner_name", [
    (ORIGINAL, "Other - Book Original.docx", "hubspot_id", "_one"),
    (BOOK, "Other - Book 1.docx", "proof_hubspot_id", "_one_proof"),
])
def test_resuming_a_pass_does_not_pick_up_an_unflagged_neighbor(
        tmp_path, monkeypatch, source, extra, id_field, runner_name):
    opener = fake_drive({SUB: author_folder("Quinton Johnson"),
                         "started": in_sub(source),
                         "unflagged": in_sub(extra)},
                        hubspot={"Johnson": ready_to_proof(docproof="Do Nothing")})
    state = WatchState(tmp_path / "state.json")
    rec = state.get("started")
    setattr(rec, id_field, "hs-Johnson")
    rec.subfolder_id = SUB
    state.record(rec)
    started = []
    monkeypatch.setattr(ticklib, runner_name,
                        lambda *args, **kwargs: started.append(args[3].id))

    report = run(tmp_path, sub_proof_ws(), opener, mock=True)

    assert report.ok
    assert started == ["started"]
    assert "unflagged" not in WatchState.load(tmp_path / "state.json").files
