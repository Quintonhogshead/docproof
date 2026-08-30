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
from docproof.cover.compose import (_ART_FILL_OUTLINE_FRACTION,
                                    _CORNER_MARGIN_FRACTION, _dilate_mask,
                                    _padded_rect, _place_corners, compose)
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


def test_artslot_v22_fields_default_off():
    # v2.2 wave: gravity-safe corners, line-gap snap, the texture shelf,
    # and frame notches all default to their pre-existing behavior — a
    # spec/archetype written before this wave keeps rendering unchanged.
    slot = ArtSlot(id="focal")
    assert slot.corners_flip_vertical is False
    assert slot.snap == ""
    assert slot.texture_file == ""
    assert slot.texture_fit == "cover"
    assert slot.notch_for == ""


@pytest.mark.parametrize("slot_id", ["background", "focal", "focal2", "foreground", "texture"])
def test_artslot_id_accepts_every_widened_slot_id(slot_id):
    assert ArtSlot(id=slot_id).id == slot_id


@pytest.mark.parametrize("treatment", ["none", "duotone", "silhouette", "posterize",
                                       "sticker", "photo_soft"])
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


@pytest.mark.parametrize("snap", ["", "line_gap"])
def test_artslot_accepts_every_documented_snap_value(snap):
    assert ArtSlot(id="focal", snap=snap).snap == snap


def test_artslot_rejects_an_undocumented_snap_value():
    with pytest.raises(ValidationError):
        ArtSlot(id="focal", snap="magnet")


@pytest.mark.parametrize("texture_fit", ["tile", "cover"])
def test_artslot_accepts_every_documented_texture_fit(texture_fit):
    assert ArtSlot(id="focal", texture_fit=texture_fit).texture_fit == texture_fit


def test_artslot_rejects_an_undocumented_texture_fit():
    with pytest.raises(ValidationError):
        ArtSlot(id="focal", texture_fit="stretch")


def test_artslot_notch_for_round_trips():
    assert ArtSlot(id="frame", notch_for="emblem").notch_for == "emblem"


def test_artslot_texture_file_accepts_a_real_shelf_name():
    from docproof.cover.textures import TEXTURES
    name = next(iter(TEXTURES))
    assert ArtSlot(id="focal", texture_file=name).texture_file == name


def test_artslot_texture_file_rejects_an_unknown_name():
    # Deliverable 5's own acceptance test: an unknown texture_file fails
    # LOUDLY at spec validation, not silently three steps later in
    # compose() as a blank layer.
    with pytest.raises(ValidationError, match="not on the shelf"):
        ArtSlot(id="focal", texture_file="totally-not-a-real-plate")


def test_scrimspec_accepts_halo_kind():
    assert ScrimSpec(kind="halo", protects="title").kind == "halo"


def test_artprompt_slot_accepts_every_widened_slot_id():
    for slot_id in ("background", "focal", "focal2", "foreground", "texture"):
        assert ArtPrompt(slot=slot_id, prompt="x").slot == slot_id


def test_artprompt_treatment_defaults_to_none_and_is_settable():
    assert ArtPrompt(slot="focal2", prompt="x").treatment == "none"
    assert ArtPrompt(slot="focal2", prompt="x", treatment="duotone").treatment == "duotone"


def test_artprompt_accepts_photo_soft_treatment():
    assert ArtPrompt(slot="focal2", prompt="x", treatment="photo_soft").treatment == "photo_soft"


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
    # High enough contrast between the two ramp ends that the art-vs-ground
    # contrast floor (v2.1 BODY-fix wave — the ramp's own MEAN luminance,
    # against a blank/black ground here since `focal` is this spec's only
    # layer) clears _ART_CONTRAST_FLOOR without tripping an accent swap;
    # gamma decoding skews a linear-in-sRGB ramp's mean well toward its
    # darker end, so "the two hex endpoints look high-contrast" is not by
    # itself enough headroom (#102040/#f0c020 measures ~0.119, just under
    # the 0.12 floor).
    bg_hex, fg_hex = "#102040", "#fdf6e3"
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
# v2.2 wave, deliverable 6: photo_soft treatment — the one recipe that makes
# a photographic/photoreal art prompt shelf-safe (blur, desaturate, contrast
# lift, grain, then the duotone ramp).
# ===========================================================================

def _sharp_split_png(path: Path, size: tuple[int, int],
                     left_rgb: tuple[int, int, int] = (255, 0, 0),
                     right_rgb: tuple[int, int, int] = (0, 100, 255)) -> None:
    """A hard vertical edge, one fully saturated color on each side, no
    anti-aliasing at all — a stand-in for "photographic detail" sharp
    enough that ANY real blur leaves a visible transitional band at the
    seam, which is exactly what photo_soft's own "measurably blurred"
    acceptance test needs to detect."""
    img = Image.new("RGB", size)
    px = img.load()
    half = size[0] // 2
    for x in range(size[0]):
        rgb = left_rgb if x < half else right_rgb
        for y in range(size[1]):
            px[x, y] = rgb
    img.convert("RGBA").save(path)


