"""One manuscript, plus what the author told the press, into a marketing plan —
and the plan back to the folder.

The marketing-plan analog of `promo.py`, and the same idempotent shape: gather
the inputs, write the plan, put it beside the book, mark it done — with the
marker that hides the manuscript from the next tick written last, so it means
"everything before me finished".

Two things differ from the copy stage. First the inputs: the plan reads more of
the folder than the teaser does — the author's blurb doc and their answers to
the press's publicity questionnaire sit beside the book, found by name pattern
and read for their text, then handed to the model as grounding. Second the gate:
the plan gates and writes back on its *own* HubSpot property — the "Marketing
Plan" field the press already flips `Needed` -> `Uploaded` by hand — not the
status dropdown the format and copy stages share. So a book can be formatted,
promo'd, and planned in any order without the three stages touching each other's
markers or values.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docproof import promo as promolib
from docproof.batch import new_job_id

from app.jobs import Job, JobRunner, JobStore

from . import drive
from .drive import DOCX_MIME, DriveFile
from .naming import is_output_name
from .prep import _already_there, fetch  # generic download+convert; reused verbatim
from .settings import WatchSettings
from .stages import (AT_PROP, JOB_PROP, OUTPUT_PROP, PLAN_DONE, PLAN_FAILED,
                     PLAN_PENDING, PLAN_PROP, REASON_PROP, SOURCE_PROP)
from .state import FileRecord, WatchState

log = logging.getLogger("docproof.app.watch.plan")

REASON_LIMIT = 90
TEXT_SUFFIXES = (".txt", ".md")

__all__ = ["fetch", "make_job", "run_job", "artifacts", "upload_outputs",
           "mark_source", "failure_note", "find_input_files", "read_sibling_text",
           "gather_inputs", "PlanInputs"]


# --- gathering the inputs -----------------------------------------------------

@dataclass(frozen=True)
class PlanInputs:
    """What the folder gave the plan beyond the book: the author's blurbs and
    their questionnaire answers, each an empty string when the folder held no
    such file. Both are optional — the prompt degrades to a thinner but valid
    plan — so a folder with only a manuscript still yields a plan."""

    blurbs: str = ""
    questionnaire: str = ""


def find_input_files(listing: list[DriveFile], manuscript: DriveFile,
                     ws: WatchSettings) -> tuple[DriveFile | None, DriveFile | None]:
    """The blurb file and the questionnaire file among the manuscript's
    siblings, matched by the configured name patterns.

    Pure and network-free, so which file is which can be tested without Drive.
    Never returns the manuscript itself, a folder, or a file DocProof wrote — a
    prior run's `- marketing plan.docx` must not be mistaken for the blurbs. An
    empty pattern disables that input; a bad regex is treated as no pattern
    rather than raised, so one typo cannot stop the pass."""
    return (_match_sibling(listing, manuscript, ws.plan_blurb_pattern),
            _match_sibling(listing, manuscript, ws.plan_form_pattern))


def _match_sibling(listing: list[DriveFile], manuscript: DriveFile,
                   pattern: str) -> DriveFile | None:
    if not pattern:
        return None
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        log.warning("Ignoring a bad plan file pattern %r (%s).", pattern, e)
        return None
    # Sorted by name so a folder with two candidates picks the same one every
    # pass rather than whatever order Drive listed them in.
    for f in sorted(listing, key=lambda f: f.name):
        if f.id == manuscript.id or f.is_folder:
            continue
        if f.app_properties.get(OUTPUT_PROP) or is_output_name(f.name):
            continue
        if rx.search(f.name):
            return f
    return None


def read_sibling_text(token: str, file: DriveFile, dest_dir: str | Path, *,
                      opener=drive._open_url) -> str:
    """The text of a blurb or questionnaire sibling, however it is stored.

    A plain-text file is read straight; everything else (a .docx, a native
    Google Doc, a LibreOffice-convertible) goes through `prep.fetch` to a .docx
    and is read with promo's tolerant manuscript reader — the same reader that
    copes with tracked changes, which a form export may carry."""
    suffix = Path(file.name).suffix.lower()
    if suffix in TEXT_SUFFIXES and not file.is_google_doc:
        raw = drive.download_bytes(token, file.id, opener=opener,
                                   what="download a plan input")
        return raw.decode("utf-8", errors="replace").strip()
    local = fetch(token, file, dest_dir, opener=opener)
    return promolib.read_manuscript(local).text.strip()


def gather_inputs(token: str, manuscript: DriveFile, listing: list[DriveFile],
                  ws: WatchSettings, dest_dir: str | Path, *,
                  opener=drive._open_url) -> PlanInputs:
    """The blurb and questionnaire text for this book, or blanks where a folder
    has neither. A sibling that will not download or read is logged and treated
    as absent — a missing blurb must not sink a plan the book alone can carry."""
    blurb_file, form_file = find_input_files(listing, manuscript, ws)
    return PlanInputs(
        blurbs=_read_or_skip(token, blurb_file, dest_dir, "blurbs",
                             opener=opener),
        questionnaire=_read_or_skip(token, form_file, dest_dir, "questionnaire",
                                    opener=opener))


def _read_or_skip(token: str, file: DriveFile | None, dest_dir: str | Path,
                  what: str, *, opener) -> str:
    if file is None:
        return ""
    try:
        text = read_sibling_text(token, file, dest_dir, opener=opener)
        log.info("Plan %s read from %s (%d chars).", what, file.name, len(text))
        return text
    except Exception as e:                 # noqa: BLE001 - one input, not the plan
        log.warning("Could not read the %s file %s (%s); writing the plan "
                    "without it.", what, file.name, e)
        return ""


# --- generating it ------------------------------------------------------------

def make_job(local: Path, ws: WatchSettings, *, pen_name: str = "",
             blurbs: str = "", questionnaire: str = "") -> Job:
    """A marketing-plan job for this manuscript. `plan_only` sends it down the
    plan path in `JobRunner`; `approval` starts where the toggle says, exactly
    like promo — "auto" ships without a gate, "pending" waits in the panel."""
    approval = "auto" if ws.plan_auto_upload else "pending"
    return Job(id=new_job_id(local.name), filename=local.name,
               source_path=str(local), model=ws.plan_model_or_default,
               mode="now", kind="promo", plan_only=True, approval=approval,
               effort=ws.plan_effort, plan_author=pen_name, plan_blurbs=blurbs,
               plan_questionnaire=questionnaire, source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job, *,
            mock: bool = False) -> Job:
    """Run it here and now, and hand back what the record says afterwards. The
    blanket catch mirrors `promo.run_job`: an unpredicted exception must leave a
    failed job, and an oversize book carries its numbers so the tick can email a
    person the size and how to run it by hand."""
    store.save(job)
    try:
        if mock:
            _run_mock(runner, store, job)
        else:
            runner.run_one(job.id)
    except Exception as e:                 # noqa: BLE001 - mirrors promo.run_job
        log.exception("Writing the marketing plan for %s failed", job.filename)
        extra = ({"error_kind": "oversize", "words": e.words,
                  "input_tokens": e.tokens}
                 if isinstance(e, promolib.PromoTooLarge) else {})
        store.update(job.id, state="failed", error=str(e), **extra)
    return store.get(job.id) or job


def _run_mock(runner: JobRunner, store: JobStore, job: Job) -> None:
    """`JobRunner._run_plan` with the model taken out, so a rehearsal can drive
    the whole round trip — sign in, list, download, gather, generate, upload,
    mark — against a real Drive folder without spending anything."""
    cfg = runner.config_for(job)
    meta = promolib.PlanMeta(
        title="", author_name=job.plan_author, keywords=job.plan_keywords,
        back_cover=job.plan_blurbs, author_city=job.plan_city,
        questionnaire=job.plan_questionnaire)
    prepared = promolib.prepare_plan(cfg, job.source_path, meta,
                                     config_dir=runner.config_path.parent,
                                     override_dir=store.paths.promo)
    store.update(job.id, state="running", done=0, total=1,
                 words=prepared.manuscript.word_count)
    plan, usage = promolib.run_plan_mock(prepared)
    out = runner.results_dir(job)
    store.update(job.id, results_dir=str(out))
    promolib.finish_plan(prepared, plan, cfg, out_dir=out,
                         source_path=job.source_path)
    store.update(job.id, state="done", results_dir=str(out), error=None,
                 words=prepared.manuscript.word_count, unverified=0,
                 verified=True)


# --- putting it back ----------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    mime: str


def artifacts(job: Job) -> list[Artifact]:
    """The one document that belongs in the folder: `<base> - marketing plan.docx`.

    The editable `marketing_plan.json` stays local — it is the panel's source of
    truth, not a deliverable — exactly as promo keeps `promo.json` back."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    for doc in sorted(out.glob("* - marketing plan.docx")):
        return [Artifact(doc, doc.name, DOCX_MIME)]
    return []


