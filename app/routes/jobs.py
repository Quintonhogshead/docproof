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
from docproof.variants import VARIANT_KEYS

from . import common
from .. import features as featureslib
from .. import settings as settingslib
from ..auth import owner_for
from ..jobs import Job, JobRunner, JobStore, read_usage
from ..prompts import list_prompts
from ..report import build_report
from ..settings import CONFIG_PATH, ERROR_DIR, Paths
from ..usage import build_usage


class JobRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    model: str
    mode: str = "batch"                       # "now" | "batch"
    schedule_at: str | None = None            # "HH:MM" local
    min_confidence: str = "medium"
    # Which English this manuscript is written in. Empty keeps the config's own
    # variant, so an older page that doesn't send it — and the watcher, which
    # never does — behaves exactly as before.
    variant: str = ""
    # Reasoning depth for this run. None falls back to the saved default, so an
    # older page that doesn't send it keeps whatever the defaults screen set.
    effort: str | None = None
    # Which model reads the whole book for the glossary pass. None falls back to
    # the saved default; "off" turns the pass off for this run.
    glossary_model: str | None = None
    # file_id → chunk ids to review. A file absent from this map, or a null
    # entry, means the whole document.
    selections: dict[str, list[str] | None] | None = None
    # What to do with these documents: review them for errors, or prepare them
    # for the house InDesign template.
    kind: str = "review"                      # "review" | "prep"
    prep_output: str = "book"       # "book" | "indesign" | "tracked" | "both" | "all"
    # Book output only: the operator's answers for the sketch. Empty means
    # "let the detector read them off the opening pages".
    prep_subject: str = ""
    prep_title: str = ""
    prep_author: str = ""
    # Per-run pass toggles, {feature_id: on}. Omitted or empty leaves the config
    # defaults. Unknown ids are refused, not ignored — see create_jobs.
    features: dict[str, bool] | None = None
    # Multi-round review: review the manuscript this many times, each round
    # reading the previous round's corrections. None falls back to the saved
    # default; 1 is the ordinary single review. judge_prompt is the panel-edited
    # judge instructions; empty means the built-in default.
    rounds: int | None = None
    judge_prompt: str = ""
    # The between-round judge model. None/empty falls back to the config default.
    judge_model: str | None = None
    # The continuity read's editable system prompt; empty means the built-in
    # default. continuity_only runs that whole-book contradiction read on its
    # own, with every detector pass and sweep stripped off.
    continuity_prompt: str = ""
    continuity_only: bool = False
    # The judge gates' models and editable instructions. None/empty falls back to
    # the config default; each gate itself is a feature toggle.
    meaning_model: str | None = None
    meaning_prompt: str = ""
    fix_model: str | None = None
    fix_prompt: str = ""
    # A label for which effort tier the picker applied, carried onto the job card
    # only. The tier is a client-side macro over the controls above, so this
    # never changes how the job runs; empty means a custom or older submission.
    preset: str = ""


class RejudgeRequest(BaseModel):
    """Which gates to run over a finished review, and on what. Nothing else is
    re-run, so the switches here ARE the job — an empty request is refused
    rather than quietly rebuilding the same document."""
    meaning_check: bool = False
    fix_check: bool = False
    meaning_model: str | None = None
    fix_model: str | None = None
    meaning_prompt: str = ""
    fix_prompt: str = ""


# The states a job stays in for good: it has stopped, so it can be removed from
# the results list. Everything else is still moving and must be aborted first.
_TERMINAL = ("done", "failed", "cancelled")


