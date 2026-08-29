"""The effects rack (§7.4a): slot treatments (duotone/silhouette/posterize/
sticker), double exposure (mask_from), the widened background/focal/focal2/
foreground/texture art-slot vocabulary, knockout/art_fill text modes,
mirrored corner frames, motif scatter, and the archetype retrofits that
adopt them.

No network anywhere. Canvases are tiny (400x640) — all geometry is
fractional, so this exercises the exact same code paths as the real
1600x2560 canvas in a fraction of the time (the same reasoning
test_cover_compose.py's own docstring gives). Every compose()-level test
builds its own minimal CoverSpec by hand (art/text/layers assembled
directly, not via build_spec/archetypes) for precise control over exactly
which slot gets which effect — the same idiom test_cover_compose.py already
uses whenever a test needs pixel-exact control over what the composer sees.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageOps
from pydantic import ValidationError

from docproof.cover.archetypes import (ARCHETYPES, Archetype, ArchetypeArt,
                                       ArchetypeText, ArchetypeZone)
from docproof.cover.compose import (_ART_FILL_OUTLINE_FRACTION, _dilate_mask,
                                    _padded_rect, compose)
from docproof.cover.model import (ArtPrompt, ArtSlot, Brief, CoverSpec,
                                  Direction, Directions, LayerRef, Palette,
                                  ScrimSpec, TextSlot, Zone, build_spec)
from docproof.cover.typeset import fit_text, text_mask
from docproof.providers import strict_json_schema

CANVAS = (400, 640)


# -- fixtures / helpers --------------------------------------------------------

def _palette(**overrides) -> Palette:
    data = dict(background="#101820", primary="#c9382c", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _art(id="focal", **overrides) -> ArtSlot:  # noqa: A002 - matches the field name
    data = dict(id=id, transparent=True, fit="contain")
    data.update(overrides)
    return ArtSlot(**data)


def _text(id="title", **overrides) -> TextSlot:  # noqa: A002
    data = dict(id=id, content="Ash", zone=Zone(x=0.1, y=0.1, w=0.8, h=0.2),
               font_family="Spectral", size_min=0.05, size_max=0.05)
    data.update(overrides)
    return TextSlot(**data)


def _spec(art=(), text=(), layers=(), scrims=(), palette=None, **overrides) -> CoverSpec:
    data = dict(archetype="synthetic", concept_name="Test Concept",
               rationale="test rationale", palette=palette or _palette(),
               art=list(art), scrims=list(scrims), text=list(text),
               layers=list(layers))
    data.update(overrides)
    return CoverSpec(**data)


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary")
    data.update(overrides)
    return Brief(**data)


def _direction_for_synthetic(archetype_name: str, art_prompts: list[dict]) -> Direction:
    return Direction(concept_name="Test", rationale="test", archetype=archetype_name,
                     palette=_palette(), title_font="Spectral", author_font="Spectral",
                     art_prompts=art_prompts, texture=False)


def _flat_opaque_png(path: Path, size: tuple[int, int], rgb: tuple[int, int, int]) -> None:
    Image.new("RGBA", size, (*rgb, 255)).save(path)


def _blob_png(path: Path, size: tuple[int, int] = (200, 200),
             fg: tuple[int, int, int, int] = (200, 50, 50, 255)) -> None:
    """A hard-edged (no anti-aliasing) circle on a transparent field — every
    pixel's alpha is unambiguously 0 or 255, so silhouette/sticker/mask_from
    tests never land exactly on the threshold boundary."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    margin = round(min(size) * 0.1)
    ImageDraw.Draw(img).ellipse((margin, margin, size[0] - margin, size[1] - margin), fill=fg)
    img.save(path)


def _asymmetric_blob_png(path: Path, size: tuple[int, int] = (100, 100)) -> None:
    """A triangle, not a circle: mirror(shape) != shape and flip(shape) !=
    shape, so a corners-mirroring test can tell "actually mirrored" apart
    from "just copied the same tile four times.\""""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    w, h = size
    ImageDraw.Draw(img).polygon(
        [(w * 0.1, h * 0.15), (w * 0.85, h * 0.35), (w * 0.3, h * 0.9)],
        fill=(210, 60, 40, 255))
    img.save(path)


def _gray_gradient_png(path: Path, size: tuple[int, int] = (200, 100)) -> None:
    """A fully opaque horizontal grayscale ramp — luminance actually varies
    across it, which duotone/posterize tests need to see more than one
    output bucket."""
    img = Image.new("L", size)
    px = img.load()
    for x in range(size[0]):
        v = round(255 * x / max(1, size[0] - 1))
        for y in range(size[1]):
            px[x, y] = v
    img.convert("RGBA").save(path)


def _find_interior_and_ring_pixel(mask: Image.Image, ring: Image.Image):
    """The first (mask==255, ring==0) pixel ("interior") and the first
    (ring==255) pixel ("ring"), in one raster pass. Used to probe
    art_fill's two regions without a full-canvas assertion."""
    mpx, rpx = mask.load(), ring.load()
    w, h = mask.size
    interior = ring_xy = None
    for y in range(h):
        for x in range(w):
            if interior is None and mpx[x, y] == 255 and rpx[x, y] == 0:
                interior = (x, y)
            if ring_xy is None and rpx[x, y] == 255:
                ring_xy = (x, y)
            if interior is not None and ring_xy is not None:
                return interior, ring_xy
    return interior, ring_xy


