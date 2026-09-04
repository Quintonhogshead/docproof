"""A run that STOPS still hands off.

The whole external proofing loop hangs on one file: DocWatch marks a book
`awaiting` and then waits for `<surname> - Book 2 - outcome.json` to appear
beside it. A run that stopped at the ladder has no manuscript and no letter,
but it does have an answer — `needs_human`, with the reason — and if that
answer never reaches the folder the book sits at "Ready for Proofing"
indefinitely with nobody told.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from galley import driver as gd
from .test_driver import (FIXTURE, FakeSpawner, MECH_PLAN, _deliverable,
                          _driver, _plan)


@pytest.fixture()
def book(tmp_path) -> Path:
    dest = tmp_path / "Ford - Book 1.docx"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def _ws(book: Path, tmp_path: Path) -> Path:
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, MECH_PLAN)
    return ws


def _failing(ws: Path, phase: str, rc: int = 5) -> FakeSpawner:
    return FakeSpawner(ws, fail=phase, rc=rc)


# --- the partial hand-off -----------------------------------------------------

def test_a_stopped_run_uploads_its_verdict(book, tmp_path):
    ws = _ws(book, tmp_path)
    uploaded: list[tuple[str, str]] = []

    def upload(files, folder_id):
        uploaded.extend((p.name, folder_id) for p in files)
        return [f"id-{i}" for i, _ in enumerate(files)]

    result = _driver(book, tmp_path, spawn=_failing(ws, "ladder"),
                     drive_folder_id="folder-A", upload=upload,
                     handoff_dir=tmp_path / "handoff").run()

    assert result.outcome == "needs_human"
    assert result.stopped_at == "ladder"
    names = [n for n, _f in uploaded]
    # The verdict is the required file, and it went to the author's folder
    # under the name DocWatch looks for.
    assert "Ford - Book 2 - outcome.json" in names
    assert {f for _n, f in uploaded} == {"folder-A"}
    # The decision log rides along, because a person picking this up needs to
    # know what was decided before it stopped.
    assert "Ford - Book 2 - decision-log.md" in names
    # …and nothing that does not exist is invented.
    assert "Ford - Book 2.docx" not in names
    delivered = json.loads(
        (tmp_path / "handoff" / "Ford - Book 2 - outcome.json"
         ).read_text("utf-8"))
    assert delivered["outcome"] == "needs_human"
    assert "exited 5" in delivered["reason"]


def test_the_drivers_own_verdict_beats_a_settle_done(book, tmp_path):
    """A run that settled `done` and then failed to certify must NOT hand back
    `done`: the run says it is not finished, and the hand-off says what the run
    says."""
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    run = ws / "runs" / "final"
    run.mkdir(parents=True, exist_ok=True)
    (run / "findings.json").write_text(json.dumps({"findings": []}), "utf-8")
    (run / "outcome.json").write_text(json.dumps(
        {"outcome": "done", "reason": "no open items", "evidence": {},
         "hubspot": {}, "set_by": "assess"}), encoding="utf-8")

    result = _driver(book, tmp_path, spawn=_failing(ws, "certify", rc=4),
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "needs_human"
    delivered = json.loads(
        (tmp_path / "handoff" / "Ford - Book 2 - outcome.json"
         ).read_text("utf-8"))
    assert delivered["outcome"] == "needs_human"
    assert delivered["set_by"] == "galley drive"


def test_a_gate_refusal_also_comes_back(book, tmp_path):
    """The earliest possible stop — the plan gate — still delivers a verdict."""
    ws = _ws(book, tmp_path)
    _plan(ws, MECH_PLAN.replace("TOTAL est. $2.80", "TOTAL est. $99.00"))
    uploaded: list[str] = []
    result = _driver(book, tmp_path, spawn=FakeSpawner(ws),
                     drive_folder_id="folder-A",
                     upload=lambda files, folder: uploaded.extend(
                         p.name for p in files) or ["id-0"],
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "approve"
    assert "Ford - Book 2 - outcome.json" in uploaded


def test_a_failed_upload_on_a_stop_never_masks_the_reason(book, tmp_path):
    """Reporting a failure must not raise a second one. The run's reason stays
    the reason it stopped, and the files are on disk for a person."""
    ws = _ws(book, tmp_path)
    said: list[str] = []

    def upload(_files, _folder):
        raise RuntimeError("Drive is down")

    result = _driver(book, tmp_path, spawn=_failing(ws, "ladder"),
                     drive_folder_id="folder-A", upload=upload,
                     handoff_dir=tmp_path / "handoff",
                     log=said.append).run()
    assert result.outcome == "needs_human"
    assert "exited 5" in result.reason              # not the upload's message
    assert any("upload failed" in line for line in said)
    assert (tmp_path / "handoff" / "Ford - Book 2 - outcome.json").is_file()


def test_no_drive_folder_means_the_files_are_just_written(book, tmp_path):
    ws = _ws(book, tmp_path)
    result = _driver(book, tmp_path, spawn=_failing(ws, "ladder"),
                     handoff_dir=tmp_path / "handoff").run()
    assert result.uploaded == []
    assert [p.name for p in result.handoff]
    assert (tmp_path / "handoff" / "Ford - Book 2 - outcome.json").is_file()


# --- build_handoff's two modes ------------------------------------------------

def test_a_partial_handoff_ships_what_exists(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    runs = ws / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "outcome.json").write_text(json.dumps(
        {"outcome": "needs_human", "reason": "the ladder died", "evidence": {},
         "hubspot": {}, "set_by": "galley drive"}), encoding="utf-8")
    (ws / "deliverable").mkdir(parents=True, exist_ok=True)
    (ws / "deliverable" / gd.DECISION_LOG_NAME).write_text("# log\n", "utf-8")

    written = gd.build_handoff(ws, book.name, tmp_path / "out",
                               outcome_sources=[runs / "outcome.json"],
                               partial=True)
    assert sorted(p.name for p in written) == [
        "Ford - Book 2 - decision-log.md",
        "Ford - Book 2 - outcome.json",
    ]


def test_a_partial_handoff_still_needs_a_verdict(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    with pytest.raises(gd.DriverError, match="no outcome.json"):
        gd.build_handoff(ws, book.name, tmp_path / "out", partial=True)


def test_a_finished_handoff_still_demands_all_five(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    (ws / "deliverable" / "letter.md").unlink()
    with pytest.raises(gd.DriverError, match="no editor's letter"):
        gd.build_handoff(ws, book.name, tmp_path / "out",
                         outcome_sources=[ws / "deliverable" / "outcome.json"])
