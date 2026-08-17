"""The promo panel's HTTP surface.

Two jobs. The first is manual promo runs and the copy they produce: drop a
finished manuscript, generate a teaser and a set of social posts, read them
back, edit them, and download the two documents. These are ordinary promo jobs
in the app's own store, owned by whoever made them, the same as a review.

The second is the promo settings — the values the watched-folder pipeline reads
to gate on HubSpot and decide whether to ship copy or hold it for approval.
Those live in the watcher's own settings file and, on the web build, are an
administrator's to change, the same as the rest of the watch configuration.

Approving a book the *watcher* generated and is holding is not here yet: that
crosses into the watcher's own job store and its Drive/HubSpot context, and is a
follow-up. A hold-mode book is delivered by the next pass once its job is
approved (see `tick._deliver_approved_promo`); until the panel can do that, auto
mode ships without a gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from docproof import batch as batchlib
from docproof.promo import MarketingPlan, PromoResult
from docproof.promo.writers import write_plan, write_posts, write_teaser
from docproof.providers import lookup

from . import common
from .. import settings as settingslib
from ..auth import owner_for
from ..jobs import Job, JobRunner, JobStore
from ..watch.settings import WatchSettings


class PromoRunRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    model: str
    # The reasoning dial, same levels the review and prep pipelines use. Promo
    # honours it too — it scales what the model writes back — so the panel's
    # slider is not decorative. Defaults to the shipped "low".
    effort: str = "low"
    # The human override for a book over the single-pass token limit: set by a
    # person who saw the size at drop time and chose to run it anyway.
    allow_oversize: bool = False


class PlanRunRequest(BaseModel):
    """A manual marketing-plan run. Same manuscript + model + effort as a copy
    run, plus the operator-typed metadata the plan prompt takes that the copy
    call does not. Every metadata field is optional — the prompt degrades to a
    thinner but valid plan — so a bare manuscript still produces one."""
    file_ids: list[str] = Field(min_length=1)
    model: str
    effort: str = "low"
    allow_oversize: bool = False
    author: str = ""                   # display / pen name, printed on the plan
    blurbs: str = ""                   # back-cover synopsis + endorsement blurbs
    city: str = ""                     # for the local-opportunities section
    keywords: str = ""                 # the press's positioning keywords
    questionnaire: str = ""            # the author's publicity-questionnaire answers


class PromoDraft(BaseModel):
    """The editable copy: exactly the shape of promo.json, so a saved draft is a
    validated `PromoResult`, never free-form text the writers then choke on."""
    teaser: str
    posts: list[dict]


class PlanDraft(BaseModel):
    """The editable plan: exactly the shape of marketing_plan.json, one Markdown
    string, so a saved draft is a validated `MarketingPlan` the writer re-renders
    to the .docx."""
    plan: str


class PromoSettings(BaseModel):
    promo_enabled: bool | None = None
    hubspot_promo_ready_value: str | None = None
    hubspot_promo_done_value: str | None = None
    promo_auto_upload: bool | None = None
    promo_model: str | None = None


def _draft_name(job: Job, which: str) -> str | None:
    stem = Path(job.filename).stem or "manuscript"
    return {"teaser": f"{stem} - teaser.docx",
            "posts": f"{stem} - social posts.docx",
            "plan": f"{stem} - marketing plan.docx"}.get(which)


def _rewrite_documents(job: Job, result: PromoResult) -> None:
    """Re-make the two .docx from the copy in hand — after an edit, so what a
    person downloads is what they approved, not what the model first wrote."""
    out = Path(job.results_dir)
    stem = Path(job.filename).stem or "manuscript"
    write_teaser(out / f"{stem} - teaser.docx", result.teaser, title=stem)
    write_posts(out / f"{stem} - social posts.docx", result.posts, title=stem)


def _rewrite_plan(job: Job, plan: MarketingPlan) -> None:
    """Re-render the plan .docx from the edited Markdown — the plan analog of
    `_rewrite_documents`."""
    out = Path(job.results_dir)
    stem = Path(job.filename).stem or "manuscript"
    write_plan(out / f"{stem} - marketing plan.docx", plan.plan, title=stem)


def register(app: FastAPI) -> None:

    settings_gate = common.admin_gate(
        app, "Only an administrator can change the promo settings.")

    def _promo_job(job_id: str, owner: str) -> Job | None:
        """A promo job the caller may see, or None — a 404 that reads the same
        whether the job is missing, not a promo job, or someone else's."""
        job = app.state.store.get(job_id)
        if job is None or not job.is_promo:
            return None
        if app.state.web and job.owner_id != owner:
            return None
        return job

    def _load_result(job: Job) -> PromoResult:
        path = Path(job.results_dir or "") / "promo.json"
        if not path.is_file():
            raise HTTPException(404, "This run has no promo copy yet.")
        return PromoResult.model_validate(json.loads(path.read_text("utf-8")))

    # --- manual runs ----------------------------------------------------------

    @app.post("/api/promo/run")
    def run_promo(req: PromoRunRequest,
                  owner: str = Depends(owner_for)) -> dict:
        """Generate promo copy for one or more dropped manuscripts."""
        paths = app.state.paths
        runner: JobRunner = app.state.runner
        info = lookup(req.model)
        if info is None:
            raise HTTPException(400, f"Unknown model {req.model!r}")
        if not settingslib.get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")

        # Resolve every id before enqueuing any, so a bad id fails the whole
        # request rather than starting some of it.
        sources = {}
        for file_id in req.file_ids:
            source = common.resolve_upload(paths, file_id, owner)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            sources[file_id] = source

        if app.state.web:
            cap = common.cap_for(app.state.accounts.get_user(owner))
            if cap is not None and common.month_spend(app.state.store, owner) \
                    >= cap:
                raise HTTPException(
                    402, f"You have reached your ${cap:.2f} monthly limit. Ask "
                         f"an administrator to raise it.")

        group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
        created = []
        for file_id in req.file_ids:
            source = sources[file_id]
            job = Job(
                id=batchlib.new_job_id(source.name),
                filename=source.name, source_path=str(source),
                model=req.model, mode="now", kind="promo", group_id=group_id,
                effort=req.effort, allow_oversize=req.allow_oversize,
                created_at=datetime.now(timezone.utc).isoformat(),
                owner_id=owner)
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.post("/api/promo/plan")
    def run_plan(req: PlanRunRequest, owner: str = Depends(owner_for)) -> dict:
        """Generate a marketing plan for one or more dropped manuscripts.

        The manual path of promo's third deliverable: the same drop-a-manuscript
        flow as the copy run, plus the operator-typed metadata (pen name,
        blurbs, city, keywords) the plan prompt takes. It reuses the promo job
        store, runner, and cost cap — a plan job is a promo job with
        `plan_only` set — so nothing here duplicates the copy run's plumbing."""
        paths = app.state.paths
        runner: JobRunner = app.state.runner
        info = lookup(req.model)
        if info is None:
            raise HTTPException(400, f"Unknown model {req.model!r}")
        if not settingslib.get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")

        sources = {}
        for file_id in req.file_ids:
            source = common.resolve_upload(paths, file_id, owner)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            sources[file_id] = source

        if app.state.web:
            cap = common.cap_for(app.state.accounts.get_user(owner))
            if cap is not None and common.month_spend(app.state.store, owner) \
                    >= cap:
                raise HTTPException(
                    402, f"You have reached your ${cap:.2f} monthly limit. Ask "
                         f"an administrator to raise it.")

        group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
        created = []
        for file_id in req.file_ids:
            source = sources[file_id]
            job = Job(
                id=batchlib.new_job_id(source.name),
                filename=source.name, source_path=str(source),
                model=req.model, mode="now", kind="promo", group_id=group_id,
                effort=req.effort, allow_oversize=req.allow_oversize,
                plan_only=True, plan_author=req.author.strip(),
                plan_blurbs=req.blurbs.strip(), plan_city=req.city.strip(),
                plan_keywords=req.keywords.strip(),
                plan_questionnaire=req.questionnaire.strip(),
                created_at=datetime.now(timezone.utc).isoformat(),
                owner_id=owner)
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.get("/api/promo/jobs")
    def list_promo_jobs(owner: str = Depends(owner_for)) -> dict:
        scope = owner if app.state.web else None
        jobs = [j.to_api() for j in app.state.store.all(scope) if j.is_promo]
        return {"jobs": jobs}

    @app.get("/api/promo/jobs/{job_id}/draft")
    def read_draft(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """The editable copy behind a finished run, plus what the grounding
        checks flagged — the terms not found in the book, and any claims the
        entailment pass judged unsupported — so the editor can point at them."""
        job = _promo_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such promo run.")
        result = _load_result(job)
        terms, claims = [], []
        verify_path = Path(job.results_dir or "") / "promo_verify.json"
        if verify_path.is_file():
            data = json.loads(verify_path.read_text("utf-8"))
            terms = data.get("terms") or []
            claims = [c for c in (data.get("claims") or [])
                      if not c.get("supported", True)]
        return {"teaser": result.teaser,
                "posts": [p.model_dump() for p in result.posts],
                "unverified": job.unverified or 0,
                "flagged_terms": terms, "unsupported_claims": claims}

    @app.put("/api/promo/jobs/{job_id}/draft")
    def save_draft(job_id: str, draft: PromoDraft,
                   owner: str = Depends(owner_for)) -> dict:
        """Save edited copy and re-make the two documents from it."""
        job = _promo_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such promo run.")
        if not (job.results_dir and Path(job.results_dir).is_dir()):
            raise HTTPException(409, "This run has nothing to edit yet.")
        try:
            result = PromoResult.model_validate(draft.model_dump())
        except ValidationError as e:
            raise HTTPException(400, f"That is not valid promo copy: {e}") from e
        Path(job.results_dir, "promo.json").write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8")
        _rewrite_documents(job, result)
        return {"ok": True}

    @app.get("/api/promo/jobs/{job_id}/plan")
    def read_plan(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """The editable marketing plan behind a finished plan run — the Markdown
        the panel shows in a textarea and re-renders from on save."""
        job = _promo_job(job_id, owner)
        if job is None or not job.is_plan:
            raise HTTPException(404, "No such marketing-plan run.")
        path = Path(job.results_dir or "") / "marketing_plan.json"
        if not path.is_file():
            raise HTTPException(404, "This run has no plan yet.")
        plan = MarketingPlan.model_validate(json.loads(path.read_text("utf-8")))
        return {"plan": plan.plan}

    @app.put("/api/promo/jobs/{job_id}/plan")
    def save_plan(job_id: str, draft: PlanDraft,
                  owner: str = Depends(owner_for)) -> dict:
        """Save an edited plan and re-render its document from it."""
        job = _promo_job(job_id, owner)
        if job is None or not job.is_plan:
            raise HTTPException(404, "No such marketing-plan run.")
        if not (job.results_dir and Path(job.results_dir).is_dir()):
            raise HTTPException(409, "This run has nothing to edit yet.")
        try:
            plan = MarketingPlan.model_validate(draft.model_dump())
        except ValidationError as e:
            raise HTTPException(400, f"That is not a valid plan: {e}") from e
        Path(job.results_dir, "marketing_plan.json").write_text(
            json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8")
        _rewrite_plan(job, plan)
        return {"ok": True}

    @app.get("/api/promo/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str, owner: str = Depends(owner_for)):
        """Serve the teaser or the posts document."""
        job = _promo_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this run yet.")
        name = _draft_name(job, which)
        if name is None:
            raise HTTPException(404, "Unknown file.")
        path = Path(job.results_dir) / name
        if not path.is_file():
            raise HTTPException(404, f"{name} is missing.")
        return FileResponse(path, filename=path.name)

    # --- watch pipeline settings ----------------------------------------------

    @app.get("/api/promo/settings")
    def read_settings() -> dict:
        ws = WatchSettings.load(app.state.watch.home)
        return _settings_payload(ws)

    @app.put("/api/promo/settings", dependencies=[Depends(settings_gate)])
    def write_settings(update: PromoSettings) -> dict:
        ws = WatchSettings.load(app.state.watch.home)
        if update.promo_model is not None and update.promo_model:
            if lookup(update.promo_model) is None:
                raise HTTPException(400, f"{update.promo_model} is not a model "
                                         f"DocProof knows.")
        for name in ("promo_enabled", "hubspot_promo_ready_value",
                     "hubspot_promo_done_value", "promo_auto_upload",
                     "promo_model"):
            value = getattr(update, name)
            if value is not None:
                setattr(ws, name, value)
        ws.save(app.state.watch.home)
        return _settings_payload(ws)

    def _settings_payload(ws: WatchSettings) -> dict:
        return {"promo_enabled": ws.promo_enabled,
                "hubspot_promo_ready_value": ws.hubspot_promo_ready_value,
                "hubspot_promo_done_value": ws.hubspot_promo_done_value,
                "promo_auto_upload": ws.promo_auto_upload,
                "promo_model": ws.promo_model,
                # Read-only context, so the form can say what promo inherits
                # when its own model is blank and that it needs HubSpot on.
                "fallback_model": ws.model,
                "hubspot_enabled": ws.hubspot_enabled}
