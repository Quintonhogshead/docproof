#!/usr/bin/env python
"""Cover Canvas as a plain local server (no pywebview, no autoupdate).

app/canvas_desktop.py is the packaged Mac window; this is the same FastAPI app
without the native shell, so the editor can be opened in an ordinary browser
tab and driven headlessly. Same key gate, same job store pinning.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--jobs", default=str(
        Path.home() / "Desktop/Longsword-Covers/_canvas_jobs"))
    ap.add_argument("--key", default="canvas")
    args = ap.parse_args()

    os.environ["COVER_DATA_PATH"] = args.jobs
    os.environ["COVER_KEY"] = args.key

    from app.canvas_desktop import build_shell_app
    from app.settings import default_root

    app = build_shell_app(default_root())
    print(f"cover key: {args.key}   jobs: {args.jobs}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
