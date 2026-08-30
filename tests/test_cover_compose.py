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
from PIL import Image, ImageColor, ImageDraw

from docproof.cover import typeset
from docproof.cover.archetypes import ARCHETYPES, zone_px
from docproof.cover.model import (ArtSlot, Brief, CoverSpec, Direction,
                                  LayerRef, Palette, Shadow, TextSlot, Zone,
                                  build_spec)
from docproof.cover.compose import (EBOOK_H, EBOOK_W, PROCEDURAL_SYNTHESIZERS,
                                    THUMB_LARGE, THUMB_SMALL, ComposeError,
                                    compose, save_renders)

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
    # archetype's default scrim strength (0.25); escalating a couple of 0.15
    # steps of a black scrim should be enough to pass without ever hitting
    # the cap. (v2 BODY wave: the gray here is calibrated a touch lighter
    # than a pre-worst-region-scoring test would have needed — full_bleed_
    # art's title scrim is `gradient_down`, whose leading edge is BY
    # DEFINITION always the weakest-protected row; worst-region scoring
    # (§ legibility autopilot) now scores the slot by that row, not by the
    # zone's mean, so the background has to be forgiving enough for even
    # that weakest row to clear threshold once escalation is done.)
    _flat_png(tmp_path / "bg.png", CANVAS, (120, 120, 120))
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


# -- legibility autopilot: worst-REGION scoring (v2 BODY wave) --------------

