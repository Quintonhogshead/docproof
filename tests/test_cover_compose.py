"""docproof/cover/compose.py: the deterministic renderer — spec + assets ->
pixels, plus the legibility autopilot that makes the output look designed
rather than merely generated (docs/cover_designer_spec.md §7.3, §11).

No network anywhere in compose.py, so nothing here needs monkeypatching.
Canvases are tiny (400x640) except the one test that pins EBOOK_W/EBOOK_H,
since all geometry is fractional and a small canvas exercises the exact same
code paths in a fraction of the time. Where a test needs pixel-exact control
over what the legibility autopilot measures, it writes a real flat-color (or
deliberately patterned) PNG to a tmp_path job_dir rather than trusting the
procedural gradient's exact numbers.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from docproof.cover.archetypes import ARCHETYPES, zone_px
from docproof.cover.model import Brief, Direction, Palette, Shadow, build_spec
from docproof.cover.compose import (EBOOK_H, EBOOK_W, THUMB_LARGE,
                                    THUMB_SMALL, ComposeError, compose,
                                    save_renders)

CANVAS = (400, 640)


def _palette(**overrides) -> Palette:
    data = dict(background="#101820", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _direction(**overrides) -> Direction:
    data = dict(concept_name="Test Concept", rationale="A test rationale.",
               archetype="big_type", palette=_palette(),
               title_font="Playfair Display", author_font="Spectral",
               art_prompts={}, texture=False)
    data.update(overrides)
    return Direction(**data)


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary")
    data.update(overrides)
    return Brief(**data)


def _spec(archetype_name="big_type", **direction_overrides):
    archetype = ARCHETYPES[archetype_name]
    direction = _direction(archetype=archetype_name, **direction_overrides)
    return build_spec(direction, _brief(), archetype)


def _flat_png(path: Path, size: tuple[int, int], rgb: tuple[int, int, int]) -> None:
    Image.new("RGBA", size, (*rgb, 255)).save(path)


# -- procedural render: the $0 fallback --------------------------------------

def test_procedural_big_type_spec_renders_at_the_requested_canvas():
    spec = _spec("big_type")   # no assets anywhere - fully procedural
    image, report = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    assert image.size == CANVAS
    assert image.mode == "RGB"
    assert "title" in report.contrast
    assert "author" in report.contrast
    assert "title" in report.fitted_sizes


def test_procedural_full_bleed_and_cutout_sandwich_also_render_procedurally():
    # Neither archetype's launch design assumes a $0 render, but compose()
    # must still produce *something* rather than crash when an asset is
    # simply absent (§7.3: "synthesizes procedural layers otherwise").
    for name in ("full_bleed_art", "cutout_sandwich"):
        spec = _spec(name, art_prompts={})
        image, report = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
        assert image.size == CANVAS
        assert report.contrast   # at least the title was measured and drawn


def test_default_canvas_is_the_ebook_target():
    assert (EBOOK_W, EBOOK_H) == (1600, 2560)
    spec = _spec("big_type")
    image, _ = compose(spec, Path("/nonexistent-job-dir"))   # canvas omitted
    assert image.size == (EBOOK_W, EBOOK_H)


# -- determinism ---------------------------------------------------------------

def test_two_composes_of_the_same_procedural_spec_are_byte_identical():
    spec = _spec("big_type", texture=True)   # also exercises the grain layer
    image1, report1 = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    image2, report2 = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()
    assert report1 == report2


def test_grain_texture_alone_is_deterministic_across_composes():
    spec = _spec("full_bleed_art", art_prompts={}, texture=True)
    image1, _ = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    image2, _ = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()


# -- optional slots -------------------------------------------------------------

def test_empty_optional_subtitle_is_skipped_not_measured_or_drawn():
    spec = _spec("full_bleed_art", art_prompts={})   # brief has no subtitle
    subtitle = next(t for t in spec.text if t.id == "subtitle")
    assert subtitle.optional is True and subtitle.content == ""
    _, report = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    assert "subtitle" not in report.contrast
    assert "subtitle" not in report.fitted_sizes


# -- legibility autopilot: contrast + scrim escalation -----------------------

def test_dark_text_on_dark_background_escalates_its_scrim_past_the_default(tmp_path):
    # A mid-gray flat background fails light title text's contrast at the
    # archetype's default scrim strength (0.25); escalating a few 0.15 steps
    # of a black scrim should be enough to pass without ever hitting the cap.
    _flat_png(tmp_path / "bg.png", CANVAS, (150, 150, 150))
    spec = _spec("full_bleed_art", art_prompts={"background": "irrelevant"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "bg.png"
    initial_strength = next(s.strength for s in spec.scrims if s.protects == "title")

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    title_scrim_idx = next(i for i, s in enumerate(spec.scrims) if s.protects == "title")
    assert report.scrim_final[title_scrim_idx] > initial_strength
    assert report.scrim_final[title_scrim_idx] <= 0.85
    assert report.contrast["title"] >= 4.5
    assert not any("title" in w for w in report.warnings)


def test_contrast_that_still_fails_at_max_scrim_strength_flips_text_color(tmp_path):
    # Background AND scrim both the same mid-gray (~worst-case WCAG
    # luminance for the two fallback ink colors): compositing gray-over-
    # itself at any alpha is a no-op, so escalation legitimately climbs to
    # the 0.85 cap without ever improving contrast, forcing the color flip —
    # and even the flip cannot clear the 4.5 threshold from this luminance.
    worst_case_gray = "#767676"
    _flat_png(tmp_path / "bg.png", CANVAS, (0x76, 0x76, 0x76))
    spec = _spec("full_bleed_art", art_prompts={"background": "irrelevant"},
                palette=_palette(background=worst_case_gray, text=worst_case_gray,
                                scrim=worst_case_gray))
    for art in spec.art:
        if art.id == "background":
            art.asset = "bg.png"

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    for idx in report.scrim_final:
        assert report.scrim_final[idx] == pytest.approx(0.85)
    assert report.contrast["title"] < 4.5
    assert any("title" in w and "still falls short" in w for w in report.warnings)
    assert any("flipped text color to #111111" in w for w in report.warnings)


def test_contrast_thresholds_differ_for_title_versus_subtitle(tmp_path):
    # full_bleed_art's subtitle has no protecting scrim at all, so its
    # contrast decision rests entirely on the lower 3.0 threshold — confirm
    # a luminance that fails title's 4.5 bar still cleanly passes subtitle's.
    _flat_png(tmp_path / "bg.png", CANVAS, (90, 90, 90))
    spec = _spec("full_bleed_art", art_prompts={"background": "irrelevant"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "bg.png"
    for t in spec.text:
        if t.id == "subtitle":
            t.content = "A Novel"   # force it to actually render/measure

    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert report.contrast["subtitle"] >= 3.0


# -- legibility autopilot: busy backdrop adds a default shadow --------------

def _busy_dot_pattern(size: tuple[int, int]) -> Image.Image:
    """Sparse bright dots on black: high local variance (stddev well over
    the 0.22 trigger) while the MEAN stays dark enough that light text still
    passes contrast without any scrim escalation — isolating the shadow
    decision from the contrast/escalation path."""
    img = Image.new("RGB", size, (0, 0, 0))
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if x % 4 == 0 and y % 4 == 0:
                px[x, y] = (255, 255, 255)
    return img.convert("RGBA")


def test_busy_backdrop_auto_adds_the_default_shadow(tmp_path):
    _busy_dot_pattern(CANVAS).save(tmp_path / "busy.png")
    _flat_png(tmp_path / "flat.png", CANVAS, (10, 10, 10))

    def make(asset: str, shadow):
        spec = _spec("big_type", texture=False)
        for art in spec.art:
            if art.id == "background":
                art.asset = asset
        for t in spec.text:
            if t.id == "title":
                t.shadow = shadow
        return spec

    busy_auto, r_busy_auto = compose(make("busy.png", None), tmp_path, canvas=CANVAS)
    busy_explicit, r_busy_explicit = compose(make("busy.png", Shadow()), tmp_path, canvas=CANVAS)
    flat_auto, _ = compose(make("flat.png", None), tmp_path, canvas=CANVAS)
    flat_explicit, _ = compose(make("flat.png", Shadow()), tmp_path, canvas=CANVAS)

    # Busy backdrop: stddev > 0.22 triggers the SAME default Shadow() the
    # explicit-shadow spec asked for, so the two renders must match exactly.
    assert busy_auto.tobytes() == busy_explicit.tobytes()
    # Flat backdrop: stddev stays low, no auto-shadow, so shadow=None truly
    # differs from an explicit shadow — proving the busy-case match above
    # isn't simply "shadow never changes anything".
    assert flat_auto.tobytes() != flat_explicit.tobytes()
    # And sanity: contrast passed cleanly in the busy case, with no
    # escalation/flip warning muddying which mechanism fired.
    assert r_busy_auto.warnings == []
    assert r_busy_explicit.warnings == []


# -- regression: the legibility sampler must read the RIGHT rectangle -------

def test_legibility_sampling_reads_the_slots_own_zone_not_the_whole_canvas(tmp_path):
    # Regression guard: zone_px() returns (left, top, WIDTH, HEIGHT), not
    # (left, top, right, bottom). Paint the canvas a mid-gray that gives
    # marginal contrast everywhere EXCEPT a pure-black rectangle placed
    # exactly at the author zone (which sits away from the origin) — if the
    # autopilot ever samples the wrong rectangle (or a degenerate one), it
    # will not see that black patch and will wrongly flag author's contrast.
    spec = _spec("cutout_sandwich", art_prompts={"background": "x", "focal": "y"},
                palette=_palette(background="#808080", text="#f5f1e8"))
    author = next(t for t in spec.text if t.id == "author")
    left, top, w, h = zone_px(author.zone, CANVAS)

    bg = Image.new("RGBA", CANVAS, (128, 128, 128, 255))
    for y in range(top, top + h):
        for x in range(left, left + w):
            bg.putpixel((x, y), (0, 0, 0, 255))
    bg.save(tmp_path / "bg.png")
    for art in spec.art:
        if art.id == "background":
            art.asset = "bg.png"
        if art.id == "focal":
            art.asset = ""   # nothing generated - no focal drawn

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    # Light text over a correctly-sampled pure-black author zone is a clean
    # pass; cutout_sandwich has no scrim at all, so any flip would show up
    # as a warning naming "author" specifically.
    assert report.contrast["author"] >= 4.5
    assert not any("author" in w for w in report.warnings)


# -- cutout_sandwich's opaque-focal degradation (§5.2.3) ---------------------

def _solid_rgba(size: tuple[int, int], rgba: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, rgba)


def test_opaque_focal_asset_swaps_behind_its_title_and_warns(tmp_path):
    _flat_png(tmp_path / "background.png", CANVAS, (60, 60, 90))
    _solid_rgba((200, 300), (200, 50, 50, 255)).save(tmp_path / "focal.png")   # fully opaque

    spec = _spec("cutout_sandwich", art_prompts={"background": "x", "focal": "y"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "background.png"
        if art.id == "focal":
            art.asset = "focal.png"
    assert next(a for a in spec.art if a.id == "focal").transparent is True

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("focal" in w and "title" in w for w in report.warnings)


def test_focal_asset_with_real_transparency_does_not_swap_or_warn(tmp_path):
    _flat_png(tmp_path / "background.png", CANVAS, (60, 60, 90))
    focal = Image.new("RGBA", (200, 300), (200, 50, 50, 0))
    for y in range(60, 240):
        for x in range(60, 140):
            focal.putpixel((x, y), (200, 50, 50, 255))
    focal.save(tmp_path / "focal.png")

    spec = _spec("cutout_sandwich", art_prompts={"background": "x", "focal": "y"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "background.png"
        if art.id == "focal":
            art.asset = "focal.png"

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.warnings == []


# -- ComposeError ---------------------------------------------------------------

def test_unreadable_art_asset_raises_composeerror_with_a_human_sentence(tmp_path):
    spec = _spec("full_bleed_art", art_prompts={"background": "x"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "does_not_exist.png"
    with pytest.raises(ComposeError) as excinfo:
        compose(spec, tmp_path, canvas=CANVAS)
    message = str(excinfo.value)
    assert "background" in message
    assert "does_not_exist.png" in message


def test_corrupt_art_asset_raises_composeerror(tmp_path):
    (tmp_path / "background.png").write_bytes(b"not actually a png")
    spec = _spec("full_bleed_art", art_prompts={"background": "x"})
    for art in spec.art:
        if art.id == "background":
            art.asset = "background.png"
    with pytest.raises(ComposeError):
        compose(spec, tmp_path, canvas=CANVAS)


# -- save_renders() -------------------------------------------------------------

def test_save_renders_writes_all_four_files_in_the_documented_order(tmp_path):
    image = Image.new("RGB", CANVAS, (10, 20, 30))
    paths = save_renders(image, tmp_path, version=3, concept=1)
    assert paths == [
        "renders/v3_c1.png", "renders/v3_c1.jpg",
        "renders/v3_c1_thumb300.png", "renders/v3_c1_thumb100.png",
    ]
    for rel in paths:
        assert (tmp_path / rel).is_file()


def test_save_renders_thumbnails_are_the_documented_widths_and_preserve_aspect(tmp_path):
    image = Image.new("RGB", CANVAS, (10, 20, 30))
    paths = save_renders(image, tmp_path, version=1, concept=0)
    thumb300 = Image.open(tmp_path / paths[2])
    thumb100 = Image.open(tmp_path / paths[3])
    assert thumb300.width == THUMB_LARGE
    assert thumb100.width == THUMB_SMALL
    assert thumb300.height == round(CANVAS[1] * THUMB_LARGE / CANVAS[0])
    assert thumb100.height == round(CANVAS[1] * THUMB_SMALL / CANVAS[0])


def test_save_renders_file_formats_are_correct(tmp_path):
    image = Image.new("RGB", CANVAS, (10, 20, 30))
    paths = save_renders(image, tmp_path, version=1, concept=0)
    assert Image.open(tmp_path / paths[0]).format == "PNG"
    assert Image.open(tmp_path / paths[1]).format == "JPEG"
    assert Image.open(tmp_path / paths[2]).format == "PNG"
    assert Image.open(tmp_path / paths[3]).format == "PNG"


def test_save_renders_flattens_rgba_input_to_rgb(tmp_path):
    image = Image.new("RGBA", CANVAS, (10, 20, 30, 255))
    paths = save_renders(image, tmp_path, version=1, concept=0)
    assert Image.open(tmp_path / paths[0]).mode == "RGB"
