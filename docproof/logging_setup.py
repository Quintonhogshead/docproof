from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "run.log"

    root = logging.getLogger("docproof")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    root.addHandler(fh)
    root.addHandler(ch)
    # The galley package logs under its own name; without this its lines
    # (verify progress, settle rounds) never reached run.log or the console
    # and surfaced only through Python's last-resort stderr handler.
    gal = logging.getLogger("galley")
    gal.setLevel(logging.DEBUG)
    gal.handlers.clear()
    gal.addHandler(fh)
    gal.addHandler(ch)
    return log_path