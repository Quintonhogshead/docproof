"""Local HTTP surface for the DocProof app.

Binds to localhost only. Every route is thin: it validates input, touches the
job store, and returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from docproof import __version__ as _docproof_version
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

from .accounts import AccountError, User
from .auth import current_user, owner_for, require_admin
from .jobs import Job, JobRunner, JobStore, read_usage
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
from .settings import (ENV_VARS, PROVIDERS, Paths, Settings, default_root,
                       delete_api_key, get_api_key, key_status, resource_root,
                       set_api_key)
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


class AdminCreateUser(BaseModel):
    email: str
    password: str
    is_admin: bool = False
    monthly_cap: float | None = None


class AdminUpdateUser(BaseModel):
    """Everything an administrator can change about an account. A field left out
    is left alone; monthly_cap is the exception the other way — sending it as
    null clears the cap (back to the server default), which is different from
    not sending it, so the route reads `model_fields_set` to tell them apart."""
    disabled: bool | None = None
    is_admin: bool | None = None
    monthly_cap: float | None = None
    password: str | None = None


class KeyUpdate(BaseModel):
    key: str | None = None


# The provider labels the portal shows, matching the desktop Settings screen.
KEY_DISPLAY = {"anthropic": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}


# --- spend caps ---------------------------------------------------------------

CAP_ENV = "DOCPROOF_DEFAULT_CAP"


def default_cap() -> float | None:
    """The monthly ceiling for a user who has none of their own, or None for no
    ceiling. Set DOCPROOF_DEFAULT_CAP on the server to put a floor of caution
    under every ordinary account at once."""
    raw = os.environ.get(CAP_ENV)
    try:
        return float(raw) if raw else None
    except ValueError:
        log.warning("Ignoring unreadable %s=%r", CAP_ENV, raw)
        return None


def cap_for(user: User | None) -> float | None:
    """What this user may spend this month, or None for no limit. Administrators
    are never capped — that is what God Mode means — and neither is the desktop
    build, where `user` is None."""
    if user is None or user.is_admin:
        return None
    return user.monthly_cap if user.monthly_cap is not None else default_cap()


def month_spend(store: JobStore, owner: str) -> float:
    """What this owner's jobs have cost so far in the current calendar month."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum(j.cost or 0.0 for j in store.all(owner)
               if (j.created_at or "").startswith(prefix))


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
               watch_home: Path | None = None,
               web: bool = False, session_secret: str | None = None,
               https_only: bool = True) -> FastAPI:
    """Build the app. Raises FolderInUse when another DocProof owns this home.

    The claim is tied to `start_runner` because the runner is what does the
    damage: a second worker thread over one job folder adopts the first's
    in-flight review and runs it again. An app built without one only reads.

    `web=True` is the hosted build: it adds sign-in (accounts, sessions, and a
    gate over every /api route) and makes each request's work belong to the
    user who made it. `web=False` is the desktop app, unchanged — no accounts,
    no gate, one local owner. Everything between the two builds is shared."""
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
    app.state.web = web

    _register(app)
    if web:
        # Sign-in, sessions, and the gate over every /api route. Added after the
        # routes exist so the gate wraps all of them; the desktop build skips
        # this entirely and stays open, as it always was.
        from .accounts import Accounts
        from .auth import install_auth
        from .keystore import KeyStore
        install_auth(app, Accounts(paths.users_db), secret=session_secret,
                     https_only=https_only)
        # Provider keys an administrator sets in the portal live on the volume
        # and are loaded into the environment here, where get_api_key looks
        # first. env_keys remembers what the environment itself provided (a fly
        # secret, say) so removing a portal key brings that back without a
        # restart. A portal key takes precedence while it exists.
        keystore = KeyStore(paths.keys_db)
        app.state.keystore = keystore
        app.state.env_keys = {p: os.environ.get(ENV_VARS[p]) for p in PROVIDERS}
        for provider in PROVIDERS:
            stored = keystore.get(provider)
            if stored:
                os.environ[ENV_VARS[provider]] = stored
        # God Mode: the admin-only routes for managing users, caps and keys.
        # Only the web build has users to manage, so only it registers these.
        _register_admin(app)
    static = resource_root() / "app" / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