def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str | None = None,
                   opener=drive._open_url) -> list[str]:
    """Put the plan in the folder, once. The same record-as-you-go discipline as
    promo, on the plan's own `plan_uploaded` map: an upload that landed but was
    not written down would be uploaded again next tick, and a plan left by an
    interrupted earlier tick — recognised by the source id it carries — is
    adopted rather than duplicated."""
    dest = dest_folder_id or ws.folder_id
    placed = []
    for artifact in artifacts(job):
        if artifact.name in rec.plan_uploaded:
            continue
        orphan = _already_there(listing, file.id, artifact.name)
        if orphan is not None:
            log.info("%s is already in the folder from an earlier run",
                     artifact.name)
            rec.plan_uploaded[artifact.name] = orphan.id
            state.record(rec)
            continue
        new_id = drive.upload(token, dest, artifact.path,
                              name=artifact.name, mime_type=artifact.mime,
                              app_properties={OUTPUT_PROP: "1",
                                              SOURCE_PROP: file.id,
                                              JOB_PROP: job.id},
                              opener=opener)
        rec.plan_uploaded[artifact.name] = new_id
        state.record(rec)
        placed.append(artifact.name)
    return placed


def mark_source(token: str, file: DriveFile, rec: FileRecord,
                state: WatchState, *, status: str, reason: str | None = None,
                opener=drive._open_url) -> None:
    """Write the plan's marker on the manuscript: `pending`, `done`, or `failed`.

    Its own property (`docproof.plan`), never formatting's or promo's, so the
    three stages never overwrite each other's "done". Written after everything it
    summarises, so — for `done` — it means the plan is in the folder and HubSpot
    has moved on."""
    props = {PLAN_PROP: status,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason:
        props[REASON_PROP] = reason[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.plan_marked = ("delivered" if status == PLAN_DONE
                       else "failed" if status == PLAN_FAILED else "pending")
    state.record(rec)


def failure_note(job: Job, file: DriveFile, reason: str) -> Path | None:
    """A short note, for publishers who would rather the folder said the plan
    could not be written than said nothing."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return None
    stem = Path(job.filename).stem or "manuscript"
    path = out / f"plan_failed_{stem}.md"
    path.write_text(
        f"# A marketing plan for {file.name} was not written\n\n"
        f"DocProof tried to write a marketing plan for this manuscript and "
        f"could not.\n\n"
        f"**Nothing about the manuscript was changed.**\n\n"
        f"What went wrong:\n\n> {reason}\n",
        encoding="utf-8")
    return path
