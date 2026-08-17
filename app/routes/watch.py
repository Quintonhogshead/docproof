"""The watched Drive folder's panel.

Every route here answers with the whole panel, the way `POST /api/tick` does:
one round trip does the thing and brings back what the screen needs to redraw,
so the page never has to guess what a change did.

This imports the watcher, but never its CLI: `app.watch.cli._logging` clears
the docproof logger's handlers, which for a long-lived server means it stops
logging.
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from docproof.providers import lookup

from . import common
from .. import settings as settingslib
from ..settings import ENV_VARS
from ..watch import auth as authlib
from ..watch import daily as dailylib
from ..watch import schedule as schedulelib
from ..watch import status as watchlib
from ..watch.drive import DriveError
from ..watch.runner import WatchRunner
from ..watch.settings import GOOGLE_KEY, WatchSettings, folder_id_from
from ..watch.tick import NotConfigured


class WatchUpdate(BaseModel):
    """Partial, like SettingsUpdate: the panel sends what changed."""

    # An address pasted out of a browser, or a bare id. Either is fine.
    folder: str | None = None
    model: str | None = None
    prep_output: str | None = None       # indesign | tracked | both
    upload_notes: bool | None = None
    upload_failure_note: bool | None = None
    # Subfolder mode's one book-to-book knob (the mode itself is CLI-set):
    # prepare only "<surname> - Book Original" in each author's folder.
    require_source_label: bool | None = None
    # Bounds so a slip in the UI cannot spend a morning's worth of manuscripts
    # in one pass, or set a clock that never stops going off.
    max_files_per_tick: int | None = Field(default=None, ge=1, le=50)
    auto_ticks: bool | None = None
    tick_every_minutes: int | None = Field(default=None, ge=5, le=1440)
    # Fixed times of day for the in-app clock, as ["HH:MM", …], and the zone
    # they are read in. An empty list turns the fixed-times clock off and hands
    # the interval above back its job; a missing field leaves both untouched.
    tick_at_times: list[str] | None = None
    tick_timezone: str | None = None
    # The Drive output archive. A separate folder box (an address or a bare id,
    # parsed like the watched folder), a switch, and whether to keep the source.
    archive_enabled: bool | None = None
    archive_folder: str | None = None
    archive_include_source: bool | None = None


class WatchAuth(BaseModel):
    """The OAuth client to sign in with. Blank means "use the saved one"."""

    client_id: str | None = None
    client_secret: str | None = None


class WatchSchedule(BaseModel):
    times: str = ",".join(schedulelib.DEFAULT_TIMES)


def register(app: FastAPI) -> None:

    # One shared Drive watcher for the whole server, so on the web build only an
    # administrator manages it — the same gate the shared prompts and style
    # guide use. The desktop build has one user and no gate, so it passes.
    may_manage = common.admin_gate(
        app, "Only an administrator can manage DocWatch.")

    def callback_uri(request: Request) -> str:
        """Where Google sends the browser back to, on this same server.

        Built from the Host header rather than `request.base_url` because the
        server sits behind a TLS-terminating proxy it does not read forwarded
        headers from, so `base_url` can say http; the redirect Google is handed
        must be the https address a person actually reached. Only a local host,
        used for a dev run, is left on http."""
        host = request.headers.get("host") or request.url.netloc
        local = host.split(":")[0] in ("localhost", "127.0.0.1")
        scheme = "http" if local else "https"
        return f"{scheme}://{host}/api/watch/auth/callback"

    def store_google_token(token: str) -> None:
        """Keep the refresh token the way the web build keeps every other key:
        in the volume's keystore and live in the environment at once, where
        `get_api_key` already looks first. The Mac Keychain is not on a Linux
        server, so `set_api_key` is not the road here."""
        app.state.keystore.set(GOOGLE_KEY, token)
        os.environ[ENV_VARS[GOOGLE_KEY]] = token

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

    @app.get("/api/watch", dependencies=[Depends(may_manage)])
    def read_watch() -> dict:
        return watch_payload()

    @app.put("/api/watch", dependencies=[Depends(may_manage)])
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
            if update.prep_output not in ("book", "indesign", "tracked",
                                          "both", "all"):
                raise HTTPException(400, "prep_output must be 'book', 'indesign', "
                                         "'tracked', 'both' or 'all'")
            ws.prep_output = update.prep_output
        # The archive folder is pasted the same way the watched one is: an empty
        # box means "unchanged", so a person can flip the switch without
        # re-pasting the address every time.
        if update.archive_folder:
            try:
                ws.archive_folder_id = folder_id_from(update.archive_folder)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        # The times are normalised on the way in — parsed, padded, deduped and
        # sorted — so the panel gets back a clean list whatever a person typed,
        # and both clocks read the same shape. An empty list is a real value: it
        # turns the fixed-times clock off, so it is set rather than skipped.
        if update.tick_at_times is not None:
            try:
                times = (schedulelib.parse_times(update.tick_at_times)
                         if update.tick_at_times else [])
            except schedulelib.ScheduleError as e:
                raise HTTPException(400, str(e)) from None
            ws.tick_at_times = [f"{h:02d}:{m:02d}" for h, m in times]
        if update.tick_timezone is not None:
            tz = update.tick_timezone.strip()
            if tz:
                try:
                    dailylib.zone(tz)         # validate; the value is the name
                except schedulelib.ScheduleError as e:
                    raise HTTPException(400, str(e)) from None
            ws.tick_timezone = tz
        for name in ("upload_notes", "upload_failure_note",
                     "require_source_label",
                     "max_files_per_tick", "auto_ticks", "tick_every_minutes",
                     "archive_enabled", "archive_include_source"):
            value = getattr(update, name)
            if value is not None:
                setattr(ws, name, value)
        ws.save(watch.home)
        return watch_payload()

    @app.get("/api/watch/auth", dependencies=[Depends(may_manage)])
    def read_watch_auth() -> dict:
        return watch_payload()

    @app.post("/api/watch/auth", dependencies=[Depends(may_manage)])
    def start_watch_auth(body: WatchAuth, request: Request) -> dict:
        """Begin signing in to Google.

        Two roads, because the two builds sit in different places. The desktop
        app opens the consent page in the real browser and catches the answer
        on a loopback listener (Google refuses OAuth inside an embedded web
        view, and a password belongs in Google's page anyway). The web build
        has no browser to open on the server and no loopback a person can reach,
        so it hands the page back for the browser already on the screen to go
        to, and Google returns the answer to `/api/watch/auth/callback`."""
        watch: WatchRunner = app.state.watch
        ws = WatchSettings.load(watch.home)
        client_id = (body.client_id or ws.client_id or "").strip()
        client_secret = (body.client_secret or ws.client_secret or "").strip()
        if not client_id or not client_secret:
            raise HTTPException(400,
                                "Signing in needs a Google OAuth client. "
                                "docs/watch.md walks through the five minutes "
                                "in the Google Cloud console that makes one.")
        if app.state.web:
            redirect_uri = callback_uri(request)
            state, url = authlib.web_consent(client_id, redirect_uri)
            # Saved now so the callback — a bare GET from Google, carrying no
            # body — has the client to finish with. Unlike the desktop flow this
            # is safe before the token exists: the panel reads "signed in" from
            # the token, never from a saved client.
            ws.client_id, ws.client_secret = client_id, client_secret
            ws.save(watch.home)
            app.state.watch_auth = {"state": state, "client_id": client_id,
                                    "client_secret": client_secret,
                                    "redirect_uri": redirect_uri}
            return {"consent_url": url, **watch_payload()}
        watch.begin_sign_in(client_id, client_secret)
        return watch_payload()

    @app.get("/api/watch/auth/callback", dependencies=[Depends(may_manage)])
    def finish_watch_auth(request: Request) -> RedirectResponse:
        """Where Google returns the browser after the consent page.

        A redirect back to the panel either way — a person is looking at a
        browser, not a JSON response. The pending sign-in is single-use: it is
        cleared whether the exchange worked or not, so a stale tab cannot replay
        it. Web build only; the desktop app never registers this address."""
        def back(error: str = "") -> RedirectResponse:
            target = ("/?watch_auth=error&msg=" + quote(error) + "#watch"
                      if error else "/#watch")
            return RedirectResponse(target, status_code=303)

        pending = getattr(app.state, "watch_auth", None)
        app.state.watch_auth = None
        if not pending:
            return back("This sign-in expired before it finished. "
                        "Start it again.")
        try:
            code = authlib.parse_redirect(str(request.url), pending["state"])
            token = authlib.exchange_code(
                pending["client_id"], pending["client_secret"], code,
                pending["redirect_uri"])
        except DriveError as e:
            return back(str(e))
        store_google_token(token)
        return back()

    @app.delete("/api/watch/auth", dependencies=[Depends(may_manage)])
    def forget_watch_auth() -> dict:
        """Forget the sign-in, keep the client.

        The client id and secret are not the secret — an installed application
        cannot keep one — and signing in again needs them. On the web build the
        token is a key in the volume's keystore, so it is cleared there and in
        the environment; a token the server was given as an env secret at boot
        comes back, the way removing a portal provider key does."""
        if app.state.web:
            app.state.keystore.delete(GOOGLE_KEY)
            restore = getattr(app.state, "google_env", None)
            if restore:
                os.environ[ENV_VARS[GOOGLE_KEY]] = restore
            else:
                os.environ.pop(ENV_VARS[GOOGLE_KEY], None)
        else:
            settingslib.delete_api_key("google")
        return watch_payload()

    @app.post("/api/watch/run", dependencies=[Depends(may_manage)])
    def run_watch() -> dict:
        watch: WatchRunner = app.state.watch
        watch_needs(WatchSettings.load(watch.home))
        started = watch.run_now()
        # Not an error when it is already going. The button is disabled while a
        # pass runs, so this is only ever a double click, and answering "it is
        # already doing what you asked" in red would be the wrong noise.
        return {"started": started, **watch_payload()}

    @app.post("/api/watch/preview", dependencies=[Depends(may_manage)])
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

    @app.put("/api/watch/schedule", dependencies=[Depends(may_manage)])
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

    @app.delete("/api/watch/schedule", dependencies=[Depends(may_manage)])
    def clear_watch_schedule() -> dict:
        watch: WatchRunner = app.state.watch
        if sys.platform != "darwin":
            raise HTTPException(501, "Looking while DocProof is closed needs "
                                     "a Mac.")
        schedulelib.uninstall(path=watch.agent_path,
                              **({"run": watch.launchctl}
                                 if watch.launchctl else {}))
        return watch_payload()