def _result_name(job: Job, which: str) -> str | None:
    """What a job called the file the user is asking for."""
    stem = Path(job.filename).stem or "document"
    names = {
        # "docx" is the old name for this route, kept so a page left open
        # across an upgrade keeps working.
        "document": get_format(job.filename).reviewed_name(job.filename),
        "docx": get_format(job.filename).reviewed_name(job.filename),
        "changes": get_format(job.filename).change_log_name(job.filename),
        "summary": "summary.md",
        "findings": "findings.json",
        "book": f"book_{stem}.docx",
        "indesign": f"tagged_{stem}.docx",
        "tracked": f"tracked_{stem}.docx",
        "notes": "prep_notes.md",
        "prep": "prep.json",
        "placed": f"placed_{stem}.indd",
    }
    if job.is_prep and which in ("document", "docx"):
        # Whatever this job actually wrote, so one "open it" button works for
        # any output choice — counting the archive as well as the disk, so a
        # restored prep job (no local results yet) still resolves to the file it
        # really produced rather than crashing on a None results_dir.
        def _present(n: str) -> bool:
            if job.results_dir and (Path(job.results_dir) / n).is_file():
                return True
            return n in job.drive_files
        return next((n for n in (names["book"], names["indesign"],
                                 names["tracked"])
                     if _present(n)), names["book"])
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

    def _resolve_result(job: Job, name: str) -> Path | None:
        """The local path for one of a job's files, fetched back from the Drive
        archive and re-cached if the local copy is gone — the thing that lets a
        results tab still work after a redeploy wiped the folder, or after a
        volume loss left the record with no local results at all. None only when
        the file is in neither place; the caller answers the 404.

        The happy path — the file is on disk — costs one `is_file` and never
        touches Drive."""
        from ..watch import archive as archivelib
        if job.results_dir and (Path(job.results_dir) / name).is_file():
            return Path(job.results_dir) / name
        runner = app.state.runner
        if runner.notify_home is None or not job.drive_files.get(name):
            return None
        # A restored job has no results folder yet; claim one on the volume so the
        # fetched file (and its siblings, next click) has a durable home.
        dest_dir = (Path(job.results_dir) if job.results_dir
                    else runner.results_dir(job))
        fetched = archivelib.fetch_file(runner.notify_home, job, name, dest_dir)
        if fetched is not None and fetched.is_file():
            if not job.results_dir:
                app.state.store.update(job.id, results_dir=str(dest_dir))
            return fetched
        return None

    def _ensure_source(job: Job) -> Job:
        """Make sure the submitted manuscript is on disk before a re-run needs
        it, fetching `source - <name>` back from the archive when a wipe took the
        original. Leaves the job untouched when the source is already there or the
        archive cannot supply it — the run then fails as it would have."""
        from ..watch import archive as archivelib
        if job.source_path and Path(job.source_path).is_file():
            return job
        runner = app.state.runner
        original = Path(job.filename).name
        if runner.notify_home is None or not job.drive_files.get(f"source - {original}"):
            return job
        dest_dir = (app.state.paths.uploads / (job.owner_id or "local")
                    / f"restored-{job.id}")
        got = archivelib.fetch_file(runner.notify_home, job,
                                    f"source - {original}", dest_dir,
                                    save_as=original)
        if got is not None and got.is_file():
            return app.state.store.update(job.id, source_path=str(got)) or job
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
        if req.prep_output not in ("book", "indesign", "tracked", "both", "all"):
            raise HTTPException(
                400, "prep_output must be 'book', 'indesign', 'tracked', "
                     "'both' or 'all'")
        unknown = featureslib.unknown_features(req.features)
        if unknown:
            raise HTTPException(
                400, f"Unknown feature(s): {', '.join(sorted(unknown))}")
        # Prep reads its windows in order — a paragraph's meaning depends on
        # what came before it — so there is no batch form of it to offer.
        mode = "now" if req.kind == "prep" else req.mode
        if req.effort is not None and req.effort not in settingslib.EFFORT_LEVELS:
            raise HTTPException(
                400, f"effort must be one of {', '.join(settingslib.EFFORT_LEVELS)}")
        # Refused here rather than left to Config's Literal, which would raise
        # mid-run — after the upload, and on the batch path days after submit.
        if req.variant and req.variant not in VARIANT_KEYS:
            raise HTTPException(
                400, f"variant must be one of {', '.join(VARIANT_KEYS)}")
        effort = req.effort or app.state.settings.effort
        # Multi-round review is a review-only knob; prep is always a single pass.
        rounds = req.rounds if req.rounds is not None else app.state.settings.rounds
        if req.kind == "prep":
            rounds = 1
        if not 1 <= rounds <= 4:
            raise HTTPException(400, "rounds must be between 1 and 4")
        # The judge only runs with 2+ rounds, so only vet its model then: it must
        # be a real catalog model and its vendor must have a key on file.
        if req.judge_model and rounds > 1:
            jinfo = lookup(req.judge_model)
            if jinfo is None:
                raise HTTPException(400, f"Unknown judge model {req.judge_model!r}")
            if not settingslib.get_api_key(jinfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {jinfo.display} (the judge "
                         f"model). Add one in Settings first.")
        # The judge gates' models. A pick is applied to the run config whether or
        # not its gate is on, so the model itself is ALWAYS checked against the
        # catalog — an unvetted id must never reach a config. Whether a key is on
        # file only matters if the gate will actually run, and that is the
        # config's answer, not the request's: the panel sends the switch, but a
        # house config could ship a gate on with the switch untouched.
        _house = None
        for _gate, _picked in (("meaning_check", req.meaning_model),
                               ("fix_check", req.fix_model)):
            if not _picked:
                continue
            ginfo = lookup(_picked)
            if ginfo is None:
                raise HTTPException(
                    400, f"Unknown {_gate.replace('_', '-')} model {_picked!r}")
            asked = (req.features or {}).get(_gate)
            if asked is None:
                _house = _house or load_config(CONFIG_PATH)
                gate_on = getattr(_house, _gate).enabled
            else:
                gate_on = bool(asked)
            if gate_on and not settingslib.get_api_key(ginfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {ginfo.display} (the "
                         f"{_gate.replace('_', '-')} model). Add one in "
                         f"Settings first.")
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
                # Multi-round review runs the whole review once per round, so the
                # cap must count all of them (the between-round judge is on top of
                # this, so this stays a floor).
                est *= rounds
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
                variant=req.variant,
                effort=effort,
                glossary_model=req.glossary_model or app.state.settings.glossary_model,
                features=req.features or {},
                rounds=rounds,
                judge_prompt=req.judge_prompt,
                judge_model=req.judge_model or "",
                continuity_prompt=req.continuity_prompt,
                continuity_only=req.continuity_only,
                meaning_model=req.meaning_model or "",
                meaning_prompt=req.meaning_prompt,
                fix_model=req.fix_model or "",
                fix_prompt=req.fix_prompt,
                preset=req.preset,
                selection=(req.selections or {}).get(file_id) or None,
                created_at=datetime.now(timezone.utc).isoformat(),
                kind=req.kind,
                prep_output=req.prep_output,
                prep_subject=req.prep_subject.strip(),
                prep_title=req.prep_title.strip(),
                prep_author=req.prep_author.strip(),
                owner_id=owner,
            )
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.get("/api/features")
    def features() -> dict:
        """The per-run pass switches to render, each with the value this run
        would take if left untouched. Read off a config built the way a review's
        is — the shipped defaults plus the two settings-backed toggles — so the
        panel shows the real baseline, not the bare code default."""
        cfg = load_config(CONFIG_PATH)
        cfg.comments = app.state.settings.comments
        cfg.report_explanations = app.state.settings.explanations
        # Rounds is not a boolean feature (a count + an editable prompt), so it
        # rides alongside the catalog rather than in it. judge_prompt_default is
        # what the panel pre-fills its textarea placeholder with; an empty submit
        # keeps the engine's built-in default.
        from docproof.verifier import default_judge_prompt
        # Continuity likewise carries an editable prompt (its reader's system
        # prompt) and a run-alone switch, so its default rides alongside the
        # catalog the same way the round judge's does.
        from docproof.continuity import default_continuity_prompt
        # Each judge gate is a boolean in the catalog above, but its judge also
        # carries an editable prompt, so those defaults ride alongside the same
        # way the round judge's does.
        from docproof.judges import default_prompt
        return {"features": featureslib.feature_catalog(cfg),
                "rounds": {"default": app.state.settings.rounds, "max": 4,
                           "judge_prompt_default": default_judge_prompt()},
                "continuity": {"prompt_default": default_continuity_prompt()},
                "meaning": {"prompt_default": default_prompt("meaning")},
                "fix": {"prompt_default": default_prompt("fix")}}

    def _card(job: Job) -> dict:
        """A job as the results card needs it: its own fields plus whether the
        "Finish collecting" affordance applies (a failed batch whose vendor
        results are still there to re-collect, cheaply, instead of resubmitting).
        The flag is computed here because it depends on batch state on disk that
        the plain `to_api` can't see."""
        d = job.to_api()
        d["recoverable"] = app.state.runner.can_recover(job)
        # Same reason: whether a finished review still has the record and the
        # manuscript a re-judge needs is a question about files on disk.
        d["rejudgeable"] = app.state.runner.can_rejudge(job)
        return d

    @app.get("/api/jobs")
    def list_jobs(owner: str = Depends(owner_for)) -> dict:
        # Promo has its own panel, so its jobs stay out of the Results list —
        # a promo run is not a reviewed document and its card would not render
        # as one. It is still reachable by id (download, delete) from there.
        scope = owner if app.state.web else None
        return {"jobs": [_card(j) for j in app.state.store.all(scope)
                         if not j.is_promo]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, owner: str = Depends(owner_for)) -> dict:
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        return _card(job)

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
        # A retry re-runs from the manuscript, so make sure it is on disk — a
        # wipe (or a restore from the archive) may have taken it; the archive has
        # a copy when the source was kept.
        _ensure_source(job)
        store.update(job_id, state="queued", error=None, done=0)
        return runner.enqueue(store.get(job_id)).to_api()

    @app.post("/api/jobs/{job_id}/recover")
    def recover(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Finish an overnight review that failed *after* its batch completed.

        The batch is already billed and its results still sit at the vendor, so
        this re-collects them instead of running the review again — unlike
        Retry, which resubmits a fresh batch and pays twice. Only offered for a
        failed review that still has a completed batch waiting to be collected;
        an audit failure is refused here (that one has "Download anyway")."""
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state != "failed":
            raise HTTPException(400, "Only reviews that need attention can be "
                                     "recovered.")
        updated = runner.recover(job_id)
        if updated is None:
            raise HTTPException(
                400, "There is no completed overnight batch to recover for this "
                     "review — use Retry to run it again.")
        return updated.to_api()

    @app.post("/api/jobs/{job_id}/archive")
    def archive_now(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Try pushing a job's outputs to the Drive archive again, now.

        For the "Retry archive" affordance on a job whose automatic attempts
        gave up (Drive kept refusing). Resets the attempt count so the try is
        fresh, then archives inline — the same idempotent write the ticker does,
        so a partial earlier attempt is resumed, not duplicated. Best-effort:
        the card comes back with the new archive state either way."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if runner.notify_home is None:
            raise HTTPException(400, "The Drive archive is not set up.")
        from ..watch import archive as archivelib
        from ..watch.settings import WatchSettings
        if not archivelib.is_enabled(WatchSettings.load(runner.notify_home)):
            raise HTTPException(
                400, "The Drive archive is switched off. Turn it on under "
                     "DocWatch first.")
        store.update(job_id, archive="", archive_attempts=0, archive_error="")
        runner.archive_job(job_id)
        return _card(store.get(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Abort a job. A queued or scheduled one is pulled back before it
        spends anything; a running one is told to stop, and the worker halts it
        between calls — cancelling every call not yet started, so the abort
        stops the spend, not just the folding.

        An overnight batch already at the vendor is the one thing that can't be
        recalled: it will bill whether or not we collect it, so it is refused
        rather than pretending to stop. A job in its brief file-writing
        (collecting) moment is past the point of stopping too."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")

        if job.state == "running":
            # The worker owns the state now; signal it and let it flip the job
            # to cancelled when it next comes up for air. The record still reads
            # "running" for the moment, which is honest — it is, until it stops.
            runner.request_cancel(job_id)
            return store.get(job_id).to_api()

        if job.state not in ("queued", "scheduled"):
            raise HTTPException(
                400, "This one is already past the point of stopping — an "
                     "overnight batch can't be recalled, and a finished job "
                     "has nothing left to cancel.")
        updated = store.update_if(job_id, expect=job.state, state="cancelled",
                                  error=None)
        if updated is None:
            # It moved on in the instant between the check above and this one.
            # If it started running, catch it there instead of failing the call.
            if (moved := store.get(job_id)) is not None and moved.state == "running":
                runner.request_cancel(job_id)
                return moved.to_api()
            raise HTTPException(
                400, "This one just moved on, so there is nothing left to "
                     "cancel.")
        # A cancelled job never runs again, so any checkpoint a failed earlier
        # attempt left behind is just clutter in the job folder now.
        runner.discard_checkpoint(job_id)
        return updated.to_api()

    @app.post("/api/jobs/clear")
    def clear_jobs(owner: str = Depends(owner_for)) -> dict:
        """Clear every finished job from the results list at once — the ones
        that are done, failed, or were cancelled. Active work is left alone.
        Removes each job's produced documents along with its record."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        scope = owner if app.state.web else None
        removed = [job.id for job in store.all(scope)
                   if job.state in _TERMINAL and runner.delete_job(job.id)]
        return {"removed": removed}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Remove one finished job from the results list, produced documents
        and all. Only a job that has stopped — done, failed, or cancelled — can
        be removed; abort a running one first."""
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state not in _TERMINAL:
            raise HTTPException(
                400, "This one is still active. Abort it first, then remove it.")
        runner.delete_job(job_id)
        return {"ok": True, "id": job_id}

    @app.post("/api/jobs/{job_id}/rejudge")
    def rejudge(job_id: str, req: RejudgeRequest,
                owner: str = Depends(owner_for)) -> dict:
        """Run the judge gates over a review that already ran.

        No detector call is made — the corrections come off the record the
        original run left — so a manuscript proofread before these gates existed
        can be gated now for the price of the gates alone. The result lands
        beside the original as its own review; the first is never overwritten."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review.")
        if not app.state.runner.can_rejudge(job):
            raise HTTPException(
                409, "This review can't be re-judged — it needs to have "
                     "finished, kept its findings record, and still have the "
                     "manuscript it read.")
        gates = {"meaning_check": req.meaning_check,
                 "fix_check": req.fix_check}
        if not any(gates.values()):
            raise HTTPException(
                400, "Switch on the meaning check, the fix check, or both — a "
                     "re-judge runs the gates and nothing else.")
        models = {}
        for gate, picked in (("meaning_check", req.meaning_model),
                             ("fix_check", req.fix_model)):
            if not (picked and gates[gate]):
                continue
            info = lookup(picked)
            if info is None:
                raise HTTPException(400, f"Unknown model {picked!r}")
            if not settingslib.get_api_key(info.provider):
                raise HTTPException(
                    400, f"No API key saved for {info.display}. Add one in "
                         f"Settings first.")
            models[gate] = picked
        _ensure_source(job)
        updated = app.state.runner.rejudge(
            job_id, gates=gates, models=models,
            prompts={"meaning_check": req.meaning_prompt,
                     "fix_check": req.fix_prompt})
        if updated is None:
            raise HTTPException(409, "This review can't be re-judged.")
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
        # The replay re-ingests the manuscript, so fetch it back from the archive
        # if a wipe took it (the checkpoint the replay also needs is another
        # matter — one that survived only a results wipe, not a whole-volume loss,
        # still has its checkpoint in the job folder).
        _ensure_source(job)
        updated = app.state.runner.download_anyway(job_id)
        if updated is None:
            raise HTTPException(409, "This review can't be written out.")
        return updated.to_api()

    @app.get("/api/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str, owner: str = Depends(owner_for)):
        """Serve a result over HTTP — the browser build's way of handing a file
        over, and the fallback when the desktop window cannot open one.

        Not gated on a local results folder any more: a job whose folder was
        wiped by a redeploy, or one restored from the archive with no local
        results at all, still serves — `_resolve_result` fetches the file back
        from Drive and re-caches it."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this review yet")
        name = _result_name(job, which)
        if name is None:
            raise HTTPException(404, "Unknown file")
        path = _resolve_result(job, name)
        if path is None:
            raise HTTPException(404, f"{name} is missing")
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
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        path = _resolve_result(job, "prep.json")
        if path is None:
            raise HTTPException(404, "This job has no prep notes")
        data = json.loads(path.read_text("utf-8"))
        # Which deliverables exist, counting the archive so a restored job's
        # buttons still light up (their first click fetches the file back).
        def _present(name: str) -> bool:
            if job.results_dir and (Path(job.results_dir) / name).is_file():
                return True
            return name in job.drive_files
        data["files"] = {kind: _present(name)
                         for kind, name in
                         (("book", f"book_{Path(job.filename).stem}.docx"),
                          ("indesign", f"tagged_{Path(job.filename).stem}.docx"),
                          ("tracked", f"tracked_{Path(job.filename).stem}.docx"))}
        return data

    @app.get("/api/usage")
    def usage(owner: str = Depends(owner_for)) -> dict:
        """Tokens, calls and estimated spend, from every source on the one card.

        Both job stores count — the app's and the watcher's — and within each,
        live jobs and the ledger snapshots of cleared ones alike, because a
        cleared job's cost still came off the card. On the desktop that is the
        whole machine. On the web it is scoped: a regular user sees their own app
        jobs, and an administrator sees the full bill — every user's jobs and the
        watcher's — because the watcher belongs to the organisation, not to any
        one user."""
        web = app.state.web
        user = app.state.accounts.get_user(owner) if web else None
        if web and not (user and user.is_admin):
            return build_usage(common.store_spend(app.state.store, owner),
                               read_usage)
        rows = (common.store_spend(app.state.store)
                + common.watch_spend(app.state.watch))
        return build_usage(rows, read_usage)

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """The findings, read back as prose rather than as a record."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this review yet")
        path = _resolve_result(job, "findings.json")
        if path is None:
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
        # Built with `_card`, exactly as /api/jobs is: the panel renders from
        # whichever of the two answered last, so a flag on one and not the other
        # makes a button appear and vanish depending on how the screen was
        # reached. This is the endpoint the Results screen opens on.
        return {"jobs": [_card(j) for j in app.state.store.all(scope)
                         if not j.is_promo]}