def _split_png(path: Path, size: tuple[int, int], left_rgb: tuple[int, int, int],
              right_rgb: tuple[int, int, int]) -> None:
    """A vertical two-tone split — half genuinely legible, half genuinely
    not — built from two pastes (fast; no per-pixel Python loop)."""
    w, h = size
    img = Image.new("RGBA", size, (*right_rgb, 255))
    img.paste(Image.new("RGBA", (w // 2, h), (*left_rgb, 255)), (0, 0))
    img.save(path)


def test_half_busy_half_clear_zone_fails_worst_region_even_though_mean_would_pass(tmp_path):
    # The real-world case this scoring change exists for: a title zone half
    # over a bright, clear field (great contrast) and half over a near-
    # black field (terrible contrast) for DARK ink. The MEAN luminance
    # across the whole zone lands around mid-gray — enough for dark ink to
    # clear 4.5 on its own, with NO escalation, under the old mean-only
    # score. Assert that whole-zone mean reading directly (proving "v1
    # mean would pass" isn't just asserted, it's measured), then assert the
    # real compose() — which scores by worst REGION — refuses to accept
    # that: it escalates the (light-colored) scrim strength past zero to
    # actually fix the dark half, and only reports success once the WORST
    # cell, not the average, clears threshold.
    from PIL import ImageColor

    from docproof.cover.archetypes import zone_px
    from docproof.cover.compose import _contrast_against_luminance, _zone_stats

    _split_png(tmp_path / "bg.png", CANVAS, (245, 245, 245), (10, 10, 10))
    # dark ink; a LIGHT scrim color, so escalating it can actually brighten
    # (help) the dark half instead of only ever darkening the light half.
    spec = _spec("big_type", art_prompts={},
                palette=_palette(text="#111111", scrim="#f5f1e8"))
    for art in spec.art:
        if art.id == "background":
            art.asset = "bg.png"
    title = next(t for t in spec.text if t.id == "title")
    initial_strength = next(s.strength for s in spec.scrims if s.protects == "title")
    assert initial_strength == 0.0   # big_type's own documented default

    bg_image = Image.open(tmp_path / "bg.png").convert("RGBA")
    left, top, w, h = zone_px(title.zone, CANVAS)
    rect = (left, top, left + w, top + h)
    mean_lum, _ = _zone_stats(bg_image, rect)
    mean_ratio = _contrast_against_luminance(ImageColor.getrgb("#111111"), mean_lum)
    assert mean_ratio >= 4.5   # confirms this case really would have passed on the mean

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    title_scrim_idx = next(i for i, s in enumerate(spec.scrims) if s.protects == "title")
    assert report.scrim_final[title_scrim_idx] > initial_strength   # it had to escalate
    assert report.contrast["title"] >= 4.5   # and, escalated, the WORST cell now passes too
    assert not any("title" in w for w in report.warnings)


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

    # Not a blanket "no warnings at all": this minimal hand-built spec (a
    # flat background, no subtitle content) has real empty canvas above the
    # title zone that the v2.1 BODY-fix wave's dead-band metric is entitled
    # to flag — this test is specifically about the opaque-focal swap-and-
    # warn mechanism (see test_opaque_focal_asset_swaps_behind_its_title_
    # and_warns, its positive counterpart), so assert THAT warning's
    # absence rather than the absence of every warning any mechanism could
    # ever raise.
    assert not any("focal" in w and "title" in w for w in report.warnings)


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


# ===========================================================================
# v2 BODY wave: procedural texture/art shelf
# ===========================================================================

_TEST_PALETTE = Palette(background="#c23b22", primary="#1c1712",
                        accent="#e8c468", text="#f6ede1", scrim="#000000")


def test_procedural_synthesizer_registry_matches_the_model_layers_kinds():
    from docproof.cover.model import PROCEDURAL_KINDS
    assert set(PROCEDURAL_SYNTHESIZERS) == set(PROCEDURAL_KINDS)


@pytest.mark.parametrize("name", list(PROCEDURAL_SYNTHESIZERS))
def test_every_synthesizer_is_deterministic(name):
    synth = PROCEDURAL_SYNTHESIZERS[name]
    img1 = synth(CANVAS, _TEST_PALETTE, "some_slot", 7)
    img2 = synth(CANVAS, _TEST_PALETTE, "some_slot", 7)
    assert img1.tobytes() == img2.tobytes()


@pytest.mark.parametrize("name", list(PROCEDURAL_SYNTHESIZERS))
def test_every_synthesizer_returns_a_canvas_sized_rgba_image(name):
    img = PROCEDURAL_SYNTHESIZERS[name](CANVAS, _TEST_PALETTE, "slot", 1)
    assert img.size == CANVAS
    assert img.mode == "RGBA"


def test_every_synthesizer_produces_visually_distinct_output():
    # Every pair of the seven shelf entries, run against the identical
    # (canvas, palette, slot id, version), must NOT produce identical
    # bytes — proving each is really its own pattern, not a copy in
    # disguise.
    outputs = {name: synth(CANVAS, _TEST_PALETTE, "vine_left", 3).tobytes()
              for name, synth in PROCEDURAL_SYNTHESIZERS.items()}
    names = list(outputs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert outputs[a] != outputs[b], f"{a} and {b} produced identical pixels"


def test_synthesizer_output_varies_by_slot_id_and_version_where_seeded():
    # paper and speckle are explicitly seeded from (version, slot_id, name)
    # — two different slots (or two different versions of the same slot)
    # must not stamp identical noise.
    for name in ("paper", "speckle"):
        synth = PROCEDURAL_SYNTHESIZERS[name]
        base = synth(CANVAS, _TEST_PALETTE, "slot_a", 1).tobytes()
        other_slot = synth(CANVAS, _TEST_PALETTE, "slot_b", 1).tobytes()
        other_version = synth(CANVAS, _TEST_PALETTE, "slot_a", 2).tobytes()
        assert base != other_slot
        assert base != other_version


def test_legacy_texture_id_falls_back_to_grain_unchanged(tmp_path):
    # No `procedural` set: an archetype/spec written before this field
    # existed must render BYTE-IDENTICAL pixels to calling the grain
    # synthesizer directly by name.
    spec = _spec("full_bleed_art", art_prompts={}, texture=True)
    image, _ = compose(spec, tmp_path, canvas=CANVAS)
    direct = PROCEDURAL_SYNTHESIZERS["grain"](CANVAS, spec.palette, "texture", spec.version)
    # The texture layer is composited at low opacity/overlay over the
    # gradient background — recomposing just the grain slot in isolation
    # and comparing its own bytes (not the whole cover) isolates the claim.
    from docproof.cover.compose import _procedural_art
    art_by_id = {a.id: a for a in spec.art}
    via_dispatch = _procedural_art(art_by_id["texture"], CANVAS, spec.palette, spec.version)
    assert via_dispatch.tobytes() == direct.tobytes()


def test_legacy_background_id_falls_back_to_gradient_unchanged():
    from docproof.cover.compose import _procedural_art
    spec = _spec("big_type", texture=False)
    art_by_id = {a.id: a for a in spec.art}
    direct = PROCEDURAL_SYNTHESIZERS["gradient"](CANVAS, spec.palette, "background", spec.version)
    via_dispatch = _procedural_art(art_by_id["background"], CANVAS, spec.palette, spec.version)
    assert via_dispatch.tobytes() == direct.tobytes()


def test_an_ungenerated_slot_with_no_procedural_default_draws_nothing():
    from docproof.cover.compose import _procedural_art
    slot = ArtSlot(id="focal")   # generatable elsewhere, but no asset, no procedural
    assert _procedural_art(slot, CANVAS, _TEST_PALETTE, 1) is None


# ===========================================================================
# v2.2 wave, deliverable 5: the stocked texture shelf (ArtSlot.texture_file)
# ===========================================================================

def test_textures_registry_loads_the_real_shelf():
    from docproof.cover.textures import TEXTURES, TEXTURES_DIR
    assert TEXTURES   # the shelf is not empty
    for name, path in TEXTURES.items():
        assert path.is_file(), f"{name} -> {path} does not exist on disk"
        assert path.parent == TEXTURES_DIR


def test_texture_file_slot_composes_with_cover_fit(tmp_path):
    from docproof.cover.textures import TEXTURES
    name = next(iter(TEXTURES))
    slot = ArtSlot(id="background", texture_file=name, texture_fit="cover")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_TEST_PALETTE, art=[slot], scrims=[], text=[],
                     layers=[LayerRef(kind="art", ref="background")])

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    # A "cover"-fit shelf plate fills the WHOLE canvas — real tonal range
    # somewhere, not a blank/never-painted layer.
    assert image.size == CANVAS
    assert image.convert("L").getextrema() != (0, 0)


def test_texture_file_slot_tile_fit_repeats_the_plate_across_the_canvas(tmp_path):
    from docproof.cover.compose import TEXTURES as compose_textures

    # A tiny synthetic plate, much smaller than CANVAS, inserted directly
    # into the shared registry — docproof.cover.model and
    # docproof.cover.compose both import the SAME dict object from
    # docproof.cover.textures (see that module's own docstring on why), so
    # mutating it here in place is visible to ArtSlot's own field
    # validation too, with no monkeypatching of either module needed.
    tiny_path = tmp_path / "tiny_check.png"
    Image.new("RGBA", (40, 40), (10, 200, 10, 255)).save(tiny_path)
    compose_textures["tiny_check_v22"] = tiny_path
    try:
        slot = ArtSlot(id="background", texture_file="tiny_check_v22", texture_fit="tile")
        spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                         palette=_TEST_PALETTE, art=[slot], scrims=[], text=[],
                         layers=[LayerRef(kind="art", ref="background")])

        image, _ = compose(spec, tmp_path, canvas=CANVAS)

        # The plate repeats every 40px in both directions — two points
        # exactly one tile-period apart must match. A "cover" fit (the
        # other test above) would never produce this exact periodicity at
        # a 400x640 canvas from a 40x40 source.
        assert image.getpixel((5, 5)) == image.getpixel((45, 5))
        assert image.getpixel((5, 5)) == image.getpixel((5, 45))
    finally:
        del compose_textures["tiny_check_v22"]


# -- rule_frame geometry ------------------------------------------------------

def _runs_of_ink(values: list[int]) -> list[tuple[int, int]]:
    """[start, end) index ranges where `values` is nonzero — turns a raw
    alpha scanline into "how many separate strokes did this cross.\""""
    runs = []
    start = None
    for i, v in enumerate(values):
        if v > 0 and start is None:
            start = i
        elif v == 0 and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(values)))
    return runs


