"""inspect.py — the element-inspection kit: every check here recreates,
synthetically, a defect class from the first real $0-lane cover and
asserts the probe catches it as numbers."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.inspect import (audit_assets, contact_gaps,
                                    containment_check, containment_gaps,
                                    ink_bbox, isolate, opening_bbox,
                                    ruled_crop, seam_scan, surface_line)
from docproof.cover.model import (ArtSlot, Brief, Direction, LayerRef,
                                  Palette, build_spec)

CANVAS = (400, 640)


def _spec():
    direction = Direction(
        concept_name="Inspect", rationale="r.", archetype="big_type",
        palette=Palette(background="#101820", primary="#f5f1e8",
                        accent="#c9a227", text="#f5f1e8", scrim="#000000"),
        title_font="Playfair Display", author_font="Spectral",
        art_prompts={}, texture=False)
    brief = Brief(title="The Lighthouse", author="J. R. Vance",
                  genre="literary")
    return build_spec(direction, brief, ARCHETYPES["big_type"])


# ---------------------------------------------------------------- ruled_crop
def test_ruled_crop_size_and_grid_ink():
    img = Image.new("RGB", (300, 300), (10, 10, 30))
    out = ruled_crop(img, (50, 50, 150, 150), step=20, scale=2)
    assert out.size == (200, 200)
    # gridlines are drawn in the red channel well above the background
    reds = [out.getpixel((x, 0))[0] for x in range(out.width)]
    assert max(reds) > 200


# ------------------------------------------------------------------ ink_bbox
def test_ink_bbox_reports_haze_below_feet():
    """The float bug: solid figure + near-invisible haze under it."""
    img = Image.new("RGBA", (100, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((30, 20, 70, 120), fill=(5, 5, 5, 255))       # her
    d.rectangle((30, 121, 70, 180), fill=(5, 5, 5, 12))       # haze
    report = ink_bbox(img)
    assert report["raw"][3] == 181
    assert report["hard"][3] == 121
    assert report["haze"][3] == 60                            # bottom pad


def test_ink_bbox_empty_layer():
    report = ink_bbox(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
    assert report["raw"] is None and report["hard"] is None


# ----------------------------------------------------------------- seam_scan
def _sky_with_seam(seam_x: int, delta: int) -> Image.Image:
    img = Image.new("L", (400, 300))
    px = img.load()
    for y in range(300):
        for x in range(400):
            base = 40 + round(6 * (y / 300))          # gentle vertical ramp
            px[x, y] = base + (delta if x >= seam_x else 0)
    return img.convert("RGB")


def test_seam_scan_finds_banding_step():
    hits = seam_scan(_sky_with_seam(137, 9), band=(20, 280))
    assert len(hits) == 1
    assert abs(hits[0]["x"] - 137) <= 2
    assert hits[0]["step"] == pytest.approx(9.0, abs=0.5)
    assert hits[0]["rows"] == (20, 280)


def test_seam_scan_stripes_merge_one_seam():
    """No band: the stripe sweep finds a full-height seam once, with the
    merged row span — a localized seam would report only its stripes."""
    hits = seam_scan(_sky_with_seam(137, 9), stripe=100)
    assert len(hits) == 1
    assert abs(hits[0]["x"] - 137) <= 2
    assert hits[0]["rows"][0] == 0 and hits[0]["rows"][1] == 300


def test_seam_scan_quiet_on_smooth_sky():
    assert seam_scan(_sky_with_seam(137, 0), band=(20, 280)) == []


def test_column_profile_shows_step_at_seam():
    from docproof.cover.inspect import column_profile
    profile = column_profile(_sky_with_seam(137, 9), y=150,
                             x_range=(100, 180), step=5)
    lums = dict(profile)
    assert lums[135] + 9 == lums[140]        # the step, visible as numbers


# -------------------------------------------------------------- surface_line
def test_surface_line_tracks_sloped_crest():
    """Dark sky over bright snow, crest sloping down to the right."""
    img = Image.new("L", (200, 200), 25)
    d = ImageDraw.Draw(img)
    for x in range(200):
        crest = 100 + x // 10                          # slope
        d.line((x, crest, x, 199), fill=210)
    line = surface_line(img.convert("RGB"), x_range=(0, 200),
                        y_range=(60, 180), sample_every=20)
    for x, y in line:
        assert abs(y - (100 + x // 10)) <= 2


def test_contact_gaps_signs():
    surface = [(0, 100), (50, 110)]
    gaps = contact_gaps(surface, [(2, 90), (52, 115)])
    assert gaps[0]["gap"] == 10       # floating 10px above
    assert gaps[1]["gap"] == -5       # sunk 5px in


# ------------------------------------------------------------------- isolate
def test_isolate_renders_single_slot(tmp_path: Path):
    spec = _spec()
    solo = isolate(spec, "background", tmp_path, canvas=CANVAS)
    assert solo.size == CANVAS
    # no text layers in the isolate: it must differ from the full render
    from docproof.cover.compose import compose
    full, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert list(solo.getdata()) != list(full.getdata())


def test_isolate_unknown_slot_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        isolate(_spec(), "nope", tmp_path, canvas=CANVAS)


# -------------------------------------------------------------- audit_assets
def test_audit_assets_flags_hazy_cutout(tmp_path: Path):
    spec = _spec()
    hazy = Image.new("RGBA", (80, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(hazy)
    d.rectangle((10, 10, 70, 60), fill=(0, 0, 0, 255))
    d.rectangle((10, 61, 70, 118), fill=(0, 0, 0, 10))
    (tmp_path / "assets").mkdir()
    hazy.save(tmp_path / "assets" / "cutout.png")
    spec.art[0].asset = "assets/cutout.png"
    findings = audit_assets(spec, tmp_path)
    assert findings and findings[0]["flag"] is True
    assert "float" in findings[0]["note"]


def test_audit_assets_clean_asset_unflagged(tmp_path: Path):
    spec = _spec()
    clean = Image.new("RGBA", (40, 40), (0, 0, 0, 255))
    (tmp_path / "assets").mkdir()
    clean.save(tmp_path / "assets" / "clean.png")
    spec.art[0].asset = "assets/clean.png"
    findings = audit_assets(spec, tmp_path)
    assert findings and findings[0]["flag"] is False


# ------------------------------------------- opening_bbox / containment_check
def _ring_asset(w: int = 200, h: int = 320, thick: int = 20) -> Image.Image:
    """An ornate-frame stand-in: an opaque rail ring with a transparent
    interior hole (and, like a real frame asset, ink whose bbox says
    nothing about where the opening is)."""
    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    d.rectangle((0, 0, w - 1, h - 1), outline=(201, 162, 39, 255),
                width=thick)
    return ring


def test_opening_bbox_measures_hole_not_bbox():
    """The frame debacle's root: the opening must be flooded, not derived
    from the container's bbox (which here spans the whole asset)."""
    report = opening_bbox(_ring_asset())
    assert report["closed"] is True
    x0, y0, x1, y1 = report["bbox"]
    assert 18 <= x0 <= 22 and 18 <= y0 <= 22
    assert 178 <= x1 <= 182 and 298 <= y1 <= 302


