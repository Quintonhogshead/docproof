"""One manuscript, into promo copy, and the copy back to the folder.

The promo analog of `prep.py`, and the same idempotent shape: fetch the book,
write the copy, put the two documents beside it, mark it done — with the marker
that hides the file from the next tick written last, so it means "everything
before me finished".

What differs from prep is the marker and the gate at the end. Prep always ships
what it made; promo may hold it. In hold mode a run generates the copy and marks
the book `pending` — enough to stop the next tick regenerating it — and stops
there, leaving the two .docx for a person to approve in the panel before they go
to Drive. `deliver` is the step that ships an approved (or auto) book, and it is
safe to repeat: every upload is recorded, and writing a HubSpot value the record
already holds is a no-op.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docproof import promo as promolib

from app.jobs import Job, JobRunner, JobStore

from . import drive
from .drive import DOCX_MIME, DriveFile
from .prep import fetch  # generic download + convert; promo reuses it verbatim
from .settings import WatchSettings
from .stages import (AT_PROP, JOB_PROP, OUTPUT_PROP, PROMO_DONE, PROMO_FAILED,
                     PROMO_PENDING, PROMO_PROP, REASON_PROP, SOURCE_PROP)
from .state import FileRecord, WatchState

log = logging.getLogger("docproof.app.watch.promo")

REASON_LIMIT = 90

__all__ = ["fetch", "make_job", "run_job", "artifacts", "upload_outputs",
           "mark_source", "failure_note"]


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    mime: str



def make_job(local: Path, ws: WatchSettings) -> Job:
    """A promo job for this manuscript. `approval` starts where the toggle says:
    "auto" ships without a gate, "pending" waits for a person in the panel. The
    generation itself doesn't read this — `deliver` does."""
    from docproof.batch import new_job_id

    approval = "auto" if ws.promo_auto_upload else "pending"
    return Job(id=new_job_id(local.name), filename=local.name,
               source_path=str(local), model=ws.promo_model_or_default,
               mode="now", kind="promo", approval=approval, source="watch",
               created_at=datetime.now(timezone.utc).isoformat())


def run_job(runner: JobRunner, store: JobStore, job: Job, *,
            mock: bool = False) -> Job:
    """Run it here and now, and hand back what the record says afterwards. The
    blanket catch mirrors `JobRunner._work`: an unpredicted exception must leave
    a failed job, not one stuck at `running`."""
    store.save(job)
    try:
        if mock:
            _run_mock(runner, store, job)
        else:
            runner.run_one(job.id)
    except Exception as e:                # noqa: BLE001 - mirrors _work
        log.exception("Writing promo copy for %s failed", job.filename)
        # An oversize book (raised straight from prepare in the mock path)
        # carries its numbers, so the tick can email a person the size and how to
        # run it by hand rather than counting a blind failure.
        extra = ({"error_kind": "oversize", "words": e.words,
                  "input_tokens": e.tokens}
                 if isinstance(e, promolib.PromoTooLarge) else {})
        store.update(job.id, state="failed", error=str(e), **extra)
    return store.get(job.id) or job


def _run_mock(runner: JobRunner, store: JobStore, job: Job) -> None:
    """`JobRunner._run_promo` with the model taken out, so a rehearsal can drive
    the whole round trip — sign in, list, download, generate, upload, mark —
    against a real Drive folder without spending anything."""
    cfg = runner.config_for(job)
    prepared = promolib.prepare(cfg, job.source_path,
                                config_dir=runner.config_path.parent,
                                override_dir=store.paths.promo)
    store.update(job.id, state="running", done=0, total=1,
                 words=prepared.manuscript.word_count)
    result, usage = promolib.run_mock(prepared)
    out = runner.results_dir(job)
    store.update(job.id, results_dir=str(out))
    outputs = promolib.finish(prepared, result, usage, cfg, out_dir=out,
                              source_path=job.source_path)
    store.update(job.id, state="done", results_dir=str(out), error=None,
                 words=outputs.words, unverified=len(outputs.unverified),
                 verified=not outputs.unverified)



def artifacts(job: Job) -> list[Artifact]:
    """The two documents that belong in the folder: the teaser and the posts.

    Named as the engine wrote them — `<base> - teaser.docx`,
    `<base> - social posts.docx` — which already sort beside the book. The
    editable `promo.json` stays local; it is the panel's source of truth, not a
    deliverable."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return []
    found: list[Artifact] = []
    for teaser in sorted(out.glob("* - teaser.docx")):
        found.append(Artifact(teaser, teaser.name, DOCX_MIME))
        break
    for posts in sorted(out.glob("* - social posts.docx")):
        found.append(Artifact(posts, posts.name, DOCX_MIME))
        break
    return found


def upload_outputs(token: str, file: DriveFile, job: Job, ws: WatchSettings,
                   rec: FileRecord, state: WatchState,
                   listing: list[DriveFile], *, dest_folder_id: str | None = None,
                   opener=drive._open_url) -> list[str]:
    """Put the teaser and the posts in the folder, once each.

    The same record-as-you-go discipline as prep, on promo's own `uploaded`
    map: an upload that landed but was not written down would be uploaded again
    next tick. An output left in the folder by an interrupted earlier tick —
    recognised by the source id it carries — is adopted rather than duplicated."""
    from .prep import _already_there

    dest = dest_folder_id or ws.folder_id
    placed = []
    for artifact in artifacts(job):
        if artifact.name in rec.promo_uploaded:
            continue
        orphan = _already_there(listing, file.id, artifact.name)
        if orphan is not None:
            log.info("%s is already in the folder from an earlier run",
                     artifact.name)
            rec.promo_uploaded[artifact.name] = orphan.id
            state.record(rec)
            continue
        new_id = drive.upload(token, dest, artifact.path,
                              name=artifact.name, mime_type=artifact.mime,
                              app_properties={OUTPUT_PROP: "1",
                                              SOURCE_PROP: file.id,
                                              JOB_PROP: job.id},
                              opener=opener)
        rec.promo_uploaded[artifact.name] = new_id
        state.record(rec)
        placed.append(artifact.name)
    return placed


def mark_source(token: str, file: DriveFile, rec: FileRecord,
                state: WatchState, *, status: str, reason: str | None = None,
                opener=drive._open_url) -> None:
    """Write promo's marker on the manuscript: `pending`, `done`, or `failed`.

    Its own property, never formatting's, so the two stages never overwrite each
    other's "done". Written after everything it summarises, so — for `done` — it
    means the copy is in the folder and HubSpot has moved on."""
    props = {PROMO_PROP: status,
             AT_PROP: datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason:
        props[REASON_PROP] = reason[:REASON_LIMIT]
    drive.set_app_properties(token, file.id, props, opener=opener)
    rec.promo_marked = ("delivered" if status == PROMO_DONE
                        else "failed" if status == PROMO_FAILED else "pending")
    state.record(rec)


def failure_note(job: Job, file: DriveFile, reason: str) -> Path | None:
    """A short note, for publishers who would rather the folder said promo
    could not be written than said nothing."""
    out = Path(job.results_dir or "")
    if not out.is_dir():
        return None
    stem = Path(job.filename).stem or "manuscript"
    path = out / f"promo_failed_{stem}.md"
    path.write_text(
        f"# Promo copy for {file.name} was not written\n\n"
        f"DocProof tried to write a teaser and social posts for this "
        f"manuscript and could not.\n\n"
        f"**Nothing about the manuscript was changed.**\n\n"
        f"What went wrong:\n\n> {reason}\n",
        encoding="utf-8")
    return path
