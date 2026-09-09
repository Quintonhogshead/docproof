"""Format Book 1 before Galley records its proofreading source.

Keep the download and a verified, immutable formatting result per input hash.
The result retains the input's filename so the usual Book 2 handoff names work.
Completed intakes are reused byte for byte; proofreading never resumes against
a freshly rebuilt document with different paragraph locations or file hashes.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

from app.settings import resource_root
from docproof import prep
from docproof.checkpoint import Checkpoint
from docproof.config import Config
from docproof.providers import build_provider
from docproof.providers.subagent import DEFAULT_MODEL
from docproof.utils.files import write_atomic
from galley.manifest import sha256_file
from galley.state_machine import RunStateMachine

INTAKE_DIR = "intake"
RECEIPT_NAME = "formatting.json"
VERSION = 1


class IntakeError(RuntimeError):
    """Formatting is incomplete or its saved source cannot be trusted."""


def configuration() -> Config:
    """The agent's plain Word manuscript, using its subscription for tags."""
    cfg = Config()
    cfg.api.model = DEFAULT_MODEL
    cfg.api.provider = "anthropic"
    cfg.api.claude_lane = "subagent"
    cfg.prep.book_design = "prep/book_manuscript.yaml"
    cfg.prep.outputs = ["book"]
    cfg.prep.strip_direct_formatting = True
    cfg.prep.verify = True
    return cfg


def _require_hash(path: Path, expected: str) -> None:
    if not path.is_file() or sha256_file(path) != expected:
        raise IntakeError(
            f"The saved formatting intake changed or is missing: {path}. "
            "Restore the saved file before resuming this proofread.")


def format_for_proof(source: Path, workspace: Path, *, progress=None) -> Path:
    """Return the verified source to seed, before any proofreading phase.

    Unfinished windows use Prep's checkpoint. A completed receipt freezes the
    original and formatted bytes, even across a formatter/config upgrade.
    Existing proofreads from before this feature retain their recorded source.
    """
    source, workspace = Path(source), Path(workspace)
    source_hash = sha256_file(source)
    intake = workspace / INTAKE_DIR / source_hash
    original = intake / "original" / source.name
    formatted = intake / "formatted" / source.name
    receipt_path = intake / RECEIPT_NAME

    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text("utf-8"))
            if (receipt["schema_version"] != VERSION
                    or receipt["original_sha256"] != source_hash
                    or receipt["source_name"] != source.name
                    or receipt["verified"] is not True
                    or not receipt["formatted_sha256"]):
                raise ValueError("inconsistent formatting receipt")
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise IntakeError(f"Cannot read the saved formatting receipt: {receipt_path}") from e
        _require_hash(original, source_hash)
        _require_hash(formatted, receipt["formatted_sha256"])
        return formatted

    original.parent.mkdir(parents=True, exist_ok=True)
    if not original.exists():
        shutil.copy2(source, original)
    _require_hash(original, source_hash)

    state_path = workspace / "state.json"
    if state_path.exists():
        machine = RunStateMachine.load(state_path)
        if machine.current and machine.source_sha256 == source_hash:
            # Upgrading a running agent must not replace an existing baseline.
            recorded = workspace / "source" / machine.source_name
            _require_hash(recorded, source_hash)
            return recorded

    cfg = configuration()
    prepared = prep.prepare(cfg, original, config_dir=resource_root() / "config")
    checkpoint = Checkpoint(intake / "checkpoint.json", fingerprint={
        "kind": "galley_formatting", "intake_version": VERSION,
        "source_sha256": source_hash, "config": cfg.model_dump(mode="json"),
        "prompt": prepared.prompt.render(prepared.sheet),
    })
    checkpoint.load()
    tags, usage = prep.run(cfg, prepared, build_provider(cfg),
                           checkpoint=checkpoint, progress=progress)
    unanswered = [tag for tag in tags if tag.source == "unanswered"]
    if unanswered:
        # Prep's interactive fallback needs a person. An unattended intake
        # must retry those classifications before starting a proofread.
        checkpoint.delete()
        raise IntakeError(
            f"Formatting left {len(unanswered)} paragraph(s) unclassified; "
            "proofreading has not started. Retry formatting.")
    try:
        outputs = prep.finish(prepared, tags, usage, cfg,
                              out_dir=intake / "results", source_path=original,
                              outputs=["book"])
    except prep.VerificationFailed:
        checkpoint.delete()
        raise
    if not outputs.verifications or not all(c.ok for c in outputs.verifications):
        raise IntakeError("Formatting did not pass verification; proofreading has not started.")

    formatted.parent.mkdir(parents=True, exist_ok=True)
    # The receipt is committed last. A crash before it is written can safely
    # replay the checkpoint; the driver has not seen this source yet.
    shutil.copy2(outputs.documents["book"], formatted)
    _require_hash(original, source_hash)
    write_atomic(receipt_path, json.dumps({
        "schema_version": VERSION,
        "source_name": source.name,
        "original_sha256": source_hash,
        "formatted_sha256": sha256_file(formatted),
        "verified": True,
        "design": cfg.prep.book_design,
        "model": cfg.api.model,
        "lane": cfg.api.claude_lane,
        "verifications": [dataclasses.asdict(c) for c in outputs.verifications],
        "flags": outputs.flags,
        "notes": str(outputs.notes_md.relative_to(intake)),
        "usage": dataclasses.asdict(usage),
    }, indent=2) + "\n")
    return formatted
