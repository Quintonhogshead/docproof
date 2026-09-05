"""Run app proofreading or read external outcomes, then deliver artifacts under
shared house names.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docproof.formats.base import DocumentFormat

from app.jobs import Job, JobRunner, JobStore
from galley.tiers import GALLEY_DEFAULT_BUDGET

from . import drive, naming
from .drive import DOCX_MIME, DriveFile
from .prep import fetch  # generic download + convert; proofing reuses it verbatim
from .settings import WatchSettings
from .stages import (AT_PROP, JOB_PROP, OUTPUT_PROP, PROOF_PROP, REASON_PROP,
                     SOURCE_PROP)
from .state import FileRecord, WatchState

log = logging.getLogger("docproof.app.watch.proof")

MARKDOWN_MIME = "text/markdown"
JSON_MIME = "application/json"

REASON_LIMIT = 90

# Compatibility name retained for watcher callers.
DEFAULT_BUDGET = GALLEY_DEFAULT_BUDGET

__all__ = ["Verdict", "artifacts", "assess", "budget_for", "fetch",
           "hand_off_names", "make_job", "mark_source", "outcome_in_folder",
           "read_outcome", "run_job", "upload_outputs"]


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    mime: str


@dataclass(frozen=True)
class Verdict:
    """Terminal outcome, reason, and optional local artifact path."""

    outcome: str                 # "done" | "needs_human"
    reason: str = ""
    path: Path | None = None     # the outcome.json this came from, if local

    @property
    def done(self) -> bool:
        return self.outcome == "done"



def budget_for(ws: WatchSettings) -> float:
    """What one book may cost: the configured budget, or its tier's default."""
    if ws.proof_budget_usd and ws.proof_budget_usd > 0:
        return float(ws.proof_budget_usd)
    return DEFAULT_BUDGET.get(ws.proof_tier, DEFAULT_BUDGET["T2"])


def make_job(local: Path, ws: WatchSettings) -> Job:
    """Create a synchronous Galley job; adapters choose models and the tier
    supplies default limits.
    """
    from docproof.batch import new_job_id

    return Job(id=new_job_id(local.name), filename=local.name,
               source_path=str(local), model="", mode="now", kind="galley",
               tier=ws.proof_tier or "T2", budget_usd=budget_for(ws),
               source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job) -> Job:
    """Run a job synchronously and return its stored state. Record unexpected
    exceptions as failures.
    """
    store.save(job)
    try:
        runner.run_one(job.id)
    except Exception as e:                # noqa: BLE001 - mirrors _work
        log.exception("Proofreading %s failed", job.filename)
        store.update(job.id, state="failed", error=str(e))
    return store.get(job.id) or job


def assess(job: Job, *, done_value: str,
           needs_human_value: str = "") -> Verdict:
    """Assess a finished Galley job and save its verdict beside the artifacts."""
    from galley.outcome import (Outcome, assess as galley_assess,
                                hubspot_fields)

    out = Path(job.results_dir or "")
    if not out.is_dir():
        return Verdict("needs_human",
                       "The proofread produced no results folder, so there is "
                       "nothing to judge it by.")
    # A failed certificate remains needs_human when delivery resumes.
    if job.state == "needs_human":
        existing = Outcome.load(out)
        certificate_reasons = [
            warning for warning in (job.warnings or [])
            if "Not certified" in warning or "certificate" in warning.lower()
        ]
        reason = "; ".join(certificate_reasons)
        if not reason and existing is not None and existing.outcome == "needs_human":
            reason = existing.reason
        reason = reason or (
            "The proofread needs human review because its delivery certificate "
            "did not pass.")
        outcome = Outcome(
            outcome="needs_human", reason=reason,
            evidence=(existing.evidence if existing is not None else {}),
            hubspot=hubspot_fields("needs_human", done_value=done_value,
                                   needs_human_value=needs_human_value),
            set_by="watch")
        path = outcome.save(out)
        return Verdict("needs_human", outcome.reason, path)
    try:
        outcome = galley_assess(out, done_value=done_value,
                                needs_human_value=needs_human_value)
    except Exception as e:                # noqa: BLE001 - a verdict, not a crash
        log.exception("Could not assess the proofread of %s", job.filename)
        outcome = Outcome(
            outcome="needs_human",
            reason=f"The proofread finished but its numbers could not be read "
                   f"({e}), so DocProof will not call it done.",
            set_by="watch")
    path = outcome.save(out)
    return Verdict(outcome.outcome, outcome.reason, path)



