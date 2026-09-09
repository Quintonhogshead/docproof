from docproof.website.render import PAGES, TEMPLATES, build_pages, render_page


SPEC = {
    "template_id": "literary-journal",
    "author": {"name": "Ava <Writer>", "bio_short": "Stories & essays", "bio": "Longer biography", "portrait_asset_id": "portrait"},
    "headline": "A bright & dangerous story",
    "intro": "A carefully made introduction.",
    "primary_cta": {"label": "Buy now", "url": "https://books.example/buy"},
    "books": [{"id": "book-1", "title": "The <Book>", "subtitle": "A novel", "description": "Description", "hook": "The hook", "cover_asset_id": "cover", "publication_date": "2026", "links": [{"label": "Order", "url": "https://books.example/order"}]}],
    "contact": {"email": "ava@example.test", "headline": "Say hello", "message": "A real route.", "links": [{"label": "Instagram", "url": "https://instagram.example/ava"}]},
    "praise": [{"quote": "A <wonder>", "attribution": "Reviewer"}], "excerpt": "Once upon a time.",
    "seo": {"description": "Author site"},
}
ASSETS = [{"id": "cover", "filename": "cover.jpg", "media_type": "image/jpeg", "sha256": "x", "alt": "The cover", "approved": True, "focal_x": .5, "focal_y": .5}, {"id": "portrait", "filename": "p.jpg", "media_type": "image/jpeg", "sha256": "y", "alt": "Portrait", "approved": True, "focal_x": .5, "focal_y": .5}]


def test_render_builds_deterministic_full_bundle_with_placeholders():
    first = build_pages(SPEC, assets=ASSETS, release_id="r1")
    assert first == build_pages(SPEC, assets=ASSETS, release_id="r1")
    assert set(first) == set(PAGES)
    assert "__DOCPROOF_BASE__/about" in first["home"]
    assert "__DOCPROOF_ASSETS__/cover" in first["home"]
    assert 'data-release="r1"' in first["home"]
    assert "<script>" in first["home"]


def test_render_escapes_text_and_drops_unsafe_urls_and_assets():
    spec = {**SPEC, "headline": "<script>alert(1)</script>", "primary_cta": {"label": "Click", "url": "javascript:alert(1)"}}
    html = render_page(spec, "home", assets=[{**ASSETS[0], "approved": False}])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:" not in html
    assert "media--empty" in html


def test_all_five_templates_are_full_layouts():
    pages = []
    for template in TEMPLATES:
        spec = {**SPEC, "template_id": template}
        html = render_page(spec, "home", assets=ASSETS, base_url="/preview")
        assert f"template-{template}" in html
        assert "/preview/contact" in html
        assert "prefers-reduced-motion" in html
        pages.append(html)
    assert len(set(pages)) == len(TEMPLATES)


def test_every_template_handles_a_catalogue_and_missing_portrait():
    another = {**SPEC["books"][0], "id": "book-2", "title": "A Second, Very Long Canonical Book Title"}
    for template in TEMPLATES:
        spec = {**SPEC, "template_id": template, "books": [SPEC["books"][0], another],
                "author": {**SPEC["author"], "portrait_asset_id": ""}}
        pages = build_pages(spec, assets=ASSETS)
        assert all("<!doctype html>" in page for page in pages.values())
        assert "A Second, Very Long Canonical Book Title" in pages["books"]
        assert "portrait-monogram" in pages["about"]


def test_missing_primary_cta_uses_safe_internal_books_route_and_book_has_full_copy():
    spec = {**SPEC, "primary_cta": {"label": "", "url": ""}}
    html = render_page(spec, "home", assets=ASSETS, base_url="/private-preview")
    assert 'href="/private-preview/books"' in html
    assert 'class="hook">The hook' in html
    assert 'class="description">Description' in html
    assert "Author · Writer · Storyteller" not in html


def test_long_names_and_empty_optional_content_are_supported():
    spec = {**SPEC, "author": {"name": "The Very Long Pen Name That Cannot Fit In A Typical Masthead", "bio_short": "", "bio": "", "portrait_asset_id": ""}, "books": [], "praise": [], "excerpt": ""}
    html = render_page(spec, "books")
    assert "The Very Long Pen Name" in html
    assert "Books are being added" in html


def test_rejects_unknown_page():
    import pytest
    with pytest.raises(ValueError):
        render_page(SPEC, "pricing")
