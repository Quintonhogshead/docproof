"""Preserve incoming Word revisions and freeze an accepted working baseline.

This is the same accepted-view policy as Galley's established prep intake.
Only revision-bearing parts change; comments and other package members survive.
The original, baseline and receipt are published together before any model call.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

from docproof.ingest import accept_all_revisions, find_revisions, preflight
from docproof.utils.xml_helpers import paragraph_text, walk_package
from galley.fixed_calls import _atomic, _hash, _load, _locked
from galley.manifest import sha256_file

VERSION = "fixed-intake-v1"


class FixedIntakeError(ValueError):
    pass


def _texts(pkg):
    return {p.para_id: paragraph_text(p.element) for p in walk_package(pkg)}


def validate_intake(directory, evidence, source=None):
    """Validate the frozen intake without changing a file or starting a model."""
    directory = Path(directory).resolve() / "intake"
    path = directory / "receipt.json"
    if sha256_file(path) != evidence.get("receipt_sha256"):
        raise FixedIntakeError("The incoming-revision receipt changed")
    receipt = _load(path)
    name = receipt.get("name", "")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise FixedIntakeError("The intake manuscript name is invalid")
    original, baseline = directory / "original" / name, directory / "accepted" / name
    expected = {"version": VERSION, "receipt_sha256": sha256_file(path),
                "original_sha256": receipt.get("original_sha256"),
                "baseline_sha256": receipt.get("baseline_sha256")}
    if (evidence != expected or receipt.get("version") != VERSION or
            receipt.get("policy") != "accept_all_first" or
            sha256_file(original) != evidence["original_sha256"] or
            sha256_file(baseline) != evidence["baseline_sha256"]):
        raise FixedIntakeError("The preserved original or accepted baseline changed")
    if source is not None and Path(source).resolve() != baseline.resolve():
        raise FixedIntakeError("The proofread does not use its accepted baseline")
    pkg = preflight(baseline, "abort")
    if _hash(_texts(pkg)) != receipt.get("accepted_text_sha256"):
        raise FixedIntakeError("The accepted baseline text changed")
    return baseline


def prepare_source(source, directory):
    """Return a revision-free source and optional source-bound intake evidence.

    Clean manuscripts retain their existing workflow identity. Interrupted
    baseline creation can retry; a published baseline is verified, never rebuilt.
    """
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if source.suffix.lower() != ".docx":
        raise FixedIntakeError("The fixed workflow currently requires a Word manuscript")
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(directory / ".intake.lock"):
        destination = directory / "intake"
        if destination.exists():
            receipt = _load(destination / "receipt.json")
            evidence = {"version": VERSION,
                        "receipt_sha256": sha256_file(destination / "receipt.json"),
                        "original_sha256": receipt["original_sha256"],
                        "baseline_sha256": receipt["baseline_sha256"]}
            if source.name != receipt["name"] or sha256_file(source) != evidence["original_sha256"]:
                raise FixedIntakeError("The incoming manuscript changed; use a fresh workspace")
            return validate_intake(directory, evidence), evidence
        pkg = preflight(source, "ignore")
        if not find_revisions(pkg):
            return source, None
        if (directory / "workflow.json").exists():
            raise FixedIntakeError("Existing review evidence predates revision intake; use a fresh workspace")
        # Read/resolve the preserved copy, not a potentially changing download.
        with tempfile.TemporaryDirectory(prefix=".intake-", dir=directory) as staging:
            staging = Path(staging)
            original, baseline = staging / "original" / source.name, staging / "accepted" / source.name
            original.parent.mkdir()
            baseline.parent.mkdir()
            shutil.copyfile(source, original)
            original_sha = sha256_file(original)
            pkg = preflight(original, "ignore")
            resolved = accept_all_revisions(pkg)
            texts = _texts(pkg)
            pkg.save(baseline)
            if _texts(preflight(baseline, "abort")) != texts:
                raise FixedIntakeError("Saving the accepted baseline changed paragraph text or identities")
            with ZipFile(original) as before, ZipFile(baseline) as after:
                if before.namelist() != after.namelist():
                    raise FixedIntakeError("Revision intake changed the Word package inventory")
                changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
                if not set(changed) <= set(resolved):
                    raise FixedIntakeError("Revision intake changed a protected Word package member")
            if sha256_file(source) != original_sha:
                raise FixedIntakeError("The incoming manuscript changed during intake")
            receipt = {"version": VERSION, "policy": "accept_all_first", "name": source.name,
                       "original_sha256": original_sha, "baseline_sha256": sha256_file(baseline),
                       "accepted_text_sha256": _hash(texts), "paragraphs": len(texts),
                       "resolved_revision_elements": resolved, "changed_parts": changed}
            _atomic(staging / "receipt.json", receipt)
            for path in (original, baseline):
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            from docproof import platform_io
            for parent in (original.parent, baseline.parent, staging):
                platform_io.sync_directory(parent)
            os.replace(staging, destination)
            platform_io.sync_directory(directory)
        evidence = {"version": VERSION, "receipt_sha256": sha256_file(destination / "receipt.json"),
                    "original_sha256": original_sha, "baseline_sha256": receipt["baseline_sha256"]}
        return validate_intake(directory, evidence), evidence
