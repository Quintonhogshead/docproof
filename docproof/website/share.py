"""A one-file, offline design review containing only public website content."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .export import ExportError
from .models import Asset, SiteSpec
from .render import TEMPLATES, build_pages


THEME_LABELS = {
    "literary-journal": ("Literary Journal", "Warm, editorial, considered"),
    "midnight-narrative": ("Midnight Narrative", "Dark fantasy, gold and electric blue"),
    "public-voice": ("Public Voice", "Confident, clear, author focused"),
    "cover-gallery": ("Cover Gallery", "Visual, collected, expansive"),
    "storybook-studio": ("Storybook Studio", "Bright, refined, imaginative"),
}


def shareable_html(spec: SiteSpec | dict[str, Any], *,
                   assets: Sequence[Asset | dict[str, Any]] = (),
                   asset_paths: Mapping[str, str | Path] | None = None,
                   shell_path: str | Path | None = None) -> str:
    """Embed twenty rendered pages and one copy of each referenced image.

    The shell receives complete public HTML, never the CRM record, source
    manuscript, questionnaire, local image paths, or provider configuration.
    Image tokens are resolved in the browser so repeated pages do not repeat
    base64 images in the file sent to a reviewer.
    """
    public = spec if isinstance(spec, SiteSpec) else SiteSpec.model_validate(spec)
    rows = [a if isinstance(a, Asset) else Asset.model_validate(a) for a in assets]
    registry = {a.id: a for a in rows}
    if len(registry) != len(rows):
        raise ExportError("Duplicate asset IDs in the design review.")
    required = {public.author.portrait_asset_id}
    required.update(book.cover_asset_id for book in public.books)
    required.discard("")
    paths = asset_paths or {}
    embedded = {}
    for asset_id in sorted(required):
        asset = registry.get(asset_id)
        if not asset or not asset.approved:
            raise ExportError(f"Referenced asset {asset_id!r} is missing or unapproved.")
        if asset_id not in paths:
            raise ExportError(f"Asset {asset_id!r} needs a local source file.")
        source = Path(paths[asset_id])
        if not source.is_file():
            raise ExportError(f"Local source for asset {asset_id!r} is not a file.")
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != asset.sha256:
            raise ExportError(f"Local source for asset {asset_id!r} does not match its recorded hash.")
        embedded[asset_id] = f"data:{asset.media_type};base64," + base64.b64encode(body).decode("ascii")

    data = {
        "author": public.author.name,
        "book_title": public.books[0].title if public.books else "",
        "default_template": public.template_id,
        "templates": [dict(id=t, name=THEME_LABELS[t][0], description=THEME_LABELS[t][1])
                      for t in TEMPLATES],
        "pages": {t: build_pages(public.model_copy(update={"template_id": t}),
                                 assets=[registry[a].model_dump() for a in sorted(required)],
                                 preview=True)
                  for t in TEMPLATES},
        "assets": embedded,
    }
    # A user's text must not be able to end the inert JSON script element.
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    shell = Path(shell_path) if shell_path else (
        Path(__file__).resolve().parents[2] / "app/static/websites/shareable-gallery.html")
    html = shell.read_text(encoding="utf-8")
    if html.count("__DOCPROOF_SITE_DATA__") != 1:
        raise ExportError("The design review shell must have exactly one content slot.")
    return html.replace("__DOCPROOF_SITE_DATA__", encoded)
