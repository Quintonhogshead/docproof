"""`docproof-serve` — run the hosted, multi-user web build.

This is what a server runs, and it is deliberately not `app.run`: that one is
the desktop launcher — it binds to localhost, opens a browser, and has no
sign-in. This binds to 0.0.0.0 for a proxy in front, opens nothing, and turns
the login gate on.

Put HTTPS in front of it (a reverse proxy or the platform's own), and never
expose this port to the internet directly. It refuses to start without a
session secret or any API key: a web build missing the first signs nobody in
safely, and missing the second can review nothing — either way, failing at
boot is kinder than failing at eleven at night.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .auth import SESSION_ENV, resolve_secret
from .lock import FolderInUse
from .main import create_app
from .settings import PROVIDERS, key_status

log = logging.getLogger("docproof.serve")


def _has_any_key() -> bool:
    return any(s.get("configured") for s in key_status().values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="docproof-serve",
        description="Run the hosted, multi-user DocProof web app.")
    ap.add_argument("--home", help="where accounts, jobs and settings live "
                                   "(default: DOCPROOF_HOME)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to bind (default 0.0.0.0, for a proxy)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8000")),
                    help="port to bind (default: $PORT, else 8000)")
    ap.add_argument("--insecure-cookies", action="store_true",
                    help="allow the session cookie over plain HTTP — only for "
                         "local testing behind no TLS")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    # Fail fast, and say exactly what to set, rather than starting a server that
    # can't sign anyone in.
    try:
        resolve_secret()
    except RuntimeError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2
    if not _has_any_key():
        print(f"\nNo API key is set. Give the server at least one of "
              f"{', '.join(p.upper() + '_API_KEY' for p in PROVIDERS)} so it "
              f"can run reviews.\n", file=sys.stderr)
        return 2

    home = Path(args.home).expanduser() if args.home else None
    try:
        app = create_app(home, web=True, https_only=not args.insecure_cookies)
    except FolderInUse as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 2

    log.info("DocProof (web) listening on %s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
