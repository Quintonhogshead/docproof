"""Cover Canvas as a Mac application.

The same shape as app/desktop.py, and for the same reason: the editor is
already a local HTTP server with a static front-end, so the desktop build is
a native window pointed at it rather than a second implementation. uvicorn
runs on a background thread (app.desktop.serve, reused verbatim); pywebview
owns the main thread, because macOS requires the UI to be there.

Three deliberate differences from the DocProof window:

- **No job runner, no folder lock.** `create_app(start_runner=False)` — the
  canvas never reviews a manuscript, and claiming the DocProof home would
  stop a real DocProof window from opening beside it. Nothing here writes
  anything outside a cover job directory.
- **The cover key is defaulted, not demanded.** Every /api/canvas endpoint
  is behind Cover Studio's key gate (app/routes/cover.py:_gate), which on a
  public deployment is the whole point — but this window binds to
  127.0.0.1, has one owner, and the front-end asks for the key before it
  will list a single job. So a shell with no COVER_KEY in the environment
  gets LOCAL_KEY, logged on startup, to be typed into the unlock box once
  per session. A deployment that sets its own key keeps it.
- **`--job` opens straight into a cover.** The editor reads `?job=<id>` from
  its own URL, so the shell can start on a specific cover instead of the
  picker.

Closing the window ends the process; the canvas document is saved after
every op batch, so there is nothing in memory to lose.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from urllib.parse import quote

from docproof.cover import pipeline as cover_pipeline

from .desktop import free_port, serve, wait_until_serving
from .main import create_app
from .routes import cover as cover_routes
from .settings import default_root

log = logging.getLogger("docproof.app.canvas_desktop")

TITLE = "Cover Canvas"
WINDOW = (1500, 950)
MIN_WINDOW = (1100, 720)

# The key this shell falls back to when the environment names none. Fixed
# rather than random on purpose: it is typed by hand into the unlock box, it
# only ever guards a loopback socket on the owner's own machine, and a fresh
# secret every launch would be a password prompt with no password manager.
LOCAL_KEY = "canvas"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cover-canvas")
    ap.add_argument("--home", help="the DocProof app home the server is built "
                                   "against (default: DOCPROOF_HOME, else "
                                   "~/Library/Application Support/DocProof)")
    ap.add_argument("--jobs", help="where cover jobs live (default: "
                                   "COVER_DATA_PATH, else ./cover_jobs)")
    ap.add_argument("--job", help="open this cover job id straight away")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    import webview                            # deferred: heavy, and optional

    if args.jobs:
        # Read back out of the environment by cover_pipeline.default_root()
        # below — one answer for the job store, however it was chosen.
        os.environ["COVER_DATA_PATH"] = str(Path(args.jobs).expanduser())
    key = os.environ.get("COVER_KEY")
    if not key:
        os.environ["COVER_KEY"] = key = LOCAL_KEY

    root = Path(args.home).expanduser() if args.home else default_root()
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"

    app = create_app(root, start_runner=False)
    # The main app registers canvas but deliberately not Cover Studio's own
    # routes (those are the quest site's). The shell wants both: the picker
    # lists jobs via /api/cover/jobs, and a person at this window should be
    # able to roll a brand-new cover without opening the quest site.
    cover_routes.register(app)
    # Pinned once here, the way app/quest_site.py pins it: the routes read
    # app.state rather than the environment per request, so a --jobs given on
    # the command line cannot drift from the store the window is showing.
    app.state.cover_data_root = cover_pipeline.default_root()
    server = serve(app, port)

    try:
        if not wait_until_serving(url):
            log.error("Cover Canvas did not start listening on %s.", url)
            return 1
        log.info("Cover Canvas is running at %s (cover jobs in %s)",
                 url, app.state.cover_data_root)
        log.info("Cover key for this window: %s", key)
        target = f"{url}/canvas"
        if args.job:
            target += f"?job={quote(args.job)}"
        webview.create_window(TITLE, target, width=WINDOW[0], height=WINDOW[1],
                              min_size=MIN_WINDOW)
        webview.start()                       # blocks until the window closes
    finally:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
