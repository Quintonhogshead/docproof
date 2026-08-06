"""Local HTTP surface for the DocProof app.

Binds to localhost only. Every route is thin: it validates input, touches the
job store, and returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
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
from docproof.voice import VOICE_KEYS

from .jobs import TERMINAL_STATES, Job, JobRunner, JobStore, read_usage
from .lock import FolderInUse, FolderLock
from .usage import build_usage
# The watcher, but never its CLI: `app.watch.cli._logging` clears the docproof
# logger's handlers, which for a long-lived server means it stops logging.
from .watch import schedule as schedulelib
from .watch import status as watchlib
from .watch.drive import DriveError
from .watch.runner import WatchRunner
from .watch.settings import WatchSettings, folder_id_from
from .watch.tick import NotConfigured
from .prompts import (PromptError, assembled_passes, clear_override,
                      list_prompts, sample_user_turn, save_override)
from .report import build_report
from .settings import (Paths, Settings, default_root, delete_api_key,
                       get_api_key, key_status, resource_root, set_api_key)
from .update import (Rebuilder, UpdateError, perform_update,
                     refuse_reason)
from .version import build_info, check_for_update, download_release

log = logging.getLogger("docproof.app")

CONFIG_PATH = resource_root() / "config" / "default.yaml"
ERROR_DIR = CONFIG_PATH.parent / "error_types"
# Output tokens can't be known in advance; this is a per-request allowance used
# only to turn the estimate into a number with the right order of magnitude.
OUTPUT_TOKEN_GUESS = 600
# A style sheet is a page of YAML. Anything this size is the wrong file.
MAX_SHEET_BYTES = 1_000_000
# How long an unclaimed staged upload survives a restart. Long enough that a
# window left open overnight can still start its review in the morning; short
# enough that abandoned drops do not sit on the disk for months.
UPLOAD_GRACE_SECONDS = 24 * 3600


class JobRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    model: str
    mode: str = "batch"                       # "now" | "batch"
    schedule_at: str | None = None            # "HH:MM" local
    min_confidence: str = "medium"
    # The dictionary scan, per submission. Null means whatever the config
    # says, which is what a caller that does not know about these means.
    spellcheck: bool | None = None
    dictionary: str | None = Field(default=None, max_length=500)
    # preserve | query | correct — null means whatever the config says.
    voice: str | None = None
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
    change_log: bool | None = None
    prep_output: str | None = None
    indesign_template: str | None = None
    anthropic_key: str | None = None
    openai_key: str | None = None
    gemini_key: str | None = None
    # Not an AI key: a read-only token for checking published releases.
    github_token: str | None = None


class WatchUpdate(BaseModel):
    """Partial, like SettingsUpdate: the panel sends what changed."""

    # An address pasted out of a browser, or a bare id. Either is fine.
    folder: str | None = None
    model: str | None = None
    prep_output: str | None = None       # indesign | tracked | both
    upload_notes: bool | None = None
    upload_failure_note: bool | None = None
    # Bounds so a slip in the UI cannot spend a morning's worth of manuscripts
    # in one pass, or set a clock that never stops going off.
    max_files_per_tick: int | None = Field(default=None, ge=1, le=50)
    auto_ticks: bool | None = None
    tick_every_minutes: int | None = Field(default=None, ge=5, le=1440)


class WatchAuth(BaseModel):
    """The OAuth client to sign in with. Blank means "use the saved one"."""

    client_id: str | None = None
    client_secret: str | None = None


class WatchSchedule(BaseModel):
    times: str = ",".join(schedulelib.DEFAULT_TIMES)


def watch_home_for(root: Path) -> Path:
    """Where the watcher keeps its things, derived from the app's home.

    The same answer as `default_watch_home()` for an ordinary install, and a
    safer one everywhere else: an app started with `--home somewhere` gets a
    watcher beside it rather than reaching for the real one, which is also what
    keeps a test's tmp_path from ever touching it. DOCPROOF_WATCH_HOME still
    wins, because the terminal honours it and the two doors have to agree about
    which folder they are both looking at."""
    env = os.environ.get("DOCPROOF_WATCH_HOME")
    return Path(env).expanduser() if env else Path(root) / "watch"


def create_app(root: Path | None = None, *, start_runner: bool = True,
               poll_seconds: int = 120,
               watch_home: Path | None = None) -> FastAPI:
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
    # Deliberately not given the watch home's lock here. The app claims its own
    # folder for as long as it is open; the watcher's is claimed only for the
    # length of a pass, because a scheduled run started by macOS has to be able
    # to take it while a window is open.
    watch = WatchRunner(watch_home or watch_home_for(paths.root))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_runner:
            runner.start()
            watch.start()
            # Tied to start_runner like everything else destructive: only the
            # instance that owns the folder cleans it. A thread so a slow disk
            # does not hold up the window opening.
            threading.Thread(target=sweep_uploads, args=(paths, store),
                             name="uploads-sweep", daemon=True).start()
        yield
        runner.stop()
        watch.stop()
        if lock:
            lock.release()

    app = FastAPI(title="DocProof", lifespan=lifespan)
    app.state.paths = paths
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner
    app.state.watch = watch
    app.state.rebuild = Rebuilder()
    app.state.lock = lock

    _register(app)
    static = resource_root() / "app" / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


def _upload_folders_in_use(store: JobStore) -> set[Path]:
    """The staged folders some job will read again. A job that has not reached
    a terminal state goes back to its source document — a scheduled one submits
    it later, a waiting one applies the vendor's results to it — so its folder
    has to survive any cleanup."""
    return {Path(job.source_path).resolve().parent
            for job in store.all() if job.state not in TERMINAL_STATES}


def sweep_uploads(paths: Paths, store: JobStore) -> int:
    """Delete staged uploads nothing needs any more, and say how many went.

    Runs at startup. A staged file is kept while any unfinished job references
    it, and for a grace period regardless — a browser window left open across
    a restart still holds ids for files it staged before, and its "Start
    review" click should keep working."""
    uploads = paths.uploads.resolve()
    if not uploads.is_dir():
        return 0
    in_use = _upload_folders_in_use(store)
    cutoff = time.time() - UPLOAD_GRACE_SECONDS
    removed = 0
    for folder in uploads.iterdir():
        if not folder.is_dir() or folder.resolve() in in_use:
            continue
        try:
            if folder.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += 1
    if removed:
        log.info("Swept %d stale staged upload(s)", removed)
    return removed


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
    def upload(files: list[UploadFile]) -> dict:
        """Stage documents and preflight them immediately, so a file docproof
        refuses to touch says so at drop time rather than at 11pm.

        Both pipelines are preflighted, because the user has not chosen yet:
        an .idml can be reviewed but never prepped, a manuscript with tracked
        changes in it is refused by both, and a .doc can be prepped only once
        LibreOffice has turned it into a .docx. All of that is local parsing,
        so answering both questions costs one extra read of the file.

        Deliberately not `async`: preflight is seconds of parsing per document
        and conversion is a LibreOffice subprocess, and a coroutine doing that
        holds the event loop — a five-file drop used to freeze every other
        request until it finished. A plain function runs on the thread pool
        instead, so the app keeps answering while a stack is staged."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)

        # Write everything to disk first, in arrival order, hashing as it
        # streams: a file whose bytes match an earlier file in the same drop
        # is the same document twice, and it is kept once.
        todo: list[dict | tuple[str, Path, str]] = []
        seen: dict[str, str] = {}
        for position, upload_file in enumerate(files):
            name = Path(upload_file.filename or "document.docx").name
            # Each upload gets its own folder so the document keeps its real
            # name — that name ends up on the file the user opens. The
            # position suffix keeps two files landing in the same microsecond
            # out of each other's folders.
            stamp = (datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                     + f"-{position}")
            folder = paths.uploads / stamp
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / name
            digest = hashlib.sha256()
            with dest.open("wb") as fh:
                while chunk := upload_file.file.read(1 << 20):
                    fh.write(chunk)
                    digest.update(chunk)
            sha = digest.hexdigest()
            if sha in seen:
                shutil.rmtree(folder, ignore_errors=True)
                todo.append({"filename": name, "ok": False, "duplicate": True,
                             "sha256": sha,
                             "error": f"The same file as {seen[sha]}, which "
                                      f"is already in this drop — kept once."})
                continue
            seen[sha] = name
            todo.append((stamp, dest, sha))

        # The heavy part — conversion and both preflights — runs a few files
        # at a time, so one slow conversion does not stall the whole drop.
        def stage_one(item: dict | tuple[str, Path, str]) -> dict:
            if isinstance(item, dict):        # a duplicate, already answered
                return item
            stamp, dest, sha = item
            entry = _stage(cfg, paths, stamp, dest, sha)
            if not entry["ok"]:
                shutil.rmtree(dest.parent, ignore_errors=True)
            return entry

        with ThreadPoolExecutor(max_workers=4) as pool:
            staged = list(pool.map(stage_one, todo))
        return {"files": staged}

    @app.delete("/api/files/{file_id:path}")
    def remove_upload(file_id: str) -> dict:
        """Delete one staged upload, because it left the list on the page.

        Refuses nothing and never errors: a file already gone was removed
        twice, and a file an unfinished job still reads is kept — the job
        outranks the list. Either way the page has nothing to do about it,
        so the answer just says what happened."""
        paths: Paths = app.state.paths
        store: JobStore = app.state.store
        path = _resolve_upload(paths, file_id)
        if path is None:
            return {"removed": False, "reason": "gone"}
        if path.parent.resolve() in _upload_folders_in_use(store):
            return {"removed": False, "reason": "in use"}
        shutil.rmtree(path.parent, ignore_errors=True)
        return {"removed": True}

    def _stage(cfg, paths: Paths, stamp: str, dest: Path, sha256: str) -> dict:
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
                       # The hash of the bytes as dropped, before any
                       # conversion: it is how the page recognises a document
                       # it is already holding.
                       "sha256": sha256, "note": note}
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

    @app.post("/api/version/update")
    def version_update() -> dict:
        """Install the newest release over this one and reopen.

        Only ever runs because somebody pressed the button the launch-time
        banner shows. It refuses — before downloading anything — while a
        document is being worked on, when running from the source, and when
        running straight off a disk image."""
        try:
            return perform_update(app.state.runner)
        except UpdateError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/version/rebuild")
    def version_rebuild() -> dict:
        """Rebuild this app from the checkout it came from, and install it.

        The machine DocProof is written on has no release to install — the
        newest DocProof there is whatever is in the checkout. It used to be
        told to go and run `tools/update.sh` in a terminal, which is a strange
        thing for an application to say when it knows where its own source is.

        A minute of work, so it answers immediately and the page asks how it is
        going. The three refusals an update makes are made here first, before
        the thread exists — somebody who clicks this mid-review should be told
        so now rather than a second later, in a progress line."""
        reason = refuse_reason(app.state.runner)
        if reason:
            raise HTTPException(400, reason)
        return asdict(app.state.rebuild.start(app.state.runner))

    @app.get("/api/version/rebuild")
    def version_rebuild_state() -> dict:
        return asdict(app.state.rebuild.state())

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
        if req.voice is not None and req.voice not in VOICE_KEYS:
            raise HTTPException(
                400, f"voice must be one of {', '.join(VOICE_KEYS)}")
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
                spellcheck=req.spellcheck,
                dictionary=req.dictionary,
                voice=req.voice,
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
        started can be pulled back, so that is all this offers. A job holding
        for room in the vendor's queue is one of those: nothing has been sent,
        and it is the one a user watching a backlog most wants to pull."""
        store: JobStore = app.state.store
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state not in ("queued", "scheduled", "holding"):
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
        """Tokens, calls and estimated spend across every job on this machine.

        Two job stores, one bill. The watcher keeps its own home — a separate
        folder is a separate lock, which is what lets a pass and this window
        run at the same moment — but the money comes off the same card, and a
        figure that quietly left half of it out would be the wrong figure."""
        watch: WatchRunner = app.state.watch
        return build_usage([*app.state.store.all(), *watch.jobs()], read_usage)

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

    # -- the watched folder ---------------------------------------------------
    #
    # Every route here answers with the whole panel, the way `POST /api/tick`
    # does: one round trip does the thing and brings back what the screen needs
    # to redraw, so the page never has to guess what a change did.

    def watch_payload() -> dict:
        watch: WatchRunner = app.state.watch
        signing = watch.sign_in_state()
        return {
            "watch": watchlib.status(watch.home, agent_path=watch.agent_path),
            "run": watch.state(),
            "sign_in": asdict(signing) if signing else None,
            "can_schedule": sys.platform == "darwin",
        }

    def watch_needs(ws: WatchSettings) -> None:
        """Refuse in the app's own words.

        `tick.py` says "Run `docproof-watch init`", which is right for the only
        caller that shows its messages to somebody holding a terminal. In here
        there is no terminal — there are cards, and they are what to name."""
        need = watchlib.missing(ws)
        if need == "folder":
            raise HTTPException(400, "Choose the folder to watch first — it is "
                                     "the second card on this screen.")
        if need == "auth":
            raise HTTPException(400, "Sign in to Google first — it is the first "
                                     "card on this screen.")

    @app.get("/api/watch")
    def read_watch() -> dict:
        return watch_payload()

    @app.put("/api/watch")
    def write_watch(update: WatchUpdate) -> dict:
        watch: WatchRunner = app.state.watch
        ws = WatchSettings.load(watch.home)
        if update.folder is not None:
            try:
                ws.folder_id = folder_id_from(update.folder)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        if update.model is not None:
            if lookup(update.model) is None:
                raise HTTPException(400, f"{update.model} is not a model "
                                         f"DocProof knows.")
            ws.model = update.model
        if update.prep_output is not None:
            if update.prep_output not in ("indesign", "tracked", "both"):
                raise HTTPException(400, "prep_output must be 'indesign', "
                                         "'tracked' or 'both'")
            ws.prep_output = update.prep_output
        for name in ("upload_notes", "upload_failure_note",
                     "max_files_per_tick", "auto_ticks", "tick_every_minutes"):
            value = getattr(update, name)
            if value is not None:
                setattr(ws, name, value)
        ws.save(watch.home)
        return watch_payload()

    @app.get("/api/watch/auth")
    def read_watch_auth() -> dict:
        return watch_payload()

    @app.post("/api/watch/auth")
    def start_watch_auth(body: WatchAuth) -> dict:
        """Open Google's consent page and start waiting for the answer.

        Returns immediately with the sign-in marked `waiting`; the page polls.
        The consent page opens in the real browser rather than in DocProof's
        own window on purpose — Google refuses OAuth inside an embedded web
        view, and a password belongs in Google's page anyway."""
        watch: WatchRunner = app.state.watch
        ws = WatchSettings.load(watch.home)
        client_id = (body.client_id or ws.client_id or "").strip()
        client_secret = (body.client_secret or ws.client_secret or "").strip()
        if not client_id or not client_secret:
            raise HTTPException(400,
                                "Signing in needs a Google OAuth client. "
                                "docs/watch.md walks through the five minutes "
                                "in the Google Cloud console that makes one.")
        watch.begin_sign_in(client_id, client_secret)
        return watch_payload()

    @app.delete("/api/watch/auth")
    def forget_watch_auth() -> dict:
        """Forget the sign-in, keep the client.

        The client id and secret are not the secret — an installed application
        cannot keep one — and signing in again needs them."""
        delete_api_key("google")
        return watch_payload()

    @app.post("/api/watch/run")
    def run_watch() -> dict:
        watch: WatchRunner = app.state.watch
        watch_needs(WatchSettings.load(watch.home))
        started = watch.run_now()
        # Not an error when it is already going. The button is disabled while a
        # pass runs, so this is only ever a double click, and answering "it is
        # already doing what you asked" in red would be the wrong noise.
        return {"started": started, **watch_payload()}

    @app.post("/api/watch/preview")
    def preview_watch() -> dict:
        """What a pass would do, without doing any of it.

        Synchronous: a dry run is one token refresh and one listing, so there
        is nothing to wait for and a real answer can come back with it."""
        watch: WatchRunner = app.state.watch
        watch_needs(WatchSettings.load(watch.home))
        try:
            report = watch.preview()
        except NotConfigured:
            watch_needs(WatchSettings.load(watch.home))
            raise
        except DriveError as e:
            raise HTTPException(400, str(e)) from None
        return {
            "listed": report.listed,
            "new": report.new,
            # The label comes from here rather than the page, so the words for
            # a stage have one home.
            "plan": [{"name": name, "stage": stage,
                      "label": watchlib.PLAIN_STAGE.get(stage, stage)}
                     for name, stage in report.plan],
        }

    @app.put("/api/watch/schedule")
    def set_watch_schedule(body: WatchSchedule) -> dict:
        watch: WatchRunner = app.state.watch
        if sys.platform != "darwin":
            raise HTTPException(501, "Looking while DocProof is closed needs "
                                     "a Mac.")
        try:
            times = schedulelib.parse_times(body.times)
            schedulelib.install(times, watch.home, path=watch.agent_path,
                                **({"run": watch.launchctl}
                                   if watch.launchctl else {}))
        except schedulelib.ScheduleError as e:
            raise HTTPException(400, str(e)) from None
        return watch_payload()

    @app.delete("/api/watch/schedule")
    def clear_watch_schedule() -> dict:
        watch: WatchRunner = app.state.watch
        if sys.platform != "darwin":
            raise HTTPException(501, "Looking while DocProof is closed needs "
                                     "a Mac.")
        schedulelib.uninstall(path=watch.agent_path,
                              **({"run": watch.launchctl}
                                 if watch.launchctl else {}))
        return watch_payload()

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
                           "explanations", "change_log", "indesign_template"):
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
