"""Local HTTP surface for the DocProof app.

Binds to localhost only. Every route is thin: it validates input, touches the
job store, and returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docproof import batch as batchlib
from docproof import prep as preplib
from docproof.config import load_config
from docproof.formats import SUFFIXES, describe, get_format
from docproof.ingest import IngestError
from docproof.pipeline import chunk_outline, prepare
from docproof.prep import convert as prep_convert
from docproof.prep.place import PlaceError, find_indesign, place_into_template
from docproof.prep.styles import StyleSheetError
from docproof.providers import MODELS, estimate_cost, lookup

from .jobs import Job, JobRunner, JobStore, read_usage
from .lock import FolderInUse, FolderLock
from .usage import build_usage
from .prompts import (PromptError, assembled_passes, clear_override,
                      list_prompts, sample_user_turn, save_override)
from .report import build_report
from .settings import (Paths, Settings, default_root, delete_api_key,
                       get_api_key, key_status, resource_root, set_api_key)
from .version import build_info, check_for_update, download_release

log = logging.getLogger("docproof.app")

CONFIG_PATH = resource_root() / "config" / "default.yaml"
ERROR_DIR = CONFIG_PATH.parent / "error_types"
# Output tokens can't be known in advance; this is a per-request allowance used
# only to turn the estimate into a number with the right order of magnitude.
OUTPUT_TOKEN_GUESS = 600
# A style sheet is a page of YAML. Anything this size is the wrong file.
MAX_SHEET_BYTES = 1_000_000


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


class PromptUpdate(BaseModel):
    detection_prompt: str = Field(min_length=1)


class StyleFormatUpdate(BaseModel):
    """How one house style looks in the tagged .docx. Points throughout, the
    same units the style sheet is written in. Bounds are here so a slip in the
    UI cannot write a 400-point chapter heading into somebody's style set."""
    size: float | None = Field(default=None, ge=4, le=96)
    bold: bool | None = None
    italic: bool | None = None
    align: Literal["left", "center", "right"] | None = None
    space_before: float | None = Field(default=None, ge=0, le=200)
    space_after: float | None = Field(default=None, ge=0, le=200)
    page_break_before: bool | None = None
    keep_next: bool | None = None
    indent: float | None = Field(default=None, ge=-100, le=200)
    # Format keys to drop entirely, which is not the same as setting them to
    # zero: an unset size inherits the template's.
    clear: list[str] = []


class SheetFormatUpdate(BaseModel):
    styles: dict[str, StyleFormatUpdate] = {}
    trim: str | None = Field(default=None, max_length=40)
    scene_break_glyph: str | None = Field(default=None, max_length=20)


class SettingsUpdate(BaseModel):
    model: str | None = None
    min_confidence: str | None = None
    output_dir: str | None = None
    default_mode: str | None = None
    comments: bool | None = None
    explanations: bool | None = None
    prep_output: str | None = None
    indesign_template: str | None = None
    anthropic_key: str | None = None
    openai_key: str | None = None
    gemini_key: str | None = None
    # Not an AI key: a read-only token for checking published releases.
    github_token: str | None = None


def create_app(root: Path | None = None, *, start_runner: bool = True,
               poll_seconds: int = 120) -> FastAPI:
    """Build the app. Raises FolderInUse when another DocProof owns this home.

    The claim is tied to `start_runner` because the runner is what does the
    damage: a second worker thread over one job folder adopts the first's
    in-flight review and runs it again. An app built without one only reads."""
    paths = Paths(root or default_root()).ensure()
    lock = FolderLock(paths.root).acquire() if start_runner else None
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
        if lock:
            lock.release()

    app = FastAPI(title="DocProof", lifespan=lifespan)
    app.state.paths = paths
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner
    app.state.lock = lock

    _register(app)
    static = resource_root() / "app" / "static"
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


def _open_path(path: Path, *, reveal: bool = False) -> None:
    """Hand a finished file to whichever application owns it.

    The app runs inside a WKWebView that cannot display a .docx and refuses to
    download one, so serving the bytes over HTTP does nothing visible. The file
    is already on this Mac; `open` is what the user would do themselves."""
    args = ["open", "-R", str(path)] if reveal else ["open", str(path)]
    subprocess.Popen(args)