def test_opening_bbox_breached_container_is_not_closed():
    ring = _ring_asset()
    d = ImageDraw.Draw(ring)
    d.rectangle((60, 0, 140, 25), fill=(0, 0, 0, 0))   # breach the top rail
    report = opening_bbox(ring)
    assert report["closed"] is False


def test_containment_gaps_numbers():
    r = containment_gaps((100, 100, 300, 500), (120, 90, 280, 480))
    assert r["gaps"] == (20, -10, 20, 20)
    assert r["min_gap"] == -10
    assert r["contained"] is False


def test_containment_check_catches_rail_clip(tmp_path: Path):
    """End to end over compose's own placement: an element seated inside
    the frame passes; slammed into the rail zone, it FAILS — the exact
    defect that shipped twice on the Badgerbones cover."""
    spec = _spec()
    (tmp_path / "assets").mkdir()
    _ring_asset().save(tmp_path / "assets" / "frame.png")
    gem = Image.new("RGBA", (40, 40), (200, 30, 30, 255))
    gem.save(tmp_path / "assets" / "gem.png")
    spec.art.append(ArtSlot(id="frame", asset="assets/frame.png",
                            fit="contain", transparent=True,
                            anchor=[0.5, 0.5], scale=0.8))
    spec.art.append(ArtSlot(id="gem", asset="assets/gem.png",
                            fit="contain", transparent=True,
                            anchor=[0.5, 0.5], scale=0.08))
    spec.layers = spec.layers + [LayerRef(kind="art", ref="frame"),
                                 LayerRef(kind="art", ref="gem")]
    ok = containment_check(spec, tmp_path, container="frame",
                           contained="gem", canvas=CANVAS)
    assert ok["closed"] is True
    assert ok["contained"] is True and ok["min_gap"] >= ok["margin_px"]
    spec.art[-1].anchor = [0.5, 0.05]
    bad = containment_check(spec, tmp_path, container="frame",
                            contained="gem", canvas=CANVAS)
    assert bad["contained"] is False
