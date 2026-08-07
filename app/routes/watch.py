"""The watched Drive folder's panel.

Every route here answers with the whole panel, the way `POST /api/tick` does:
one round trip does the thing and brings back what the screen needs to redraw,
so the page never has to guess what a change did.

This imports the watcher, but never its CLI: `app.watch.cli._logging` clears
the docproof logger's handlers, which for a long-lived server means it stops
logging.
"""
from __future__ import annotations

import sys
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from docproof.providers import lookup

from .. import settings as settingslib
from ..watch import schedule as schedulelib
from ..watch import status as watchlib
from ..watch.drive import DriveError
from ..watch.runner import WatchRunner
from ..watch.settings import WatchSettings, folder_id_from
from ..watch.tick import NotConfigured


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


def register(app: FastAPI) -> None:

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
        settingslib.delete_api_key("google")
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
