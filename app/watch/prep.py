"""One manuscript, from the folder and back again.

Fetch it, prepare it, put the results beside it, mark it done. The order is
the whole design: every step is safe to repeat, and the marker that makes a
file invisible to the next tick is written last, so it means "everything
before me finished" rather than "somebody started".

What that buys, crash by crash:

- died after the download — downloaded again, which costs seconds
- died mid-preparation — the checkpoint replays the windows already paid for
- died after preparing, before uploading — the finished job is on disk and the
  state file points at it, so the next tick uploads without asking a model
  anything
- died after an upload, before recording it — the output is in the folder
  carrying the id of the manuscript it came from, so it is adopted rather
  than uploaded twice
- died after uploading, before marking — every artifact is already recorded,
  so the next tick only writes the marker

The model is the expensive step and the only one that is not idempotent. That
is why it sits in the middle, with something written down on either side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docproof import prep as preplib
from docproof.batch import new_job_id
from docproof.prep.convert import ensure_docx
from docproof.prep.verify import VerificationFailed

from app.jobs import Job, JobRunner, JobStore

from . import drive, naming
from .drive import DOCX_MIME, DriveFile
from .settings import WatchSettings
from .stages import (AT_PROP, FAILED, FORMATTED, JOB_PROP, OUTPUT_PROP,
                     REASON_PROP, SOURCE_PROP, STATE_PROP)
from .state import FileRecord, WatchState

log = logging.getLogger("docproof.app.watch.prep")

MARKDOWN_MIME = "text/markdown"

# appProperties allow about 124 bytes for a key and its value together. The
# whole reason lives on the job record and in the log; what goes to Drive is
# enough to recognise which failure this was.
REASON_LIMIT = 90


@dataclass(frozen=True)
class Artifact:
    """A file prep wrote, and what it should be called in the folder."""

    path: Path
    name: str
    mime: str


# --- getting it ---------------------------------------------------------------

def local_name(file: DriveFile) -> str:
    """What to call this manuscript on disk.

    A native Doc has a title rather than a filename, and a title may contain
    the one character a filename may not."""
    safe = file.name.replace("/", "-").replace(":", "-").strip() or "manuscript"
    return f"{safe}.docx" if file.is_google_doc else safe


def fetch(token: str, file: DriveFile, dest_dir: str | Path, *,
          opener=drive._open_url) -> Path:
    """The manuscript, on this machine, as a .docx.

    A `.doc` or `.odt` is converted here rather than later for the same reason
    the app converts at drop time: LibreOffice missing is a fact about this
    Mac, and finding it out before a model is called is free."""
    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / local_name(file)
    if file.is_google_doc:
        drive.export_docx(token, file.id, target, opener=opener)
    else:
        drive.download(token, file.id, target, opener=opener)
    source, note = ensure_docx(target, folder)
    if note:
        log.info("%s: %s", file.name, note)
    return source


# --- preparing it -------------------------------------------------------------

def make_job(local: Path, ws: WatchSettings) -> Job:
    return Job(id=new_job_id(local.name), filename=local.name,
               source_path=str(local), model=ws.model, mode="now", kind="prep",
               prep_output=ws.prep_output, source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job, *,
            mock: bool = False) -> Job:
    """Run it here and now, and hand back what the record says afterwards.

    Straight into `run_one` rather than the worker thread: a tick that has
    nothing else to do while a manuscript is prepared gains nothing from being
    asynchronous, and a launchd process that exits when the work is finished is
    easier to reason about than one that exits when a queue drains.

    The blanket catch mirrors `JobRunner._work`: an exception nobody predicted
    must leave a failed job rather than a job stuck at `running` forever."""
    store.save(job)
    try:
        if mock:
            _run_mock(runner, store, job)
        else:
            runner.run_one(job.id)
    except Exception as e:                # noqa: BLE001 - mirrors _work
        log.exception("Preparing %s failed", job.filename)
        store.update(job.id, state="failed", error=str(e))
    return store.get(job.id) or job


def _run_mock(runner: JobRunner, store: JobStore, job: Job) -> None:
    """`JobRunner._run_prep` with the model taken out.

    Deliberately a mirror rather than a flag threaded through the app's job
    runner: this exists so somebody can rehearse the whole round trip — sign
    in, list, download, upload, mark — against their own Drive folder without
    spending anything, and a rehearsal has no windows worth checkpointing.
    Verification is not skipped, because a rehearsal that skipped the one gate
    that protects the author's words would be rehearsing something else."""
    cfg = runner.config_for(job)
    prepared = preplib.prepare(cfg, job.source_path,
                               config_dir=runner.config_path.parent,
                               override_dir=store.paths.prep)
    store.update(job.id, state="running", done=0,
                 total=prepared.request_count,
                 words=prepared.structure.word_count)
    tags, usage = preplib.run_mock(prepared)
    out = runner.results_dir(job)
    store.update(job.id, results_dir=str(out))
    try:
        outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                 source_path=job.source_path,
                                 outputs=cfg.prep.outputs)
    except VerificationFailed as e:
        log.error("Prep for %s failed verification: %s", job.id, e)
        store.update(job.id, state="failed", error=str(e),
                     results_dir=str(out), verified=False)
        return
    store.update(job.id, state="done", results_dir=str(out), error=None,
                 tagged=outputs.tagged, applied=outputs.tagged,
                 flags=outputs.flags,
                 verified=all(c.ok for c in outputs.verifications),
                 words=outputs.words)


