from __future__ import annotations

import hashlib

import pytest

from docproof.website.export import ExportError, export_site


def _spec():
    return {
        "schema_version": 1, "renderer_version": "1.0", "template_id": "literary-journal",
        "author": {"name": "Ava Writer", "bio_short": "Ava writes books.", "bio": "Ava writes books.",
                   "portrait_asset_id": ""},
        "headline": "A story worth crossing for", "intro": "A quiet novel of courage.",
        "primary_cta": {"label": "Buy", "url": "https://books.example/buy"},
        "books": [{"id": "book-1", "title": "The Bridge", "subtitle": "", "description": "A novel.",
                   "hook": "Cross before dark.", "cover_asset_id": "cover-1",
                   "publication_date": "", "links": []}],
        "contact": {"email": "", "headline": "", "message": "", "links": []},
        "praise": [], "excerpt": "", "seo": {"description": "Ava Writer's novel."},
    }


def _asset(payload: bytes):
    return {"id": "cover-1", "filename": "cover.jpg", "media_type": "image/jpeg",
            "sha256": hashlib.sha256(payload).hexdigest(), "alt": "The cover",
            "approved": True, "focal_x": .5, "focal_y": .5}


def test_export_writes_four_direct_file_pages_and_hash_checked_assets(tmp_path):
    source = tmp_path / "source-cover.jpg"
    source.write_bytes(b"cover bytes")
    result = export_site(_spec(), tmp_path / "preview", assets=[_asset(b"cover bytes")],
                         asset_paths={"cover-1": source}, release_id="r-1")
    assert set(result.pages) == {"home", "about", "books", "contact"}
    assert all(path.is_file() for path in result.pages.values())
    assert result.assets["cover-1"].read_bytes() == b"cover bytes"
    assert result.assets["cover-1"] == tmp_path / "preview" / "literary-journal" / "assets" / "cover-1"
    home = result.pages["home"].read_text(encoding="utf-8")
    assert 'href="../about/index.html"' in home
    assert 'href="../home/index.html"' in home
    assert 'src="../assets/cover-1"' in home
    assert "source-cover.jpg" not in home
    # Every relative export URL opens from index.html without an HTTP directory
    # redirect. Assets live beside the four page directories in this template.
    assert (result.pages["home"].parent / "../assets/cover-1").resolve() == result.assets["cover-1"]
    for page, path in result.pages.items():
        for target in ("home", "about", "books", "contact"):
            assert f'href="../{target}/index.html"' in path.read_text(encoding="utf-8")
            assert (path.parent / f"../{target}/index.html").resolve() == result.pages[target]


def test_export_rejects_missing_or_changed_public_asset_source(tmp_path):
    asset = _asset(b"expected")
    with pytest.raises(ExportError, match="needs a local"):
        export_site(_spec(), tmp_path / "preview", assets=[asset])
    source = tmp_path / "cover.jpg"
    source.write_bytes(b"changed")
    with pytest.raises(ExportError, match="does not match"):
        export_site(_spec(), tmp_path / "preview", assets=[asset],
                    asset_paths={"cover-1": source})
