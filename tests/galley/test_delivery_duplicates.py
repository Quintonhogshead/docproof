"""Ambiguous upload acknowledgements need no operator when all bytes agree."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from galley import driver as gd


def _setup(tmp_path, monkeypatch):
    from app.watch import drive

    artifact = tmp_path / "book.docx"
    artifact.write_bytes(b"frozen reviewed document")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    package = {"packet_sha256": "packet", "artifacts": [
        {"path": str(artifact), "name": artifact.name, "sha256": digest}]}
    properties = {"galley_packet": "packet", "galley_sha256": digest,
                  "galley_source": "source"}
    listing = [SimpleNamespace(id=id_, name=artifact.name, app_properties=properties)
               for id_ in ("copy-z", "copy-a")]
    downloads, uploads = [], []
    monkeypatch.setattr(gd, "drive_token", lambda: "fake-token")
    monkeypatch.setattr(drive, "list_folder", lambda token, folder: listing)
    def download(token, file_id, **kwargs):
        downloads.append(file_id)
        return artifact.read_bytes()
    monkeypatch.setattr(drive, "download_bytes", download)
    monkeypatch.setattr(drive, "upload", lambda *args, **kwargs: uploads.append(args) or "unexpected")
    return artifact, package, listing, downloads, uploads


def test_identical_remote_duplicates_are_verified_and_one_is_reused(tmp_path, monkeypatch):
    _, package, _, downloads, uploads = _setup(tmp_path, monkeypatch)
    ledger = tmp_path / "delivery.json"
    ids = gd.publish_verified_handoff(package, "folder", ledger, source_id="source")
    assert ids == ["copy-a"]
    assert downloads == ["copy-a", "copy-z"]
    assert uploads == []
    record = json.loads(ledger.read_text())["artifacts"]["book.docx"]
    assert record["verified"] is True
    assert record["equivalent_file_ids"] == ["copy-a", "copy-z"]


def test_mismatched_duplicate_blocks_before_delivery_acknowledgement(tmp_path, monkeypatch):
    from app.watch import drive

    artifact, package, _, _, uploads = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(drive, "download_bytes", lambda token, file_id, **kw:
                        artifact.read_bytes() if file_id == "copy-a" else b"changed by user")
    ledger = tmp_path / "delivery.json"
    with pytest.raises(gd.DriverError, match="different content"):
        gd.publish_verified_handoff(package, "folder", ledger, source_id="source")
    assert json.loads(ledger.read_text())["status"] == "pending"
    assert uploads == []


def test_missing_previously_acknowledged_copy_is_not_silently_recreated(tmp_path, monkeypatch):
    _, package, _, _, uploads = _setup(tmp_path, monkeypatch)
    ledger = tmp_path / "delivery.json"
    ledger.write_text(json.dumps({"packet_sha256": "packet", "folder_id": "folder",
        "artifacts": {"book.docx": {"file_id": "deleted-copy", "verified": True,
            "sha256": package["artifacts"][0]["sha256"]}}}))
    with pytest.raises(gd.DriverError, match="no longer in the folder"):
        gd.publish_verified_handoff(package, "folder", ledger, source_id="source")
    assert uploads == []