def test_rule_frame_is_a_double_rule_inset_from_the_edge():
    synth = PROCEDURAL_SYNTHESIZERS["rule_frame"]
    canvas = (400, 640)
    img = synth(canvas, _TEST_PALETTE, "rule_frame", 1)
    alpha = img.getchannel("A")
    cw, ch = canvas
    cy = ch // 2   # a horizontal scan through the vertical middle of the
                   # frame crosses both the left and right rule pairs
    row = [alpha.getpixel((x, cy)) for x in range(cw)]

    left_runs = [r for r in _runs_of_ink(row) if r[0] < cw // 4]
    assert len(left_runs) == 2, f"expected outer+inner rule, found {left_runs}"
    outer, inner = left_runs
    # Flush against the edge, before the ~5% inset: nothing drawn.
    assert row[0] == 0 and row[2] == 0
    # The outer rule sits at the documented ~4-6% inset.
    assert 0.03 * ch <= outer[0] <= 0.07 * ch
    # A real gap separates the two rules (the "double" in double-rule) —
    # and the frame's own interior, well past the inner rule, is empty.
    assert inner[0] > outer[1]
    assert row[cw // 2] == 0
    # Palette-aware: every opaque pixel resolves to palette.accent alone.
    opaque_colors = {img.getpixel((x, cy)) for x in range(cw) if row[x] > 0}
    accent_rgb = ImageColor.getrgb(_TEST_PALETTE.accent)
    assert opaque_colors and all(c[:3] == accent_rgb for c in opaque_colors)


def test_rule_frame_composes_cleanly_inside_woven_emblem_at_small_canvas():
    spec = _spec("woven_emblem", texture=False)
    image, report = compose(spec, Path("/nonexistent-job-dir"), canvas=CANVAS)
    assert image.size == CANVAS
    assert "title" in report.contrast


# -- woven_emblem: the flagship, end to end ----------------------------------

def test_woven_emblem_thumb_is_legible_at_search_result_size():
    from docproof.cover.compose import THUMB_SMALL

    spec = _spec("woven_emblem", texture=False,
                 palette=Palette(background="#c23b22", primary="#1c1712",
                                 accent="#e8c468", text="#f6ede1", scrim="#000000"))
    image, report = compose(spec, Path("/nonexistent-job-dir"), canvas=(EBOOK_W, EBOOK_H))

    # The full render passes contrast cleanly (its whole point: type is the
    # hero, ornaments never fight it) — no warnings at all.
    assert report.warnings == []
    assert report.contrast["title"] >= 4.5
    assert report.contrast["author"] >= 4.5

    thumb_h = round(EBOOK_H * THUMB_SMALL / EBOOK_W)
    thumb = image.resize((THUMB_SMALL, thumb_h), Image.Resampling.LANCZOS)
    # A legible thumb has real tonal range, not a flat wash — luminance
    # stddev comfortably above noise-floor confirms the huge title block
    # still reads as distinct light/dark shapes at 100px wide.
    stat_img = thumb.convert("L")
    extrema = stat_img.getextrema()
    assert extrema[1] - extrema[0] > 80


# ===========================================================================
# v2 BODY wave: feathered local panel scrim ("de-mute the composer")
# ===========================================================================

def test_local_panel_scrim_never_dims_a_pixel_outside_its_own_rect(tmp_path):
    from docproof.cover.compose import _apply_scrim, _scrim_rect
    from docproof.cover.model import ScrimSpec, TextSlot, Zone

    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.3, y=0.4, w=0.4, h=0.15),
                     font_family="Spectral", size_min=0.05, size_max=0.05)
    text_by_id = {"title": title}
    scrim = ScrimSpec(kind="panel", protects="title", strength=0.9)
    rect = _scrim_rect(scrim, text_by_id, CANVAS)

    base = Image.new("RGBA", CANVAS, (200, 200, 200, 255))
    out = _apply_scrim(base, scrim, 0.9, text_by_id, _palette(scrim="#000000"), CANVAS)

    left, top, right, bottom = rect
    # A ring of probe points exactly 1px outside the padded rect, all the
    # way around — every one of them must be byte-identical to the
    # untouched base, proving the feather never bled past the rect.
    cx, cy = (left + right) // 2, (top + bottom) // 2
    probes = [(max(0, left - 1), cy), (min(CANVAS[0] - 1, right), cy),
             (cx, max(0, top - 1)), (cx, min(CANVAS[1] - 1, bottom)),
             (0, 0), (CANVAS[0] - 1, 0), (0, CANVAS[1] - 1),
             (CANVAS[0] - 1, CANVAS[1] - 1)]
    for p in probes:
        assert out.getpixel(p) == (200, 200, 200, 255), f"dimmed outside the rect at {p}"
    # And the CENTER is meaningfully darkened — this is a real panel, not
    # a no-op.
    assert out.getpixel((cx, cy))[:3] != (200, 200, 200)


def test_local_panel_scrim_has_no_hard_rectangle_edge():
    # "No hard rectangle look": the transition from full strength to zero
    # is gradual across at least a few pixels, not a single-pixel cliff
    # from (near-)opaque straight to fully transparent.
    from docproof.cover.compose import _apply_scrim, _scrim_rect
    from docproof.cover.model import ScrimSpec, TextSlot, Zone

    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.2, y=0.4, w=0.6, h=0.2),
                     font_family="Spectral", size_min=0.05, size_max=0.05)
    text_by_id = {"title": title}
    scrim = ScrimSpec(kind="panel", protects="title", strength=1.0)
    rect = _scrim_rect(scrim, text_by_id, CANVAS)
    base = Image.new("RGBA", CANVAS, (255, 255, 255, 255))
    out = _apply_scrim(base, scrim, 1.0, text_by_id, _palette(scrim="#000000"), CANVAS)

    left, top, right, bottom = rect
    cy = (top + bottom) // 2
    alphas = [255 - out.getpixel((x, cy))[0] for x in range(max(0, left - 2), left + 12)]
    # Somewhere in that span the "how dark is this pixel" reading climbs
    # gradually rather than jumping straight from 0 to its peak in one step.
    diffs = [b - a for a, b in zip(alphas, alphas[1:])]
    assert max(diffs) < 200, "edge jumps in a single step — reads as a hard box"
    assert any(0 < d for d in diffs)   # and it does climb somewhere


