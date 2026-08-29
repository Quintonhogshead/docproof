"""Cover Studio's HTTP API: create a job, poll it, revise a concept, download
a file. Mirrors app/routes/quest.py's shape closely — register(app),
_provider() built fresh per call from settings, upload validation in the same
register as quest's _read_upload — with one addition quest.py doesn't need:
every endpoint here is gated behind a shared key (see _gate), because unlike
the skin call (a fraction of a cent), an image generation costs real money.

Rate limiting is deliberately NOT here: app/quest_site.py's guard_cover
middleware owns it, the same way guard_skins owns quest's, keyed on request
path rather than threaded through this module.

The job store and the actual work (the direction call, painting, composing,
revising) live in docproof.cover.pipeline — this module's job is HTTP shape
only: validate the request, build a Provider and an image client, hand both
to a pipeline function as a background task, and shape the response.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from docproof.config import load_config
from docproof.cover import pipeline as cover_pipeline
from docproof.cover.model import Brief
from docproof.ingest import IngestError
from docproof.providers import build_provider, lookup
from docproof.providers.base import ProviderError

from ..settings import CONFIG_PATH, get_api_key

# Mirrored rather than imported from docproof.quest, on purpose — the same
# call docproof.cover.direction's own LUNA_MODEL makes about docproof.quest.
# skin.LUNA_MODEL: Cover Studio shares a convention with Quest (the cheap
# model's id), not a dependency on Quest's module. Guarded because direction.
# py is a sibling module built alongside this one and may not exist yet at
# import time (see docproof.cover.pipeline's module docstring for the same
# concern) — a test that never calls _provider() must still be able to import
# this module.
try:
    from docproof.cover.direction import LUNA_MODEL
except ImportError:                                        # pragma: no cover
    LUNA_MODEL = "gpt-5.6-luna"

try:
    from docproof.cover.imaging import make_client
except ImportError:                                        # pragma: no cover
    make_client = None

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".txt", ".md"}


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    """Validate an uploaded manuscript exactly like quest's own gate
    (app/routes/quest.py's _read_upload) — same suffixes, same cap, the same
    register of human-sentence errors. Raises HTTPException; the caller
    decides what to build from the bytes."""
    name = file.filename or "manuscript"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, detail=(
            f"Cover Studio reads .docx, .txt, or .md files; {name} is a "
            f"{suffix or 'file with no extension'}."))
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail=(
            f"{name} is over the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB "
            f"upload limit."))
    if not data:
        raise HTTPException(400, detail=f"{name} is empty.")
    return name, data


def _gate(request: Request) -> None:
    """Every /api/cover/* endpoint's first move (spec §9): inert (503) on a
    deployment that hasn't set COVER_KEY, and answers only a caller holding
    it (401 otherwise). hmac.compare_digest so a wrong guess can't be timed
    against the real key."""
    cover_key = os.environ.get("COVER_KEY")
    if not cover_key:
        raise HTTPException(503, detail=(
            "Cover Studio is not enabled on this deployment."))
    supplied = request.headers.get("X-Cover-Key", "")
    if not hmac.compare_digest(supplied, cover_key):
        raise HTTPException(401, detail=(
            "That key doesn't match — check X-Cover-Key and try again."))


# Exactly what new_job_id() mints — anything else 404s before it can reach
# the filesystem. The sibling `name` parameter gets the same treatment in
# cover_get_file; a job_id of ".." would otherwise resolve job_dir() to the
# store's parent.
_JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9a-f]{6}$")


def _checked_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
    return job_id


def _data_root(request: Request) -> Path:
    """The job store's root, resolved once at app creation (quest_site.py
    reads COVER_DATA_PATH) and stashed on app.state — the same place this
    site already keeps its rate limiter and waitlist."""
    return request.app.state.cover_data_root


def _provider():
    """The cheap direction/revision model, built fresh per call — mirrors
    quest.py's own _provider() exactly (Luna, effort low)."""
    cfg = load_config(CONFIG_PATH)
    cfg.api.model = LUNA_MODEL
    cfg.api.effort = "low"
    try:
        return build_provider(cfg, api_key=get_api_key(lookup(LUNA_MODEL).provider))
    except ProviderError as e:
        raise HTTPException(503, detail=(
            f"The direction model is not available: {e}")) from e


def _image_client():
    """The gpt-image-2 client, keyed the same way the skin call is (§7.2):
    app.settings.get_api_key("openai")."""
    if make_client is None:
        raise HTTPException(503, detail=(
            "Image generation is not available on this deployment yet."))
    key = get_api_key("openai")
    if not key:
        raise HTTPException(503, detail=(
            "Image generation is not configured on this deployment (no "
            "OpenAI key)."))
    return make_client(key)


class ReviseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept: int
    notes: str = ""
    allow_new_art: bool = False


def register(app: FastAPI) -> None:

    @app.post("/api/cover/jobs", status_code=202)
    async def cover_create_job(request: Request, brief: str = Form(...),
                               manuscript: UploadFile | None = File(None)
                               ) -> dict:
        """Create a job: a Brief (JSON, in the `brief` form field) plus an
        optional manuscript for grounding. Spawns run_job in the background
        and returns immediately (§9)."""
        _gate(request)
        try:
            brief_obj = Brief.model_validate_json(brief)
        except ValidationError as e:
            raise HTTPException(400, detail=(
                f"That brief didn't validate: {e}")) from e

        # Built before anything touches disk: a missing model/key is a 503
        # regardless of whether this particular job would ever need images,
        # the same all-or-nothing stance quest.py's _provider() takes.
        provider = _provider()
        image_client = _image_client()
        root = _data_root(request)

        manuscript_name = ""
        with tempfile.TemporaryDirectory(prefix="cover-job-") as tmp:
            manuscript_path = None
            if manuscript is not None:
                manuscript_name, data = await _read_upload(manuscript)
                manuscript_path = Path(tmp) / Path(manuscript_name).name
                manuscript_path.write_bytes(data)
            try:
                job = cover_pipeline.create_job(
                    root, brief_obj, manuscript_path=manuscript_path,
                    manuscript_name=manuscript_name)
            except IngestError as e:
                raise HTTPException(400, detail=str(e)) from e

        task = asyncio.create_task(
            cover_pipeline.run_job(root, job.job_id, provider, image_client))
        cover_pipeline.register_task(job.job_id, task)
        return {"job_id": job.job_id}

    @app.get("/api/cover/jobs/{job_id}")
    async def cover_get_job(job_id: str, request: Request) -> JSONResponse:
        """Poll target (§9): the whole JobState plus total_usd. Checks for a
        job orphaned by a restart before answering, so a stuck poll turns
        into a plain, actionable error instead of hanging forever."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
        job = cover_pipeline.check_interrupted(root, job)
        payload = job.model_dump(mode="json")
        payload["total_usd"] = cover_pipeline.total_usd(job)
        return JSONResponse(payload, headers={"Cache-Control": "no-cache"})

    @app.post("/api/cover/jobs/{job_id}/revise", status_code=202)
    async def cover_revise(job_id: str, request: Request, body: ReviseBody
                           ) -> dict:
        """Revise one concept: notes, and whether to allow new art. 409
        unless that concept is ready/error — a concept mid-paint has nothing
        stable to revise yet (§9)."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
        if not (0 <= body.concept < len(job.concepts)):
            raise HTTPException(404, detail=(
                f"Concept {body.concept} does not exist on this job."))
        if job.concepts[body.concept].status not in ("ready", "error"):
            raise HTTPException(409, detail=(
                "This concept is still being worked on — wait for it to "
                "finish before revising it."))

        provider = _provider()
        image_client = _image_client()
        task = asyncio.create_task(cover_pipeline.run_revision(
            root, job_id, body.concept, body.notes, body.allow_new_art,
            provider, image_client))
        cover_pipeline.register_task(job_id, task)
        return {"job_id": job_id, "concept": body.concept}

    @app.get("/api/cover/jobs/{job_id}/file/{name}")
    async def cover_get_file(job_id: str, name: str, request: Request,
                             concept: int | None = None) -> Response:
        """Serve one render (renders/<name>), or — with ?concept=n — that
        concept's spec as a JSON download. Refuses any name that could climb
        out of the job directory (§9)."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")

        if concept is not None:
            if not (0 <= concept < len(job.concepts)):
                raise HTTPException(404, detail=(
                    f"Concept {concept} does not exist on this job."))
            payload = job.concepts[concept].spec.model_dump_json(indent=2)
            return Response(
                payload, media_type="application/json",
                headers={"Content-Disposition": (
                    f'attachment; filename="{job_id}_c{concept}_spec.json"')})

        if "/" in name or ".." in name:
            raise HTTPException(400, detail="That file name isn't a real render.")
        renders_root = (cover_pipeline.job_dir(root, job_id)
                        / cover_pipeline.RENDERS_DIR).resolve()
        target = (renders_root / name).resolve()
        if target != renders_root and renders_root not in target.parents:
            raise HTTPException(400, detail="That file name isn't a real render.")
        if not target.is_file():
            raise HTTPException(404, detail=f"No file named {name!r} on this job.")
        media_type = "image/jpeg" if target.suffix == ".jpg" else "image/png"
        return FileResponse(target, media_type=media_type,
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/cover/jobs")
    async def cover_list_jobs(request: Request) -> dict:
        """The last 20 jobs — the "pick up where I left off" list (§9)."""
        _gate(request)
        root = _data_root(request)
        jobs = cover_pipeline.list_jobs(root, limit=20)
        return {"jobs": [{"job_id": j.job_id, "title": j.brief.title,
                          "status": j.status, "created": j.created,
                          "total_usd": cover_pipeline.total_usd(j)}
                         for j in jobs]}