def _shipped_sheet() -> Path:
    cfg = load_config(CONFIG_PATH)
    return preplib.pipeline.resolve(CONFIG_PATH.parent, cfg.prep.style_sheet)


def _override_path(paths: Paths) -> Path:
    """Where a replacement style set lives. The name is the one the config
    asks for, because that is what `load_style_sheet` looks for."""
    return paths.prep / _shipped_sheet().name


def _style_payload(paths: Paths) -> dict:
    """The style set in force, as the Settings screen reads it."""
    shipped = _shipped_sheet()
    override = paths.prep / shipped.name
    try:
        sheet = preplib.load_style_sheet(shipped, override_dir=paths.prep)
    except StyleSheetError as e:
        return {"ok": False, "error": str(e), "override_path": str(override),
                "shipped_path": str(shipped), "using_override": override.is_file()}
    return {
        "ok": True,
        "name": sheet.name, "version": sheet.version, "trim": sheet.trim,
        "glyph": sheet.scene_break_glyph,
        "path": sheet.path,
        "shipped_path": str(shipped),
        "override_path": str(override),
        "using_override": Path(sheet.path).parent == paths.prep,
        "styles": [{"name": s.name, "id": s.id, "describe": s.describe,
                    "assign": s.assign, "opens": s.opens,
                    "format": dict(s.format)} for s in sheet.styles],
        "character_styles": [{"name": s.name, "id": s.id}
                             for s in sheet.character_styles],
    }


def _install_sheet(paths: Paths, body: bytes, override: Path) -> dict:
    """Put a style sheet in force, but only once it loads.

    Written beside its destination and moved onto it, so a sheet that fails to
    parse never becomes the one prep reads, and a half-written file never
    exists under the name prep looks for."""
    override.parent.mkdir(parents=True, exist_ok=True)
    staging = override.with_name(override.name + ".uploading")
    staging.write_bytes(body)
    try:
        preplib.load_style_sheet(staging)
    except StyleSheetError as e:
        # The message names the file and the problem, and was written for
        # somebody editing YAML — but the staging path is ours, not theirs.
        raise HTTPException(400, str(e).replace(f"{staging}: ", "")
                                       .replace(str(staging), "that file"))
    except Exception as e:                    # noqa: BLE001 - any bad YAML
        raise HTTPException(400, f"That file could not be read: {e}")
    else:
        staging.replace(override)
        return _style_payload(paths)
    finally:
        staging.unlink(missing_ok=True)