# ===========================================================================
# v2.2 wave, deliverable 2: halo scrim + softer panels ("the text for
# Lighthouse has a box around it")
# ===========================================================================

def _scan_alpha_row(out, rect, canvas) -> list[int]:
    """255-minus-red across a full horizontal scanline through `rect`'s
    own vertical middle — the same "how dark is this pixel" readout the
    panel hard-edge test above already uses, generalized to the WHOLE row
    (not just a span near one edge) so a halo's total absence of any edge,
    anywhere, can be checked in one pass."""
    _left, top, _right, bottom = rect
    cy = (top + bottom) // 2
    return [255 - out.getpixel((x, cy))[0] for x in range(canvas[0])]


def test_halo_scrim_has_no_detectable_hard_edge():
    from docproof.cover.compose import _apply_scrim, _scrim_rect
    from docproof.cover.model import ScrimSpec, TextSlot, Zone

    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.3, y=0.4, w=0.4, h=0.15),
                     font_family="Spectral", size_min=0.05, size_max=0.05)
    text_by_id = {"title": title}
    scrim = ScrimSpec(kind="halo", protects="title", strength=1.0)
    rect = _scrim_rect(scrim, text_by_id, CANVAS)
    base = Image.new("RGBA", CANVAS, (200, 200, 200, 255))
    out = _apply_scrim(base, scrim, 1.0, text_by_id, _palette(scrim="#000000"), CANVAS)

    alphas = _scan_alpha_row(out, rect, CANVAS)
    diffs = [abs(b - a) for a, b in zip(alphas, alphas[1:])]
    # A halo is built from a big-sigma Gaussian blur specifically so it has
    # no edge anywhere a probe could find one — a handful of alpha levels'
    # worth of step between ADJACENT pixels is noise-floor, not an edge.
    assert max(diffs) <= 4, f"halo has a detectable edge (max step {max(diffs)})"
    # And it genuinely darkens the middle — not a no-op.
    left, top, right, bottom = rect
    assert alphas[(left + right) // 2] > 40

    # Unlike panel (never dims a pixel outside its own rect — see
    # test_local_panel_scrim_never_dims_a_pixel_outside_its_own_rect), a
    # halo is deliberately UNCLIPPED: it fades past its own nominal zone
    # rather than snapping to zero at the boundary, so a probe well
    # outside the padded rect still reads meaningfully dimmed.
    assert alphas[max(0, left - 10)] > 15
    assert alphas[min(CANVAS[0] - 1, right + 10)] > 15


def test_panel_scrim_edge_step_is_looser_than_halos_but_still_gradual():
    # Same probe-line methodology as the halo test above, so the "halo has
    # no edge, panel's edge is merely SOFTER than a slab" claim is a direct,
    # comparable measurement rather than two differently-shaped tests. The
    # doubled feather (v2.2 wave, deliverable 2) is what keeps this looser
    # threshold meaningfully tighter than a hard-box's 255-in-one-step
    # would be, even though it is intentionally far looser than halo's.
    from docproof.cover.compose import _apply_scrim, _scrim_rect
    from docproof.cover.model import ScrimSpec, TextSlot, Zone

    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.3, y=0.4, w=0.4, h=0.15),
                     font_family="Spectral", size_min=0.05, size_max=0.05)
    text_by_id = {"title": title}
    scrim = ScrimSpec(kind="panel", protects="title", strength=1.0)
    rect = _scrim_rect(scrim, text_by_id, CANVAS)
    base = Image.new("RGBA", CANVAS, (200, 200, 200, 255))
    out = _apply_scrim(base, scrim, 1.0, text_by_id, _palette(scrim="#000000"), CANVAS)

    alphas = _scan_alpha_row(out, rect, CANVAS)
    diffs = [abs(b - a) for a, b in zip(alphas, alphas[1:])]
    assert max(diffs) < 150   # looser than halo's <= 4 — a panel is still
                              # hard-clipped to its rect, just no longer a
                              # single-pixel cliff to get there
    assert max(diffs) > 4     # and genuinely looser, not accidentally as
                              # soft as a halo


# ===========================================================================
# v2 BODY wave: layer-after-text drawing over already-drawn ink (the
# mechanism woven_emblem's `weave` ornament relies on) — v2.2 wave,
# deliverable 4 then added a general contact guard on top: art-after-text
# is still allowed to paint over a SMALL amount of ink (this is how a
# weave/notch/scatter motif is meant to graze a letterform's edge — every
# archetype-level render elsewhere in this suite that asserts
# `report.warnings == []` with a later art layer already proves that path
# stays silent), but no longer allowed to swallow a whole required text
# slot's ink silently.
# ===========================================================================

