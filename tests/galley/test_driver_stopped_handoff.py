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


def test_a_finished_handoff_still_demands_all_six(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    (ws / "deliverable" / "letter.md").unlink()
    with pytest.raises(gd.DriverError, match="no editor's letter"):
        gd.build_handoff(ws, book.name, tmp_path / "out",
                         outcome_sources=[ws / "deliverable" / "outcome.json"])


def test_a_finished_handoff_demands_the_verification_report(book, tmp_path):
    """The sixth file (Georgis head-to-head, 2026-09-04): a proof without its
    verification report is not a hand-off."""
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    (ws / "deliverable" / "verification.md").unlink()
    with pytest.raises(gd.DriverError, match="no verification report"):
        gd.build_handoff(ws, book.name, tmp_path / "out",
                         outcome_sources=[ws / "deliverable" / "outcome.json"])


# --- the evidence rides with the verdict ----------------------------------------

def test_a_stopped_run_ships_its_transcripts_beside_the_verdict(book, tmp_path):
    import zipfile

    ws = _ws(book, tmp_path)
    uploaded: list[str] = []

    def upload(files, folder_id):
        uploaded.extend(p.name for p in files)
        return [f"id-{i}" for i, _ in enumerate(files)]

    events: list[dict] = []
    result = _driver(book, tmp_path, spawn=_failing(ws, "ladder"),
                     drive_folder_id="folder-A", upload=upload,
                     handoff_dir=tmp_path / "handoff",
                     progress=events.append).run()

    assert result.outcome == "needs_human"
    assert "Ford - Book 2 - diagnostics.zip" in uploaded
    bundle = tmp_path / "handoff" / "Ford - Book 2 - diagnostics.zip"
    names = set(zipfile.ZipFile(bundle).namelist())
    # Every phase the driver ran left a transcript, and all of them are in.
    assert {"runs/driver/profile.log", "runs/driver/sweeps.log",
            "runs/driver/ladder.log", "runs/outcome.json",
            "PLAN.md"} <= names
    assert "runs/driver/driver.json" in names
    # Raw stream files are the transcript's source, not evidence twice over.
    assert not any(n.endswith(".stream.jsonl") for n in names)

    # …and the run narrated itself: phase boundaries, the gate, the stop.
    kinds = [(e["event"], e.get("phase")) for e in events]
    assert kinds[0] == ("phase_start", "profile")
    assert ("gate", None) in kinds
    assert ("phase_start", "ladder") in kinds
    assert kinds[-1] == ("stopped", "ladder")
    ended = [e for e in events if e["event"] == "phase_end"]
    assert ended[-1]["phase"] == "ladder" and ended[-1]["ok"] is False
    assert all(e["slug"] == "ford-book-1" and e["at"] for e in events)


def test_a_reporter_that_raises_never_sinks_the_run(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)

    def bad(_event):
        raise RuntimeError("the drawer is down")

    result = _driver(book, tmp_path, spawn=FakeSpawner(ws),
                     progress=bad).run()
    assert result.outcome == "done"


def test_a_finished_run_reports_finished(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    events: list[dict] = []
    _driver(book, tmp_path, spawn=FakeSpawner(ws),
            progress=events.append).run()
    assert events[-1]["event"] == "finished"
    assert events[-1]["outcome"] == "done"


def test_the_bundle_skips_giants_and_survives_an_empty_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "runs" / "driver").mkdir(parents=True)
    big = ws / "runs" / "driver" / "verify.log"
    big.write_bytes(b"x" * (gd.DIAGNOSTICS_MAX_FILE_BYTES + 1))
    (ws / "runs" / "driver" / "profile.log").write_text("ok\n")
    out = gd.build_diagnostics(ws, "Ford - Book 1.docx", tmp_path / "h")
    import zipfile
    names = zipfile.ZipFile(out).namelist()
    assert names == ["runs/driver/profile.log"]
    assert gd.build_diagnostics(tmp_path / "empty", "X - Book 1.docx",
                                tmp_path / "h2") is None