def test_photo_soft_output_only_uses_ramp_colors_and_is_measurably_blurred(tmp_path):
    _sharp_split_png(tmp_path / "photo.png", CANVAS)
    bg_hex, fg_hex = "#102040", "#fdf6e3"
    palette = _palette(background=bg_hex, primary=fg_hex)
    spec = _spec(art=[_art(id="focal", asset="photo.png", fit="cover", treatment="photo_soft")],
                layers=[LayerRef(kind="art", ref="focal")], palette=palette)

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    # Only ramp hues survive — the same background->primary duotone ramp
    # duotone's own test above already holds to, since photo_soft's own
    # LAST step is a real call to _duotone (see the treatment's own
    # docstring for why grain is mixed into the luminance signal BEFORE
    # the ramp, not composited after — specifically so this guarantee
    # carries over verbatim rather than being merely "close").
    bg, fg = ImageColor.getrgb(bg_hex), ImageColor.getrgb(fg_hex)
    allowed = {tuple(round(bg[c] + (fg[c] - bg[c]) * i / 255) for c in range(3))
              for i in range(256)}
    colors = set(image.getdata())
    assert colors <= allowed
    assert len(colors) > 1   # real tonal range, not a flat wash

    # Measurably blurred: the SOURCE had a razor-sharp vertical edge at the
    # midline (one solid color, then another, no transition at all) —
    # sampling straight across it in the output now shows a real
    # transitional band of intermediate values, not a single-pixel jump
    # from one ramp endpoint straight to the other.
    cy = CANVAS[1] // 2
    mid = CANVAS[0] // 2
    row = [image.getpixel((x, cy))[0] for x in range(mid - 8, mid + 8)]
    assert len(set(row)) > 2, f"edge still reads as a hard 2-value split: {row}"


def test_photo_soft_is_deterministic_across_composes(tmp_path):
    _sharp_split_png(tmp_path / "photo.png", (200, 100))
    spec = _spec(art=[_art(id="focal", asset="photo.png", fit="cover", treatment="photo_soft")],
                layers=[LayerRef(kind="art", ref="focal")])
    image1, _ = compose(spec, tmp_path, canvas=CANVAS)
    image2, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image1.tobytes() == image2.tobytes()