# ===========================================================================
# model.py: strict-schema safety (§7.4a: "no dicts/tuples on any wire model")
# ===========================================================================

def _walk_schema_nodes(node):
    """Every dict in a JSON-schema tree, including $defs entries (a plain
    dict value, walked like anything else) — no $ref resolution needed since
    every node, wherever it lives in the tree, gets visited on its own."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_schema_nodes(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema_nodes(value)


@pytest.mark.parametrize("model_cls", [CoverSpec, Directions])
def test_strict_schema_has_no_free_form_objects_or_tuple_arrays(model_cls):
    # The two OpenAI-rejection patterns this project has hit (§7.4a): a
    # free-form dict (pydantic emits `additionalProperties` as a SCHEMA, not
    # `False`, when there is no `properties` key to enumerate) and a tuple
    # (pydantic emits `prefixItems`, which strict mode rejects outright).
    # ArtSlot.anchor/offset are already `list[float]` for exactly this
    # reason (see their own docstring) — this test is what proves the whole
    # spec, not just those two fields, stays clean as the effects rack adds
    # new fields on top.
    schema = strict_json_schema(model_cls)
    for node in _walk_schema_nodes(schema):
        assert "prefixItems" not in node, node
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node


# ===========================================================================
# model.py: ArtSlot / ArtPrompt / TextSlot new fields
# ===========================================================================

def test_artslot_effects_rack_fields_default_off():
    slot = ArtSlot(id="focal")
    assert slot.treatment == "none"
    assert slot.mask_from == ""
    assert slot.corners is False
    assert slot.scatter == 0


@pytest.mark.parametrize("slot_id", ["background", "focal", "focal2", "foreground", "texture"])
def test_artslot_id_accepts_every_widened_slot_id(slot_id):
    assert ArtSlot(id=slot_id).id == slot_id


@pytest.mark.parametrize("treatment", ["none", "duotone", "silhouette", "posterize", "sticker"])
def test_artslot_accepts_every_documented_treatment(treatment):
    assert ArtSlot(id="focal", treatment=treatment).treatment == treatment


def test_artslot_rejects_an_undocumented_treatment():
    with pytest.raises(ValidationError):
        ArtSlot(id="focal", treatment="sepia")


@pytest.mark.parametrize("value", [0, 2, 6, 12])
def test_artslot_scatter_accepts_zero_and_the_documented_range(value):
    assert ArtSlot(id="focal", scatter=value).scatter == value


@pytest.mark.parametrize("value", [1, 13, -1])
def test_artslot_scatter_rejects_outside_the_documented_range(value):
    with pytest.raises(ValidationError):
        ArtSlot(id="focal", scatter=value)


def test_artprompt_slot_accepts_every_widened_slot_id():
    for slot_id in ("background", "focal", "focal2", "foreground", "texture"):
        assert ArtPrompt(slot=slot_id, prompt="x").slot == slot_id


def test_artprompt_treatment_defaults_to_none_and_is_settable():
    assert ArtPrompt(slot="focal2", prompt="x").treatment == "none"
    assert ArtPrompt(slot="focal2", prompt="x", treatment="duotone").treatment == "duotone"


def test_textslot_mode_defaults_to_fill():
    assert _text().mode == "fill"


@pytest.mark.parametrize("mode", ["fill", "knockout", "art_fill"])
def test_textslot_accepts_every_documented_mode(mode):
    assert _text(mode=mode).mode == mode


def test_textslot_rejects_an_undocumented_mode():
    with pytest.raises(ValidationError):
        _text(mode="reverse")


# ===========================================================================
# model.py: CoverSpec.mask_from validation ("must exist and precede it")
# ===========================================================================

def test_coverspec_mask_from_a_valid_earlier_reference_passes():
    art = [_art(id="background", fit="cover"),
          _art(id="focal", mask_from="background")]
    layers = [LayerRef(kind="art", ref="background"), LayerRef(kind="art", ref="focal")]
    spec = _spec(art=art, layers=layers)   # must not raise
    assert spec.art[1].mask_from == "background"


def test_coverspec_mask_from_unknown_slot_fails_validation():
    art = [_art(id="focal", mask_from="nonexistent")]
    with pytest.raises(ValidationError, match="mask_from"):
        _spec(art=art, layers=[LayerRef(kind="art", ref="focal")])


def test_coverspec_mask_from_referencing_a_later_slot_fails_validation():
    art = [_art(id="background", mask_from="focal", fit="cover"), _art(id="focal")]
    layers = [LayerRef(kind="art", ref="background"), LayerRef(kind="art", ref="focal")]
    with pytest.raises(ValidationError, match="must appear earlier"):
        _spec(art=art, layers=layers)


def test_coverspec_mask_from_self_reference_fails_validation():
    art = [_art(id="focal", mask_from="focal")]
    with pytest.raises(ValidationError):
        _spec(art=art, layers=[LayerRef(kind="art", ref="focal")])


def test_coverspec_mask_from_off_by_default_needs_no_validation():
    art = [_art(id="focal")]
    spec = _spec(art=art, layers=[LayerRef(kind="art", ref="focal")])
    assert spec.art[0].mask_from == ""


# ===========================================================================
# archetypes.py: ArchetypeArt / ArchetypeText new fields, Archetype-level
# mask_from validation, and the shipped retrofits
# ===========================================================================

def test_archetype_art_effects_rack_fields_default_off():
    art = ArchetypeArt(id="focal2", generatable=True)
    assert art.treatment == "none"
    assert art.mask_from == ""
    assert art.corners is False
    assert art.scatter == 0


@pytest.mark.parametrize("slot_id", ["background", "focal", "focal2", "foreground", "texture"])
def test_archetype_art_id_accepts_every_widened_slot_id(slot_id):
    assert ArchetypeArt(id=slot_id, generatable=False).id == slot_id


@pytest.mark.parametrize("value", [1, 13])
def test_archetype_art_scatter_rejects_outside_the_documented_range(value):
    with pytest.raises(ValidationError):
        ArchetypeArt(id="focal2", generatable=True, scatter=value)


def test_archetype_text_mode_defaults_to_fill():
    text = ArchetypeText(id="title", zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                         size_min=0.05, size_max=0.1)
    assert text.mode == "fill"


@pytest.mark.parametrize("mode", ["fill", "knockout", "art_fill"])
def test_archetype_text_accepts_every_documented_mode(mode):
    text = ArchetypeText(id="title", zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                         size_min=0.05, size_max=0.1, mode=mode)
    assert text.mode == mode


def _minimal_text() -> ArchetypeText:
    return ArchetypeText(id="title", zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                         size_min=0.05, size_max=0.1)


def test_archetype_mask_from_dangling_reference_fails_loudly():
    with pytest.raises(ValidationError, match="mask_from"):
        Archetype(name="x", describe="x", composition_note="x",
                 art=[ArchetypeArt(id="focal", generatable=True, mask_from="focal2")],
                 text=[_minimal_text()], layers=["focal", "title"])


def test_archetype_mask_from_referencing_a_later_slot_fails_loudly():
    with pytest.raises(ValidationError, match="must appear earlier"):
        Archetype(name="x", describe="x", composition_note="x",
                 art=[ArchetypeArt(id="background", generatable=True, mask_from="focal"),
                      ArchetypeArt(id="focal", generatable=True)],
                 text=[_minimal_text()], layers=["background", "focal", "title"])


def test_archetype_mask_from_valid_earlier_reference_loads_fine():
    archetype = Archetype(
        name="x", describe="x", composition_note="x",
        art=[ArchetypeArt(id="background", generatable=True),
            ArchetypeArt(id="focal", generatable=True, mask_from="background")],
        text=[_minimal_text()], layers=["background", "focal", "title"])
    assert archetype.art[1].mask_from == "background"


@pytest.mark.parametrize("name", ["cozy_mystery_graphic_stamp",
                                  "nonfiction_bold_colorblock_typographic",
                                  "thriller_bigtype_silhouette"])
def test_retrofitted_archetype_focal_icon_uses_silhouette_treatment(name):
    focal = next(a for a in ARCHETYPES[name].art if a.id == "focal")
    assert focal.treatment == "silhouette"


def test_romantasy_emblem_retrofit_mirrors_a_corner_ornament_without_touching_the_medallion():
    archetype = ARCHETYPES["romantasy_emblem"]
    focal2 = next(a for a in archetype.art if a.id == "focal2")
    assert focal2.corners is True
    assert focal2.generatable is True
    focal = next(a for a in archetype.art if a.id == "focal")
    assert focal.corners is False        # the central medallion is untouched
    assert "focal2" in archetype.layers
    assert archetype.layers.index("focal2") < archetype.layers.index("focal")


# ===========================================================================
# model.build_spec: treatment override precedence + archetype-only fields
# ===========================================================================

def _archetype_with_focal2(treatment: str = "none", text_mode: str = "fill",
                          **art_overrides) -> Archetype:
    art = ArchetypeArt(id="focal2", generatable=True, fit="contain", transparent=True,
                       treatment=treatment, **art_overrides)
    return Archetype(
        name="synthetic_effects", describe="test", composition_note="test",
        art=[art],
        text=[ArchetypeText(id="title", zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                            size_min=0.05, size_max=0.05, mode=text_mode)],
        layers=["focal2", "title"])


def test_build_spec_direction_treatment_overrides_the_archetype_default():
    archetype = _archetype_with_focal2(treatment="silhouette")
    direction = _direction_for_synthetic(
        "synthetic_effects",
        [{"slot": "focal2", "prompt": "x", "treatment": "posterize"}])
    spec = build_spec(direction, _brief(), archetype)
    focal2 = next(a for a in spec.art if a.id == "focal2")
    assert focal2.treatment == "posterize"


def test_build_spec_keeps_the_archetype_treatment_when_direction_says_none():
    # A direction's own default "none" reads as "no opinion", never as a
    # forced override of an archetype's own baked-in convention — see
    # build_spec's own comment on this exact point.
    archetype = _archetype_with_focal2(treatment="silhouette")
    direction = _direction_for_synthetic(
        "synthetic_effects", [{"slot": "focal2", "prompt": "x"}])
    spec = build_spec(direction, _brief(), archetype)
    focal2 = next(a for a in spec.art if a.id == "focal2")
    assert focal2.treatment == "silhouette"


def test_build_spec_treatment_defaults_to_none_when_neither_side_sets_it():
    archetype = _archetype_with_focal2()
    direction = _direction_for_synthetic(
        "synthetic_effects", [{"slot": "focal2", "prompt": "x"}])
    spec = build_spec(direction, _brief(), archetype)
    assert next(a for a in spec.art if a.id == "focal2").treatment == "none"


def test_build_spec_carries_corners_and_mask_from_from_the_archetype_only():
    archetype = _archetype_with_focal2(corners=True, mask_from="")
    direction = _direction_for_synthetic(
        "synthetic_effects", [{"slot": "focal2", "prompt": "x"}])
    spec = build_spec(direction, _brief(), archetype)
    focal2 = next(a for a in spec.art if a.id == "focal2")
    assert focal2.corners is True   # Direction has no field that could set this


def test_build_spec_carries_scatter_from_the_archetype_only():
    archetype = _archetype_with_focal2(scatter=5)
    direction = _direction_for_synthetic(
        "synthetic_effects", [{"slot": "focal2", "prompt": "x"}])
    spec = build_spec(direction, _brief(), archetype)
    assert next(a for a in spec.art if a.id == "focal2").scatter == 5


def test_build_spec_carries_text_mode_from_the_archetype():
    archetype = _archetype_with_focal2(text_mode="knockout")
    direction = _direction_for_synthetic(
        "synthetic_effects", [{"slot": "focal2", "prompt": "x"}])
    spec = build_spec(direction, _brief(), archetype)
    assert next(t for t in spec.text if t.id == "title").mode == "knockout"


# ===========================================================================
# typeset.text_mask: the glyph-coverage mask knockout/art_fill are built on
# ===========================================================================

def test_text_mask_empty_content_is_blank():
    slot = _text(content="", optional=True)
    fit = fit_text(slot, CANVAS)
    mask = text_mask(slot, fit, CANVAS)
    assert mask.mode == "L"
    assert mask.size == CANVAS
    assert mask.getextrema() == (0, 0)


def test_text_mask_has_ink_where_draw_text_would_have_ink():
    slot = _text(content="Ash", size_min=0.15, size_max=0.15, max_lines=1)
    fit = fit_text(slot, CANVAS)
    mask = text_mask(slot, fit, CANVAS)
    assert mask.getbbox() is not None
    assert mask.getextrema()[1] == 255   # some pixel is fully "ink"


def test_text_mask_bbox_matches_draw_texts_ink_bbox():
    from docproof.cover.typeset import draw_text
    slot = _text(content="Ash", size_min=0.15, size_max=0.15, max_lines=1)
    fit = fit_text(slot, CANVAS)
    mask = text_mask(slot, fit, CANVAS)
    drawn = draw_text(Image.new("RGBA", CANVAS, (0, 0, 0, 0)), slot, fit,
                      "#ffffff", None, CANVAS)
    assert mask.getbbox() == drawn.getbbox()


# ===========================================================================
# compose(): slot treatments (duotone/silhouette/posterize/sticker)
# ===========================================================================

def test_duotone_output_only_uses_colors_on_the_background_primary_ramp(tmp_path):
    # Built at the exact canvas size so `fit="cover"` is an identity crop —
    # a smaller source would get LANCZOS-upscaled and then CENTER-CROPPED,
    # which (for a source narrower than the canvas) only exposes a middle
    # slice of the gradient's range rather than its full 0..255 span.
    _gray_gradient_png(tmp_path / "art.png", CANVAS)
    bg_hex, fg_hex = "#102040", "#f0c020"
    palette = _palette(background=bg_hex, primary=fg_hex)
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="cover", treatment="duotone")],
                layers=[LayerRef(kind="art", ref="focal")], palette=palette)

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    bg, fg = ImageColor.getrgb(bg_hex), ImageColor.getrgb(fg_hex)
    allowed = {tuple(round(bg[c] + (fg[c] - bg[c]) * i / 255) for c in range(3))
              for i in range(256)}
    colors = set(image.getdata())
    assert colors <= allowed
    assert len(colors) > 1   # the gradient really did produce more than one step


def test_duotone_is_deterministic_across_composes(tmp_path):
    _gray_gradient_png(tmp_path / "art.png", (200, 100))
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="cover", treatment="duotone")],
                layers=[LayerRef(kind="art", ref="focal")])
    image1, _ = compose(spec, tmp_path, canvas=CANVAS)
    image2, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()


def test_silhouette_output_is_binary_transparent_or_flat_primary(tmp_path):
    _blob_png(tmp_path / "art.png", (200, 200))
    palette = _palette(primary="#33cc55")
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="contain", treatment="silhouette")],
                layers=[LayerRef(kind="art", ref="focal")], palette=palette)

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    primary_rgb = ImageColor.getrgb("#33cc55")
    colors = set(image.getdata())
    # (0, 0, 0): the untouched, still-transparent canvas beneath the blob's
    # own transparent margin — convert("RGB") drops alpha but keeps the RGB
    # a freshly-created transparent canvas was born with (see the module
    # note on this in the treatment tests below).
    assert colors <= {primary_rgb, (0, 0, 0)}
    assert primary_rgb in colors


def test_posterize_output_uses_at_most_four_palette_colors(tmp_path):
    # Same identity-crop reasoning as the duotone ramp test above: built at
    # the exact canvas size so `fit="cover"` never crops away part of the
    # gradient's range.
    _gray_gradient_png(tmp_path / "art.png", CANVAS)
    palette = _palette(background="#101010", primary="#ff2222",
                       accent="#22ff22", text="#f5f1e8")
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="cover", treatment="posterize")],
                layers=[LayerRef(kind="art", ref="focal")], palette=palette)

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    allowed = {ImageColor.getrgb(palette.get(r))
              for r in ("background", "primary", "accent", "text")}
    colors = set(image.getdata())
    assert colors <= allowed
    assert len(colors) >= 2   # the gradient spans more than one posterize bucket
    assert len(colors) <= 4


def test_sticker_adds_a_text_colored_outline_ring_around_a_transparent_cutout(tmp_path):
    _blob_png(tmp_path / "art.png", (200, 200))
    palette = _palette(text="#ffee00")
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="contain", treatment="sticker")],
                layers=[LayerRef(kind="art", ref="focal")], palette=palette)

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    outline_rgb = ImageColor.getrgb("#ffee00")
    colors = set(image.getdata())
    assert outline_rgb in colors
    assert (0, 0, 0) in colors
    assert report.warnings == []


def test_sticker_on_an_opaque_slot_is_a_no_op_with_a_warning(tmp_path):
    _flat_opaque_png(tmp_path / "art.png", (200, 200), (10, 20, 200))
    spec = _spec(art=[_art(id="focal", asset="art.png", fit="cover", treatment="sticker")],
                layers=[LayerRef(kind="art", ref="focal")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("sticker" in w and "focal" in w for w in report.warnings)
    assert set(image.getdata()) == {(10, 20, 200)}   # untreated — a real no-op


# ===========================================================================
# compose(): mask_from double exposure
# ===========================================================================

def test_mask_from_keeps_the_later_slots_pixels_only_inside_the_earlier_slots_shape(tmp_path):
    # Both sources are built at the exact canvas size so `fit="cover"` is an
    # identity crop with no LANCZOS resampling — a resized hard edge rings
    # (classic Lanczos overshoot/undershoot on a step function), which would
    # otherwise put off-palette colors at the shape's boundary and make a
    # whole-canvas color-set assertion fragile for reasons that have nothing
    # to do with mask_from itself. Sampling well inside vs. well outside the
    # shape sidesteps that entirely.
    _blob_png(tmp_path / "shape.png", CANVAS, fg=(200, 50, 50, 255))
    _flat_opaque_png(tmp_path / "pour.png", CANVAS, (255, 255, 0))
    art = [
        _art(id="focal", asset="shape.png", fit="cover"),
        _art(id="focal2", asset="pour.png", fit="cover", mask_from="focal"),
    ]
    layers = [LayerRef(kind="art", ref="focal"), LayerRef(kind="art", ref="focal2")]
    spec = _spec(art=art, layers=layers)

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    cw, ch = CANVAS
    inside = image.getpixel((cw // 2, ch // 2))     # deep in the blob's interior
    outside = image.getpixel((2, 2))                # well outside the blob's margin
    assert inside == (255, 255, 0)                  # poured through the shape
    assert outside != (255, 255, 0)                 # never poured outside it
    assert outside != (200, 50, 50)                 # and focal's own color never shows —
                                                     # focal2 fully overpaints wherever it pours


# ===========================================================================
# compose(): mirrored corner frame
# ===========================================================================

def test_corners_mirrors_the_ornament_into_all_four_corners_exactly(tmp_path):
    _asymmetric_blob_png(tmp_path / "ornament.png", (100, 100))
    spec = _spec(art=[_art(id="focal2", asset="ornament.png", corners=True, scale=0.25)],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    cw, ch = CANVAS
    k = round(0.25 * ch)   # a 100x100 (square) source -> target_w == target_h == k
    top_left = image.crop((0, 0, k, k))
    top_right = image.crop((cw - k, 0, cw, k))
    bottom_left = image.crop((0, ch - k, k, ch))
    bottom_right = image.crop((cw - k, ch - k, cw, ch))

    assert top_left.tobytes() != top_right.tobytes()   # genuinely asymmetric source
    assert top_right.tobytes() == ImageOps.mirror(top_left).tobytes()
    assert bottom_left.tobytes() == ImageOps.flip(top_left).tobytes()
    assert bottom_right.tobytes() == ImageOps.flip(ImageOps.mirror(top_left)).tobytes()
    assert report.warnings == []


def test_corners_on_an_opaque_slot_is_a_no_op_with_a_warning(tmp_path):
    # `scale` means something different once corners falls back to a normal
    # layer (a post-fill zoom multiplier — see _fit_cover) than it does
    # under corners placement (a fraction of canvas height): left at its
    # ArtSlot default (1.0) here on purpose, so the fallback fills the
    # canvas exactly like any other untreated cover-fit slot would.
    _flat_opaque_png(tmp_path / "art.png", (100, 100), (5, 5, 5))
    spec = _spec(art=[_art(id="focal2", asset="art.png", corners=True, fit="cover")],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("corners" in w and "focal2" in w for w in report.warnings)
    assert set(image.getdata()) == {(5, 5, 5)}   # fell back to one normal cover-fit layer


# ===========================================================================
# compose(): motif scatter
# ===========================================================================

def test_scatter_never_places_a_copy_inside_a_text_zones_padded_rect(tmp_path):
    _asymmetric_blob_png(tmp_path / "motif.png", (60, 60))
    # Present in spec.text (so scatter must avoid its zone) but absent from
    # spec.layers (so it is never actually drawn) — isolates "did a scatter
    # copy land here" from "did the title's own ink land here".
    title = _text(id="title", content="ASH", zone=Zone(x=0.0, y=0.0, w=1.0, h=0.3))
    spec = _spec(art=[_art(id="focal2", asset="motif.png", scatter=6)],
                text=[title], layers=[LayerRef(kind="art", ref="focal2")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    zone_rect = _padded_rect(title.zone, CANVAS)
    cropped = image.crop(zone_rect)
    assert set(cropped.getdata()) == {(0, 0, 0)}
    assert not any("only placed" in w for w in report.warnings)


def test_scatter_placement_is_deterministic_across_composes(tmp_path):
    _asymmetric_blob_png(tmp_path / "motif.png", (60, 60))
    spec = _spec(art=[_art(id="focal2", asset="motif.png", scatter=5)],
                layers=[LayerRef(kind="art", ref="focal2")])
    image1, _ = compose(spec, tmp_path, canvas=CANVAS)
    image2, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()


def test_scatter_warns_when_it_cannot_place_every_copy(tmp_path):
    _asymmetric_blob_png(tmp_path / "motif.png", (60, 60))
    # A padded avoid-rect covering all but a ~6px sliver of the canvas: no
    # ~90px-tall scatter copy can ever land, so every one of the 12
    # requested copies is guaranteed to be skipped.
    title = _text(id="title", content="ASH", zone=Zone(x=0.0, y=0.0, w=1.0, h=0.95))
    spec = _spec(art=[_art(id="focal2", asset="motif.png", scatter=12)],
                text=[title], layers=[LayerRef(kind="art", ref="focal2")])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("only placed 0 of 12" in w for w in report.warnings)


def test_scatter_on_an_opaque_slot_is_a_no_op_with_a_warning(tmp_path):
    _flat_opaque_png(tmp_path / "art.png", (100, 100), (7, 7, 7))
    spec = _spec(art=[_art(id="focal2", asset="art.png", scatter=4, fit="cover")],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("scatter" in w and "focal2" in w for w in report.warnings)
    assert set(image.getdata()) == {(7, 7, 7)}


# ===========================================================================
# compose(): knockout / art_fill text modes
# ===========================================================================

def test_knockout_punches_glyph_shapes_out_of_a_primary_panel(tmp_path):
    palette = _palette(primary="#f5f1e8")
    title = _text(id="title", content="ASH", mode="knockout",
                 zone=Zone(x=0.1, y=0.4, w=0.8, h=0.2), size_min=0.15,
                 size_max=0.15, max_lines=1)
    spec = _spec(text=[title], layers=[LayerRef(kind="text", ref="title")], palette=palette)

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    colors = set(image.getdata())
    primary_rgb = ImageColor.getrgb(palette.primary)
    assert primary_rgb in colors     # the panel
    assert (0, 0, 0) in colors       # the punched glyph holes (nothing beneath)
    assert "title" in report.contrast


def test_knockout_panel_color_escalation_and_flip_does_not_crash(tmp_path):
    # Mirrors test_cover_compose.py's own worst-case-gray fill-mode test,
    # with mode="knockout": background, primary (the panel color under
    # knockout), and scrim are all the same gray, so escalating a same-color
    # scrim can never improve contrast, forcing the cap-then-flip path —
    # proving that path runs cleanly for a PANEL color too (§7.4a:
    # "autopilot escalation/flip logic must not crash on them").
    worst_case_gray = "#767676"
    _flat_opaque_png(tmp_path / "bg.png", CANVAS, (0x76, 0x76, 0x76))
    palette = _palette(background=worst_case_gray, primary=worst_case_gray,
                       scrim=worst_case_gray)
    title = _text(id="title", content="ASH", mode="knockout",
                 zone=Zone(x=0.1, y=0.4, w=0.8, h=0.2), size_min=0.15,
                 size_max=0.15, max_lines=1)
    scrim = ScrimSpec(kind="panel", protects="title", strength=0.1)
    spec = _spec(
        art=[_art(id="background", asset="bg.png", fit="cover", transparent=False)],
        text=[title],
        layers=[LayerRef(kind="art", ref="background"), LayerRef(kind="scrim", ref="0"),
               LayerRef(kind="text", ref="title")],
        scrims=[scrim], palette=palette)

    image, report = compose(spec, tmp_path, canvas=CANVAS)   # must not raise

    assert report.scrim_final[0] == pytest.approx(0.85)
    assert any("title" in w for w in report.warnings)


def test_art_fill_leaves_the_glyph_interior_as_an_unaltered_window_with_an_outline_ring(tmp_path):
    ground_rgb = (30, 90, 150)
    _flat_opaque_png(tmp_path / "bg.png", CANVAS, ground_rgb)
    palette = _palette(background="#1e5a96", primary="#f5f1e8")
    title = _text(id="title", content="I", mode="art_fill",
                 zone=Zone(x=0.3, y=0.4, w=0.4, h=0.2), size_min=0.15,
                 size_max=0.15, max_lines=1, align="center")
    spec = _spec(art=[_art(id="background", asset="bg.png", fit="cover", transparent=False)],
                text=[title], layers=[LayerRef(kind="art", ref="background"),
                                      LayerRef(kind="text", ref="title")],
                palette=palette)

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    fit = fit_text(title, CANVAS)
    mask = text_mask(title, fit, CANVAS)
    ring = ImageChops.subtract(_dilate_mask(mask, _ART_FILL_OUTLINE_FRACTION * CANVAS[1]), mask)
    interior_xy, ring_xy = _find_interior_and_ring_pixel(mask, ring)

    assert interior_xy is not None and ring_xy is not None
    assert image.getpixel(interior_xy) == ground_rgb            # a true window
    assert image.getpixel(ring_xy) == ImageColor.getrgb(palette.primary)
    assert "title" in report.contrast


def test_art_fill_escalation_and_flip_does_not_crash(tmp_path):
    worst_case_gray = "#767676"
    _flat_opaque_png(tmp_path / "bg.png", CANVAS, (0x76, 0x76, 0x76))
    palette = _palette(background=worst_case_gray, primary=worst_case_gray,
                       scrim=worst_case_gray)
    title = _text(id="title", content="ASH", mode="art_fill",
                 zone=Zone(x=0.1, y=0.4, w=0.8, h=0.2), size_min=0.15,
                 size_max=0.15, max_lines=1)
    scrim = ScrimSpec(kind="panel", protects="title", strength=0.1)
    spec = _spec(
        art=[_art(id="background", asset="bg.png", fit="cover", transparent=False)],
        text=[title],
        layers=[LayerRef(kind="art", ref="background"), LayerRef(kind="scrim", ref="0"),
               LayerRef(kind="text", ref="title")],
        scrims=[scrim], palette=palette)

    image, report = compose(spec, tmp_path, canvas=CANVAS)   # must not raise

    assert report.scrim_final[0] == pytest.approx(0.85)


def test_knockout_and_art_fill_skip_empty_optional_slots_like_fill_does(tmp_path):
    subtitle = _text(id="subtitle", content="", optional=True, mode="knockout")
    spec = _spec(text=[subtitle], layers=[LayerRef(kind="text", ref="subtitle")])
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert "subtitle" not in report.contrast


# ===========================================================================
# compose(): _degrade_opaque_focal generalizes to focal2/foreground (item 5)
# ===========================================================================

@pytest.mark.parametrize("slot_id", ["focal2", "foreground"])
def test_degrade_opaque_focal_also_covers_the_new_slot_ids(tmp_path, slot_id):
    _flat_opaque_png(tmp_path / "background.png", CANVAS, (60, 60, 90))
    _flat_opaque_png(tmp_path / "art.png", (200, 300), (200, 50, 50))   # opaque despite transparent=True
    spec = _spec(
        art=[_art(id="background", asset="background.png", fit="cover", transparent=False),
            _art(id=slot_id, asset="art.png", fit="contain", transparent=True)],
        text=[_text(id="title", content="ASH")],
        layers=[LayerRef(kind="art", ref="background"), LayerRef(kind="text", ref="title"),
               LayerRef(kind="art", ref=slot_id)])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any(slot_id in w and "title" in w for w in report.warnings)


# ===========================================================================
# compose(): "thing inside of thing" — TextSlot.mask_from (v2 BODY wave)
# ===========================================================================

def test_text_mask_from_clips_ink_to_the_containers_shape(tmp_path):
    # A hard-edged circle "container" well inside a much bigger text zone,
    # over a real opaque background — ink must survive deep in the
    # circle's interior and vanish well outside its margin.
    ground_rgb = (40, 60, 90)
    _flat_opaque_png(tmp_path / "ground.png", CANVAS, ground_rgb)
    _blob_png(tmp_path / "beam.png", CANVAS, fg=(255, 255, 0, 255))
    title = _text(id="title", content="ASH AND EMBER AND SALT AND STONE",
                 zone=Zone(x=0.0, y=0.0, w=1.0, h=1.0), size_min=0.03,
                 size_max=0.10, max_lines=6, mask_from="beam")
    spec = _spec(art=[_art(id="ground", asset="ground.png", fit="cover"),
                     _art(id="beam", asset="beam.png", fit="cover")],
                text=[title], layers=[LayerRef(kind="art", ref="ground"),
                                      LayerRef(kind="art", ref="beam"),
                                      LayerRef(kind="text", ref="title")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    # Far outside the circle's margin (a corner): the untouched ground
    # color — the container's own yellow AND any ink are both absent.
    assert image.getpixel((2, 2)) == ground_rgb
    # Something OTHER than plain ground shows up too (the container's own
    # fill and/or ink survived inside the circle) — clipping isn't just
    # erasing everything.
    assert set(image.getdata()) - {ground_rgb}
    assert "title" in report.contrast


def test_text_mask_from_low_coverage_warns_with_slot_and_container_named(tmp_path):
    # A tiny container relative to a title zone sized to need much more
    # room: most of the ink cannot possibly land inside it.
    _blob_png(tmp_path / "beam.png", (60, 60), fg=(255, 255, 0, 255))
    beam = _art(id="beam", asset="beam.png", fit="contain", transparent=True,
               anchor=[0.5, 0.5], scale=0.15)
    title = _text(id="title", content="A LONG TITLE ACROSS THE WHOLE FRAME",
                 zone=Zone(x=0.0, y=0.0, w=1.0, h=1.0), size_min=0.03,
                 size_max=0.09, max_lines=6, mask_from="beam")
    spec = _spec(art=[beam], text=[title],
                layers=[LayerRef(kind="art", ref="beam"),
                       LayerRef(kind="text", ref="title")])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert any("title" in w and "beam" in w and "only" in w for w in report.warnings)


def test_text_mask_from_full_coverage_does_not_warn(tmp_path):
    # A container that fully covers the (small, centered) text zone:
    # coverage should be complete, no warning.
    _flat_opaque_png(tmp_path / "beam.png", CANVAS, (255, 255, 0))
    title = _text(id="title", content="ASH", zone=Zone(x=0.3, y=0.4, w=0.4, h=0.2),
                 size_min=0.05, size_max=0.05, mask_from="beam")
    spec = _spec(art=[_art(id="beam", asset="beam.png", fit="cover")],
                text=[title], layers=[LayerRef(kind="art", ref="beam"),
                                      LayerRef(kind="text", ref="title")])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert not any("only" in w and "title" in w for w in report.warnings)


def test_text_mask_from_feathering_is_deterministic(tmp_path):
    _blob_png(tmp_path / "beam.png", CANVAS, fg=(255, 255, 0, 255))
    title = _text(id="title", content="ASH", zone=Zone(x=0.2, y=0.3, w=0.6, h=0.3),
                 size_min=0.06, size_max=0.06, mask_from="beam")
    spec = _spec(art=[_art(id="beam", asset="beam.png", fit="cover")],
                text=[title], layers=[LayerRef(kind="art", ref="beam"),
                                      LayerRef(kind="text", ref="title")])

    image1, _ = compose(spec, tmp_path, canvas=CANVAS)
    image2, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()


def test_text_mask_from_skips_empty_optional_slots_like_fill_does(tmp_path):
    _blob_png(tmp_path / "beam.png", CANVAS, fg=(255, 255, 0, 255))
    subtitle = _text(id="subtitle", content="", optional=True, mask_from="beam")
    spec = _spec(art=[_art(id="beam", asset="beam.png", fit="cover")],
                text=[subtitle], layers=[LayerRef(kind="art", ref="beam"),
                                         LayerRef(kind="text", ref="subtitle")])
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert "subtitle" not in report.contrast


def test_text_mask_from_contrast_samples_only_inside_the_container(tmp_path):
    # The title zone straddles two very different regions: an all-WHITE
    # "ground" everywhere, and a BLACK "beam" container covering only the
    # zone's bottom half. Light ink reads terribly on white (mean-of-both
    # would land around mid-gray and still fail) and excellently on black.
    # If the sampler correctly restricts to the beam's own interior, it
    # sees pure black and passes cleanly; if it ever fell back to the
    # zone's raw (unmasked) rect, the white half would drag it below 4.5.
    cw, ch = CANVAS
    _flat_opaque_png(tmp_path / "ground.png", CANVAS, (255, 255, 255))
    beam = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(beam).rectangle((0, ch // 2, cw, ch), fill=(0, 0, 0, 255))
    beam.save(tmp_path / "beam.png")

    palette = _palette(text="#f5f1e8")   # light ink
    title = _text(id="title", content="ASH", zone=Zone(x=0.1, y=0.35, w=0.8, h=0.30),
                 size_min=0.05, size_max=0.05, mask_from="beam")
    spec = _spec(art=[_art(id="ground", asset="ground.png", fit="cover"),
                     _art(id="beam", asset="beam.png", fit="cover")],
                text=[title], palette=palette,
                layers=[LayerRef(kind="art", ref="ground"),
                       LayerRef(kind="art", ref="beam"),
                       LayerRef(kind="text", ref="title")])

    _, report = compose(spec, tmp_path, canvas=CANVAS)

    assert report.contrast["title"] >= 4.5
    assert not any("title" in w and "contrast" in w for w in report.warnings)
