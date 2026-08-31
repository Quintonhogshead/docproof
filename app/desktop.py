"""DocProof as a Mac application.

The app is already a local HTTP server with a static frontend, so the desktop
build is a native window pointed at it rather than a second implementation.
uvicorn runs on a background thread; pywebview owns the main thread, because
macOS requires the UI to be there.

Closing the window ends the process: the ticker and the worker are daemon
threads, and any batch already submitted is safe on disk — the next launch
finds it by its manifest.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from .lock import FolderInUse, describe_owner
from .main import create_app
from .settings import Paths, default_root

log = logging.getLogger("docproof.app.desktop")

TITLE = "DocProof"
WINDOW = (1100, 760)
MIN_WINDOW = (820, 600)
# How long to wait for uvicorn before showing the window anyway. The window
# would just render a connection error, which is worse than a moment's wait.
STARTUP_TIMEOUT = 15.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_file(root: Path) -> Path:
    return root / "desktop.port"


def running_instance(root: Path) -> str | None:
    """The URL of a DocProof already serving this app home, if there is one.

    Two copies sharing one job directory would poll the same batches and race
    over the same manifests, so the second launch attaches to the first
    instead of starting its own server."""
    path = _port_file(root)
    if not path.is_file():
        return None
    try:
        url = f"http://127.0.0.1:{int(path.read_text('utf-8').strip())}"
    except (OSError, ValueError):
        return None
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=0.5) as resp:
            if resp.status == 200:
                return url
    except (urllib.error.URLError, OSError, TimeoutError):
        pass                                  # stale file from a hard quit
    path.unlink(missing_ok=True)
    return None


def wait_until_serving(url: str, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.05)
    return False


def _in_use_html(error: FolderInUse) -> str:
    """The refusal, as a page — a double-clicked .app has no terminal, and a
    window that never appears is indistinguishable from a broken app."""
    detail = html.escape(describe_owner(error.owner))
    return f"""
<style>
  body {{ font: 15px/1.6 -apple-system, sans-serif; margin: 0;
          padding: 2rem; color: #1c1a17; background: #fbf9f6; }}
  h1 {{ font-size: 1.2rem; margin: 0 0 .8rem; }}
  code {{ background: #f5ece1; padding: .1rem .35rem; border-radius: 5px; }}
  p {{ margin: 0 0 .9rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #ece7e0; background: #171513; }}
    code {{ background: #2b2520; }}
  }}
</style>
<h1>DocProof is already running</h1>
<p>{detail}, using the folder DocProof keeps its work in:</p>
<p><code>{html.escape(str(error.owner.get("root", "")
                         or "~/Library/Application Support/DocProof"))}</code></p>
<p>Two copies sharing that folder review the same documents twice and write
over each other's results, so this one stopped instead.</p>
<p>Quit the other copy, then open DocProof again.</p>
"""


def serve(app, port: int) -> uvicorn.Server:
    """Run uvicorn on a daemon thread and hand back the server object."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="docproof-http",
                     daemon=True).start()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="docproof-desktop")
    ap.add_argument("--home", help="where jobs and settings live "
                                   "(default: DOCPROOF_HOME, else "
                                   "~/Library/Application Support/DocProof)")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    import webview                            # deferred: heavy, and optional

    root = Path(args.home).expanduser() if args.home else default_root()
    paths = Paths(root).ensure()

    # The Covers pages (Cover Studio + Cover Canvas) ride this window too —
    # one app, one press. Same two defaults app/canvas_desktop.py sets, for
    # the same reasons: the key gate needs A key on a loopback socket with
    # one owner (LOCAL_KEY, typed into the unlock box once per session), and
    # the job store must not default cwd-relative — a Finder-launched .app
    # starts life at "/", an unwritable store that reads as forever empty.
    if not os.environ.get("COVER_KEY"):
        from .canvas_desktop import LOCAL_KEY
        os.environ["COVER_KEY"] = LOCAL_KEY
    if not os.environ.get("COVER_DATA_PATH"):
        os.environ["COVER_DATA_PATH"] = str(root / "cover_jobs")

    existing = running_instance(root)
    if existing:
        log.info("DocProof is already running; opening another window on %s",
                 existing)
        webview.create_window(TITLE, existing, width=WINDOW[0],
                              height=WINDOW[1], min_size=MIN_WINDOW)
        webview.start()
        return 0

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    try:
        app = create_app(root)
    except FolderInUse as e:
        # The port file said nobody was here, but the folder is claimed — so
        # the holder is something this launch cannot attach a window to,
        # usually a copy started from a checkout. Nothing has a terminal to
        # print to, so it has to be said on screen.
        log.error("%s", e)
        webview.create_window(TITLE, html=_in_use_html(e), width=560,
                              height=320)
        webview.start()
        return 1
    server = serve(app, port)
    _port_file(root).write_text(str(port), encoding="utf-8")

    try:
        if not wait_until_serving(url):
            log.error("DocProof did not start listening on %s in %.0fs.",
                      url, STARTUP_TIMEOUT)
            return 1
        log.info("DocProof is running at %s (documents in %s)", url, paths.root)
        webview.create_window(TITLE, url, width=WINDOW[0], height=WINDOW[1],
                              min_size=MIN_WINDOW)
        webview.start()                       # blocks until the window closes
    finally:
        _port_file(root).unlink(missing_ok=True)
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
