"""One manuscript, proofread, and the proofread back to the folder.

The Galley analog of `prep.py`, and the same idempotent shape: fetch the book,
read it, put the hand-off files beside it, mark it done — with the marker that
hides the file from the next tick written last, so it means "everything before
me finished".

Three things differ from prep, and they are the whole reason this module exists.

**The book it reads is the dev-edited one.** The house stage series runs
`Book Original` (what the author sends) -> `book 0` (formatting) -> `Book 1`
(the developmental edit, done by people) -> `Book 2` (this). So proofing's input
is `<surname> - Book 1.docx` and its output is `<surname> - Book 2.*`, exactly
the way formatting takes a `Book Original` and puts a `book 0` back in the same
folder. Either spelling of the number is read — `Book One` is the same file as
`Book 1` — and what goes back mirrors what came in. `app/watch/naming.py` owns
both tokens and both spellings.

**There are two runners.** In `app` mode DocWatch runs the read itself, through
the app's galley job (`app/jobs.py::_run_galley`): the practitioner wave loop,
adjudication, a $0 tracked-changes deliverable, the editorial letter and the
style sheet. In `external` mode DocWatch runs nothing — the Mac-side
practitioner loop does (`galley/driver.py`), on a Claude Max subscription that
cannot live on Fly — and this module only reads the verdict it left in the
folder. Both end at the same names, which is the contract:

    <surname> - Book 2.docx               the tracked-changes proofread
    <surname> - Book 2 - letter.md        the editorial letter       (optional)
    <surname> - Book 2 - style-sheet.md   the style sheet            (optional)
    <surname> - Book 2 - decision-log.md  every action, and why      (optional)
    <surname> - Book 2 - outcome.json     the verdict                (required)

**The verdict decides the CRM write, and both verdicts write.** `done` moves the
status dropdown to its proofing-done value ("Proofing Complete"); `needs_human`
moves it to "Needs Human PR", the option that puts the book in front of a human
proofreader, and *also* sends the reason to the watcher's needs-a-person email —
the CRM value says what, the email says why. Either way the book leaves "Ready
for Proofing", because a book left sitting at ready is one nobody would notice.

What is read out of outcome.json is the verdict and its reason, never the
property or the value to write. The file may have been placed in Drive by
something outside DocProof, and a file in a folder does not get to name a CRM
field: both come from the watcher's own settings. See `tick`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docproof.formats.base import DocumentFormat

from app.jobs import Job, JobRunner, JobStore

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

# What the tiers cost when the config leaves `proof_budget_usd` at zero. The
# same table the panel uses when a Galley request arrives without a budget,
# copied here rather than imported so a launchd process never drags FastAPI in.
# Kept in step with app/routes/jobs.py::GALLEY_DEFAULT_BUDGET; its keys are the
# tiers `proof_tier` may name.
DEFAULT_BUDGET = {"T0": 15.0, "T1": 30.0, "T2": 60.0, "T3": 150.0, "T4": 300.0}

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
    """A run's terminal answer, as the watcher acts on it.

    Deliberately smaller than `galley.outcome.Outcome`: the watcher needs the
    word and the sentence behind it, and nothing else. The evidence stays in
    outcome.json, which goes to the folder whole."""

    outcome: str                 # "done" | "needs_human"
    reason: str = ""
    path: Path | None = None     # the outcome.json this came from, if local

    @property
    def done(self) -> bool:
        return self.outcome == "done"


# --- reading it ---------------------------------------------------------------

def budget_for(ws: WatchSettings) -> float:
    """What one book may cost: the configured budget, or its tier's default."""
    if ws.proof_budget_usd and ws.proof_budget_usd > 0:
        return float(ws.proof_budget_usd)
    return DEFAULT_BUDGET.get(ws.proof_tier, DEFAULT_BUDGET["T2"])


def make_job(local: Path, ws: WatchSettings) -> Job:
    """A galley job for this manuscript.

    `model` is left blank on purpose — the tier and its adapters pick the
    models, exactly as the panel's own Galley job does — and `mode` is "now"
    because the orchestrator owns its own wave batching."""
    from docproof.batch import new_job_id

    return Job(id=new_job_id(local.name), filename=local.name,
               source_path=str(local), model="", mode="now", kind="galley",
               tier=ws.proof_tier or "T2", budget_usd=budget_for(ws),
               source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job) -> Job:
    """Run it here and now, and hand back what the record says afterwards.

    Straight into `run_one` rather than the worker thread, for prep's reason: a
    tick with nothing else to do gains nothing from being asynchronous. The
    blanket catch mirrors `JobRunner._work` — an unpredicted exception must
    leave a failed job, not one stuck at `running`.

    There is no mock twin here. A rehearsal (`--mock-tags`) stands proofing
    aside entirely in `tick.run_proof`: a galley run is a wave loop over a whole
    novel, and there is no version of it that both exercises the round trip and
    costs nothing."""
    store.save(job)
    try:
        runner.run_one(job.id)
    except Exception as e:                # noqa: BLE001 - mirrors _work
        log.exception("Proofreading %s failed", job.filename)
        store.update(job.id, state="failed", error=str(e))
    return store.get(job.id) or job


