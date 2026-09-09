#!/usr/bin/env python3
"""Export a portable four-page author website and a five-design local gallery.

No AI, HubSpot, WordPress, or hosting credentials are needed for the bundled
fictional demo. Pass --spec and --assets to preview another public SiteSpec.
"""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from docproof.website.models import SiteSpec
from docproof.website.render import TEMPLATES
from docproof.website.export import export_site


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    demo = REPO / "config/website/demo"
    parser.add_argument("--spec", type=Path, default=demo / "site.json", help="Public SiteSpec JSON")
    parser.add_argument("--assets", type=Path, default=demo / "assets.json", help="Approved image metadata JSON")
    parser.add_argument("--asset-dir", type=Path, default=demo / "assets", help="Directory containing approved image files")
    parser.add_argument("--output", type=Path, default=REPO / "output/website-preview")
    args = parser.parse_args(argv)
    spec = SiteSpec.model_validate_json(args.spec.read_text("utf-8"))
    assets = json.loads(args.assets.read_text("utf-8"))
    if isinstance(assets, dict):
        assets = assets["assets"]
    paths = {}
    for asset in assets:
        candidate = (args.asset_dir / asset["filename"]).resolve()
        if not candidate.is_relative_to(args.asset_dir.resolve()):
            raise ValueError("Asset paths must remain inside the supplied image directory.")
        paths[asset["id"]] = candidate
    for template in TEMPLATES:
        chosen = spec.model_copy(update={"template_id": template})
        export_site(chosen, args.output, assets=assets, asset_paths=paths, release_id="local-demo-1")
    gallery = REPO / "app/static/websites"
    args.output.mkdir(parents=True, exist_ok=True)
    gallery_html = (gallery / "gallery.html").read_text("utf-8")
    gallery_html = gallery_html.replace(
        "__DOCPROOF_AUTHOR_NAME__", escape(spec.author.name, quote=True))
    gallery_html = gallery_html.replace("__DOCPROOF_TEMPLATE_ID__", spec.template_id)
    (args.output / "gallery.html").write_text(gallery_html, encoding="utf-8")
    for name in ("gallery.css", "gallery.js"):
        shutil.copy2(gallery / name, args.output / name)
    shutil.copy2(args.output / "gallery.html", args.output / "index.html")
    is_bundled_demo = args.spec.resolve() == (demo / "site.json").resolve()
    if is_bundled_demo:
        source_note = (
            "The bundled Mara Ellison author, books, biographies and covers are "
            "fictional demo material.\n"
            "hello@example.com is an illustrative address and is not a working author inbox.\n"
            "No HubSpot records were read and nothing was published to a hosting account.\n"
        )
    else:
        source_note = (
            f"This preview was generated from the supplied public SiteSpec for "
            f"{spec.author.name}.\n"
            "Only the supplied SiteSpec, approved asset metadata, and local asset files were used.\n"
            "This export is local; nothing was published to a hosting account.\n"
        )
    (args.output / "README.txt").write_text(
        "DocProof — Author Website Preview\n\n"
        "Open index.html in a browser. Pick one of five designs, then Home, About, Books or Contact.\n"
        "Use Mobile to inspect the narrow layout. Open this page shows a full-size individual site.\n\n"
        f"{source_note}\n"
        "All files are self-contained. Keep the folders beside index.html when moving this preview.\n",
        encoding="utf-8")
    print(args.output.resolve() / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