def _register(app: FastAPI) -> None:

    # -- files ----------------------------------------------------------------

    @app.post("/api/files")
    async def upload(files: list[UploadFile]) -> dict:
        """Stage documents and preflight them immediately, so a file docproof
        refuses to touch says so at drop time rather than at 11pm.

        Both pipelines are preflighted, because the user has not chosen yet:
        an .idml can be reviewed but never prepped, a manuscript with tracked
        changes in it is refused by both, and a .doc can be prepped only once
        LibreOffice has turned it into a .docx. All of that is local parsing,
        so answering both questions costs one extra read of the file."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        staged = []
        for upload_file in files:
            name = Path(upload_file.filename or "document.docx").name
            # Each upload gets its own folder so the document keeps its real
            # name — that name ends up on the file the user opens.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            folder = paths.uploads / stamp
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / name
            with dest.open("wb") as fh:
                shutil.copyfileobj(upload_file.file, fh)

            entry = _stage(cfg, paths, stamp, dest)
            if not entry["ok"]:
                shutil.rmtree(folder, ignore_errors=True)
            staged.append(entry)
        return {"files": staged}

    def _stage(cfg, paths: Paths, stamp: str, dest: Path) -> dict:
        name = dest.name
        note = None
        if prep_convert.needs_conversion(dest):
            # .doc/.rtf/.odt/.txt are prep inputs, not DocProof formats. Convert
            # at drop time so everything after this point is a .docx.
            try:
                converted, note = prep_convert.ensure_docx(dest, dest.parent)
            except prep_convert.ConversionError as e:
                return {"filename": name, "ok": False, "error": str(e)}
            dest = converted

        entry: dict = {"id": f"{stamp}/{dest.name}", "filename": dest.name,
                       "original_filename": name, "converted": dest.name != name,
                       "note": note}
        try:
            entry["format"] = get_format(dest.name).to_api()
        except IngestError as e:
            return {**entry, "ok": False, "error": str(e)}

        review, review_error = _review_preflight(cfg, dest)
        prep, prep_error = _prep_preflight(cfg, paths, dest)
        entry.update(review or {}, review_error=review_error,
                     prep=prep, prep_error=prep_error,
                     can_review=review is not None, can_prep=prep is not None)
        entry["ok"] = review is not None or prep is not None
        if not entry["ok"]:
            entry["error"] = review_error or prep_error
        return entry

    def _review_preflight(cfg, path: Path) -> tuple[dict | None, str | None]:
        try:
            prepared = prepare(cfg, path, ERROR_DIR)
        except (IngestError, ValueError) as e:
            return None, str(e)
        return {
            "sections": len(prepared.chunks),
            "paragraphs": len(prepared.doc.paragraphs),
            "requests": prepared.request_count,
            "input_tokens": prepared.est_document_tokens,
            "passes": len(prepared.groups),
            # The section list is what the picker is built from: the user
            # chooses by reading their own prose, not by chunk id.
            "chunks": chunk_outline(prepared),
        }, None

    def _prep_preflight(cfg, paths: Paths,
                        path: Path) -> tuple[dict | None, str | None]:
        try:
            prepared = preplib.prepare(cfg, path, config_dir=CONFIG_PATH.parent,
                                       override_dir=paths.prep)
        except (IngestError, StyleSheetError, ValueError) as e:
            return None, str(e)
        structure = prepared.structure
        return {
            "paragraphs": prepared.paragraph_count,
            "blank_lines": sum(1 for p in structure.paragraphs if p.is_blank),
            "words": structure.word_count,
            "requests": prepared.request_count,
            "input_tokens": prepared.est_document_tokens,
            "output_tokens": prepared.est_output_tokens,
            "style_sheet": prepared.sheet.name,
        }, None

    # -- which build is this --------------------------------------------------

    @app.get("/api/version")
    def version() -> dict:
        return build_info()

    @app.get("/api/version/check")
    def version_check() -> dict:
        """Whether a newer DocProof exists — the local checkout on the machine
        this was built on, the published releases anywhere else. Only ever
        called because somebody pressed the button; nothing here runs on its
        own."""
        return check_for_update()

    @app.post("/api/version/download")
    def version_download() -> dict:
        """Fetch the newest release's disk image into ~/Downloads and show it.

        It is not installed. DocProof is unsigned, and an app that replaced
        itself with code pulled off the network would be asking to be trusted
        with exactly what macOS is right to refuse."""
        result = download_release()
        if not result["ok"]:
            raise HTTPException(400, result["message"])
        if sys.platform == "darwin":
            _open_path(Path(result["path"]), reveal=True)
        return result

    @app.get("/api/formats")
    def formats() -> dict:
        """What docproof can read, so the drop zone and the file picker are
        driven by the format registry rather than a hardcoded list that drifts
        the next time one is added."""
        return {"formats": describe(), "suffixes": list(SUFFIXES),
                # Not formats docproof reads: manuscripts LibreOffice turns
                # into a .docx at drop time. They belong with Word in the
                # picker, and the list belongs here rather than in the page.
                "prep_extra_suffixes": list(prep_convert.CONVERTIBLE)}

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
                                                               False),
                     # Rates travel with the model so the picker can re-price a
                     # partial selection without another round trip.
                     "input_per_mtok": m.input_per_mtok,
                     "output_per_mtok": m.output_per_mtok,
                     "batch_discount": m.batch_discount}
            if totals:
                inp, req = totals
                entry["cost_now"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * OUTPUT_TOKEN_GUESS)
                entry["cost_batch"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * OUTPUT_TOKEN_GUESS, batch=True)
            out.append(entry)
        return {"models": out, "keys": keys,
                "output_token_guess": OUTPUT_TOKEN_GUESS}

    def _token_totals(paths: Paths, ids: list[str]) -> tuple[int, int] | None:
        if not ids:
            return None
        cfg = load_config(CONFIG_PATH)
        tokens = requests = 0
        for file_id in ids:
            path = _resolve_upload(paths, file_id)
            if path is None:
                continue
            try:
                prepared = prepare(cfg, path, ERROR_DIR)
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
                mode=mode,
                group_id=group_id,
                schedule_at=req.schedule_at if mode == "batch" else None,
                min_confidence=req.min_confidence,
                selection=(req.selections or {}).get(file_id) or None,
                created_at=datetime.now(timezone.utc).isoformat(),
                kind=req.kind,
                prep_output=req.prep_output,
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

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict:
        """Stop a review before it has started spending anything.

        Once a job is running, waiting overnight on a vendor, or writing its
        files, there is nothing local left to cancel — the work is either
        already billed or already being written. Only a job that has not
        started can be pulled back, so that is all this offers."""
        store: JobStore = app.state.store
        job = store.get(job_id)
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
        return updated.to_api()

    @app.get("/api/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str):
        """Serve a result over HTTP — the browser build's way of handing a file
        over, and the fallback when the desktop window cannot open one."""
        job = app.state.store.get(job_id)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = _result_path(job, which)
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs/{job_id}/open/{which}")
    def open_result(job_id: str, which: str, reveal: bool = False) -> dict:
        """Open a result in Word, InDesign, or the Finder.

        This is the desktop window's version of the download route: the file
        already exists in the user's own Documents folder, so the honest thing
        to do with "Open in Word" is open it in Word."""
        job = app.state.store.get(job_id)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = _result_path(job, which)
        if sys.platform != "darwin":
            # Nothing to hand the file to; the page falls back to downloading.
            raise HTTPException(501, "This build can only open files on a Mac.")
        _open_path(path, reveal=reveal)
        return {"ok": True, "opened": path.name}

    @app.post("/api/jobs/{job_id}/place")
    def place_in_indesign(job_id: str) -> dict:
        """Flow a finished prep job into the house template.

        Synchronous on purpose. This takes a minute and the user is watching
        InDesign do it — a job that reported back later would be stranger than
        a button that waits."""
        job = app.state.store.get(job_id)
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
        if find_indesign() is None:
            raise HTTPException(
                400, "InDesign does not appear to be installed on this Mac.")

        out = Path(job.results_dir) / _result_name(job, "placed")
        try:
            placed = place_into_template(template, tagged, out)
        except PlaceError as e:
            raise HTTPException(400, str(e))
        _open_path(placed, reveal=True)
        return {"ok": True, "filename": placed.name, "path": str(placed)}

    @app.get("/api/jobs/{job_id}/prep")
    def prep_notes(job_id: str) -> dict:
        """What prep did, read back for the results screen."""
        job = app.state.store.get(job_id)
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

    # -- the style guide ------------------------------------------------------

    @app.get("/api/prep/styles")
    def prep_styles() -> dict:
        """The house style set as loaded, and where to put a replacement.

        The point of this route is that the style guide is data: it shows the
        publisher exactly which file is in force and what is in it."""
        return _style_payload(app.state.paths)

    @app.post("/api/prep/styles/sheet")
    async def upload_style_sheet(file: UploadFile) -> dict:
        """Take a replacement style set from the Settings screen.

        It is written under the name the config asks for, whatever the file was
        called on the way in, because `load_style_sheet` finds a replacement by
        filename. Nothing is installed until it has been loaded successfully:
        a sheet with two styles sharing a name would otherwise take prep down
        at the next run, hours after the mistake was made."""
        override = _override_path(app.state.paths)
        raw = await file.read()
        if len(raw) > MAX_SHEET_BYTES:
            raise HTTPException(
                400, f"{file.filename} is larger than a style sheet can "
                     f"sensibly be. Is it the right file?")
        return _install_sheet(app.state.paths, raw, override)

    @app.delete("/api/prep/styles/sheet")
    def reset_style_sheet() -> dict:
        """Go back to the style set DocProof ships with. This also discards any
        adjustments made below — there is one file, so there is one undo."""
        _override_path(app.state.paths).unlink(missing_ok=True)
        return _style_payload(app.state.paths)

    @app.put("/api/prep/styles/format")
    def set_style_formats(req: SheetFormatUpdate) -> dict:
        """Adjust how the styles look, and nothing else.

        Only `format` values and the two sheet-level settings can be reached
        from here. Names, ids, descriptions and roles are copied through
        untouched: the name of a style is what InDesign matches when the file
        is placed and what the model is allowed to answer, so it is not a knob.

        The edited sheet is written to the same override file an uploaded one
        goes to, leaving the shipped copy alone."""
        paths: Paths = app.state.paths
        payload = _style_payload(paths)
        if not payload["ok"]:
            raise HTTPException(400, payload["error"])

        raw = yaml.safe_load(Path(payload["path"]).read_text("utf-8")) or {}
        entries = {str(e.get("name")): e for e in raw.get("styles") or []}
        unknown = [name for name in req.styles if name not in entries]
        if unknown:
            raise HTTPException(
                400, f"This style set has no style called "
                     f"'{unknown[0]}'. It has: "
                     f"{', '.join(sorted(entries))}.")

        for name, update in req.styles.items():
            fmt = dict(entries[name].get("format") or {})
            for key, value in update.model_dump(exclude_none=True).items():
                if key != "clear":
                    fmt[key] = value
            for key in update.clear:
                fmt.pop(key, None)
            entries[name]["format"] = fmt
        if req.trim is not None:
            raw["trim"] = req.trim
        if req.scene_break_glyph is not None:
            raw["scene_break_glyph"] = req.scene_break_glyph

        body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True,
                              width=88).encode("utf-8")
        return _install_sheet(paths, body, _override_path(paths))

    # -- what it has cost -----------------------------------------------------

    @app.get("/api/usage")
    def usage() -> dict:
        """Tokens, calls and estimated spend across every job on this machine."""
        return build_usage(app.state.store.all(), read_usage)

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str) -> dict:
        """The findings, read back as prose rather than as a record."""
        job = app.state.store.get(job_id)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = Path(job.results_dir) / "findings.json"
        if not path.is_file():
            raise HTTPException(404, "This review has no findings file")
        cfg = load_config(CONFIG_PATH)
        rows = list_prompts(ERROR_DIR, app.state.paths.prompts,
                            list(cfg.error_type_keys))
        return build_report(path, {r["key"]: r["name"] for r in rows})

    # -- prompts --------------------------------------------------------------

    @app.get("/api/prompts")
    def read_prompts() -> dict:
        """What docproof is about to say to the model, and where to change it."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        cfg.error_type_override_dir = str(paths.prompts)
        cfg.report_explanations = app.state.settings.explanations
        return {
            "types": list_prompts(ERROR_DIR, paths.prompts,
                                  list(cfg.error_type_keys)),
            "passes": assembled_passes(cfg, ERROR_DIR, paths.prompts),
            "sample_user_turn": sample_user_turn(),
            "explanations": cfg.report_explanations,
        }

    @app.put("/api/prompts/{key}")
    def write_prompt(key: str, update: PromptUpdate) -> dict:
        try:
            save_override(ERROR_DIR, app.state.paths.prompts, key,
                          update.detection_prompt)
        except PromptError as e:
            raise HTTPException(400, str(e))
        return read_prompts()

    @app.delete("/api/prompts/{key}")
    def reset_prompt(key: str) -> dict:
        clear_override(app.state.paths.prompts, key)
        return read_prompts()

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
                           "default_mode", "prep_output", "comments",
                           "explanations", "indesign_template"):
            value = getattr(update, field_name)
            if value is not None:
                setattr(s, field_name, value)
        for provider, value in (("anthropic", update.anthropic_key),
                                ("openai", update.openai_key),
                                ("gemini", update.gemini_key),
                                ("github", update.github_token)):
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
        if provider not in ("anthropic", "openai", "gemini"):
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


def __getattr__(name: str):
    """`uvicorn app.main:app` still works, but only for whoever actually asks.

    This used to be a plain `app = create_app()`, which meant importing this
    module for any reason built a second app against the *default* home —
    creating its folders, ignoring any --home the caller had in mind, and now
    claiming the lock on a folder the caller never intended to touch. Built on
    demand, `uvicorn app.main:app` resolves it through here and nothing else
    pays for it."""
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