def test_an_art_layer_that_would_fully_swallow_required_text_gets_guarded(tmp_path):
    # v2.2 wave, deliverable 4 changed this scenario's outcome on purpose —
    # the v2 BODY wave's own version of this test asserted the opposite
    # (the title's ink appeared NOWHERE in the final image). An opaque,
    # full-canvas "cover"-fit art layer drawn immediately after a REQUIRED
    # text layer is not eligible for the narrower contain-fit sandwich path
    # at all (fit != "contain") — so the general ornament-vs-text contact
    # guard catching it here proves it really does apply to "any art layer
    # after any text layer," not just contain-fit sandwich pairs.
    overlay_rgb = (255, 200, 0)
    _flat_png(tmp_path / "overlay.png", CANVAS, overlay_rgb)
    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.1, y=0.3, w=0.8, h=0.3),
                     font_family="Spectral", size_min=0.15, size_max=0.15, max_lines=1)
    overlay = ArtSlot(id="weave", asset="overlay.png", fit="cover")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[overlay], scrims=[], text=[title],
                     layers=[LayerRef(kind="text", ref="title"),
                            LayerRef(kind="art", ref="weave")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    # No amount of sideways nudging can save a full-canvas opaque overlay,
    # so the guard degrades: title now draws ON TOP of the overlay instead
    # of being buried by it — provably more than one color on the page.
    assert len(set(image.getdata())) > 1
    assert report.occlusion["title<-weave"] == 0.0
    assert any("weave" in w and "title" in w and "instead of underneath" in w
              for w in report.warnings)


# ===========================================================================
# v2.1 BODY-fix wave: title occlusion guard (fix 2) — a contain-fit art
# layer drawn after a text layer (the cutout_sandwich shape) may overlap
# that text's zone, but not bury its ink.
# ===========================================================================

def _dark_blob_png(path: Path, size: tuple[int, int],
                   fill: tuple[int, int, int, int] = (10, 10, 10, 255)) -> None:
    """An opaque circle on a transparent field, real alpha at the margins
    (so _degrade_opaque_focal's own opaque-asset check passes it through
    rather than intercepting it first) — the title occlusion guard is what
    this section means to exercise, not the separate opaque-focal swap."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=fill)
    img.save(path)


def _sandwich_spec(tmp_path: Path, scale: float) -> CoverSpec:
    """A left-aligned title over a centered, dark, opaque-circle `focal`
    drawn immediately after it (fit="contain") — `scale` controls how much
    horizontal room the occlusion guard's anchor search has to work with."""
    _dark_blob_png(tmp_path / "focal.png", (200, 200))
    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.05, y=0.35, w=0.9, h=0.2),
                     font_family="Spectral", size_min=0.15, size_max=0.15,
                     max_lines=1, align="left")
    focal = ArtSlot(id="focal", asset="focal.png", fit="contain", transparent=True,
                    anchor=[0.5, 0.5], scale=scale)
    return CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[focal], scrims=[], text=[title],
                     layers=[LayerRef(kind="text", ref="title"),
                            LayerRef(kind="art", ref="focal")])


def test_occlusion_guard_shifts_the_anchor_when_the_default_position_buries_the_title(tmp_path):
    # At its declared anchor (0.5, 0.5) this focal covers ~44% of the
    # title's own ink — over the 30% limit. +0.10 (~31%) and -0.10 (~56%)
    # both still fail; +0.20 is the first candidate that clears it (~16%),
    # so this also exercises the search actually trying more than one
    # offset before succeeding, not just getting lucky on the first try.
    spec = _sandwich_spec(tmp_path, scale=0.35)

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.occlusion["title<-focal"] <= 0.30
    assert not any("focal" in w and "title" in w for w in report.warnings)
    # The title's own ink is genuinely on the page — a solid-coverage pixel
    # from the "A" glyph's stroke (see typeset.text_mask for this slot)
    # shows the text color, not the blob's dark fill.
    assert image.getpixel((54, 253)) == ImageColor.getrgb("#f5f1e8")


def test_occlusion_guard_degrades_to_drawing_text_on_top_when_no_offset_clears_it(tmp_path):
    # This focal is wide enough (scale=0.6) that no anchor offset in
    # _OCCLUSION_ANCHOR_OFFSETS gets it under the 30% limit (the best any
    # of the six manages is ~53%) — every offset failing is exactly the
    # §5.2.3 degrade trigger: draw the text back on top instead.
    spec = _sandwich_spec(tmp_path, scale=0.6)

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    # Post-degrade, the composited cover carries none of this pair's
    # occlusion any more — text draws on top of the (now-earlier) art.
    assert report.occlusion["title<-focal"] == 0.0
    assert any("focal" in w and "title" in w and "instead of underneath" in w
              for w in report.warnings)
    assert image.getpixel((54, 253)) == ImageColor.getrgb("#f5f1e8")


# ===========================================================================
# v2.2 wave, deliverable 3: line-gap snap (ArtSlot.snap == "line_gap") — an
# ornament drawn immediately after a text layer centers itself in the
# LARGEST real gap between that text's own fitted lines, instead of a fixed
# anchor point that has no idea where the glyphs actually landed.
# ===========================================================================