def assess(job: Job, *, done_value: str,
           needs_human_value: str = "") -> Verdict:
    """The verdict for a finished galley job, written to outcome.json in the
    job's own results folder so the file that goes to Drive is the file the
    numbers were read from.

    `galley.outcome.assess` reads the run dir — findings.json, the deliverable,
    and (when the run was settled) settlement.json and the verify reports — and
    applies its thresholds. The app's galley job does not settle today, so the
    reason it writes says so; that is honest rather than certified, and it is
    what the reason field is for.

    A run whose numbers cannot be read at all is `needs_human`: an unreadable
    run is not a finished one, and the one thing that must never happen is a
    book moved on in the CRM because a file could not be parsed."""
    from galley.outcome import Outcome, assess as galley_assess

    out = Path(job.results_dir or "")
    if not out.is_dir():
        return Verdict("needs_human",
                       "The proofread produced no results folder, so there is "
                       "nothing to judge it by.")
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


# --- the hand-off names -------------------------------------------------------

def hand_off_names(source_name: str) -> dict[str, str]:
    """The names this book's proofread is delivered under, keyed by role.

    `"Johnson - Book 1.docx"` -> `"Johnson - Book 2.docx"` and its companions;
    a `"Johnson - Book One.docx"` hands back `"Johnson - Book Two.*"`, because
    the number's spelling is mirrored rather than normalised.
    One place, because two sides have to agree: DocWatch writes them here in
    `app` mode and looks for them here in `external` mode, and
    `galley/driver.py` builds the practitioner's hand-off from the same
    `naming.proof_base`."""
    base = naming.proof_base(Path(source_name).stem or "manuscript")
    return {
        "manuscript": f"{base}.docx",
        "letter": f"{base}{naming.LETTER_SUFFIX}.md",
        "style_sheet": f"{base}{naming.STYLE_SHEET_SUFFIX}.md",
        "decision_log": f"{base}{naming.DECISION_LOG_SUFFIX}.md",
        "outcome": f"{base}{naming.OUTCOME_SUFFIX}.json",
    }


def artifacts(job: Job, source_name: str) -> list[Artifact]:
    """What of this run belongs in the author's folder, and what to call it
    there.

    Galley writes internal names — the tracked-changes manuscript under
    DocProof's own "- Atmosphere Press Proofreader" suffix, `letter.md`,
    `style-sheet.md`, `DECISION_LOG.md`, `outcome.json`. The folder belongs to
    people, so each is renamed onto the "<surname> - Book 2" base on the way out.

    The change log, the case file, findings.json and the rest of the run stay
    local: they are DocProof's record, and the archive is where that belongs.
    Everything but the manuscript and the verdict is optional — the galley
    runner renders the letter and the style sheet best-effort, and the decision
    log is the external driver's (`galley/journal.py`) rather than the app
    runner's — so a run that produced none of them still delivers."""
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
                           ("decision_log", "DECISION_LOG.md")):
        path = out / filename
        if path.is_file():
            found.append(Artifact(path, names[role], MARKDOWN_MIME))
    outcome = out / "outcome.json"
    if outcome.is_file():
        found.append(Artifact(outcome, names["outcome"], JSON_MIME))
    return found


def _reviewed_docx(out: Path) -> Path | None:
    """The tracked-changes manuscript in a finished run, never the change log.

    Matched on DocProof's own suffixes rather than on the job's filename, so a
    source that was converted (a .doc, a Google Doc) still resolves."""
    changelog = DocumentFormat.CHANGE_LOG_SUFFIX.lower()
    for path in sorted(out.glob(f"*{DocumentFormat.REVIEWED_SUFFIX}*.docx")):
        if path.stem.lower().endswith(changelog):
            continue
        return path
    return None


# --- reading a verdict back out of the folder ---------------------------------

def outcome_in_folder(listing: list[DriveFile], source_name: str
                      ) -> DriveFile | None:
    """This book's outcome.json in the folder, if it is there yet.

    By name, not by marker: in `external` mode the file was placed by the
    practitioner's own tooling and carries no appProperties at all. Matched with
    the same dash/case forgiveness every other name comparison here uses."""
    stem = Path(source_name).stem or "manuscript"
    for candidate in listing:
        if candidate.is_folder:
            continue
        if naming.is_proof_outcome_name(candidate.name, stem):
            return candidate
    return None


def read_outcome(token: str, file: DriveFile, *, opener=drive._open_url
                 ) -> Verdict | None:
    """The verdict inside an outcome.json sitting in Drive.

    Only two fields are read — the word and the reason — and the word must be
    one DocProof recognises. Anything else (unreadable JSON, an outcome nobody
    has heard of) reads as "not there yet" rather than as a verdict: the book
    waits, which is the posture that cannot do damage. Nothing in this file
    names the property to write; that comes from the watcher's settings."""
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


# --- putting it back ----------------------------------------------------------

def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str | None = None,
                   opener=drive._open_url) -> list[str]:
    """Put the proofread's files in the folder, once each.

    The same record-as-you-go discipline prep and promo keep, on proofing's own
    `proof_uploaded` map: an upload that landed but was not written down would
    be uploaded again next tick. An output an interrupted earlier tick left
    behind — recognised by the source id it carries — is adopted rather than
    duplicated."""
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
    """Write proofing's marker on the manuscript.

    Its own property, never formatting's, so the two passes over one book never
    overwrite each other's "done". Written after everything it summarises, so
    `done` means the files are in the folder and HubSpot has moved on.

    `awaiting` is the odd one: it is written *before* the work, because the work
    is somebody else's, and it exists so a person reading the folder can see
    that a book is out with a practitioner. `is_proof_candidate` deliberately
    does not treat it as terminal, so the next tick still looks."""
    props = {PROOF_PROP: status,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason:
        props[REASON_PROP] = reason[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.proof_marked = status
    state.record(rec)