# --- putting it back ----------------------------------------------------------

def artifacts(job: Job, ws: WatchSettings) -> list[Artifact]:
    """What of this run belongs in the folder, and what to call it there.

    Prep writes internal names (`tagged_*.docx`, `tracked_*.docx`,
    `prep_notes.md`); the folder belongs to people, so each is renamed to the
    house stage series on the way out — `<surname> - book 0.docx` for the
    InDesign-ready deliverable, with the tracked-changes copy and the notes
    beside it under the same base (see `naming.py`).

    The deliverable takes the bare base name. If a run made no InDesign-ready
    file (a tracked-only prep), the tracked file takes it instead, so the folder
    always has one `<base>.docx` to hand to the next stage."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    base = naming.format_base(Path(job.filename).stem or "manuscript")
    book = sorted(out.glob("book_*.docx"))
    tagged = sorted(out.glob("tagged_*.docx"))
    tracked = sorted(out.glob("tracked_*.docx"))
    found: list[Artifact] = []
    # The book-styled reading copy outranks the InDesign-ready file, which
    # outranks the redline; whatever exists first takes the bare base name.
    primary = book or tagged or tracked
    if primary:
        found.append(Artifact(primary[0], f"{base}.docx", DOCX_MIME))
    if tagged and primary is not tagged:    # beside the book copy
        found.append(Artifact(tagged[0],
                              f"{base}{naming.INDESIGN_SUFFIX}.docx", DOCX_MIME))
    if tracked and primary is not tracked:  # the redline sits beside it
        found.append(Artifact(tracked[0],
                              f"{base}{naming.TRACKED_SUFFIX}.docx", DOCX_MIME))
    notes = out / "prep_notes.md"
    if ws.upload_notes and notes.is_file():
        found.append(Artifact(notes, f"{base}{naming.NOTES_SUFFIX}.md",
                              MARKDOWN_MIME))
    return found


def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str | None = None,
                   opener=drive._open_url) -> list[str]:
    """Put this run's files in the folder, once each.

    `dest_folder_id` is where they land: the watched folder in the flat model,
    or the author's own subfolder in subfolder mode. It defaults to
    `ws.folder_id`, so a caller that has not been taught about subfolders keeps
    writing exactly where it did.

    Recorded one at a time: an upload that landed and was not written down
    would be uploaded again on the next tick, which is how a folder ends up
    with three copies of the same formatted manuscript."""
    dest = dest_folder_id or ws.folder_id
    placed = []
    for artifact in artifacts(job, ws):
        if artifact.name in rec.uploaded:
            continue
        orphan = _already_there(listing, file.id, artifact.name)
        if orphan is not None:
            log.info("%s is already in the folder from an earlier run",
                     artifact.name)
            rec.uploaded[artifact.name] = orphan.id
            state.record(rec)
            continue
        new_id = drive.upload(token, dest, artifact.path,
                              name=artifact.name, mime_type=artifact.mime,
                              app_properties={OUTPUT_PROP: "1",
                                              SOURCE_PROP: file.id,
                                              JOB_PROP: job.id},
                              opener=opener)
        rec.uploaded[artifact.name] = new_id
        state.record(rec)
        placed.append(artifact.name)
    return placed


def _already_there(listing: list[DriveFile], source_id: str,
                   name: str) -> DriveFile | None:
    """An output from a previous attempt at this manuscript, which the state
    file lost track of. It carries the id of the file it came from, so this is
    a fact about the folder rather than a guess from the name."""
    for candidate in listing:
        if (candidate.name == name
                and candidate.app_properties.get(SOURCE_PROP) == source_id):
            return candidate
    return None


def mark_source(token: str, file: DriveFile, job: Job, rec: FileRecord,
                state: WatchState, *, failed: str | None = None,
                opener=drive._open_url) -> None:
    """Say what happened to this manuscript, on the manuscript.

    Last, always. This marker is what makes the file invisible to the next
    tick, so it has to mean everything before it finished."""
    props = {STATE_PROP: FAILED if failed else FORMATTED,
             JOB_PROP: job.id,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if failed:
        props[REASON_PROP] = failed[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.marked = props[STATE_PROP]
    state.record(rec)


def failure_note(job: Job, file: DriveFile, reason: str) -> Path | None:
    """A short note saying nothing was changed, for publishers who would
    rather the folder said so than said nothing."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return None
    stem = Path(job.filename).stem or "manuscript"
    path = out / f"prep_failed_{stem}.md"
    path.write_text(
        f"# {file.name} was not formatted\n\n"
        f"DocProof prepared this manuscript and then checked, word for word, "
        f"that the file it had written still said exactly what the author "
        f"wrote. It did not, so the file was deleted rather than handed on.\n\n"
        f"**Nothing about the original was changed.**\n\n"
        f"What the check found:\n\n> {reason}\n\n"
        f"`prep_notes.md` from the same run says what prep intended to do, and "
        f"where the two diverged.\n",
        encoding="utf-8")
    return path
