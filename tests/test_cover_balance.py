"""docproof/cover/balance.py: the balance & symmetry engine (§15.10) — axis
snap, rail snap, gap rhythm, and the mirror/mass/margin measurements — plus
its wiring through compose() (the snap pass runs after every positioning
guard and before anything paints, so scrims and the legibility autopilot
measure final positions; the measurements run over the finished composite).

The engine itself is pure geometry over InkElements, so most tests here
hand plan_snaps/margin_audit/measure_composite hand-built bboxes and pixel
fixtures and assert exact deltas — no fonts, no rendering. The compose
integration tests then prove the wiring the strongest way available: a
deliberately 1%-off-center spec must render BYTE-IDENTICAL to its exactly-
centered control (if the ink, the scrims derived from it, and every
downstream measurement all landed on the axis, the two covers are the same
cover), and a spec with no declared axis must render byte-identical to a
legacy-shaped spec that predates the fields entirely (§15.0 constraint 2).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from docproof.cover import balance
from docproof.cover.balance import (InkElement, gap_rhythm_warnings,
                                    margin_audit, measure_composite,
                                    mirror_symmetry, plan_snaps,
                                    resolve_axis_x)
from docproof.cover.compose import compose
from docproof.cover.model import (ArtSlot, CoverSpec, LayerRef, MaskSpec,
                                  Palette, TextSlot, Zone)

CANVAS = (400, 640)          # 1% of width = 4px; snap tolerance 1.5% = 6px


def _palette(**overrides) -> Palette:
    data = dict(background="#101820", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _text(**overrides) -> TextSlot:
    data = dict(id="title", content="GULL POINT",
               zone=Zone(x=0.25, y=0.3, w=0.5, h=0.2),
               font_family="Spectral", size_min=0.02, size_max=0.06)
    data.update(overrides)
    return TextSlot(**data)


def _spec(**overrides) -> CoverSpec:
    data = dict(
        archetype="big_type", concept_name="Balance", rationale="Fixture.",
        palette=_palette(),
        art=[ArtSlot(id="background", fit="cover", procedural="gradient")],
        scrims=[], text=[_text()],
        layers=[LayerRef(kind="art", ref="background"),
               LayerRef(kind="text", ref="title")])
    data.update(overrides)
    return CoverSpec(**data)


def _el(**overrides) -> InkElement:
    data = dict(id="title", kind="text", bbox=(150, 100, 250, 130),
               align="center", snappable=True, dx_min=-1000, dx_max=1000)
    data.update(overrides)
    return InkElement(**data)


# -- axis snap (plan_snaps) ----------------------------------------------------

def test_one_percent_off_center_snaps_exactly_onto_the_axis():
    # bbox center 196 — 4px (1% of width) left of the 200px center axis.
    deltas, lines = plan_snaps([_el(bbox=(156, 100, 236, 130))],
                               "center", 0.5, CANVAS)
    assert deltas == {("text", "title"): 4}
    assert len(lines) == 1
    assert "snapped onto the center axis (+4px)" in lines[0]
    assert "49.00%" in lines[0] and "50.00%" in lines[0]


def test_exactly_on_axis_is_not_moved_and_not_logged():
    deltas, lines = plan_snaps([_el(bbox=(160, 100, 240, 130))],   # center 200
                               "center", 0.5, CANVAS)
    assert deltas == {} and lines == []


def test_five_percent_off_center_is_left_alone_as_intentional():
    deltas, lines = plan_snaps([_el(bbox=(140, 100, 220, 130))],   # center 180
                               "center", 0.5, CANVAS)
    assert deltas == {} and lines == []


def test_snap_the_travel_bounds_cannot_absorb_is_skipped_not_clamped():
    # The exact snap needs +4px but only +2 is legal: leave untouched — a
    # partial move would land on no axis at all.
    deltas, lines = plan_snaps([_el(bbox=(156, 100, 236, 130), dx_max=2)],
                               "center", 0.5, CANVAS)
    assert deltas == {} and lines == []


def test_left_axis_snaps_the_leading_edge_onto_the_rail():
    # Rail at 0.08 × 400 = 32px; leading edge at 35 is 3px off.
    deltas, lines = plan_snaps([_el(bbox=(35, 100, 300, 130), align="left")],
                               "left", 0.08, CANVAS)
    assert deltas == {("text", "title"): -3}
    assert "leading edge" in lines[0] and "left rail at 8.0%" in lines[0]


def test_right_axis_snaps_the_trailing_edge_onto_the_rail():
    # Rail at 0.92 × 400 = 368px; trailing edge at 365 is 3px off.
    deltas, _ = plan_snaps([_el(bbox=(100, 100, 365, 130), align="right")],
                           "right", 0.92, CANVAS)
    assert deltas == {("text", "title"): 3}


def test_exempt_elements_never_axis_snap():
    deltas, lines = plan_snaps([_el(bbox=(156, 100, 236, 130), snappable=False)],
                               "center", 0.5, CANVAS)
    assert deltas == {} and lines == []


def test_contain_art_elements_snap_like_text():
    deltas, lines = plan_snaps(
        [_el(id="emblem", kind="art", bbox=(156, 100, 236, 180))],
        "center", 0.5, CANVAS)
    assert deltas == {("art", "emblem"): 4}
    assert lines[0].startswith("art 'emblem'")


# -- rail snap (plan_snaps) ----------------------------------------------------

def test_two_left_rails_under_a_percent_apart_unify_to_the_topmost():
    # Leading edges 100 and 103 (0.75% of width apart) — the lower slot
    # snaps to the topmost slot's rail. Axis is a left rail far away at
    # 32px, so no axis snap interferes.
    title = _el(bbox=(100, 50, 300, 80), align="left")
    author = _el(id="author", bbox=(103, 300, 280, 330), align="left")
    deltas, lines = plan_snaps([title, author], "left", 0.08, CANVAS)
    assert deltas == {("text", "author"): -3}
    assert len(lines) == 1
    assert "unified with 'title's rail (-3px)" in lines[0]


def test_equal_rails_are_already_unified_and_stay_silent():
    title = _el(bbox=(100, 50, 300, 80), align="left")
    author = _el(id="author", bbox=(100, 300, 280, 330), align="left")
    deltas, lines = plan_snaps([title, author], "left", 0.08, CANVAS)
    assert deltas == {} and lines == []


def test_rails_two_percent_apart_are_intentional_and_stay_put():
    title = _el(bbox=(100, 50, 300, 80), align="left")
    author = _el(id="author", bbox=(108, 300, 280, 330), align="left")
    deltas, _ = plan_snaps([title, author], "left", 0.08, CANVAS)
    assert deltas == {}


def test_right_aligned_sets_unify_on_their_trailing_edges():
    title = _el(bbox=(100, 50, 303, 80), align="right")
    author = _el(id="author", bbox=(120, 300, 300, 330), align="right")
    deltas, lines = plan_snaps([title, author], "left", 0.08, CANVAS)
    assert deltas == {("text", "author"): 3}
    assert "trailing edge" in lines[0]


def test_a_fixed_slot_still_anchors_the_rail_others_unify_to():
    # The topmost slot can't move (mask-entangled, say) but it is still a
    # fixed feature of the canvas — the slot below unifies TO it.
    title = _el(bbox=(100, 50, 300, 80), align="left", snappable=False)
    author = _el(id="author", bbox=(103, 300, 280, 330), align="left")
    deltas, _ = plan_snaps([title, author], "left", 0.08, CANVAS)
    assert deltas == {("text", "author"): -3}


# -- gap rhythm (warn only) ----------------------------------------------------

def test_near_equal_gaps_warn_naming_both_and_move_nothing():
    els = [_el(bbox=(100, 100, 300, 150)),
          _el(id="subtitle", bbox=(100, 170, 300, 200)),      # gap 20
          _el(id="author", bbox=(100, 223, 300, 250))]        # gap 23
    warnings = gap_rhythm_warnings(els, CANVAS)
    assert len(warnings) == 1
    assert "title→subtitle gap 3.1%" in warnings[0]
    assert "subtitle→author gap 3.6%" in warnings[0]
    assert "consider equalizing" in warnings[0]


def test_exactly_equal_gaps_are_rhythm_not_a_near_miss():
    els = [_el(bbox=(100, 100, 300, 150)),
          _el(id="subtitle", bbox=(100, 170, 300, 200)),      # gap 20
          _el(id="author", bbox=(100, 220, 300, 250))]        # gap 20
    assert gap_rhythm_warnings(els, CANVAS) == []


def test_clearly_different_gaps_are_intentional_hierarchy():
    els = [_el(bbox=(100, 100, 300, 150)),
          _el(id="subtitle", bbox=(100, 170, 300, 200)),      # gap 20
          _el(id="author", bbox=(100, 230, 300, 250))]        # gap 30
    assert gap_rhythm_warnings(els, CANVAS) == []


def test_overlapping_slots_form_no_gap_and_break_the_comparison_chain():
    els = [_el(bbox=(100, 100, 300, 180)),
          _el(id="subtitle", bbox=(100, 170, 300, 200)),      # overlaps title
          _el(id="author", bbox=(100, 223, 300, 250))]
    assert gap_rhythm_warnings(els, CANVAS) == []


# -- measurements: mirror symmetry, mass, margins ------------------------------

def _fixture(width=200, height=100, ground=(0, 0, 0)) -> Image.Image:
    return Image.new("RGB", (width, height), ground)


def test_mirror_symmetry_is_exactly_one_for_a_mirrored_image():
    img = _fixture()
    img.paste((255, 255, 255), (80, 20, 120, 80))    # centered block
    assert mirror_symmetry(img) == 1.0


def test_mirror_symmetry_is_low_for_a_lopsided_image():
    img = _fixture()
    img.paste((255, 255, 255), (100, 0, 200, 100))   # right half white
    assert mirror_symmetry(img) < 0.1


def test_heavier_half_attribution_names_the_right_half_with_its_share():
    img = _fixture()
    img.paste((255, 255, 255), (110, 10, 190, 90))   # bright mass at right
    result = measure_composite(img, "center")
    assert result.symmetry < balance.SYMMETRY_WARN_FLOOR
    [warning] = [w for w in result.warnings if "mirror symmetry" in w]
    assert "right half carries" in warning
    share = int(warning.split("carries ")[1].split("%")[0])
    assert share > 50


def test_center_of_mass_warns_past_six_percent_and_names_the_side():
    img = _fixture()
    img.paste((255, 255, 255), (150, 10, 200, 90))   # all weight far right
    result = measure_composite(img, "center")
    assert result.center_of_mass_x > 0.56
    assert any("visual center of mass" in w and "right of the center axis" in w
              for w in result.warnings)


def test_a_uniform_canvas_has_no_weight_and_measures_dead_center():
    result = measure_composite(_fixture(ground=(120, 120, 120)), "center")
    assert result.symmetry == 1.0
    assert result.center_of_mass_x == 0.5
    assert result.warnings == []


def test_rail_axis_compositions_measure_but_never_cry_wolf():
    # A left/right-rail composition is asymmetric ON PURPOSE — the scores
    # still come back for the judge, the warnings stay quiet.
    img = _fixture()
    img.paste((255, 255, 255), (100, 0, 200, 100))
    result = measure_composite(img, "left")
    assert result.symmetry < 0.1
    assert result.warnings == []


def test_margin_audit_flags_ink_inside_the_two_percent_band():
    warnings = margin_audit([_el(id="emblem", kind="art",
                                 bbox=(2, 100, 50, 200))], CANVAS)
    assert warnings == ["art 'emblem': ink 0.5% from the left trim edge "
                        "(limit 2%)."]


def test_margin_audit_exempts_a_full_bleed_art_plate_but_never_text():
    plate = _el(id="plate", kind="art", bbox=(0, 0, 400, 640))
    assert margin_audit([plate], CANVAS) == []
    drowned = _el(bbox=(0, 0, 400, 640))             # text at all four edges
    assert len(margin_audit([drowned], CANVAS)) == 4


def test_margin_audit_ink_at_exactly_two_percent_is_clean():
    # The corners placement insets exactly 2% by construction — sitting ON
    # the line is compliant, not "under 2%".
    warnings = margin_audit([_el(id="corner", kind="art",
                                 bbox=(8, 13, 392, 627))], CANVAS)
    assert warnings == []


# -- ink measurement + translation helpers -------------------------------------

def test_ink_bbox_ignores_sub_threshold_alpha_tails():
    layer = Image.new("RGBA", CANVAS, (255, 255, 255, 10))   # faint wash
    layer.paste((255, 255, 255, 255), (100, 200, 150, 260))
    assert balance.ink_bbox(layer) == (100, 200, 150, 260)
    assert balance.ink_bbox(Image.new("RGBA", CANVAS, (0, 0, 0, 0))) is None


def test_translate_x_shifts_both_directions_and_crops_at_the_trim():
    layer = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    layer.putpixel((5, 5), (255, 0, 0, 255))
    assert balance.translate_x(layer, 3, CANVAS).getpixel((8, 5))[3] == 255
    assert balance.translate_x(layer, -3, CANVAS).getpixel((2, 5))[3] == 255
    assert balance.translate_x(layer, -6, CANVAS).getchannel("A").getbbox() is None


def test_resolve_axis_x_defaults_and_overrides():
    assert resolve_axis_x("center", 0.9) == 0.5      # center never reads it
    assert resolve_axis_x("left", None) == 0.08
    assert resolve_axis_x("right", None) == 0.92
    assert resolve_axis_x("left", 0.12) == 0.12


# -- compose integration: the snap pass ---------------------------------------

def test_one_percent_off_center_spec_renders_identical_to_the_centered_control():
    """The wave's own acceptance shape (§15.15e): a deliberately 1%-off-
    center fixture ships exactly on axis with the adjustment logged. Byte-
    equality against the exactly-centered control is the strongest form of
    "exactly on axis": it also proves the scrims/legibility machinery saw
    the SNAPPED position (they derive from the same zone the snap moved)."""
    off = _spec(text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))],
                axis="center")
    control = _spec(text=[_text(zone=Zone(x=0.25, y=0.3, w=0.5, h=0.2))],
                    axis="center")
    img_off, report_off = compose(off, Path("/nonexistent"), canvas=CANVAS)
    img_ctl, report_ctl = compose(control, Path("/nonexistent"), canvas=CANVAS)
    assert img_off.tobytes() == img_ctl.tobytes()
    [line] = report_off.adjustments
    assert "text 'title'" in line and "snapped onto the center axis" in line
    assert report_ctl.adjustments == []              # already exact — no move


def test_five_percent_off_center_spec_is_left_asymmetric():
    off = _spec(text=[_text(zone=Zone(x=0.20, y=0.3, w=0.5, h=0.2))],
                axis="center")
    control = _spec(text=[_text(zone=Zone(x=0.25, y=0.3, w=0.5, h=0.2))],
                    axis="center")
    img_off, report_off = compose(off, Path("/nonexistent"), canvas=CANVAS)
    img_ctl, _ = compose(control, Path("/nonexistent"), canvas=CANVAS)
    assert report_off.adjustments == []
    assert img_off.tobytes() != img_ctl.tobytes()


def test_two_left_aligned_slots_a_fraction_apart_unify_through_compose():
    text = [_text(zone=Zone(x=0.05, y=0.15, w=0.55, h=0.2), align="left"),
           _text(id="author", content="J. R. VANCE",
                 zone=Zone(x=0.058, y=0.6, w=0.55, h=0.15), align="left")]
    spec = _spec(text=text, axis="center",
                 layers=[LayerRef(kind="art", ref="background"),
                        LayerRef(kind="text", ref="title"),
                        LayerRef(kind="text", ref="author")])
    _, report = compose(spec, Path("/nonexistent"), canvas=CANVAS)
    assert any("unified with 'title's rail" in line
              for line in report.adjustments)


def test_snapped_compose_is_deterministic():
    spec = _spec(text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))],
                 axis="center")
    img_a, report_a = compose(spec, Path("/nonexistent"), canvas=CANVAS)
    img_b, report_b = compose(spec, Path("/nonexistent"), canvas=CANVAS)
    assert img_a.tobytes() == img_b.tobytes()
    assert report_a == report_b


def test_contain_art_one_percent_off_snaps_onto_the_axis(tmp_path):
    """The art half of the axis snap: an ornament nudged 1% off the center
    axis via `offset` renders byte-identical to the same ornament placed
    dead center — the positioned pixels themselves were translated."""
    Image.new("RGBA", (120, 120), (200, 60, 30, 255)).save(tmp_path / "orn.png")

    def build(offset_x: float) -> CoverSpec:
        return _spec(
            art=[ArtSlot(id="background", fit="cover", procedural="gradient"),
                ArtSlot(id="ornament", asset="orn.png", fit="contain",
                        scale=0.25, offset=[offset_x, 0.0])],
            axis="center",
            layers=[LayerRef(kind="art", ref="background"),
                   LayerRef(kind="art", ref="ornament"),
                   LayerRef(kind="text", ref="title")])

    img_off, report_off = compose(build(0.01), tmp_path, canvas=CANVAS)
    img_ctl, _ = compose(build(0.0), tmp_path, canvas=CANVAS)
    assert img_off.tobytes() == img_ctl.tobytes()
    assert any("art 'ornament'" in line and "snapped onto the center axis" in line
              for line in report_off.adjustments)


def test_a_from_text_clipped_title_is_exempt_from_snapping(tmp_path):
    """Art was already clipped INTO the title's glyphs at their current
    position (the mask pass ran before the snap pass) — moving the glyphs
    now would strand art-in-the-letterforms where the letters no longer
    are, so that text slot must not move even 1% off axis."""
    Image.new("RGBA", (120, 190), (30, 190, 60, 255)).save(tmp_path / "forest.png")
    spec = _spec(
        art=[ArtSlot(id="background", fit="cover", procedural="gradient"),
            ArtSlot(id="forest", asset="forest.png", fit="cover",
                    mask=MaskSpec(from_text="title"))],
        text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))],
        axis="center",
        layers=[LayerRef(kind="art", ref="background"),
               LayerRef(kind="art", ref="forest"),
               LayerRef(kind="text", ref="title")])
    _, report = compose(spec, tmp_path, canvas=CANVAS)
    assert not any("text 'title'" in line for line in report.adjustments)


# -- compose integration: measurements ----------------------------------------

def test_cover_fit_art_is_exempt_from_the_margin_audit_but_contain_is_not(
        tmp_path):
    Image.new("RGBA", (120, 120), (220, 220, 220, 255)).save(tmp_path / "orn.png")

    def build(fit: str, anchor: list[float]) -> CoverSpec:
        return _spec(
            art=[ArtSlot(id="background", fit="cover", procedural="gradient"),
                ArtSlot(id="ornament", asset="orn.png", fit=fit,
                        scale=0.25 if fit == "contain" else 1.0,
                        anchor=anchor)],
            layers=[LayerRef(kind="art", ref="background"),
                   LayerRef(kind="art", ref="ornament"),
                   LayerRef(kind="text", ref="title")])

    _, contained = compose(build("contain", [0.0, 0.5]), tmp_path, canvas=CANVAS)
    assert any("art 'ornament'" in w and "left trim edge" in w
              for w in contained.warnings)
    _, covered = compose(build("cover", [0.5, 0.5]), tmp_path, canvas=CANVAS)
    assert not any("art 'ornament'" in w for w in covered.warnings)


def test_measurements_run_even_with_no_declared_axis():
    # Report-only: a pre-wave spec's pixels are untouchable, its numbers
    # are not — the margin audit still names a flush element under
    # axis=None (the snap pass alone is gated).
    spec = _spec(text=[_text(zone=Zone(x=0.0, y=0.3, w=0.5, h=0.2),
                             align="left")])
    assert spec.axis is None
    _, report = compose(spec, Path("/nonexistent"), canvas=CANVAS)
    assert report.adjustments == []
    assert any("text 'title'" in w and "left trim edge" in w
              for w in report.warnings)


# -- §15.0 constraint 2: the byte-identical default path ----------------------

def test_axis_none_renders_byte_identical_to_a_legacy_shaped_spec():
    """The permanent, environment-independent golden proof for THIS wave
    (the same shape PR1 established for the layer engine): a spec whose
    JSON predates axis/axis_x entirely and today's default construction
    render the same pixels, report equal reports, and record zero
    adjustments."""
    modern = _spec(text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))])
    dump = modern.model_dump()
    del dump["axis"], dump["axis_x"]                 # a pre-wave archive
    legacy = CoverSpec.model_validate(dump)
    assert legacy.axis is None and legacy.axis_x is None
    img_modern, report_modern = compose(modern, Path("/nonexistent"),
                                        canvas=CANVAS)
    img_legacy, report_legacy = compose(legacy, Path("/nonexistent"),
                                        canvas=CANVAS)
    assert img_modern.tobytes() == img_legacy.tobytes()
    assert report_modern == report_legacy
    assert report_modern.adjustments == []


def test_declaring_the_axis_is_what_arms_the_snap_pass():
    # The same 1%-off fixture: axis=None leaves it alone, axis="center"
    # moves it — the gate is the declaration, not the geometry.
    off_none = _spec(text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))])
    off_center = _spec(text=[_text(zone=Zone(x=0.24, y=0.3, w=0.5, h=0.2))],
                       axis="center")
    img_none, report_none = compose(off_none, Path("/nonexistent"), canvas=CANVAS)
    img_center, report_center = compose(off_center, Path("/nonexistent"),
                                        canvas=CANVAS)
    assert report_none.adjustments == []
    assert report_center.adjustments != []
    assert img_none.tobytes() != img_center.tobytes()
