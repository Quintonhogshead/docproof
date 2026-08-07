"""Local HTTP surface for the DocProof app: building it, and nothing else.

Binds to localhost only. The routes live in `app.routes`, one module per
group; every one is thin — it validates input, touches the job store, and
returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import routes
from .jobs import JobRunner, JobStore
from .lock import FolderLock
from .settings import (CONFIG_PATH, ENV_VARS, PROVIDERS, Paths, Settings,
                       default_root, field_in_settings_file, resource_root)
from .update import Rebuilder
from .watch.runner import WatchRunner


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
    if web and not field_in_settings_file(paths, "output_dir"):
        # The default output_dir is the desktop's ~/Documents/DocProof, which on
        # a server lands on the container's throwaway filesystem — finished
        # documents there vanish on the next redeploy or restart while the job
        # records that point at them survive on the volume, so the results tab
        # 404s "…is missing". Keep results on the volume beside the job records.
        # An administrator who set output_dir themselves is left untouched.
        settings.output_dir = str(paths.results)
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

    routes.register(app)
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
        routes.register_admin(app)
    static = resource_root() / "app" / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


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