def _register_admin(app: FastAPI) -> None:
    """God Mode: user and cap management, every route behind require_admin.
    Registered only for the web build, which is the only one with accounts."""
    accounts = app.state.accounts
    store: JobStore = app.state.store

    def _row(u: User) -> dict:
        return {"id": u.id, "email": u.email, "is_admin": u.is_admin,
                "disabled": u.disabled, "monthly_cap": u.monthly_cap,
                "effective_cap": cap_for(u),
                "spent_this_month": round(month_spend(store, u.id), 4)}

    @app.get("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_list_users() -> dict:
        return {"users": [_row(u) for u in accounts.list_users()],
                "default_cap": default_cap()}

    @app.post("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_create_user(body: AdminCreateUser) -> dict:
        try:
            user = accounts.create_user(body.email, body.password,
                                        is_admin=body.is_admin,
                                        monthly_cap=body.monthly_cap)
        except AccountError as e:
            raise HTTPException(400, str(e))
        return _row(user)

    @app.put("/api/admin/users/{user_id}")
    def admin_update_user(user_id: str, body: AdminUpdateUser,
                          me: User = Depends(require_admin)) -> dict:
        target = accounts.get_user(user_id)
        if target is None:
            raise HTTPException(404, "No such user")
        # No locking yourself out: an administrator can't disable their own
        # account or drop their own admin. Another administrator has to.
        if target.id == me.id:
            if body.disabled:
                raise HTTPException(400, "You can't disable your own account.")
            if body.is_admin is False:
                raise HTTPException(
                    400, "You can't remove your own admin access.")
        try:
            if body.password is not None:
                accounts.set_password(user_id, body.password)
        except AccountError as e:
            raise HTTPException(400, str(e))
        if body.disabled is not None:
            accounts.set_disabled(user_id, body.disabled)
        if body.is_admin is not None:
            accounts.set_admin(user_id, body.is_admin)
        # Sent-as-null clears the cap; omitted leaves it. model_fields_set is
        # what tells the two apart.
        if "monthly_cap" in body.model_fields_set:
            accounts.set_cap(user_id, body.monthly_cap)
        return _row(accounts.get_user(user_id))

    @app.get("/api/admin/usage", dependencies=[Depends(require_admin)])
    def admin_usage() -> dict:
        """Every user's month-to-date spend, for the God Mode dashboard."""
        rows = []
        for u in accounts.list_users():
            totals = build_usage(store.all(u.id), read_usage)["totals"]
            rows.append({"id": u.id, "email": u.email,
                         "monthly_cap": u.monthly_cap,
                         "effective_cap": cap_for(u), **totals})
        return {"users": rows, "default_cap": default_cap()}

    # -- provider API keys ----------------------------------------------------

    def _key_rows() -> list[dict]:
        rows = []
        for provider in PROVIDERS:
            portal = app.state.keystore.get(provider) is not None
            from_env = bool(app.state.env_keys.get(provider))
            source = "portal" if portal else ("environment" if from_env else None)
            rows.append({"provider": provider,
                         "display": KEY_DISPLAY.get(provider, provider),
                         "configured": bool(get_api_key(provider)),
                         "source": source})
        return rows

    @app.get("/api/admin/keys", dependencies=[Depends(require_admin)])
    def admin_keys() -> dict:
        """Which providers have a key and where it came from — never the key
        itself, which is set once and never read back to a browser."""
        return {"keys": _key_rows()}

    @app.put("/api/admin/keys/{provider}", dependencies=[Depends(require_admin)])
    def admin_set_key(provider: str, body: KeyUpdate) -> dict:
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        key = (body.key or "").strip()
        if not key:
            raise HTTPException(400, "Paste a key, or use Remove to clear it.")
        app.state.keystore.set(provider, key)
        os.environ[ENV_VARS[provider]] = key      # in force for the next review
        log.info("Admin set the %s key", provider)
        return {"keys": _key_rows()}

    @app.delete("/api/admin/keys/{provider}",
                dependencies=[Depends(require_admin)])
    def admin_clear_key(provider: str) -> dict:
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        app.state.keystore.delete(provider)
        # Put back whatever the environment gave at boot, so removing a portal
        # key falls back to a fly secret rather than to nothing.
        restore = app.state.env_keys.get(provider)
        if restore:
            os.environ[ENV_VARS[provider]] = restore
        else:
            os.environ.pop(ENV_VARS[provider], None)
        return {"keys": _key_rows()}


def _resolve_upload(paths: Paths, file_id: str, owner: str) -> Path | None:
    """Map a staged-file id onto a path, refusing anything that escapes the
    owner's own uploads directory. The id comes from the browser, so it is
    untrusted — and staging is per-owner, so requiring the path to sit under
    uploads/<owner> is also what stops one user resolving another's file by
    guessing its id."""
    owner_root = (paths.uploads / owner).resolve()
    candidate = (paths.uploads / owner / file_id).resolve()
    if not candidate.is_relative_to(owner_root) or not candidate.is_file():
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
    async def upload(files: list[UploadFile],
                     owner: str = Depends(owner_for)) -> dict:
        """Stage documents and preflight them immediately, so a file docproof
        refuses to touch says so at drop time rather than at 11pm.

        Both pipelines are preflighted, because the user has not chosen yet:
        an .idml can be reviewed but never prepped, a manuscript with tracked
        changes in it is refused by both, and a .doc can be prepped only once
        LibreOffice has turned it into a .docx. All of that is local parsing,
        so answering both questions costs one extra read of the file.

        Staging lives under uploads/<owner>, so the id handed back is only ever
        resolvable by the user who uploaded it — one local owner on the desktop,
        the signed-in user on the web."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        staged = []
        for upload_file in files:
            name = Path(upload_file.filename or "document.docx").name
            # Each upload gets its own folder so the document keeps its real
            # name — that name ends up on the file the user opens.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            folder = paths.uploads / owner / stamp
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
    def models(file_ids: str = "", owner: str = Depends(owner_for)) -> dict:
        """The catalog, plus what these specific files would cost on each."""
        paths: Paths = app.state.paths
        keys = key_status()
        ids = [f for f in file_ids.split(",") if f]
        totals = _token_totals(paths, ids, owner)

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

    def _token_totals(paths: Paths, ids: list[str],
                      owner: str) -> tuple[int, int] | None:
        if not ids:
            return None
        cfg = load_config(CONFIG_PATH)
        tokens = requests = 0
        for file_id in ids:
            path = _resolve_upload(paths, file_id, owner)
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
        if not get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")

        # Every id is resolved before any job is enqueued: a 404 halfway
        # through used to leave the earlier files already running — the page
        # said failure, the retry ran them twice, and twice was billed twice.
        sources = {}
        for file_id in req.file_ids:
            source = _resolve_upload(paths, file_id, owner)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            sources[file_id] = source

        # Spend cap (web build only). The estimate for this very submission
        # counts, so one oversized review can't slip past a nearly-spent cap;
        # administrators and the desktop build have no cap and skip all of this.
        if app.state.web:
            cap = cap_for(app.state.accounts.get_user(owner))
            if cap is not None:
                spent = month_spend(app.state.store, owner)
                totals = _token_totals(paths, req.file_ids, owner)
                est = 0.0
                if totals:
                    inp, reqs = totals
                    est = estimate_cost(
                        req.model, input_tokens=inp,
                        output_tokens=reqs * OUTPUT_TOKEN_GUESS,
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
        _open_path(path, reveal=reveal)
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

    def _may_edit_prompts(request: Request) -> None:
        """The detection prompts are one shared setting for the whole server, so
        on the web build only an administrator may change them. The desktop
        build has one user and no gate, so this passes."""
        if app.state.web:
            user = getattr(request.state, "user", None)
            if not (user and user.is_admin):
                raise HTTPException(
                    403, "Only an administrator can change what DocProof "
                         "looks for.")

    @app.put("/api/prompts/{key}", dependencies=[Depends(_may_edit_prompts)])
    def write_prompt(key: str, update: PromptUpdate) -> dict:
        try:
            save_override(ERROR_DIR, app.state.paths.prompts, key,
                          update.detection_prompt)
        except PromptError as e:
            raise HTTPException(400, str(e))
        return read_prompts()

    @app.delete("/api/prompts/{key}", dependencies=[Depends(_may_edit_prompts)])
    def reset_prompt(key: str) -> dict:
        clear_override(app.state.paths.prompts, key)
        return read_prompts()

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
        # An empty folder box means "unchanged", the same as an absent one:
        # the panel always sends the field, and on a fresh setup it is blank —
        # refusing it here used to throw away the model and output choices
        # saved alongside it.
        if update.folder:
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
        # version rides along here because this is the one route open before
        # sign-in, so the web build can show it on the login screen too.
        return {"ok": True, "version": _docproof_version}


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
