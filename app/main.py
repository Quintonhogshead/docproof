"""Local HTTP surface for the DocProof app.

Binds to localhost only. Every route is thin: it validates input, touches the
job store, and returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docproof import batch as batchlib
from docproof.config import load_config
from docproof.ingest import IngestError
from docproof.pipeline import prepare
from docproof.providers import MODELS, estimate_cost, lookup

from .jobs import Job, JobRunner, JobStore
from .settings import (Paths, Settings, default_root, delete_api_key,
                       get_api_key, key_status, set_api_key)

log = logging.getLogger("docproof.app")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
# Output tokens can't be known in advance; this is a per-request allowance used
# only to turn the estimate into a number with the right order of magnitude.
OUTPUT_TOKEN_GUESS = 600


class JobRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    model: str
    mode: str = "batch"                       # "now" | "batch"
    schedule_at: str | None = None            # "HH:MM" local
    min_confidence: str = "medium"


class SettingsUpdate(BaseModel):
    model: str | None = None
    min_confidence: str | None = None
    output_dir: str | None = None
    default_mode: str | None = None
    comments: bool | None = None
    anthropic_key: str | None = None
    openai_key: str | None = None


def create_app(root: Path | None = None, *, start_runner: bool = True,
               poll_seconds: int = 120) -> FastAPI:
    paths = Paths(root or default_root()).ensure()
    settings = Settings.load(paths)
    store = JobStore(paths)
    runner = JobRunner(store, settings, config_path=CONFIG_PATH,
                       poll_seconds=poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_runner:
            runner.start()
        yield
        runner.stop()

    app = FastAPI(title="DocProof", lifespan=lifespan)
    app.state.paths = paths
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner

    _register(app)
    static = Path(__file__).resolve().parent / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


def _resolve_upload(paths: Paths, file_id: str) -> Path | None:
    """Map a staged-file id onto a path, refusing anything that escapes the
    uploads directory. The id comes from the browser, so it is untrusted."""
    candidate = (paths.uploads / file_id).resolve()
    uploads = paths.uploads.resolve()
    if not candidate.is_relative_to(uploads) or not candidate.is_file():
        return None
    return candidate


def _register(app: FastAPI) -> None:

    # -- files ----------------------------------------------------------------

    @app.post("/api/files")
    async def upload(files: list[UploadFile]) -> dict:
        """Stage documents and preflight them immediately, so a file docproof
        refuses to touch says so at drop time rather than at 11pm."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        error_dir = CONFIG_PATH.parent / "error_types"
        staged = []
        for upload_file in files:
            name = Path(upload_file.filename or "document.docx").name
            if not name.lower().endswith(".docx"):
                staged.append({"filename": name, "ok": False,
                               "error": "Only Word .docx files can be reviewed."})
                continue
            # Each upload gets its own folder so the document keeps its real
            # name — that name ends up on the reviewed file the user opens.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            folder = paths.uploads / stamp
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / name
            with dest.open("wb") as fh:
                shutil.copyfileobj(upload_file.file, fh)

            try:
                prepared = prepare(cfg, dest, error_dir)
            except (IngestError, ValueError) as e:
                shutil.rmtree(folder, ignore_errors=True)
                staged.append({"filename": name, "ok": False, "error": str(e)})
                continue
            staged.append({
                "id": f"{stamp}/{name}", "filename": name, "ok": True,
                "sections": len(prepared.chunks),
                "paragraphs": len(prepared.doc.paragraphs),
                "requests": prepared.request_count,
                "input_tokens": prepared.est_document_tokens,
            })
        return {"files": staged}

    # -- models ---------------------------------------------------------------

    @app.get("/api/models")
    def models(file_ids: str = "") -> dict:
        """The catalog, plus what these specific files would cost on each."""
        paths: Paths = app.state.paths
        keys = key_status()
        ids = [f for f in file_ids.split(",") if f]
        totals = _token_totals(paths, ids)

        out = []
        for m in MODELS:
            entry = {"id": m.id, "provider": m.provider, "display": m.display,
                     "blurb": m.blurb,
                     "available": keys.get(m.provider, {}).get("configured",
                                                               False)}
            if totals:
                inp, req = totals
                entry["cost_now"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * OUTPUT_TOKEN_GUESS)
                entry["cost_batch"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * OUTPUT_TOKEN_GUESS, batch=True)
            out.append(entry)
        return {"models": out, "keys": keys}

    def _token_totals(paths: Paths, ids: list[str]) -> tuple[int, int] | None:
        if not ids:
            return None
        cfg = load_config(CONFIG_PATH)
        error_dir = CONFIG_PATH.parent / "error_types"
        tokens = requests = 0
        for file_id in ids:
            path = _resolve_upload(paths, file_id)
            if path is None:
                continue
            try:
                prepared = prepare(cfg, path, error_dir)
            except (IngestError, ValueError):
                continue
            tokens += prepared.est_document_tokens
            requests += prepared.request_count
        return (tokens, requests) if requests else None

    # -- jobs -----------------------------------------------------------------

    @app.post("/api/jobs")
    def create_jobs(req: JobRequest) -> dict:
        paths: Paths = app.state.paths
        runner: JobRunner = app.state.runner
        if req.mode not in ("now", "batch"):
            raise HTTPException(400, "mode must be 'now' or 'batch'")
        info = lookup(req.model)
        if info is None:
            raise HTTPException(400, f"Unknown model {req.model!r}")
        if not get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")

        group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
        created = []
        for file_id in req.file_ids:
            source = _resolve_upload(paths, file_id)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            job = Job(
                id=batchlib.new_job_id(source.name),
                filename=source.name,
                source_path=str(source),
                model=req.model,
                mode=req.mode,
                group_id=group_id,
                schedule_at=req.schedule_at if req.mode == "batch" else None,
                min_confidence=req.min_confidence,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [j.to_api() for j in app.state.store.all()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = app.state.store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such review")
        return job.to_api()

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str) -> dict:
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state != "failed":
            raise HTTPException(400, "Only reviews that need attention can be "
                                     "retried.")
        store.update(job_id, state="queued", error=None, done=0)
        return runner.enqueue(store.get(job_id)).to_api()

    @app.get("/api/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str):
        job = app.state.store.get(job_id)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        names = {
            "docx": f"reviewed_{Path(job.filename).stem}.docx",
            "summary": "summary.md",
            "findings": "findings.json",
        }
        if which not in names:
            raise HTTPException(404, "Unknown file")
        path = Path(job.results_dir) / names[which]
        if not path.is_file():
            raise HTTPException(404, f"{names[which]} is missing")
        return FileResponse(path, filename=path.name)

    @app.post("/api/tick")
    def tick() -> dict:
        """Advance scheduled and in-flight work now instead of waiting for the
        next timer. The Jobs screen calls this when the user opens it."""
        app.state.runner.tick_once()
        return {"jobs": [j.to_api() for j in app.state.store.all()]}

    # -- settings -------------------------------------------------------------

    @app.get("/api/settings")
    def read_settings() -> dict:
        s: Settings = app.state.settings
        return {"settings": s.__dict__, "keys": key_status()}

    @app.put("/api/settings")
    def write_settings(update: SettingsUpdate) -> dict:
        s: Settings = app.state.settings
        for field_name in ("model", "min_confidence", "output_dir",
                           "default_mode", "comments"):
            value = getattr(update, field_name)
            if value is not None:
                setattr(s, field_name, value)
        for provider, value in (("anthropic", update.anthropic_key),
                                ("openai", update.openai_key)):
            if value is None:
                continue
            if value.strip():
                set_api_key(provider, value.strip())
            else:
                delete_api_key(provider)
        s.save(app.state.paths)
        return {"settings": s.__dict__, "keys": key_status()}

    @app.post("/api/settings/test/{provider}")
    def test_key(provider: str) -> dict:
        """One cheap real call, so a bad key fails here and not at 11pm."""
        if provider not in ("anthropic", "openai"):
            raise HTTPException(404, "Unknown provider")
        key = get_api_key(provider)
        if not key:
            return {"ok": False, "message": "No key saved yet."}
        model = next((m.id for m in MODELS if m.provider == provider), None)
        cfg = load_config(CONFIG_PATH)
        cfg.api.model = model
        try:
            from docproof.providers import build_provider
            p = build_provider(cfg, api_key=key)
            result = p.complete_structured(
                model=model, system="Reply with the JSON object {\"ok\": true}.",
                user="ping",
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"], "additionalProperties": False},
                schema_name="ping", max_tokens=64)
        except Exception as e:                # noqa: BLE001 - surface verbatim
            return {"ok": False, "message": str(e)}
        if result.stop_reason != "ok":
            return {"ok": False, "message": result.error or result.stop_reason}
        return {"ok": True, "message": "Key works."}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}


app = create_app()
