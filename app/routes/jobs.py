"""The work: creating jobs, watching them, and collecting what they wrote."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from docproof import batch as batchlib
from docproof.config import load_config
from docproof.formats import get_format
from docproof.prep import place as placelib
from docproof.prep.place import PlaceError
from docproof.providers import estimate_cost, lookup

from . import common
from .. import settings as settingslib
from ..auth import owner_for
from ..jobs import Job, JobRunner, JobStore, read_usage
from ..prompts import list_prompts
from ..report import build_report
from ..settings import CONFIG_PATH, ERROR_DIR, Paths
from ..usage import build_usage
from ..watch.runner import WatchRunner


class JobRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    model: str
    mode: str = "batch"                       # "now" | "batch"
    schedule_at: str | None = None            # "HH:MM" local
    min_confidence: str = "medium"
    # file_id → chunk ids to review. A file absent from this map, or a null
    # entry, means the whole document.
    selections: dict[str, list[str] | None] | None = None
    # What to do with these documents: review them for errors, or prepare them
    # for the house InDesign template.
    kind: str = "review"                      # "review" | "prep"
    prep_output: str = "indesign"             # "indesign" | "tracked" | "both"


def _result_name(job: Job, which: str) -> str | None:
    """What a job called the file the user is asking for."""
    stem = Path(job.filename).stem or "document"
    names = {
        # "docx" is the old name for this route, kept so a page left open
        # across an upgrade keeps working.
        "document": get_format(job.filename).reviewed_name(job.filename),
        "docx": get_format(job.filename).reviewed_name(job.filename),
        "summary": "summary.md",
        "findings": "findings.json",
        "indesign": f"tagged_{stem}.docx",
        "tracked": f"tracked_{stem}.docx",
        "notes": "prep_notes.md",
        "prep": "prep.json",
        "placed": f"placed_{stem}.indd",
    }
    if job.is_prep and which in ("document", "docx"):
        # Whatever this job actually wrote, so one "open it" button works
        # for either output choice.
        return next(
            (n for n in (names["indesign"], names["tracked"])
             if (Path(job.results_dir) / n).is_file()), names["indesign"])
    return names.get(which)


def _result_path(job: Job, which: str) -> Path:
    """The file on disk behind one of the result buttons.

    Raises the same 404s the download route has always raised: an unknown name
    and a name whose file was moved or deleted are different problems, and the
    person reading the message is looking at their own Documents folder."""
    name = _result_name(job, which)
    if name is None:
        raise HTTPException(404, "Unknown file")
    path = Path(job.results_dir) / name
    if not path.is_file():
        raise HTTPException(404, f"{name} is missing")
    return path


def register(app: FastAPI) -> None:

    def _owned_job(job_id: str, owner: str) -> Job | None:
        """A job the caller is allowed to see, or None — for a 404 that reads
        the same whether the job is missing or someone else's, so a job id
        can't be probed for existence. On the desktop build there are no owners
        to check; the one local user sees everything, as before."""
        job = app.state.store.get(job_id)
        if job is None:
            return None
        if app.state.web and job.owner_id != owner:
            return None
        return job

    @app.post("/api/jobs")
    def create_jobs(req: JobRequest,
                    owner: str = Depends(owner_for)) -> dict:
        paths: Paths = app.state.paths
        runner: JobRunner = app.state.runner
        if req.mode not in ("now", "batch"):
            raise HTTPException(400, "mode must be 'now' or 'batch'")
        if req.kind not in ("review", "prep"):
            raise HTTPException(400, "kind must be 'review' or 'prep'")
        if req.prep_output not in ("indesign", "tracked", "both"):
            raise HTTPException(
                400, "prep_output must be 'indesign', 'tracked' or 'both'")
        # Prep reads its windows in order — a paragraph's meaning depends on
        # what came before it — so there is no batch form of it to offer.
        mode = "now" if req.kind == "prep" else req.mode
        info = lookup(req.model)
        if info is None:
            raise HTTPException(400, f"Unknown model {req.model!r}")
        if not settingslib.get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")

        # Every id is resolved before any job is enqueued: a 404 halfway
        # through used to leave the earlier files already running — the page
        # said failure, the retry ran them twice, and twice was billed twice.
        sources = {}
        for file_id in req.file_ids:
            source = common.resolve_upload(paths, file_id, owner)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            sources[file_id] = source

        # Spend cap (web build only). The estimate for this very submission
        # counts, so one oversized review can't slip past a nearly-spent cap;
        # administrators and the desktop build have no cap and skip all of this.
        if app.state.web:
            cap = common.cap_for(app.state.accounts.get_user(owner))
            if cap is not None:
                spent = common.month_spend(app.state.store, owner)
                totals = common.token_totals(paths, req.file_ids, owner)
                est = 0.0
                if totals:
                    inp, reqs = totals
                    est = estimate_cost(
                        req.model, input_tokens=inp,
                        output_tokens=reqs * common.OUTPUT_TOKEN_GUESS,
                        batch=(mode == "batch")) or 0.0
                if spent + est > cap:
                    raise HTTPException(
                        402, f"This review would put you over your "
                             f"${cap:.2f} monthly limit — you have used "
                             f"${spent:.2f} so far this month. Ask an "
                             f"administrator to raise it.")

        group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
        created = []
        for file_id in req.file_ids:
            source = sources[file_id]
            job = Job(
                id=batchlib.new_job_id(source.name),
                filename=source.name,
                source_path=str(source),
                model=req.model,
                mode=mode,
                group_id=group_id,
                schedule_at=req.schedule_at if mode == "batch" else None,
                min_confidence=req.min_confidence,
                selection=(req.selections or {}).get(file_id) or None,
                created_at=datetime.now(timezone.utc).isoformat(),
                kind=req.kind,
                prep_output=req.prep_output,
                owner_id=owner,
            )
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.get("/api/jobs")
    def list_jobs(owner: str = Depends(owner_for)) -> dict:
        scope = owner if app.state.web else None
        return {"jobs": [j.to_api() for j in app.state.store.all(scope)]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, owner: str = Depends(owner_for)) -> dict:
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        return job.to_api()

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, owner: str = Depends(owner_for)) -> dict:
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state != "failed":
            raise HTTPException(400, "Only reviews that need attention can be "
                                     "retried.")
        store.update(job_id, state="queued", error=None, done=0)
        return runner.enqueue(store.get(job_id)).to_api()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Stop a review before it has started spending anything.

        Once a job is running, waiting overnight on a vendor, or writing its
        files, there is nothing local left to cancel — the work is either
        already billed or already being written. Only a job that has not
        started can be pulled back, so that is all this offers."""
        store: JobStore = app.state.store
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state not in ("queued", "scheduled"):
            raise HTTPException(
                400, "This one has already started, so there is nothing left "
                     "to cancel.")
        updated = store.update_if(job_id, expect=job.state, state="cancelled",
                                  error=None)
        if updated is None:
            # It started in the instant between the check above and this one.
            raise HTTPException(
                400, "This one just started, so there is nothing left to "
                     "cancel.")
        # A cancelled job never runs again, so any checkpoint a failed earlier
        # attempt left behind is just clutter in the job folder now.
        app.state.runner.discard_checkpoint(job_id)
        return updated.to_api()

    @app.post("/api/jobs/{job_id}/download-anyway")
    def download_anyway(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Write the document for a review that failed the reject-all audit.

        The integrity check found text that changed without a tracked change
        around it; this hands the file over anyway, clearly flagged, reusing the
        review already paid for (no new model calls). The user has seen the
        mismatch on the results card before pressing this."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review.")
        if job.state != "failed":
            raise HTTPException(409, "This review did not fail the audit.")
        updated = app.state.runner.download_anyway(job_id)
        if updated is None:
            raise HTTPException(409, "This review can't be written out.")
        return updated.to_api()

    @app.get("/api/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str, owner: str = Depends(owner_for)):
        """Serve a result over HTTP — the browser build's way of handing a file
        over, and the fallback when the desktop window cannot open one."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = _result_path(job, which)
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs/{job_id}/open/{which}")
    def open_result(job_id: str, which: str, reveal: bool = False,
                    owner: str = Depends(owner_for)) -> dict:
        """Open a result in Word, InDesign, or the Finder.

        This is the desktop window's version of the download route: the file
        already exists in the user's own Documents folder, so the honest thing
        to do with "Open in Word" is open it in Word."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = _result_path(job, which)
        if sys.platform != "darwin":
            # Nothing to hand the file to; the page falls back to downloading.
            raise HTTPException(501, "This build can only open files on a Mac.")
        common.open_path(path, reveal=reveal)
        return {"ok": True, "opened": path.name}

    @app.post("/api/jobs/{job_id}/place")
    def place_in_indesign(job_id: str,
                          owner: str = Depends(owner_for)) -> dict:
        """Flow a finished prep job into the house template.

        Synchronous on purpose. This takes a minute and the user is watching
        InDesign do it — a job that reported back later would be stranger than
        a button that waits."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_prep:
            raise HTTPException(
                400, "Only a manuscript prepared for layout can be placed.")
        if job.state != "done":
            raise HTTPException(400, "This one is not finished yet.")
        tagged = _result_path(job, "indesign")

        template = (app.state.settings.indesign_template or "").strip()
        if not template:
            raise HTTPException(
                400, "Choose your InDesign template in Settings first — "
                     "DocProof places the manuscript into a copy of it.")
        if not Path(template).is_file():
            raise HTTPException(
                400, f"The template is not at {template} any more. Set it "
                     f"again in Settings.")
        if sys.platform != "darwin":
            raise HTTPException(501, "Placing needs InDesign on a Mac.")
        if placelib.find_indesign() is None:
            raise HTTPException(
                400, "InDesign does not appear to be installed on this Mac.")

        out = Path(job.results_dir) / _result_name(job, "placed")
        try:
            placed = placelib.place_into_template(template, tagged, out)
        except PlaceError as e:
            raise HTTPException(400, str(e))
        common.open_path(placed, reveal=True)
        return {"ok": True, "filename": placed.name, "path": str(placed)}

    @app.get("/api/jobs/{job_id}/prep")
    def prep_notes(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """What prep did, read back for the results screen."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this job yet")
        path = Path(job.results_dir) / "prep.json"
        if not path.is_file():
            raise HTTPException(404, "This job has no prep notes")
        data = json.loads(path.read_text("utf-8"))
        data["files"] = {kind: (Path(job.results_dir) / name).is_file()
                         for kind, name in
                         (("indesign", f"tagged_{Path(job.filename).stem}.docx"),
                          ("tracked", f"tracked_{Path(job.filename).stem}.docx"))}
        return data

    @app.get("/api/usage")
    def usage(owner: str = Depends(owner_for)) -> dict:
        """Tokens, calls and estimated spend.

        On the desktop it is every job on this machine: two job stores, one
        bill — the watcher keeps its own home (a separate folder is a separate
        lock, which lets a pass and this window run at once), but the money
        comes off the same card, so leaving half of it out would be the wrong
        figure. On the web it is this user's own jobs only; the watcher isn't
        theirs, so it has no place in their total."""
        if app.state.web:
            return build_usage(app.state.store.all(owner), read_usage)
        watch: WatchRunner = app.state.watch
        return build_usage([*app.state.store.all(), *watch.jobs()], read_usage)

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """The findings, read back as prose rather than as a record."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = Path(job.results_dir) / "findings.json"
        if not path.is_file():
            raise HTTPException(404, "This review has no findings file")
        cfg = load_config(CONFIG_PATH)
        rows = list_prompts(ERROR_DIR, app.state.paths.prompts,
                            list(cfg.error_type_keys))
        return build_report(path, {r["key"]: r["name"] for r in rows})

    @app.post("/api/tick")
    def tick(owner: str = Depends(owner_for)) -> dict:
        """Advance scheduled and in-flight work now instead of waiting for the
        next timer. The Jobs screen calls this when the user opens it.

        The tick itself advances everyone's batch work — it is the one shared
        clock — but the list it returns is only the caller's own, the same as
        /api/jobs."""
        app.state.runner.tick_once()
        scope = owner if app.state.web else None
        return {"jobs": [j.to_api() for j in app.state.store.all(scope)]}