def hand_off_names(source_name: str) -> dict[str, str]:
    """Return artifact names by role, preserving Book 1/Book One spelling in
    Book 2/Book Two output.
    """
    base = naming.proof_base(Path(source_name).stem or "manuscript")
    return {
        "manuscript": f"{base}.docx",
        "letter": f"{base}{naming.LETTER_SUFFIX}.md",
        "style_sheet": f"{base}{naming.STYLE_SHEET_SUFFIX}.md",
        "decision_log": f"{base}{naming.DECISION_LOG_SUFFIX}.md",
        "verification": f"{base}{naming.VERIFICATION_SUFFIX}.md",
        "outcome": f"{base}{naming.OUTCOME_SUFFIX}.json",
    }


def artifacts(job: Job, source_name: str) -> list[Artifact]:
    """List available manuscript, letter, style sheet, decision log,
    verification report, and outcome files under house names. Exclude internal run artifacts.
    """
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    names = hand_off_names(source_name)
    found: list[Artifact] = []

    reviewed = _reviewed_docx(out)
    if reviewed is not None:
        found.append(Artifact(reviewed, names["manuscript"], DOCX_MIME))
    for role, filename in (("letter", "letter.md"),
                           ("style_sheet", "style-sheet.md"),
                           ("decision_log", "DECISION_LOG.md"),
                           ("verification", "verification.md")):
        path = out / filename
        if path.is_file():
            found.append(Artifact(path, names[role], MARKDOWN_MIME))
    outcome = out / "outcome.json"
    if outcome.is_file():
        found.append(Artifact(outcome, names["outcome"], JSON_MIME))
    return found


def _reviewed_docx(out: Path) -> Path | None:
    """Find the reviewed manuscript by output suffix, excluding change logs and
    supporting converted sources.
    """
    changelog = DocumentFormat.CHANGE_LOG_SUFFIX.lower()
    for path in sorted(out.glob(f"*{DocumentFormat.REVIEWED_SUFFIX}*.docx")):
        if path.stem.lower().endswith(changelog):
            continue
        return path
    return None



def outcome_in_folder(listing: list[DriveFile], source_name: str
                      ) -> DriveFile | None:
    """Find an external outcome by house filename, allowing dash and case
    variants without requiring app markers.
    """
    stem = Path(source_name).stem or "manuscript"
    for candidate in listing:
        if candidate.is_folder:
            continue
        if naming.is_proof_outcome_name(candidate.name, stem):
            return candidate
    return None


def read_outcome(token: str, file: DriveFile, *, opener=drive._open_url
                 ) -> Verdict | None:
    """Read a recognized outcome and reason from Drive; return None for invalid
    data. CRM fields come from watcher settings.
    """
    from galley.outcome import OUTCOMES

    try:
        raw = drive.download_bytes(token, file.id, opener=opener,
                                   what="read the proofread outcome")
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:                # noqa: BLE001 - a wait, not a failure
        log.warning("Could not read %s (%s); waiting for a readable one.",
                    file.name, e)
        return None
    if not isinstance(payload, dict):
        log.warning("%s is not an outcome record; waiting.", file.name)
        return None
    outcome = str(payload.get("outcome", "")).strip()
    if outcome not in OUTCOMES:
        log.warning("%s says outcome %r, which DocProof does not recognise; "
                    "waiting.", file.name, outcome)
        return None
    return Verdict(outcome, str(payload.get("reason", "")).strip())



def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str | None = None,
                   opener=drive._open_url) -> list[str]:
    """Upload unrecorded artifacts and save each resulting id. Adopt matching
    files from interrupted uploads by source id and name.
    """
    from .prep import _already_there

    dest = dest_folder_id or ws.folder_id
    placed = []
    for artifact in artifacts(job, file.name):
        if artifact.name in rec.proof_uploaded:
            continue
        orphan = _already_there(listing, file.id, artifact.name)
        if orphan is not None:
            log.info("%s is already in the folder from an earlier run",
                     artifact.name)
            rec.proof_uploaded[artifact.name] = orphan.id
            state.record(rec)
            continue
        new_id = drive.upload(token, dest, artifact.path,
                              name=artifact.name, mime_type=artifact.mime,
                              app_properties={OUTPUT_PROP: "1",
                                              SOURCE_PROP: file.id,
                                              JOB_PROP: job.id},
                              opener=opener)
        rec.proof_uploaded[artifact.name] = new_id
        state.record(rec)
        placed.append(artifact.name)
    return placed


def mark_source(token: str, file: DriveFile, rec: FileRecord,
                state: WatchState, *, status: str, reason: str | None = None,
                opener=drive._open_url) -> None:
    """Write the proofing marker after delivery and CRM updates. The awaiting
    marker is nonterminal and is written before external work starts.
    """
    props = {PROOF_PROP: status,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason:
        props[REASON_PROP] = reason[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.proof_marked = status
    state.record(rec)
