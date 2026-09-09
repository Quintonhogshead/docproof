#!/usr/bin/env python3
"""Package an author website's five design options into one offline HTML file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from docproof.website.share import shareable_html


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    demo = REPO / "config/website/demo"
    parser.add_argument("--spec", type=Path, default=demo / "site.json")
    parser.add_argument("--assets", type=Path, default=demo / "assets.json")
    parser.add_argument("--asset-dir", type=Path, default=demo / "assets")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text("utf-8"))
    assets = json.loads(args.assets.read_text("utf-8"))
    if isinstance(assets, dict):
        assets = assets["assets"]
    paths = {}
    for asset in assets:
        candidate = (args.asset_dir / asset["filename"]).resolve()
        if not candidate.is_relative_to(args.asset_dir.resolve()):
            raise ValueError("Image paths must stay inside the supplied asset directory.")
        paths[asset["id"]] = candidate
    html = shareable_html(spec, assets=assets, asset_paths=paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"{args.output.resolve()} ({args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
