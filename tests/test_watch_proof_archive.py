"""DocWatch reads the fixed lane's verdict back from its own Drive archive.

The Galley agent hands the author folder the redline alone and files the record
— including "<surname> - Book Two - outcome.json" — under the archive, tagging
each file with the source Book 1's Drive id. The ticker finds the verdict by
that tag, not by folder, and applies it exactly as it would one dropped beside
the book: HubSpot moves, the Book 1 is marked done, the email says so.
"""
from __future__ import annotations

import json

from app.watch import proof as prooflib
from app.watch.archive import SOURCE_PROP
from app.watch.stages import PROOF_AWAITING, PROOF_DONE, PROOF_PROP

from .fakes import fake_drive
from .test_watch_proof import (MANUSCRIPT, author_folder, hs_props, in_sub,
                               ready_to_proof, run, sub_proof_ws)


def _archived_outcome(opener, payload: dict, *, name: str, source_id: str,
                      parent: str = "archive-run",
                      modified: str = "2026-09-16T12:00:00.000Z") -> str:
    file_id = f"arch-{name}"
    opener.files[file_id] = {"id": file_id, "name": name,
                             "mimeType": "application/json",
                             "appProperties": {SOURCE_PROP: source_id,
                                               "galley_destination": "archive"},
                             "parents": [parent], "modifiedTime": modified,
                             "size": "512"}
    opener.content[file_id] = json.dumps(payload).encode()
    return file_id


def _watch(**over):
    return sub_proof_ws(proof_runner="external", archive_enabled=True,
                        archive_folder_id="archive-root", **over)


def test_a_verdict_filed_in_the_archive_moves_the_book_on(tmp_path):
    ws = _watch()
    opener = fake_drive({
        "sf-a": author_folder("John Smith"),
        "m-a": in_sub("Smith - Book 1.docx", sub="sf-a"),
    }, docx=MANUSCRIPT, hubspot={"john": ready_to_proof(last="Smith", first="John")})
    run(tmp_path, ws, opener)
    assert opener.files["m-a"]["appProperties"][PROOF_PROP] == PROOF_AWAITING

    _archived_outcome(opener, {"outcome": "done", "reason": "The second Astra reading found 3 core "
                                                            "mechanical errors (ceiling 25) and no publication "
                                                            "blocker; proofread complete."},
                      name="Smith - Book Two - outcome.json", source_id="m-a")
    report = run(tmp_path, ws, opener)

    assert report.proofed == ["Smith - Book 1.docx"]
    assert hs_props(opener, "john")["docproof"] == "Proofing Complete"
    assert opener.files["m-a"]["appProperties"][PROOF_PROP] == PROOF_DONE


def test_a_needs_human_verdict_in_the_archive_is_applied_too(tmp_path):
    ws = _watch()
    opener = fake_drive({
        "sf-a": author_folder("John Smith"),
        "m-a": in_sub("Smith - Book 1.docx", sub="sf-a"),
    }, docx=MANUSCRIPT, hubspot={"john": ready_to_proof(last="Smith", first="John")})
    run(tmp_path, ws, opener)
    _archived_outcome(opener, {"outcome": "needs_human", "reason": "27 core mechanical errors remained."},
                      name="Smith - Book Two - outcome.json", source_id="m-a")
    report = run(tmp_path, ws, opener)
    assert report.proofed == ["Smith - Book 1.docx"]
    assert hs_props(opener, "john")["docproof"] == "Needs Human PR"


def test_only_this_books_outcome_counts(tmp_path):
    """Two Smiths, two archived verdicts: the tag ties each to its own Book 1,
    where a name match alone could not."""
    ws = _watch()
    opener = fake_drive({
        "sf-a": author_folder("John Smith"),
        "m-a": in_sub("Smith - Book 1.docx", sub="sf-a"),
        "sf-b": author_folder("Jane Smith"),
        "m-b": in_sub("Smith - Book 1.docx", sub="sf-b"),
    }, docx=MANUSCRIPT,
        hubspot={"john": ready_to_proof(last="Smith", first="John"),
                 "jane": ready_to_proof(last="Smith", first="Jane")})
    run(tmp_path, ws, opener)
    _archived_outcome(opener, {"outcome": "done", "reason": "clean"},
                      name="Smith - Book Two - outcome.json", source_id="m-b", parent="run-b")
    report = run(tmp_path, ws, opener)
    assert len(report.proofed) == 1
    assert hs_props(opener, "jane")["docproof"] == "Proofing Complete"
    assert hs_props(opener, "john")["docproof"] == "Ready for Proofing"
    assert opener.files["m-a"]["appProperties"][PROOF_PROP] == PROOF_AWAITING


def test_the_archive_is_not_consulted_when_it_is_off(tmp_path):
    ws = sub_proof_ws(proof_runner="external")
    opener = fake_drive({
        "sf-a": author_folder("John Smith"),
        "m-a": in_sub("Smith - Book 1.docx", sub="sf-a"),
    }, docx=MANUSCRIPT, hubspot={"john": ready_to_proof(last="Smith", first="John")})
    run(tmp_path, ws, opener)
    _archived_outcome(opener, {"outcome": "done", "reason": "clean"},
                      name="Smith - Book Two - outcome.json", source_id="m-a")
    report = run(tmp_path, ws, opener)
    assert report.proofed == []
    assert opener.files["m-a"]["appProperties"][PROOF_PROP] == PROOF_AWAITING


def test_lookup_ignores_files_that_are_not_this_books_outcome():
    """The query is by tag; the name still has to be the house outcome name, so
    the archived report tagged with the same id is never read as a verdict."""
    from app.watch.drive import DriveFile
    from app.watch.state import FileRecord

    rows = [
        {"id": "r", "name": "Smith - Book Two - proofreading report.md", "mimeType": "text/markdown",
         "appProperties": {SOURCE_PROP: "m-a"}, "modifiedTime": "2026-09-16T12:00:00.000Z"},
        {"id": "o", "name": "Smith - Book Two - outcome.json", "mimeType": "application/json",
         "appProperties": {SOURCE_PROP: "m-a"}, "modifiedTime": "2026-09-16T12:00:00.000Z"},
    ]
    opener = fake_drive({row["id"]: row for row in rows})
    ws = _watch()
    book = DriveFile.from_api({"id": "m-a", "name": "Smith - Book 1.docx", "mimeType": "x",
                               "modifiedTime": "2026-09-01T00:00:00.000Z"})
    found = prooflib.outcome_in_archive("token", ws, book, FileRecord(file_id="m-a", name=book.name),
                                        opener=opener)
    assert found is not None and found.id == "o"