def test_photo_soft_needs_no_transparency_precondition(tmp_path):
    # Unlike sticker, photo_soft has no "transparent slots only"
    # precondition — well-defined (and expected to be used) on a fully
    # opaque photographic background, with no warning at all.
    _flat_opaque_png(tmp_path / "photo.png", (100, 100), (200, 60, 40))
    spec = _spec(art=[_art(id="focal", asset="photo.png", fit="cover", treatment="photo_soft")],
                layers=[LayerRef(kind="art", ref="focal")])

    _image, report = compose(spec, tmp_path, canvas=CANVAS)

    assert not any("photo_soft" in w for w in report.warnings)


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
    # v2.2 wave, deliverable 1 (gravity-safe corners): the DEFAULT is no
    # longer a full kaleidoscope mirror — bottom copies stay upright (only
    # horizontally mirrored on the right side), so top-left/bottom-left are
    # byte-identical to each other and top-right/bottom-right are
    # byte-identical to each other. See
    # test_corners_flip_vertical_restores_the_old_full_mirror_behavior for
    # the opt-in full-mirror case.
    _asymmetric_blob_png(tmp_path / "ornament.png", (100, 100))
    spec = _spec(art=[_art(id="focal2", asset="ornament.png", corners=True, scale=0.25)],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, report = compose(spec, tmp_path, canvas=CANVAS)

    cw, ch = CANVAS
    k = round(0.25 * ch)   # a 100x100 (square) source -> target_w == target_h == k
    # v2.1 BODY-fix wave: each copy is inset _CORNER_MARGIN_FRACTION from
    # its own corner (fix 1), not flush to it — crop windows follow suit.
    mx, my = round(_CORNER_MARGIN_FRACTION * cw), round(_CORNER_MARGIN_FRACTION * ch)
    top_left = image.crop((mx, my, mx + k, my + k))
    top_right = image.crop((cw - mx - k, my, cw - mx, my + k))
    bottom_left = image.crop((mx, ch - my - k, mx + k, ch - my))
    bottom_right = image.crop((cw - mx - k, ch - my - k, cw - mx, ch - my))

    assert top_left.tobytes() != top_right.tobytes()   # genuinely asymmetric source
    assert top_right.tobytes() == ImageOps.mirror(top_left).tobytes()
    assert bottom_left.tobytes() == top_left.tobytes()       # upright, not v-flipped
    assert bottom_right.tobytes() == top_right.tobytes()     # upright, not v-flipped
    # Not a blanket "no warnings at all": this minimal hand-built spec is a
    # small corner ornament on an otherwise blank canvas (no background
    # layer at all), so the v2.1 BODY-fix wave's dead-band metric is
    # entitled to flag the wide-open middle — this test is specifically
    # about corners' own mirroring/no-op-warning behavior.
    assert not any("corners" in w for w in report.warnings)

    # And the margin itself: nothing from the ornament reaches the canvas
    # edge. compose() flattens to RGB, and this spec has no background
    # layer at all, so a never-painted edge pixel stays exactly (0, 0, 0) —
    # the regression probe fix 1 asks for ("zero ornament alpha within 1px
    # of any canvas edge").
    for edge in (image.crop((0, 0, cw, 1)), image.crop((0, ch - 1, cw, ch)),
                image.crop((0, 0, 1, ch)), image.crop((cw - 1, 0, cw, ch))):
        assert set(edge.getdata()) == {(0, 0, 0)}


def test_corners_flip_vertical_restores_the_old_full_mirror_behavior(tmp_path):
    # corners_flip_vertical=True is the v2.2 wave's opt-in for a genuinely
    # top/bottom-symmetric ornament that WANTS the fuller kaleidoscope
    # effect — byte-for-byte the original (pre-gravity-fix) behavior.
    _asymmetric_blob_png(tmp_path / "ornament.png", (100, 100))
    spec = _spec(art=[_art(id="focal2", asset="ornament.png", corners=True,
                          corners_flip_vertical=True, scale=0.25)],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    cw, ch = CANVAS
    k = round(0.25 * ch)
    mx, my = round(_CORNER_MARGIN_FRACTION * cw), round(_CORNER_MARGIN_FRACTION * ch)
    top_left = image.crop((mx, my, mx + k, my + k))
    bottom_left = image.crop((mx, ch - my - k, mx + k, ch - my))
    bottom_right = image.crop((cw - mx - k, ch - my - k, cw - mx, ch - my))

    assert bottom_left.tobytes() == ImageOps.flip(top_left).tobytes()
    assert bottom_right.tobytes() == ImageOps.flip(ImageOps.mirror(top_left)).tobytes()


def _bottom_heavy_probe_png(path: Path, size: tuple[int, int] = (100, 100)) -> None:
    """A probe ornament whose own weight is concentrated at the BOTTOM — a
    small solid square sitting flush against the source image's own bottom
    edge, everything else transparent. The gravity regression this wave's
    deliverable 1 asks for: "a probe ornament with a distinctive
    bottom-heavy pixel must keep that pixel at the BOTTOM in all four
    corners by default" — a v-flipped copy would move this mark to the
    TOP of its own corner tile instead."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    w, h = size
    mark = round(h * 0.15)
    ImageDraw.Draw(img).rectangle((0, h - mark, w - 1, h - 1), fill=(230, 180, 40, 255))
    img.save(path)


def test_corners_default_keeps_a_bottom_heavy_mark_at_the_bottom_in_every_corner(tmp_path):
    # The gravity regression: a mirrored ornament's own bottom-heavy weight
    # (a honey drip, a hanging charm) must not point UP on the bottom
    # corners under the new default.
    _bottom_heavy_probe_png(tmp_path / "drip.png")
    spec = _spec(art=[_art(id="focal2", asset="drip.png", corners=True, scale=0.25)],
                layers=[LayerRef(kind="art", ref="focal2")])

    image, _ = compose(spec, tmp_path, canvas=CANVAS)

    cw, ch = CANVAS
    k = round(0.25 * ch)
    mx, my = round(_CORNER_MARGIN_FRACTION * cw), round(_CORNER_MARGIN_FRACTION * ch)
    tiles = {
        "top_left": image.crop((mx, my, mx + k, my + k)),
        "top_right": image.crop((cw - mx - k, my, cw - mx, my + k)),
        "bottom_left": image.crop((mx, ch - my - k, mx + k, ch - my)),
        "bottom_right": image.crop((cw - mx - k, ch - my - k, cw - mx, ch - my)),
    }
    mark_rgb = (230, 180, 40)
    for name, tile in tiles.items():
        tw, th = tile.size
        top_half = set(tile.crop((0, 0, tw, th // 2)).getdata())
        bottom_half = set(tile.crop((0, th // 2, tw, th)).getdata())
        assert mark_rgb not in top_half, f"{name}: bottom-heavy mark floated to the top"
        assert mark_rgb in bottom_half, f"{name}: bottom-heavy mark missing from the bottom"


def test_place_corners_keeps_a_margin_so_nothing_touches_the_canvas_edge():
    # A probe ornament fully opaque right up to its OWN edges — exactly the
    # shape that would show alpha bleeding to the canvas edge under the
    # OLD flush (or a hypothetical centered-on-the-corner-point) placement.
    probe = Image.new("RGBA", (40, 40), (200, 30, 30, 255))
    result = _place_corners(probe, CANVAS, scale=0.1)

    cw, ch = CANVAS
    alpha = result.getchannel("A")
    edge_bands = (
        alpha.crop((0, 0, cw, 2)), alpha.crop((0, ch - 2, cw, ch)),      # top/bottom, 2px
        alpha.crop((0, 0, 2, ch)), alpha.crop((cw - 2, 0, cw, ch)))      # left/right, 2px
    for band in edge_bands:
        assert band.getextrema()[1] == 0   # max alpha in the band is 0


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


# ===========================================================================
# Deep-stack wave PR1 (§15.1-§15.3, §15.7, §15.13): the effects.py engine —
# blend table, first-class masks, adjust layers, and the final-composite
# legibility re-check ladder. Validator-level coverage for the same wave
# lives in tests/test_cover_model.py; this section is the pixel half.
# ===========================================================================

from PIL import ImageStat  # noqa: E402

from docproof.cover.effects import (BLEND_TABLE,  # noqa: E402
                                    TEMPERATURE_MAX_SHIFT, apply_adjust,
                                    apply_mask, blend_rgb, gradient_mask,
                                    resolve_mask)
from docproof.cover.model import (AdjustLayer, BLEND_MODES,  # noqa: E402
                                  GradientMask, MaskSpec)

_MASK_CANVAS = (40, 64)


def _gray_rgb(vals, size=(2, 2)) -> Image.Image:
    band = Image.new("L", size)
    band.putdata(list(vals))
    return Image.merge("RGB", (band, band, band))


def _synthetic_composite(size=(60, 80)) -> Image.Image:
    """A fixed, drawn (never random) RGBA test composite with real variety:
    a vertical gray ramp, a bright disc, and a dark block — enough tonal
    range that every adjust op visibly does something, and deterministic
    down to the byte so "apply twice, compare bytes" is meaningful."""
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    for y in range(h):
        v = round(255 * y / (h - 1))
        for x in range(w):
            px[x, y] = (v, v, v, 255)
    draw = ImageDraw.Draw(img)
    draw.ellipse((w * 0.55, h * 0.1, w * 0.9, h * 0.35), fill=(250, 240, 220, 255))
    draw.rectangle((w * 0.1, h * 0.7, w * 0.4, h * 0.9), fill=(12, 10, 16, 255))
    return img


# -- §15.1: the blend table vs hand-computed fixtures ------------------------

def test_blend_table_stays_in_lockstep_with_the_model_literal():
    # model.BLEND_MODES is the wire's source of truth; the table implements
    # everything but "normal" (plain alpha-over, no formula).
    assert set(BLEND_TABLE) == set(BLEND_MODES) - {"normal"}


@pytest.mark.parametrize("mode,base,source,expected", [
    # screen = 255 - (255-a)(255-b)//255
    ("screen", [0, 255, 100, 128], [0, 255, 200, 128], [0, 255, 222, 192]),
    # add saturates at 255
    ("add", [0, 255, 100, 200], [0, 255, 200, 100], [0, 255, 255, 255]),
    ("lighten", [0, 255, 100, 128], [10, 200, 200, 40], [10, 255, 200, 128]),
    ("darken", [0, 255, 100, 128], [10, 200, 200, 40], [0, 200, 100, 40]),
    # color_dodge = min(255, a*256 // (256-b))
    ("color_dodge", [100, 0, 255, 30], [200, 128, 255, 10], [255, 0, 255, 31]),
])
def test_blend_modes_match_hand_computed_two_by_two_fixtures(mode, base, source, expected):
    out = blend_rgb(_gray_rgb(base), _gray_rgb(source), mode)
    for band in out.split():
        assert list(band.getdata()) == expected


def test_color_dodge_with_a_black_source_is_the_identity():
    # a * 256 // (256 - 0) == a exactly — the mode's own sanity anchor.
    base = _gray_rgb([0, 51, 128, 255])
    out = blend_rgb(base, _gray_rgb([0, 0, 0, 0]), "color_dodge")
    assert out.tobytes() == base.tobytes()


def test_new_blend_modes_render_through_compose_deterministically(tmp_path):
    _flat_opaque_png(tmp_path / "glow.png", (60, 60), (240, 180, 60))
    art = [_art(id="background", fit="cover", transparent=False, procedural="gradient"),
          _art(id="glow", fit="contain", transparent=False, asset="glow.png",
               blend="color_dodge", opacity=0.8)]
    spec = _spec(art=art, text=[_text()],
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="art", ref="glow"),
                        LayerRef(kind="text", ref="title")])
    image_a, _ = compose(spec, tmp_path, canvas=CANVAS)
    image_b, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image_a.tobytes() == image_b.tobytes()


# -- §15.2: gradient mask synthesis ------------------------------------------

def test_linear_gradient_mask_ramps_top_transparent_to_bottom_opaque():
    mask = gradient_mask(GradientMask(), _MASK_CANVAS)   # angle=90 default
    w, h = _MASK_CANVAS
    assert mask.size == _MASK_CANVAS
    top, mid, bottom = (mask.getpixel((w // 2, y)) for y in (0, h // 2, h - 1))
    assert top < 8                      # Lanczos upsample may leave a hair above 0
    assert bottom > 247
    assert top < mid < bottom


def test_linear_gradient_mask_start_end_remap_the_ramp():
    mask = gradient_mask(GradientMask(start=0.5, end=1.0), _MASK_CANVAS)
    w, h = _MASK_CANVAS
    # Alpha must not begin rising until halfway down.
    assert mask.getpixel((w // 2, round(h * 0.2))) < 8
    assert mask.getpixel((w // 2, round(h * 0.45))) < 16
    assert mask.getpixel((w // 2, h - 1)) > 247


def test_radial_gradient_mask_is_transparent_at_center_opaque_at_corners():
    mask = gradient_mask(GradientMask(kind="radial"), _MASK_CANVAS)
    w, h = _MASK_CANVAS
    # Quarter-scale synthesis quantizes the exact center by a few 8-bit
    # steps (the sampled pixel sits between grid points) — "transparent
    # core" means near-zero, not literally zero.
    assert mask.getpixel((w // 2, h // 2)) < 24
    assert mask.getpixel((0, 0)) > 230
    assert mask.getpixel((w - 1, h - 1)) > 230


# -- §15.2: mask resolution (combination, invert, sources) -------------------

def _half_opaque_layer(canvas=_MASK_CANVAS, rgb=(255, 255, 255)) -> Image.Image:
    """Canvas-sized RGBA: fully opaque `rgb` on the left half, fully
    transparent on the right — a hard, unambiguous stencil source."""
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((0, 0, canvas[0] // 2 - 1, canvas[1] - 1),
                                  fill=(*rgb, 255))
    return img


def test_resolve_from_layer_matches_the_legacy_hard_stencil():
    ref = _half_opaque_layer()
    mask = resolve_mask(MaskSpec(from_layer="plate"), _MASK_CANVAS,
                        {"plate": ref}, {})
    expected = ref.getchannel("A").point(lambda v: 255 if v > 127 else 0)
    assert mask.tobytes() == expected.tobytes()


def test_resolve_mask_multiplies_its_sources_together():
    ref = _half_opaque_layer()
    combined = resolve_mask(
        MaskSpec(from_layer="plate", gradient=GradientMask()),
        _MASK_CANVAS, {"plate": ref}, {})
    w, h = _MASK_CANVAS
    assert combined.getpixel((w - 4, h - 4)) == 0        # stencil kills right half
    assert combined.getpixel((w // 4, 0)) < 8            # gradient kills the top
    assert combined.getpixel((w // 4, h - 1)) > 247      # both allow bottom-left


def test_resolve_mask_invert_applies_after_combination():
    ref = _half_opaque_layer()
    spec = dict(from_layer="plate", gradient=GradientMask())
    straight = resolve_mask(MaskSpec(**spec), _MASK_CANVAS, {"plate": ref}, {})
    inverted = resolve_mask(MaskSpec(**spec, invert=True), _MASK_CANVAS,
                            {"plate": ref}, {})
    assert inverted.tobytes() == ImageChops.invert(straight).tobytes()


def test_resolve_luminance_of_is_gated_by_the_source_alpha():
    ref = _half_opaque_layer(rgb=(255, 255, 255))   # white where opaque
    mask = resolve_mask(MaskSpec(luminance_of="plate"), _MASK_CANVAS,
                        {"plate": ref}, {})
    w, h = _MASK_CANVAS
    assert mask.getpixel((w // 4, h // 2)) == 255   # bright AND opaque
    assert mask.getpixel((w - 4, h // 2)) == 0      # transparent side masks out


def test_apply_mask_multiplies_alpha_and_preserves_color():
    layer = Image.new("RGBA", _MASK_CANVAS, (200, 40, 40, 255))
    mask = gradient_mask(GradientMask(), _MASK_CANVAS)
    out = apply_mask(layer, mask)
    assert out.getchannel("A").tobytes() == mask.tobytes()
    assert out.convert("RGB").tobytes() == layer.convert("RGB").tobytes()


# -- §15.2: the fold and first-class masks through compose -------------------

def test_legacy_mask_from_and_explicit_maskspec_render_identical_bytes(tmp_path):
    """The fold's whole contract: an old spec spelled with mask_from and the
    same spec spelled with mask.from_layer are the SAME cover, byte for
    byte."""
    _blob_png(tmp_path / "vessel.png")
    _flat_opaque_png(tmp_path / "pour.png", (120, 120), (220, 80, 40))

    def build(**mask_fields):
        art = [_art(id="background", fit="cover", transparent=False,
                    procedural="gradient"),
              _art(id="vessel", asset="vessel.png"),
              _art(id="pour", asset="pour.png", fit="cover", **mask_fields)]
        return _spec(art=art, text=[_text()],
                     layers=[LayerRef(kind="art", ref="background"),
                            LayerRef(kind="art", ref="vessel"),
                            LayerRef(kind="art", ref="pour"),
                            LayerRef(kind="text", ref="title")])

    legacy, _ = compose(build(mask_from="vessel"), tmp_path, canvas=CANVAS)
    firstclass, _ = compose(build(mask=MaskSpec(from_layer="vessel")),
                            tmp_path, canvas=CANVAS)
    assert legacy.tobytes() == firstclass.tobytes()


def test_spec_using_no_new_fields_matches_spec_with_new_fields_at_defaults(tmp_path):
    """The permanent, environment-independent golden-bytes proof (§15.0
    constraint 2): a legacy-shaped spec and the same spec with every new
    field explicitly at its default render the exact same pixels."""
    legacy_art = [ArtSlot(id="background", fit="cover", procedural="gradient"),
                  ArtSlot(id="texture", fit="cover", procedural="grain",
                          opacity=0.06, blend="overlay")]
    explicit_art = [ArtSlot(id="background", fit="cover", procedural="gradient",
                            mask=None, blend="normal"),
                    ArtSlot(id="texture", fit="cover", procedural="grain",
                            opacity=0.06, blend="overlay", mask=None)]
    layers = [LayerRef(kind="art", ref="background"),
             LayerRef(kind="scrim", ref="0"),
             LayerRef(kind="text", ref="title"),
             LayerRef(kind="art", ref="texture")]
    scrims = [ScrimSpec(kind="panel", protects="title", strength=0.2)]
    legacy = _spec(art=legacy_art, scrims=scrims, text=[_text()], layers=layers)
    explicit = _spec(art=explicit_art, scrims=scrims, text=[_text()],
                     layers=layers, adjust=[])
    image_a, report_a = compose(legacy, tmp_path, canvas=CANVAS)
    image_b, report_b = compose(explicit, tmp_path, canvas=CANVAS)
    assert image_a.tobytes() == image_b.tobytes()
    assert report_a == report_b


def test_gradient_masked_art_blends_two_plates_through_compose(tmp_path):
    _flat_opaque_png(tmp_path / "sky.png", (100, 160), (200, 60, 30))
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient"),
          _art(id="sky", asset="sky.png", fit="cover",
               mask=MaskSpec(gradient=GradientMask(angle=90)))]
    spec = _spec(art=art, text=[_text()],
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="art", ref="sky"),
                        LayerRef(kind="text", ref="title")])
    image, _ = compose(spec, tmp_path, canvas=CANVAS)
    w, h = CANVAS
    top = image.getpixel((w // 2, 2))
    bottom = image.getpixel((w // 2, h - 3))
    # Top: the mask zeroes the red plate — background gradient shows.
    # Bottom: the plate is fully opaque — red wins.
    assert bottom[0] > 180 and bottom[0] - bottom[2] > 100
    assert top[0] < 90


def test_mask_from_text_pours_art_into_the_title_glyphs(tmp_path):
    """§15.13 part 1: an art layer clipped to a text slot's fitted glyph
    alpha — the text slot itself stays OUT of `layers` (the art IS the
    title), which the model allows and the ink cache resolves anyway."""
    _flat_opaque_png(tmp_path / "forest.png", (120, 190), (30, 190, 60))
    title = _text(content="ASH", zone=Zone(x=0.05, y=0.3, w=0.9, h=0.4),
                  size_min=0.1, size_max=0.2)
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient"),
          _art(id="forest", asset="forest.png", fit="cover",
               mask=MaskSpec(from_text="title"))]
    spec = _spec(art=art, text=[title],
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="art", ref="forest")])
    image_a, _ = compose(spec, tmp_path, canvas=CANVAS)
    image_b, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image_a.tobytes() == image_b.tobytes()    # from_text is deterministic

    fit = fit_text(title, CANVAS)
    ink = text_mask(title, fit, CANVAS)
    ink_px, img_px = ink.load(), image_a.load()
    inside = outside = None
    for y in range(0, CANVAS[1], 3):
        for x in range(0, CANVAS[0], 3):
            if inside is None and ink_px[x, y] == 255:
                inside = img_px[x, y]
            if outside is None and y < 8:
                outside = img_px[x, y]
    assert inside is not None
    # Inside a glyph: the green plate. Far outside the zone: background.
    assert inside[1] > 150 and inside[1] - inside[0] > 80
    assert outside[1] < 90


def test_art_clipped_to_an_empty_text_slot_warns(tmp_path):
    _flat_opaque_png(tmp_path / "plate.png", (60, 60), (200, 60, 30))
    subtitle = _text(id="subtitle", content="", optional=True,
                     zone=Zone(x=0.1, y=0.5, w=0.8, h=0.2))
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient"),
          _art(id="plate", asset="plate.png", fit="cover",
               mask=MaskSpec(from_text="subtitle"))]
    spec = _spec(art=art, text=[_text(), subtitle],
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="art", ref="plate"),
                        LayerRef(kind="text", ref="title")])
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert any("fully masked out" in w for w in report.warnings)


# -- §15.3: adjust ops, each deterministic on a fixed composite --------------

_ADJUST_CASES = [
    AdjustLayer(id="fx_lift", op="grade", brightness=0.2, contrast=-0.15,
                saturation=-0.3, temperature=0.4),
    AdjustLayer(id="fx_map", op="gradient_map", stops=["#101820", "#c9a227"]),
    AdjustLayer(id="fx_wash", op="color_wash", color="#3355aa", opacity=0.6,
                blend="screen"),
    AdjustLayer(id="fx_vign", op="vignette", strength=0.7),
    AdjustLayer(id="fx_bloom", op="bloom", threshold=0.5, strength=1.0,
                radius=0.04),
    AdjustLayer(id="fx_blur", op="blur", radius=0.04),
]


@pytest.mark.parametrize("adjust", _ADJUST_CASES, ids=lambda a: a.op)
def test_each_adjust_op_is_deterministic_and_actually_does_something(adjust):
    base = _synthetic_composite()
    once = apply_adjust(base, adjust, _palette(), base.size)
    twice = apply_adjust(base, adjust, _palette(), base.size)
    assert once.tobytes() == twice.tobytes()
    assert once.tobytes() != base.tobytes()
    assert once.getchannel("A").tobytes() == base.getchannel("A").tobytes()


def test_grade_with_all_zero_params_is_a_true_no_op():
    base = _synthetic_composite()
    out = apply_adjust(base, AdjustLayer(id="fx_nop", op="grade"),
                       _palette(), base.size)
    assert out.tobytes() == base.tobytes()


def test_grade_temperature_shifts_red_up_and_blue_down():
    base = Image.new("RGBA", (8, 8), (128, 128, 128, 255))
    out = apply_adjust(base, AdjustLayer(id="fx_warm", op="grade",
                                         temperature=0.5),
                       _palette(), (8, 8))
    shift = round(0.5 * TEMPERATURE_MAX_SHIFT)
    assert out.getpixel((4, 4)) == (128 + shift, 128, 128 - shift, 255)


def test_gradient_map_output_contains_only_ramp_colors():
    base = _synthetic_composite()
    for stops in (["#101820", "#c9a227"], ["#101820", "#8a3b2c", "#f5f1e8"]):
        out = apply_adjust(base, AdjustLayer(id="fx_map", op="gradient_map",
                                             stops=stops),
                           _palette(), base.size)
        strip = Image.new("L", (256, 1))
        strip.putdata(list(range(256)))
        if len(stops) == 2:
            ramp = ImageOps.colorize(strip, black=stops[0], white=stops[1])
        else:
            ramp = ImageOps.colorize(strip, black=stops[0], white=stops[2],
                                     mid=stops[1])
        allowed = set(ramp.getdata())
        assert set(out.convert("RGB").getdata()) <= allowed


def test_color_wash_composites_through_the_blend_table():
    base = _synthetic_composite()
    white_screen = apply_adjust(
        base, AdjustLayer(id="fx_w", op="color_wash", color="#ffffff",
                          blend="screen"), _palette(), base.size)
    # screen(x, 255) == 255 for every pixel — the wash saturates the canvas.
    assert set(white_screen.convert("RGB").getdata()) == {(255, 255, 255)}
    black_multiply = apply_adjust(
        base, AdjustLayer(id="fx_b", op="color_wash", color="#000000",
                          blend="multiply"), _palette(), base.size)
    assert set(black_multiply.convert("RGB").getdata()) == {(0, 0, 0)}


def test_vignette_leaves_the_center_nearly_untouched_and_shades_corners():
    base = Image.new("RGBA", (64, 96), (180, 180, 180, 255))
    out = apply_adjust(base, AdjustLayer(id="fx_v", op="vignette",
                                         strength=0.8, color="#000000"),
                       _palette(), (64, 96))
    # The quarter-scale ramp costs the exact center a few 8-bit steps
    # (same as the scrim vignette painter) — the design contract is
    # "center essentially untouched, corners strongly shaded."
    center = out.getpixel((32, 48))
    corner = out.getpixel((1, 1))
    assert center[0] >= 170
    assert corner[0] < 100
    assert center[0] - corner[0] > 60
    assert center[3] == 255


def test_bloom_is_a_no_op_when_nothing_clears_the_threshold():
    base = Image.new("RGBA", (32, 48), (40, 40, 40, 255))
    out = apply_adjust(base, AdjustLayer(id="fx_bloom", op="bloom",
                                         threshold=0.75, strength=1.0,
                                         radius=0.05),
                       _palette(), (32, 48))
    assert out.tobytes() == base.tobytes()


def test_bloom_grows_a_glow_around_bright_pixels():
    base = Image.new("RGBA", (40, 60), (20, 20, 20, 255))
    ImageDraw.Draw(base).ellipse((14, 20, 26, 32), fill=(255, 255, 255, 255))
    out = apply_adjust(base, AdjustLayer(id="fx_bloom", op="bloom",
                                         threshold=0.5, strength=1.0,
                                         radius=0.05),
                       _palette(), (40, 60))
    # A dark pixel just outside the disc brightens; a far corner does not.
    assert out.getpixel((11, 26))[0] > base.getpixel((11, 26))[0]
    assert out.getpixel((2, 55)) == (20, 20, 20, 255)


def test_blur_through_a_mask_only_touches_the_masked_side():
    base = _synthetic_composite((64, 64))
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).rectangle((32, 0, 63, 63), fill=255)
    out = apply_adjust(base, AdjustLayer(id="fx_dof", op="blur", radius=0.06),
                       _palette(), (64, 64), mask_img=mask)
    assert (out.crop((0, 0, 32, 64)).tobytes()
            == base.crop((0, 0, 32, 64)).tobytes())
    assert (out.crop((32, 0, 64, 64)).tobytes()
            != base.crop((32, 0, 64, 64)).tobytes())


def test_adjust_mask_zero_region_is_byte_identical_to_base():
    base = _synthetic_composite()
    mask = Image.new("L", base.size, 0)     # fully masked out
    out = apply_adjust(base, AdjustLayer(id="fx_lift", op="grade",
                                         brightness=0.8),
                       _palette(), base.size, mask_img=mask)
    assert out.tobytes() == base.tobytes()


def test_adjust_layer_walks_in_z_order_through_compose(tmp_path):
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient")]
    adjust = [AdjustLayer(id="fx_wash", op="color_wash", color="#ffffff",
                          opacity=1.0)]
    spec = _spec(art=art, adjust=adjust, text=[_text()],
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="adjust", ref="fx_wash"),
                        LayerRef(kind="text", ref="title")])
    image, _ = compose(spec, tmp_path, canvas=CANVAS)
    # The wash sits above the background: every non-text pixel is white.
    assert image.getpixel((4, 4)) == (255, 255, 255)
    assert image.getpixel((CANVAS[0] - 5, CANVAS[1] - 5)) == (255, 255, 255)


def test_masked_adjust_through_compose_only_grades_the_masked_region(tmp_path):
    # A mid-tone background, because grade's brightness is multiplicative
    # (ImageEnhance): lifting a pure-black bottom row would prove nothing.
    palette = _palette(background="#405060")
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient")]
    layers = [LayerRef(kind="art", ref="background"),
             LayerRef(kind="text", ref="title")]
    plain = _spec(art=art, text=[_text()], layers=layers, palette=palette)
    graded = _spec(art=art, text=[_text()], palette=palette,
                   adjust=[AdjustLayer(
                       id="fx_bright", op="grade", brightness=0.6,
                       mask=MaskSpec(gradient=GradientMask(start=0.5, end=0.75)))],
                   layers=layers + [LayerRef(kind="adjust", ref="fx_bright")])
    image_plain, _ = compose(plain, tmp_path, canvas=CANVAS)
    image_graded, _ = compose(graded, tmp_path, canvas=CANVAS)
    w, h = CANVAS
    # Above the ramp's start the mask is 0 — untouched; near the bottom the
    # mask is 1.0 — visibly brightened.
    assert (image_plain.crop((0, 0, w, round(h * 0.3))).tobytes()
            == image_graded.crop((0, 0, w, round(h * 0.3))).tobytes())
    y_probe = h - 4
    assert (sum(image_graded.getpixel((w // 2, y_probe)))
            > sum(image_plain.getpixel((w // 2, y_probe))) + 30)


# -- §15.7: the final-composite legibility re-check ladder -------------------

def _ladder_spec(wash_hex: str, scrims=()) -> CoverSpec:
    """Dark procedural ground, light-ink title, and ONE fx_ color_wash
    finishing layer above the text whose gray level decides the ladder's
    fate — gray enough to fail the ink on the finished cover, while the
    draw-time autopilot (which never sees the wash) approves everything."""
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient")]
    adjust = [AdjustLayer(id="fx_wash", op="color_wash", color=wash_hex,
                          opacity=1.0)]
    layers = [LayerRef(kind="art", ref="background")]
    layers += [LayerRef(kind="scrim", ref=str(i)) for i in range(len(scrims))]
    layers += [LayerRef(kind="text", ref="title"),
              LayerRef(kind="adjust", ref="fx_wash")]
    return _spec(art=art, adjust=adjust, scrims=list(scrims), text=[_text()],
                 layers=layers)


def test_recheck_ladder_escalates_scrims_then_attenuates_to_a_pass(tmp_path):
    # #717171 fails the light ink (≈4.3 < 4.5) but keeps it the best option
    # (no flip); the wash buries the scrim, so escalation alone can't fix it
    # — one halving of fx_wash then lets the dark ground back through.
    spec = _ladder_spec("#717171",
                        scrims=[ScrimSpec(kind="panel", protects="title",
                                          strength=0.15)])
    image_a, report = compose(spec, tmp_path, canvas=CANVAS)
    assert report.scrim_final[0] == pytest.approx(0.85)   # (a) ran to the cap
    halvings = [w for w in report.warnings if "halved" in w]
    assert halvings == ["fx_wash halved to 0.5 to keep title legible."]
    assert report.contrast["title"] >= 4.5
    assert not any("flipped text color" in w for w in report.warnings)
    image_b, _ = compose(spec, tmp_path, canvas=CANVAS)
    assert image_a.tobytes() == image_b.tobytes()         # fully deterministic


def test_recheck_ladder_flips_ink_then_exhausts_attenuation_at_the_floor(tmp_path):
    # #787878 fails BOTH inks with the dark fallback measuring best → the
    # flip fires; halving the wash then darkens the ground under a dark
    # ink, so nothing ever passes and every fx_ layer walks down to ≤0.05.
    spec = _ladder_spec("#787878")
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert any("flipped text color to #111111" in w for w in report.warnings)
    halvings = [w for w in report.warnings if "halved" in w]
    assert len(halvings) == 5                       # 1.0 → 0.03125 (≤ 0.05)
    assert "0.03125" in halvings[-1]
    assert any("still" in w and "finishing attenuation" in w
               for w in report.warnings)
    assert report.contrast["title"] < 4.5


def test_recheck_leaves_slots_without_finishing_above_on_the_legacy_path(tmp_path):
    # No adjust/fx_ machinery above the title: a later layer that steals
    # its contrast must produce exactly the launch-era warning — never the
    # ladder (§15.0's byte-identical-default-path constraint). The burying
    # layer is a light SCRIM (explicit zone over the title, drawn after
    # it): late art would be rescued by the v2.2 text-contact guard before
    # the safety net ever saw it, which is the guard doing its job.
    art = [_art(id="background", fit="cover", transparent=False,
                procedural="gradient")]
    scrims = [ScrimSpec(kind="panel", strength=0.9,
                        zone=Zone(x=0.05, y=0.05, w=0.9, h=0.35))]
    spec = _spec(art=art, scrims=scrims,
                 text=[_text()],
                 palette=_palette(scrim="#f5f1e8"),
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="text", ref="title"),
                        LayerRef(kind="scrim", ref="0")])
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert any("a layer drawn later covers this text" in w
               for w in report.warnings)
    assert not any("halved" in w for w in report.warnings)
