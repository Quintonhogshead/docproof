"""Which build this is, and the ways it can become a newer one."""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException

from docproof import __version__ as _docproof_version

from . import common
from .. import update as updatelib
from .. import version as versionlib


def register(app: FastAPI) -> None:

    @app.get("/api/version")
    def version() -> dict:
        return versionlib.build_info()

    @app.get("/api/version/check")
    def version_check() -> dict:
        """Whether a newer DocProof exists — the local checkout on the machine
        this was built on, the published releases anywhere else. Only ever
        called because somebody pressed the button; nothing here runs on its
        own."""
        return versionlib.check_for_update()

    @app.post("/api/version/update")
    def version_update() -> dict:
        """Install the newest release over this one and reopen.

        Only ever runs because somebody pressed the button the launch-time
        banner shows. It refuses — before downloading anything — while a
        document is being worked on, when running from the source, and when
        running straight off a disk image."""
        try:
            return updatelib.perform_update(app.state.runner)
        except updatelib.UpdateError as e:
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
        reason = updatelib.refuse_reason(app.state.runner)
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
        result = versionlib.download_release()
        if not result["ok"]:
            raise HTTPException(400, result["message"])
        if sys.platform == "darwin":
            common.open_path(Path(result["path"]), reveal=True)
        return result

    @app.get("/healthz")
    def healthz() -> dict:
        # version rides along here because this is the one route open before
        # sign-in, so the web build can show it on the login screen too.
        return {"ok": True, "version": _docproof_version}
