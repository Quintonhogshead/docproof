"""Local HTTP surface for the DocProof app: building it, and nothing else.

Binds to localhost only. The routes live in `app.routes`, one module per
group; every one is thin — it validates input, touches the job store, and
returns JSON. The pipeline itself lives in docproof/.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import routes
from .jobs import JobRunner, JobStore
from .lock import FolderLock

from starlette.types import Scope

log = logging.getLogger("docproof.app.main")


class FreshStaticFiles(StaticFiles):
    """StaticFiles that makes the browser revalidate every load.

    The frontend ships app.js / styles.css / index.html at fixed URLs with no
    content hash, so without an explicit policy a browser caches them
    heuristically and, after a deploy, keeps rendering the OLD page while the
    footer version — a live /api/version call — already reads the NEW one. That
    split is exactly the "it deployed but I don't see my change" confusion.

    `Cache-Control: no-cache` does not mean "don't cache"; it means "cache, but
    revalidate before use". Starlette already sends an ETag and Last-Modified and
    answers a conditional request with a 304, so an ordinary load is a tiny
    revalidation that transfers nothing when the file is unchanged — and the
    first load after a deploy sees the new ETag and gets the fresh file. Cheap,
    and no hard refresh required. setdefault so an explicit header (a 404's, say)
    is left alone."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response
from .settings import (CONFIG_PATH, ENV_VARS, KEY_PROVIDERS, PROVIDERS, Paths,
                       Settings, default_root, field_in_settings_file,
                       resource_root)
from .update import Rebuilder
from .watch.runner import WatchRunner


def _maybe_boot_restore(watch_home: Path, store, paths) -> None:
    """Rebuild the job list from the Drive archive when this machine boots with
    none of its own — the empty-volume disaster restore exists for.

    A fresh or recreated volume comes up with an empty jobs folder; if the
    archive is on, its manifests hold every record, so restore runs on a
    background thread and the list fills in before anyone notices. A machine that
    still has its jobs never triggers it (the folder is not empty), and a build
    with no archive configured does nothing. Best-effort: a restore that will not
    run must never stop the app from starting."""
    try:
        from .watch import archive
        if not archive.wants_boot_restore(watch_home, paths):
            return
        import threading
        log.info("No local jobs on boot; rebuilding from the Drive archive.")
        threading.Thread(
            target=lambda: archive.restore(watch_home, store),
            name="docproof-boot-restore", daemon=True).start()
    except Exception:                         # noqa: BLE001 - never block startup
        log.exception("Boot-time archive restore could not start")


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
    if web:
        # On a server there is no user Documents folder and no durable results
        # location off the mounted volume: finished documents written anywhere
        # else land on the container's throwaway filesystem and vanish on the
        # next redeploy or restart — which here is several times a day — while
        # the job records that point at them survive on the volume, so the
        # results tab 404s "…is missing". So the web build ALWAYS keeps results
        # on the volume beside the job records, whatever settings.json says.
        #
        # Not merely "unless an administrator chose one": a persisted output_dir
        # is the data-loss bug, not a preference. The Settings screen
        # round-trips the field on every save, so any save made while it read
        # the desktop default (an older build that still showed the field on the
        # web, a direct API call) pins the ephemeral path permanently — and the
        # old "respect a saved value" guard then stepped aside for exactly the
        # value that loses documents. Clamp it, and say so when overriding one.
        on_volume = str(paths.results)
        if settings.output_dir != on_volume:
            if field_in_settings_file(paths, "output_dir"):
                log.warning(
                    "Web build: overriding persisted output_dir %r with the "
                    "volume path %r so finished documents survive a redeploy.",
                    settings.output_dir, on_volume)
            settings.output_dir = on_volume
    store = JobStore(paths)
    # The watcher's home holds the one email address and Google sign-in every
    # pipeline's completion mail goes through, so the app's runner is told where
    # it is: a finished app job emails the same log a watched book does.
    wh = watch_home or watch_home_for(paths.root)
    runner = JobRunner(store, settings, config_path=CONFIG_PATH,
                       poll_seconds=poll_seconds, notify_home=wh)
    # Deliberately not given the watch home's lock here. The app claims its own
    # folder for as long as it is open; the watcher's is claimed only for the
    # length of a pass, because a scheduled run started by macOS has to be able
    # to take it while a window is open.
    watch = WatchRunner(wh)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_runner:
            runner.start()
            watch.start()
            _maybe_boot_restore(wh, store, paths)
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
    # The web build's in-browser Google sign-in parks its one pending request
    # here between the consent redirect and Google's callback. One shared slot,
    # because the watcher is one shared identity. Desktop never touches it.
    app.state.watch_auth = None

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
        # KEY_PROVIDERS, not PROVIDERS: Sapling's key is set and stored in the
        # portal like a review provider's, even though it never reviews anything.
        app.state.env_keys = {p: os.environ.get(ENV_VARS[p]) for p in KEY_PROVIDERS}
        for provider in KEY_PROVIDERS:
            stored = keystore.get(provider)
            if stored:
                os.environ[ENV_VARS[provider]] = stored
        # The Drive watcher's refresh token is kept like a provider key but is
        # not one of the review PROVIDERS, so it is loaded on its own: whatever
        # a person signed in for through DocWatch lives in the keystore and must
        # be back in the environment after a redeploy, and the boot env value is
        # remembered so forgetting a keystore token falls back to a fly secret.
        from .watch.settings import GOOGLE_KEY
        app.state.google_env = os.environ.get(ENV_VARS[GOOGLE_KEY])
        stored_google = keystore.get(GOOGLE_KEY)
        if stored_google:
            os.environ[ENV_VARS[GOOGLE_KEY]] = stored_google
        # God Mode: the admin-only routes for managing users, caps and keys.
        # Only the web build has users to manage, so only it registers these.
        routes.register_admin(app)
    static = resource_root() / "app" / "static"
    if static.is_dir():
        app.mount("/", FreshStaticFiles(directory=static, html=True), name="static")
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