def _round_blob_png(path: Path, size: tuple[int, int] = (100, 100),
                    rgb: tuple[int, int, int] = (255, 255, 0)) -> None:
    """A solid circle on a transparent field, margin clear of the source's
    own edges — real per-pixel alpha variation (unlike a flat opaque
    rectangle), so _degrade_opaque_focal's opaque-asset check never
    intercepts this before the line-gap snap ever gets a turn."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((5, 5, size[0] - 5, size[1] - 5), fill=(*rgb, 255))
    img.save(path)


def _two_line_title() -> TextSlot:
    """A title whose own balanced-break search reliably lands on exactly
    two lines at this CANVAS/zone/font combination ("ASH AND" / "HONEY") —
    verified directly against typeset.fit_text, not just assumed, so a
    future font-metric change that broke this assumption would fail the
    test loudly rather than silently exercising the one-line fallback path
    instead."""
    return TextSlot(id="title", content="ASH AND HONEY",
                    zone=Zone(x=0.1, y=0.25, w=0.8, h=0.35),
                    font_family="Spectral", case="upper", valign="bottom",
                    size_min=0.05, size_max=0.15, max_lines=2)


def test_line_gap_snap_centers_a_small_ornament_in_the_gap_with_no_glyph_contact(tmp_path):
    _round_blob_png(tmp_path / "ornament.png")
    title = _two_line_title()
    fit = typeset.fit_text(title, CANVAS)
    assert len(fit.lines) == 2, fit.lines   # confirms the two-line premise
    ink_boxes = [b for b in typeset.line_ink_boxes(title, fit, CANVAS) if b is not None]
    expected_gap_center = (ink_boxes[0][3] + ink_boxes[1][1]) / 2.0

    weave = ArtSlot(id="weave", asset="ornament.png", fit="contain", transparent=True,
                   anchor=[0.5, 0.5], scale=0.03, snap="line_gap")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[weave], scrims=[], text=[title],
                     layers=[LayerRef(kind="text", ref="title"),
                            LayerRef(kind="art", ref="weave")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    # Clean fit on the very first try: no nudge needed, zero measured
    # contact with the title's own ink.
    assert report.occlusion["title<-weave"] == 0.0
    assert not any("weave" in w for w in report.warnings)

    yellow = [(x, y) for y in range(CANVAS[1]) for x in range(CANVAS[0])
             if image.getpixel((x, y))[:3] == (255, 255, 0)]
    assert yellow, "the ornament never rendered at all"
    ys = [y for _x, y in yellow]
    xs = [x for x, _y in yellow]
    measured_center = (min(ys) + max(ys)) / 2.0
    assert abs(measured_center - expected_gap_center) <= 1.5
    # Horizontal center follows the slot's own anchor.x, not the gap.
    assert abs((min(xs) + max(xs)) / 2.0 - 0.5 * CANVAS[0]) <= 1.5

    # Pixel-verified no glyph contact: not one ornament pixel coincides
    # with a title glyph pixel (the title's own ink color never touches a
    # yellow one — sampled by re-measuring the title's own mask directly).
    ink_mask = typeset.text_mask(title, fit, CANVAS)
    for x, y in yellow:
        assert ink_mask.getpixel((x, y)) == 0, f"ornament pixel ({x},{y}) sits on glyph ink"


def test_line_gap_snap_falls_back_to_just_below_the_last_baseline_for_one_line(tmp_path):
    _round_blob_png(tmp_path / "ornament.png")
    # Same zone/font as the two-line case, but short enough content that
    # the fit search settles on a single line — no internal gap to snap
    # into at all.
    title = TextSlot(id="title", content="ASH", zone=Zone(x=0.1, y=0.25, w=0.8, h=0.35),
                     font_family="Spectral", case="upper", valign="bottom",
                     size_min=0.05, size_max=0.15, max_lines=2)
    fit = typeset.fit_text(title, CANVAS)
    assert len(fit.lines) == 1, fit.lines
    ink_bottom = typeset.line_ink_boxes(title, fit, CANVAS)[0][3]

    weave = ArtSlot(id="weave", asset="ornament.png", fit="contain", transparent=True,
                   anchor=[0.5, 0.5], scale=0.03, snap="line_gap")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[weave], scrims=[], text=[title],
                     layers=[LayerRef(kind="text", ref="title"),
                            LayerRef(kind="art", ref="weave")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.occlusion["title<-weave"] == 0.0
    assert not any("weave" in w for w in report.warnings)

    ys = [y for y in range(CANVAS[1]) for x in range(CANVAS[0])
         if image.getpixel((x, y))[:3] == (255, 255, 0)]
    # Below the last line's own ink, never overlapping or above it.
    assert min(ys) >= ink_bottom


def test_line_gap_snap_warns_and_falls_back_when_not_a_valid_sandwich(tmp_path):
    # snap="line_gap" on a slot that ISN'T a contain-fit layer immediately
    # after a non-empty text layer has nothing to snap against — documented
    # fallback (drawn normally) plus a warning, the same convention
    # corners/scatter already use for their own precondition failures.
    _round_blob_png(tmp_path / "ornament.png")
    weave = ArtSlot(id="weave", asset="ornament.png", fit="cover", snap="line_gap")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[weave], scrims=[], text=[],
                     layers=[LayerRef(kind="art", ref="weave")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("weave" in w and "snap" in w and "line_gap" in w
              for w in report.warnings)
    assert (255, 255, 0) in set(image.getdata())   # still drew something


# ===========================================================================
# v2.1 BODY-fix wave: art-vs-ground contrast floor (fix 3) — silhouette and
# duotone commit an art slot's whole shape to one ink color; that color must
# actually differ from whatever ground it sits on.
# ===========================================================================

def test_dark_silhouette_on_a_dark_ground_forces_the_accent_switch(tmp_path):
    _flat_png(tmp_path / "background.png", CANVAS, (10, 10, 12))   # near-black ground
    # _silhouette only reads the source's ALPHA (never its RGB — see that
    # function's own docstring), so the fill color here is irrelevant; a
    # fully opaque circle is all that matters for shape.
    _dark_blob_png(tmp_path / "focal.png", (200, 200), fill=(255, 255, 255, 255))

    # primary is near-black too — silhouette-on-primary would be
    # essentially invisible against the near-black background.
    palette = _palette(background="#0a0a0c", primary="#0d0d10", accent="#f2c744")
    background = ArtSlot(id="background", asset="background.png", fit="cover")
    focal = ArtSlot(id="focal", asset="focal.png", fit="contain", transparent=True,
                    anchor=[0.5, 0.5], scale=0.4, treatment="silhouette")
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=palette, art=[background, focal], scrims=[], text=[],
                     layers=[LayerRef(kind="art", ref="background"),
                            LayerRef(kind="art", ref="focal")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("focal" in w and "silhouette" in w for w in report.warnings)
    # The silhouette's own opaque color is no longer primary (near-black) —
    # it cleared the floor by switching toward accent/white, so its actual
    # fill color now differs meaningfully from the near-black ground.
    colors = {c for c in image.getdata() if c != (10, 10, 12)}
    assert colors   # the shape is still there
    for c in colors:
        # every non-background pixel is far enough from the ground to be
        # visibly a different color, not a near-black-on-near-black smear
        assert sum(abs(a - b) for a, b in zip(c, (10, 10, 12))) > 60


# ===========================================================================
# v2.1 BODY-fix wave: dead-band metric (fix 4) — the tallest empty vertical
# stretch of the finished cover, as a fraction of canvas height.
# ===========================================================================

def test_dead_band_warns_on_a_deliberately_gappy_spec(tmp_path):
    _flat_png(tmp_path / "background.png", CANVAS, (20, 20, 24))
    background = ArtSlot(id="background", asset="background.png", fit="cover")
    # A single small author line pinned to the very bottom — everything
    # above it (the whole top ~85% of the canvas) is flat, unbroken
    # background with nothing crossing it.
    author = TextSlot(id="author", content="J. R. VANCE",
                      zone=Zone(x=0.1, y=0.92, w=0.8, h=0.06),
                      font_family="Spectral", size_min=0.03, size_max=0.03,
                      max_lines=1)
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[background], scrims=[], text=[author],
                     layers=[LayerRef(kind="art", ref="background"),
                            LayerRef(kind="text", ref="author")])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.dead_band_frac >= 0.28
    assert any("empty band" in w for w in report.warnings)


def test_dead_band_is_silent_when_ink_regularly_breaks_up_the_canvas(tmp_path):
    _flat_png(tmp_path / "background.png", CANVAS, (20, 20, 24))
    background = ArtSlot(id="background", asset="background.png", fit="cover")
    # Five short lines spread evenly down the whole canvas — nowhere is
    # there a band anywhere near 28% of the height with nothing crossing it.
    text = [
        TextSlot(id="title", content="ONE TWO THREE",
                 zone=Zone(x=0.1, y=0.06, w=0.8, h=0.10),
                 font_family="Spectral", size_min=0.05, size_max=0.05, max_lines=1),
        TextSlot(id="subtitle", content="FOUR FIVE SIX",
                 zone=Zone(x=0.1, y=0.30, w=0.8, h=0.10), optional=True,
                 font_family="Spectral", size_min=0.05, size_max=0.05, max_lines=1),
        TextSlot(id="author", content="SEVEN EIGHT NINE",
                 zone=Zone(x=0.1, y=0.54, w=0.8, h=0.10),
                 font_family="Spectral", size_min=0.05, size_max=0.05, max_lines=1),
        TextSlot(id="series", content="TEN ELEVEN TWELVE",
                 zone=Zone(x=0.1, y=0.78, w=0.8, h=0.10), optional=True,
                 font_family="Spectral", size_min=0.05, size_max=0.05, max_lines=1),
    ]
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_palette(), art=[background], scrims=[], text=text,
                     layers=[LayerRef(kind="art", ref="background"),
                            *(LayerRef(kind="text", ref=t.id) for t in text)])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.dead_band_frac < 0.28
    assert not any("empty band" in w for w in report.warnings)


# -- frame-containment clamp (v2.1, owner-reported frame bleed) ---------------

def _framed_spec(title_zone, title_content="AN EXTREMELY WIDE TITLE LINE",
                 frame_procedural="rule_frame"):
    # Minimal hand-built spec: procedural ground + a frame-family member +
    # one title. `frame_procedural` defaults to the original "rule_frame"
    # so every pre-existing caller keeps testing exactly what it always
    # did; the v2.2 wave's frame-family clamp test sweeps every sibling
    # through the same parameter.
    from docproof.cover.model import (ArtSlot, CoverSpec, LayerRef, TextSlot,
                                      Zone)
    return CoverSpec(
        archetype="big_type", concept_name="Frame Probe",
        rationale="frame clamp regression", palette=_TEST_PALETTE,
        art=[ArtSlot(id="background"),
             ArtSlot(id="frame", procedural=frame_procedural)],
        scrims=[],
        text=[TextSlot(id="title", content=title_content,
                       zone=Zone(**title_zone), font_family="Spectral",
                       size_min=0.02, size_max=0.06, max_lines=2)],
        layers=[LayerRef(kind="art", ref="background"),
                LayerRef(kind="art", ref="frame"),
                LayerRef(kind="text", ref="title")])


def test_frame_clamp_keeps_title_ink_inside_the_rule_frame(tmp_path):
    # An over-wide declared zone (nearly full-bleed) must not produce ink
    # across the frame: render with and without the title and require the
    # difference to live entirely inside the frame's inner rect.
    from docproof.cover.compose import _frame_inner_rect
    from PIL import ImageChops
    zone = dict(x=0.02, y=0.30, w=0.96, h=0.25)
    with_title, _ = compose(_framed_spec(zone), tmp_path, canvas=CANVAS)
    without_title, _ = compose(_framed_spec(zone, title_content=""),
                               tmp_path, canvas=CANVAS)
    diff = ImageChops.difference(with_title.convert("RGB"),
                                 without_title.convert("RGB"))
    fx, fy, fw, fh = _frame_inner_rect(CANVAS)
    left = round(fx * CANVAS[0]); top = round(fy * CANVAS[1])
    right = round((fx + fw) * CANVAS[0]); bottom = round((fy + fh) * CANVAS[1])
    px = diff.load()
    outside = [(x, y) for y in range(CANVAS[1]) for x in range(CANVAS[0])
               if (x < left or x >= right or y < top or y >= bottom)
               and px[x, y] != (0, 0, 0)]
    assert outside == []
    # And the title genuinely rendered somewhere inside.
    assert diff.getbbox() is not None


def test_frame_clamp_refuses_to_crush_a_zone_and_warns(tmp_path):
    # A zone almost entirely outside the frame would clamp below 40% of its
    # width — the clamp must refuse, keep the declared zone, and warn.
    zone = dict(x=0.0, y=0.30, w=0.14, h=0.25)
    _, report = compose(_framed_spec(zone), tmp_path, canvas=CANVAS)
    assert any("rule frame" in w and "crush" in w for w in report.warnings)


# ===========================================================================
# v2.2 wave, deliverable 7: the frame family + interactions
# ===========================================================================

_FRAME_KINDS = ["rule_frame", "frame_hairline", "frame_thickthin",
               "frame_corners", "frame_deco", "frame_octagon"]


@pytest.mark.parametrize("kind", _FRAME_KINDS)
def test_frame_clamp_triggers_on_every_frame_family_kind(tmp_path, kind):
    # _frame_clamp_text's gating (generalized from "procedural == rule_frame
    # literally" to ANY FRAME_PROCEDURAL_KINDS member) must fire for every
    # sibling, not just the original rule_frame — all six share the exact
    # same inset geometry, so an over-wide title zone gets clamped
    # identically regardless of which one a spec actually draws.
    zone = dict(x=0.02, y=0.30, w=0.96, h=0.25)
    with_title, _ = compose(_framed_spec(zone, frame_procedural=kind), tmp_path, canvas=CANVAS)
    without_title, _ = compose(
        _framed_spec(zone, title_content="", frame_procedural=kind), tmp_path, canvas=CANVAS)
    from docproof.cover.compose import _frame_inner_rect
    from PIL import ImageChops
    diff = ImageChops.difference(with_title.convert("RGB"), without_title.convert("RGB"))
    fx, fy, fw, fh = _frame_inner_rect(CANVAS)
    left = round(fx * CANVAS[0]); top = round(fy * CANVAS[1])
    right = round((fx + fw) * CANVAS[0]); bottom = round((fy + fh) * CANVAS[1])
    px = diff.load()
    outside = [(x, y) for y in range(CANVAS[1]) for x in range(CANVAS[0])
              if (x < left or x >= right or y < top or y >= bottom)
              and px[x, y] != (0, 0, 0)]
    assert outside == [], f"{kind}: title ink crossed the frame's own inner rect"
    assert diff.getbbox() is not None   # and it genuinely rendered somewhere


def test_apply_frame_notches_erases_only_inside_the_padded_target_bbox():
    from docproof.cover.compose import _apply_frame_notches, _NOTCH_PAD_FRACTION

    canvas = CANVAS
    frame_slot = ArtSlot(id="frame", procedural="rule_frame", notch_for="emblem")
    target_slot = ArtSlot(id="emblem")
    art_by_id = {"frame": frame_slot, "emblem": target_slot}

    frame_rgb = (201, 162, 39)
    frame_img = Image.new("RGBA", canvas, (*frame_rgb, 255))
    target_img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    box = (150, 250, 250, 350)
    ImageDraw.Draw(target_img).rectangle(box, fill=(255, 255, 255, 255))
    positioned = {"frame": frame_img, "emblem": target_img}
    warnings: list[str] = []

    _apply_frame_notches(positioned, art_by_id, canvas, warnings)

    result = positioned["frame"]
    alpha = result.getchannel("A")
    cw, ch = canvas
    pad_x = round(_NOTCH_PAD_FRACTION * cw)
    pad_y = round(_NOTCH_PAD_FRACTION * ch)

    # Absent (fully erased) well inside the padded hole.
    assert alpha.getpixel(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)) == 0
    # Absent right at the box's own edge too (the pad only grows the hole).
    assert alpha.getpixel((box[0] + 2, box[1] + 2)) == 0
    # Present just outside the padded hole on every side.
    assert alpha.getpixel((box[0] - pad_x - 3, (box[1] + box[3]) // 2)) == 255
    assert alpha.getpixel((box[2] + pad_x + 3, (box[1] + box[3]) // 2)) == 255
    assert alpha.getpixel(((box[0] + box[2]) // 2, box[1] - pad_y - 3)) == 255
    assert alpha.getpixel(((box[0] + box[2]) // 2, box[3] + pad_y + 3)) == 255
    # Present far away from the notch entirely.
    assert alpha.getpixel((10, 10)) == 255


def test_frame_notch_composes_end_to_end_around_an_overlapping_emblem(tmp_path):
    # The interaction move end to end: a frame_octagon slot notched around
    # an emblem drawn later in z-order (so the notch mechanism has to see
    # past the single-forward-pass ordering constraint — see
    # _apply_frame_notches' own docstring) — frame lines vanish under the
    # emblem's own footprint, survive everywhere else.
    _round_blob_png(tmp_path / "emblem.png", (140, 140))
    frame = ArtSlot(id="frame", procedural="frame_octagon", notch_for="emblem")
    emblem = ArtSlot(id="emblem", asset="emblem.png", fit="contain", transparent=True,
                     anchor=[0.5, 0.5], scale=0.30)
    spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                     palette=_TEST_PALETTE, art=[frame, emblem], scrims=[], text=[],
                     layers=[LayerRef(kind="art", ref="frame"),
                            LayerRef(kind="art", ref="emblem")])

    notched, _ = compose(spec, tmp_path, canvas=CANVAS)

    plain_spec = CoverSpec(archetype="synthetic", concept_name="t", rationale="t",
                           palette=_TEST_PALETTE,
                           art=[ArtSlot(id="frame", procedural="frame_octagon")],
                           scrims=[], text=[],
                           layers=[LayerRef(kind="art", ref="frame")])
    plain, _ = compose(plain_spec, tmp_path, canvas=CANVAS)

    # Somewhere under the emblem's own footprint, the notched frame's pixel
    # differs from the plain (un-notched) frame's — proof the notch
    # actually erased something there, not just a no-op.
    cw, ch = CANVAS
    cx, cy = cw // 2, ch // 2
    span = round(0.30 * ch) // 2
    differs = any(
        notched.getpixel((x, y)) != plain.getpixel((x, y))
        for y in range(max(0, cy - span), min(ch, cy + span))
        for x in range(max(0, cx - span), min(cw, cx + span)))
    assert differs, "no pixel difference under the emblem — the notch did nothing"

    # And far from the emblem, in a corner well outside any notch pad, the
    # frame reads identically whether notched or not.
    assert notched.getpixel((2, 2)) == plain.getpixel((2, 2))
