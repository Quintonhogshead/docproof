"""Start DocProof and open it in the default browser.

This is the entry point a packaged .app runs. It binds to 127.0.0.1 on a free
port — nothing is reachable from the network.
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .lock import FolderInUse
from .main import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="docproof-app")
    ap.add_argument("--home", help="where jobs and settings live "
                                   "(default: DOCPROOF_HOME, else "
                                   "~/Library/Application Support/DocProof)")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    try:
        app = create_app(Path(args.home).expanduser() if args.home else None)
    except FolderInUse as e:
        # A terminal is looking at this, so the message is the whole handling.
        print(f"\n{e}\n", file=sys.stderr)
        return 2

    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    print(f"DocProof is running at {url}\nPress Ctrl+C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
