"""Write a deterministic Website Studio preview that opens directly from disk.

This is intentionally a tiny final-mile adapter. It calls the same renderer as
staff preview and WordPress staging, then copies only approved, hash-verified
public assets into a portable directory tree. It never reads a source bundle,
manuscript, prompt, or questionnaire.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from .models import Asset, SiteSpec
from .render import PAGES, build_pages


class ExportError(ValueError):
    """A local artifact cannot be made safely or completely."""


@dataclass(frozen=True)
class ExportResult:
    root: Path
    pages: dict[str, Path]
    assets: dict[str, Path]


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ExportError("Export path escapes its selected output directory.") from exc
    return resolved


def _asset_rows(assets: Sequence[Asset | dict[str, Any]]) -> list[Asset]:
    rows: list[Asset] = []
    seen: set[str] = set()
    for value in assets:
        asset = value if isinstance(value, Asset) else Asset.model_validate(value)
        if asset.id in seen:
            raise ExportError(f"Duplicate asset ID {asset.id!r}.")
        seen.add(asset.id)
        rows.append(asset)
    return rows


def _required_asset_ids(spec: SiteSpec) -> set[str]:
    ids = {spec.author.portrait_asset_id}
    ids.update(book.cover_asset_id for book in spec.books)
    return {asset_id for asset_id in ids if asset_id}


def _copy_asset(asset: Asset, source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ExportError(f"Local source for asset {asset.id!r} is not a file.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != asset.sha256:
        raise ExportError(f"Local source for asset {asset.id!r} does not match its recorded hash.")
    # The asset ID passed the strict token contract; this also guards future
    # alternate model implementations.
    if destination.name != asset.id:
        raise ExportError("Unsafe export asset destination.")
    shutil.copyfile(source, destination)


def _direct_file_links(html: str) -> str:
    """Directory URLs work over HTTP; file viewers need index.html spelled out."""
    for page in PAGES:
        html = html.replace(f'href="../{page}"', f'href="../{page}/index.html"')
    return html


def export_site(spec: SiteSpec | dict[str, Any], output_dir: str | Path, *,
                assets: Sequence[Asset | dict[str, Any]] = (),
                asset_paths: Mapping[str, str | Path] | None = None,
                release_id: str = "") -> ExportResult:
    """Export one self-contained template directory for direct local viewing.

    Output is {output_dir}/{template}/{home,about,books,contact}/index.html
    plus {output_dir}/{template}/assets/{asset_id}. asset_paths maps approved
    asset IDs to local source files. A referenced public asset without a local,
    matching file is an error rather than a broken standalone preview.
    """
    public_spec = spec if isinstance(spec, SiteSpec) else SiteSpec.model_validate(spec)
    rows = _asset_rows(assets)
    registry = {asset.id: asset for asset in rows}
    required = _required_asset_ids(public_spec)
    for asset_id in required:
        asset = registry.get(asset_id)
        if asset is None:
            raise ExportError(f"Referenced asset {asset_id!r} is missing from export metadata.")
        if not asset.approved:
            raise ExportError(f"Referenced asset {asset_id!r} is not approved for public export.")

    root = Path(output_dir).expanduser().resolve()
    if root == Path(root.anchor):
        raise ExportError("Choose a specific output directory, not a filesystem root.")
    template_root = _inside(root, root / public_spec.template_id)
    assets_root = _inside(template_root, template_root / "assets")
    assets_root.mkdir(parents=True, exist_ok=True)

    supplied = asset_paths if asset_paths is not None else {}
    copied: dict[str, Path] = {}
    for asset in rows:
        if not asset.approved:
            continue
        source_value = supplied.get(asset.id)
        if source_value is None:
            if asset.id in required:
                raise ExportError(f"Asset {asset.id!r} needs a local source file for standalone export.")
            continue
        destination = _inside(assets_root, assets_root / asset.id)
        _copy_asset(asset, Path(source_value).expanduser(), destination)
        copied[asset.id] = destination

    rendered = build_pages(public_spec, assets=[asset.model_dump() for asset in rows],
                           asset_base="../assets", base_url="..", preview=False,
                           release_id=release_id)
    pages: dict[str, Path] = {}
    for page, html in rendered.items():
        folder = _inside(template_root, template_root / page)
        folder.mkdir(parents=True, exist_ok=True)
        path = _inside(folder, folder / "index.html")
        path.write_text(_direct_file_links(html), encoding="utf-8")
        pages[page] = path
    return ExportResult(root=template_root, pages=pages, assets=copied)
