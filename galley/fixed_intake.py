"""Preserve the incoming manuscript and freeze a working baseline.

Three transformations produce the baseline, in this order:

1. Incoming Word revisions are accepted (the same accepted-view policy as
   Galley's established prep intake). Only revision-bearing parts change.
2. Page-runover paragraphs — one paragraph a typeset export split across a page
   boundary — are rejoined (`docproof.runover`). Only word/document.xml changes.
3. Silent normalization (`docproof.normalize`): straight quotation marks are
   curled, runs of spaces collapsed, and every ellipsis set to the house form
   (… with a non-breaking space before it). These are house conventions, not
   corrections; they change text elements in place with no revision markup, in
   any part that holds paragraphs, so the tracked copy and the report carry
   only editorial corrections.

Comments and every other package member survive. The original, baseline and
receipt are published together before any model call; a manuscript needing
neither transformation keeps its own identity and no baseline is created.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

from docproof.ingest import accept_all_revisions, find_revisions, preflight
from docproof.normalize import (house_ellipsis_edits, house_ellipsis_package, normalize_package,
                                normalize_text)
from docproof.runover import POLICY as RUNOVER_POLICY, find_runover_joins, join_runover_paragraphs
from docproof.variants import detect_variant, load_variant
from docproof.utils.xml_helpers import paragraph_text, walk_package
from galley.fixed_calls import _atomic, _hash, _load, _locked
from galley.manifest import sha256_file

VERSION = "fixed-intake-v3"
NORMALIZATION_POLICY = "silent-quotes-spaces-house-ellipsis-v1"


class FixedIntakeError(ValueError):
    pass


def _texts(pkg):
    return {p.para_id: paragraph_text(p.element) for p in walk_package(pkg)}


def _configuration(cfg):
    if cfg is None:
        from galley.fixed_policy import configuration
        cfg = configuration()
    return cfg


def _dictionary_name(cfg):
    return cfg.spellcheck.dictionary or "en_US"


def _variant(cfg, texts):
    """The same rule prepare applies: an explicit variant, else the spelling
    evidence, else US. Only the primary quotation mark matters here."""
    key = cfg.variant if cfg.variant != "auto" else (detect_variant(list(texts.values())) or "us")
    return key, load_variant(key)


def _normalization_pending(texts, cfg, variant):
    """Paragraph ids whose text silent normalization would still change."""
    return [pid for pid, text in texts.items() if text and (
        normalize_text(text, quotes=True, spaces=True, variant=variant) != text
        or house_ellipsis_edits(text, style=cfg.style.ellipsis, variant=variant))]


def validate_intake(directory, evidence, source=None, cfg=None):
    """Validate the frozen intake without changing a file or starting a model."""
    cfg = _configuration(cfg)
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
    texts = _texts(pkg)
    if _hash(texts) != receipt.get("accepted_text_sha256"):
        raise FixedIntakeError("The accepted baseline text changed")
    if (receipt.get("runover_policy") != RUNOVER_POLICY
            or not isinstance(receipt.get("runover_joins"), list)
            or receipt.get("paragraphs") != len(texts)):
        raise FixedIntakeError("The intake receipt lacks its page-runover accounting")
    if find_runover_joins(pkg, cfg, dictionary=_dictionary_name(cfg))[0]:
        raise FixedIntakeError("The accepted baseline still contains unjoined page runovers")
    normalization = receipt.get("normalization")
    if (not isinstance(normalization, dict) or normalization.get("policy") != NORMALIZATION_POLICY
            or normalization.get("ellipsis_style") != cfg.style.ellipsis):
        raise FixedIntakeError("The intake receipt lacks its silent-normalization accounting")
    if _normalization_pending(texts, cfg, load_variant(normalization.get("variant", "us"))):
        raise FixedIntakeError("The accepted baseline still contains unnormalized quotes, spaces or ellipses")
    return baseline


def prepare_source(source, directory, cfg=None):
    """Return a revision-free, runover-joined source and optional intake evidence.

    Clean manuscripts retain their existing workflow identity. Interrupted
    baseline creation can retry; a published baseline is verified, never rebuilt.
    """
    cfg = _configuration(cfg)
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if source.suffix.lower() != ".docx":
        raise FixedIntakeError("The fixed workflow currently requires a Word manuscript")
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(directory / ".intake.lock"):
        destination = directory / "intake"
        if destination.exists():
            receipt = _load(destination / "receipt.json")
            if receipt.get("version") != VERSION:
                raise FixedIntakeError("The intake baseline predates fixed-intake-v3 (silent normalization); use a fresh workspace")
            evidence = {"version": VERSION,
                        "receipt_sha256": sha256_file(destination / "receipt.json"),
                        "original_sha256": receipt["original_sha256"],
                        "baseline_sha256": receipt["baseline_sha256"]}
            if source.name != receipt["name"] or sha256_file(source) != evidence["original_sha256"]:
                raise FixedIntakeError("The incoming manuscript changed; use a fresh workspace")
            return validate_intake(directory, evidence, cfg=cfg), evidence
        pkg = preflight(source, "ignore")
        variant_key, variant = _variant(cfg, _texts(pkg))
        if (not find_revisions(pkg) and not find_runover_joins(pkg, cfg, dictionary=_dictionary_name(cfg))[0]
                and not _normalization_pending(_texts(pkg), cfg, variant)):
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
            joins, runover = join_runover_paragraphs(pkg, cfg, dictionary=_dictionary_name(cfg))
            # Quotes and spaces first, then the ellipsis lead: the two passes
            # both touch the spaces around an ellipsis and must not overlap.
            curled = normalize_package(pkg, quotes=True, spaces=True, variant=variant)
            ellipses = house_ellipsis_package(pkg, style=cfg.style.ellipsis, variant=variant)
            texts = _texts(pkg)
            if _normalization_pending(texts, cfg, variant):
                raise FixedIntakeError("Silent normalization did not converge on the accepted baseline")
            pkg.save(baseline)
            if _texts(preflight(baseline, "abort")) != texts:
                raise FixedIntakeError("Saving the accepted baseline changed paragraph text or identities")
            with ZipFile(original) as before, ZipFile(baseline) as after:
                if before.namelist() != after.namelist():
                    raise FixedIntakeError("Revision intake changed the Word package inventory")
                changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
                allowed = (set(resolved) | ({"word/document.xml"} if joins else set())
                           | set(curled.parts) | set(ellipses.parts))
                if not set(changed) <= allowed:
                    raise FixedIntakeError("Intake changed a protected Word package member")
            if sha256_file(source) != original_sha:
                raise FixedIntakeError("The incoming manuscript changed during intake")
            receipt = {"version": VERSION, "policy": "accept_all_first", "name": source.name,
                       "original_sha256": original_sha, "baseline_sha256": sha256_file(baseline),
                       "accepted_text_sha256": _hash(texts), "paragraphs": len(texts),
                       "resolved_revision_elements": resolved, "changed_parts": changed,
                       "runover_policy": RUNOVER_POLICY, "runover_joins": joins,
                       "runover_refusals": runover["refusals"], "indent_convention": runover["convention"],
                       "paragraph_id_space": "accepted-before-join",
                       "normalization": {"policy": NORMALIZATION_POLICY, "variant": variant_key,
                                         "ellipsis_style": cfg.style.ellipsis,
                                         "quotes": curled.quotes, "spaces": curled.spaces,
                                         "ambiguous_quotes": curled.ambiguous, "ellipses": ellipses.ellipses,
                                         "paragraphs": curled.paragraphs + ellipses.paragraphs,
                                         "parts": sorted(set(curled.parts) | set(ellipses.parts))}}
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
        return validate_intake(directory, evidence, cfg=cfg), evidence
