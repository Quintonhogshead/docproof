"""docproof/cover/typeset.py: measurement, the fit search, balanced line
breaking, and tracked/effect text rendering onto a Pillow layer.

Every test uses the real bundled Spectral face, resolved through
fonts.font_path (never a hardcoded path), and a tiny 400x640 canvas so the
12-iteration binary search and the brute-force line-break search stay fast.
No network anywhere in typeset.py, so nothing here needs monkeypatching —
see docs/cover_designer_spec.md §11's typeset bullets for what this covers.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageFont

from docproof.cover.fonts import font_path
from docproof.cover.model import Shadow, Stroke, TextSlot, Zone
from docproof.cover.typeset import (FIT_ITERATIONS, LINE_HEIGHT,
                                    MAX_BRUTE_WORDS, draw_text, fit_text,
                                    measure)

CANVAS = (400, 640)


def _slot(**overrides) -> TextSlot:
    data = dict(id="title", content="A Title",
               zone=Zone(x=0.05, y=0.05, w=0.9, h=0.4),
               font_family="Spectral", size_min=0.03, size_max=0.12, max_lines=3)
    data.update(overrides)
    return TextSlot(**data)


def _font(size_px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path("Spectral"), size_px)


def _blank() -> Image.Image:
    return Image.new("RGBA", CANVAS, (0, 0, 0, 0))


def _opaque_pixel_count(img: Image.Image) -> int:
    return sum(img.getchannel("A").histogram()[1:])


# -- module constants pin the spec's numbers ---------------------------------

def test_constants_match_the_spec():
    assert FIT_ITERATIONS == 12
    assert MAX_BRUTE_WORDS == 12
    assert LINE_HEIGHT == 1.08


# -- measure() ----------------------------------------------------------------

def test_measure_empty_string_is_zero():
    assert measure("", _font(40), 10.0) == 0.0


def test_measure_zero_tracking_equals_font_getlength():
    font = _font(40)
    assert measure("Hello", font, 0.0) == font.getlength("Hello")


def test_measure_tracking_widens_the_measured_width():
    font = _font(40)
    tight = measure("Hello World", font, 0.0)
    tracked = measure("Hello World", font, 5.0)
    assert tracked > tight
    # "Hello World" is 11 characters -> 10 inter-glyph gaps, one per gap only
    # (never at the ends) — see the measure() docstring.
    assert tracked == pytest.approx(tight + 5.0 * 10)


def test_measure_single_character_has_no_gap_to_track():
    font = _font(40)
    assert measure("H", font, 25.0) == font.getlength("H")


# -- fit_text(): the binary search --------------------------------------------

def test_fit_search_monotonicity_longer_title_is_smaller_or_equal_size():
    short = _slot(content="Ash")
    long = _slot(content="The Kingdom of Ash and Embers at the End of the World")
    assert fit_text(long, CANVAS).size_px <= fit_text(short, CANVAS).size_px


def test_fit_result_never_exceeds_the_zone_when_it_fits():
    slot = _slot(content="The Lighthouse at Gull Point", max_lines=3)
    fit = fit_text(slot, CANVAS)
    assert fit.fits is True
    font = _font(fit.size_px)
    zone_w_px = slot.zone.w * CANVAS[0]
    zone_h_px = slot.zone.h * CANVAS[1]
    for line in fit.lines:
        assert measure(line, font, fit.tracking_px) <= zone_w_px + 1e-6
    assert len(fit.lines) * fit.size_px * LINE_HEIGHT <= zone_h_px + 1e-6


def test_fit_result_fits_true_carries_no_warning():
    fit = fit_text(_slot(content="Short"), CANVAS)
    assert fit.fits is True
    assert fit.warning is None


def test_unbreakable_word_shrinks_below_size_min_until_it_fits():
    # A tiny zone (5% of a 400px-wide canvas = 20px) paired with a size that
    # cannot shrink (size_min == size_max) guarantees even one line of this
    # long, hard-to-break text overflows the zone width at the floor — the
    # no-cropped-glyphs escalation (2026-08-30 addendum, superseding §7.3's
    # "rendered anyway at the floor") must SHRINK BELOW size_min until the
    # widest line genuinely fits, never render overflowing ink.
    slot = _slot(
        content="Supercalifragilisticexpialidocious Antidisestablishmentarianism",
        zone=Zone(x=0.0, y=0.0, w=0.05, h=0.03), max_lines=1,
        size_min=0.05, size_max=0.05)
    fit = fit_text(slot, CANVAS)
    assert fit.fits is False               # the slot's own floor was breached
    assert fit.warning is not None
    assert "title" in fit.warning
    assert "below size_min" in fit.warning
    assert fit.size_px < slot.size_min * CANVAS[1]
    assert fit.lines   # still rendered — never nothing
    font = _font(fit.size_px)
    zone_w_px = slot.zone.w * CANVAS[0]
    for line in fit.lines:
        assert measure(line, font, fit.tracking_px) <= zone_w_px + 1e-6


def test_fit_text_empty_content_yields_no_lines_and_does_not_crash():
    fit = fit_text(_slot(content="", optional=True), CANVAS)
    assert fit.lines == ()
    assert fit.fits is True
    assert fit.warning is None


@pytest.mark.parametrize("case,expected", [
    ("upper", "A LOWERCASE TITLE"),
    ("title", "A Lowercase Title"),
    ("as_is", "a lowercase title"),
])
def test_fit_text_applies_case_before_breaking(case, expected):
    slot = _slot(content="a lowercase title", case=case, max_lines=1,
                size_min=0.02, size_max=0.02)
    fit = fit_text(slot, CANVAS)
    assert fit.lines == (expected,)


def test_fit_text_tracking_px_scales_with_the_chosen_size():
    fit = fit_text(_slot(content="Ash", tracking=100.0), CANVAS)
    assert fit.tracking_px == pytest.approx(100.0 / 1000.0 * fit.size_px)


def test_fit_text_zero_tracking_slot_has_zero_tracking_px():
    fit = fit_text(_slot(content="Ash", tracking=0.0), CANVAS)
    assert fit.tracking_px == 0.0


def test_fit_text_size_frac_is_size_px_over_canvas_height():
    fit = fit_text(_slot(content="Ash"), CANVAS)
    assert fit.size_frac == pytest.approx(fit.size_px / CANVAS[1])


# -- balanced breaking beats greedy on a known title -------------------------

def _pvariance(widths: list[float]) -> float:
    if len(widths) <= 1:
        return 0.0
    mean = sum(widths) / len(widths)
    return sum((w - mean) ** 2 for w in widths) / len(widths)


def test_balanced_breaking_beats_greedy_on_a_known_title():
    # Zone width and a pinned size (size_min == size_max) chosen so both a
    # naive greedy wrap AND the balanced search produce a 3-line result that
    # FITS — this isolates "which breaking looks better" from "which one
    # merely fits", which is the point of the balanced search (§7.3 point 4).
    content = "The Long Way Down to the Sea"
    zone_w_px, size_px = 390, 60
    slot = _slot(content=content, case="as_is", tracking=0.0, max_lines=3,
                zone=Zone(x=0.0, y=0.0, w=zone_w_px / CANVAS[0], h=250 / CANVAS[1]),
                size_min=size_px / CANVAS[1], size_max=size_px / CANVAS[1])
    fit = fit_text(slot, CANVAS)
    assert fit.fits is True
    assert fit.size_px == pytest.approx(size_px)

    font = _font(fit.size_px)
    balanced_widths = [measure(line, font, 0.0) for line in fit.lines]

    # A plain greedy word-wrap over the identical words/font/zone width.
    words = content.split()
    greedy_lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = current + [word]
        if current and measure(" ".join(trial), font, 0.0) > zone_w_px:
            greedy_lines.append(" ".join(current))
            current = [word]
        else:
            current = trial
    if current:
        greedy_lines.append(" ".join(current))
    greedy_widths = [measure(line, font, 0.0) for line in greedy_lines]

    assert len(fit.lines) == len(greedy_lines) == 3
    assert fit.lines != tuple(greedy_lines)
    assert _pvariance(balanced_widths) < _pvariance(greedy_widths)


# -- draw_text(): rendering ---------------------------------------------------

def test_draw_text_with_no_lines_returns_the_same_base_object():
    base = _blank()
    fit = fit_text(_slot(content="", optional=True), CANVAS)
    out = draw_text(base, _slot(content="", optional=True), fit, "#ffffff", None, CANVAS)
    assert out is base


def test_draw_text_paints_ink_without_mutating_base():
    base = _blank()
    slot = _slot(content="Ash", max_lines=1)
    fit = fit_text(slot, CANVAS)
    out = draw_text(base, slot, fit, "#ffffff", None, CANVAS)
    assert out.getbbox() is not None
    assert base.getbbox() is None
    assert out is not base


def test_draw_text_alignment_moves_ink_left_to_right():
    fit = fit_text(_slot(content="Ash", align="left", max_lines=1), CANVAS)
    left_bbox = draw_text(_blank(), _slot(content="Ash", align="left", max_lines=1),
                          fit, "#ffffff", None, CANVAS).getbbox()
    center_bbox = draw_text(_blank(), _slot(content="Ash", align="center", max_lines=1),
                            fit, "#ffffff", None, CANVAS).getbbox()
    right_bbox = draw_text(_blank(), _slot(content="Ash", align="right", max_lines=1),
                           fit, "#ffffff", None, CANVAS).getbbox()
    assert left_bbox[0] < center_bbox[0] < right_bbox[0]


def test_draw_text_color_is_applied_to_the_glyph_ink():
    slot = _slot(content="Ash", max_lines=1)
    fit = fit_text(slot, CANVAS)
    red = draw_text(_blank(), slot, fit, "#ff0000", None, CANVAS)
    blue = draw_text(_blank(), slot, fit, "#0000ff", None, CANVAS)
    bbox = red.getbbox()
    # Fully-opaque interior pixels carry the fill color exactly (only
    # anti-aliased edge pixels are partial-alpha blends), so any alpha==255
    # pixel is a reliable probe.
    for y in range(bbox[1], bbox[3]):
        for x in range(bbox[0], bbox[2]):
            if red.getpixel((x, y))[3] == 255:
                assert red.getpixel((x, y)) == (255, 0, 0, 255)
                assert blue.getpixel((x, y)) == (0, 0, 255, 255)
                return
    pytest.fail("no fully-opaque glyph pixel found to probe")


def test_draw_text_stroke_widens_the_ink_bounding_box():
    plain_slot = _slot(content="Ash", max_lines=1, stroke=None)
    stroked_slot = _slot(content="Ash", max_lines=1,
                         stroke=Stroke(width=0.02, color="#ff0000"))
    fit = fit_text(plain_slot, CANVAS)   # same geometry either way
    plain_bbox = draw_text(_blank(), plain_slot, fit, "#ffffff", None, CANVAS).getbbox()
    stroked_bbox = draw_text(_blank(), stroked_slot, fit, "#ffffff", None, CANVAS).getbbox()

    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1])

    assert area(stroked_bbox) > area(plain_bbox)


def test_draw_text_shadow_adds_pixels_not_present_without_it():
    slot = _slot(content="Ash", max_lines=1)
    fit = fit_text(slot, CANVAS)
    no_shadow = draw_text(_blank(), slot, fit, "#ffffff", None, CANVAS)
    with_shadow = draw_text(_blank(), slot, fit, "#ffffff", Shadow(), CANVAS)
    assert no_shadow.tobytes() != with_shadow.tobytes()
    assert _opaque_pixel_count(with_shadow) > _opaque_pixel_count(no_shadow)


def test_draw_text_tracking_widens_the_rendered_block_like_it_widens_measure():
    tight = _slot(content="ASH", tracking=0.0, max_lines=1)
    tracked = _slot(content="ASH", tracking=300.0, max_lines=1)
    tight_fit = fit_text(tight, CANVAS)
    tracked_fit = fit_text(tracked, CANVAS)
    tight_bbox = draw_text(_blank(), tight, tight_fit, "#ffffff", None, CANVAS).getbbox()
    tracked_bbox = draw_text(_blank(), tracked, tracked_fit, "#ffffff", None, CANVAS).getbbox()

    def width(b):
        return b[2] - b[0]

    assert width(tracked_bbox) > width(tight_bbox)


def test_draw_text_multiline_lines_stack_top_to_bottom_without_overlap():
    slot = _slot(content="Ash and Embers of the Old World", align="left",
                zone=Zone(x=0.05, y=0.05, w=0.35, h=0.5), max_lines=4,
                size_min=0.03, size_max=0.03)
    fit = fit_text(slot, CANVAS)
    assert len(fit.lines) >= 2
    out = draw_text(_blank(), slot, fit, "#ffffff", None, CANVAS)
    assert out.getbbox() is not None
    # Row i's vertical center should be strictly below row i-1's.
    line_h = fit.size_px * LINE_HEIGHT
    centers = [line_h * (i + 0.5) for i in range(len(fit.lines))]
    assert centers == sorted(centers)
    assert len(set(centers)) == len(centers)


# -- expressive typography (§15.12) — the four type moves ---------------------
# Every move must change WHERE ink lands while text_mask stays pixel-aligned
# with draw_text's ink (the guards' seam), and the legacy paths must stay
# untouched for slots that carry no move (the golden-bytes check proves that
# end to end; these tests pin the mechanisms).

from PIL import ImageFont as _ImageFont

from docproof.cover.fonts import FAMILIES
from docproof.cover.typeset import (EMPHASIS_LARGER_SCALE, STACK_RATIO_CAP,
                                    line_ink_boxes, text_mask)


def _ink_and_mask_bboxes(slot):
    fit = fit_text(slot, CANVAS)
    ink = draw_text(_blank(), slot, fit, "#ffffff", None, CANVAS)
    return fit, ink.getbbox(), text_mask(slot, fit, CANVAS).getbbox()


# -- justify_stack ------------------------------------------------------------

def test_justify_stack_line_widths_fill_the_zone_within_one_pixel():
    slot = _slot(content="The Long Way Down", fit_mode="justify_stack",
                size_min=0.02, size_max=0.35, max_lines=3)
    fit = fit_text(slot, CANVAS)
    assert fit.line_sizes_px and len(fit.line_sizes_px) == len(fit.lines)
    zone_w_px = slot.zone.w * CANVAS[0]
    ratio_cap = STACK_RATIO_CAP * min(fit.line_sizes_px)
    for line, size_px in zip(fit.lines, fit.line_sizes_px):
        if (size_px >= slot.size_max * CANVAS[1] - 1e-6
                or size_px >= ratio_cap - 1e-6):
            continue   # clamped lines legitimately underfill
        tracking_px = slot.tracking / 1000.0 * size_px
        width = measure(line, _font(size_px), tracking_px)
        assert width <= zone_w_px + 1e-6
        assert width >= zone_w_px - 1.0   # "within 1px of the zone width"


def test_justify_stack_ratio_cap_holds_and_still_creates_contrast():
    # "THE" alone on a line wants to render huge against the long line —
    # the cap limits it to STACK_RATIO_CAP× the smallest, not to sameness.
    slot = _slot(content="THE Antidisestablishmentarianism Conspiracy",
                fit_mode="justify_stack", case="as_is",
                size_min=0.01, size_max=0.5, max_lines=3,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.9))
    fit = fit_text(slot, CANVAS)
    assert len(fit.lines) >= 2
    smallest, largest = min(fit.line_sizes_px), max(fit.line_sizes_px)
    assert largest / smallest <= STACK_RATIO_CAP + 1e-6
    assert largest / smallest > 1.5    # the drama survived the cap


def test_justify_stack_total_height_fits_the_zone():
    slot = _slot(content="A Short Stack of Words Here", fit_mode="justify_stack",
                size_min=0.02, size_max=0.4, max_lines=4,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.2))
    fit = fit_text(slot, CANVAS)
    zone_h_px = slot.zone.h * CANVAS[1]
    block_h = sum(s * LINE_HEIGHT for s in fit.line_sizes_px)
    assert block_h <= zone_h_px + 1e-6


def test_justify_stack_shrinks_proportionally_when_no_stack_fits_the_height():
    # A zone so short even the shortest candidate stack overflows: the
    # winner (scored by least height overflow) shrinks proportionally to
    # exactly the zone height — same break, every line scaled by the same
    # factor (max_lines=1 pins the break so the two fits are comparable).
    tall = _slot(content="The Conspiracy of Ravens", fit_mode="justify_stack",
                size_min=0.005, size_max=0.5, max_lines=1,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.5))
    tall_fit = fit_text(tall, CANVAS)
    short = tall.model_copy(update={"zone": Zone(x=0.05, y=0.05, w=0.9, h=0.04)})
    short_fit = fit_text(short, CANVAS)
    assert short_fit.lines == tall_fit.lines   # same break, just shrunk
    zone_h_px = round(short.zone.h * CANVAS[1])   # zone_px's integer-px grid
    block_h = sum(s * LINE_HEIGHT for s in short_fit.line_sizes_px)
    assert block_h <= zone_h_px + 1e-6
    assert block_h == pytest.approx(zone_h_px, rel=0.02)  # shrunk TO it,
    assert short_fit.line_sizes_px[0] < tall_fit.line_sizes_px[0]  # not past


def test_justify_stack_never_overflows_zone_width_even_on_the_floor_brief():
    # The 2026-08-30 addendum's immunity pin: the thriller repro title in a
    # justify_stack slot keeps every line inside the zone width regardless
    # of size_min — a line whose fill size falls below the floor keeps the
    # below-floor size (warned) instead of clamping up into an overflow.
    slot = _slot(content="The Lighthouse at Gull Point", case="upper",
                fit_mode="justify_stack", max_lines=2,
                size_min=0.07, size_max=0.12,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.3))
    fit = fit_text(slot, CANVAS)
    zone_w_px = slot.zone.w * CANVAS[0]
    for line, size_px in zip(fit.lines, fit.line_sizes_px):
        tracking_px = slot.tracking / 1000.0 * size_px
        assert measure(line, _font(size_px), tracking_px) <= zone_w_px + 1e-6
    mask_bbox = text_mask(slot, fit, CANVAS).getbbox()
    zone_left = round(slot.zone.x * CANVAS[0])
    assert mask_bbox[0] >= zone_left - 1
    assert mask_bbox[2] <= zone_left + round(slot.zone.w * CANVAS[0]) + 1


def test_justify_stack_draw_and_mask_agree_and_rows_stack_in_order():
    slot = _slot(content="The Long Way Down", fit_mode="justify_stack",
                size_min=0.02, size_max=0.3, max_lines=3)
    fit, ink_bbox, mask_bbox = _ink_and_mask_bboxes(slot)
    assert ink_bbox == mask_bbox
    boxes = [b for b in line_ink_boxes(slot, fit, CANVAS) if b is not None]
    tops = [b[1] for b in boxes]
    assert tops == sorted(tops)


# -- floor escalation (uniform), 2026-08-30 addendum --------------------------

def test_floor_escalation_allows_extra_lines_when_the_zone_is_tall():
    # Wide-but-impossible within max_lines at the floor, in a zone tall
    # enough for more rows: escalation step 1 exceeds max_lines at the
    # floor size rather than overflowing the zone width.
    slot = _slot(content="The Lighthouse at the Point of No Return",
                case="upper", max_lines=1, size_min=0.055, size_max=0.06,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.5))
    fit = fit_text(slot, CANVAS)
    assert fit.fits is False
    assert "exceeded max_lines" in (fit.warning or "")
    assert len(fit.lines) > slot.max_lines
    assert fit.size_px == pytest.approx(slot.size_min * CANVAS[1])
    font = _font(fit.size_px)
    zone_w_px = slot.zone.w * CANVAS[0]
    for line in fit.lines:
        assert measure(line, font, fit.tracking_px) <= zone_w_px + 1e-6
    block_h = len(fit.lines) * fit.size_px * LINE_HEIGHT
    assert block_h <= slot.zone.h * CANVAS[1] + 1e-6


def test_every_fit_path_keeps_ink_inside_the_zone():
    # Zone overflow must be impossible on every fit path — sample the
    # regular path, both escalations, and justify_stack with one repro
    # each, asserting the rendered alpha's bbox sits inside the zone rect
    # (padded a few px: glyph em-boxes legitimately poke a hair past the
    # nominal LINE_HEIGHT rows).
    fixtures = [
        _slot(content="Gull Point"),
        _slot(content="The Lighthouse at the Point of No Return",
             case="upper", max_lines=1, size_min=0.055, size_max=0.06,
             zone=Zone(x=0.05, y=0.05, w=0.9, h=0.5)),
        _slot(content="Supercalifragilisticexpialidocious",
             zone=Zone(x=0.1, y=0.1, w=0.2, h=0.05), max_lines=1,
             size_min=0.05, size_max=0.05),
        _slot(content="The Lighthouse at Gull Point", case="upper",
             fit_mode="justify_stack", max_lines=2,
             size_min=0.07, size_max=0.12,
             zone=Zone(x=0.05, y=0.05, w=0.9, h=0.3)),
    ]
    for slot in fixtures:
        fit = fit_text(slot, CANVAS)
        bbox = text_mask(slot, fit, CANVAS).getbbox()
        assert bbox is not None
        zone_left = round(slot.zone.x * CANVAS[0])
        zone_top = round(slot.zone.y * CANVAS[1])
        zone_right = zone_left + round(slot.zone.w * CANVAS[0])
        zone_bottom = zone_top + round(slot.zone.h * CANVAS[1])
        pad = max(4, round(fit.size_px * 0.25))
        assert bbox[0] >= zone_left - pad
        assert bbox[2] <= zone_right + pad
        assert bbox[1] >= zone_top - pad
        assert bbox[3] <= zone_bottom + pad


# -- arc ----------------------------------------------------------------------

def test_arc_fit_measures_the_chord_so_the_fit_is_unchanged():
    flat = _slot(content="Gull Point", max_lines=1)
    arced = _slot(content="Gull Point", max_lines=1, arc=0.3)
    flat_fit, arc_fit = fit_text(flat, CANVAS), fit_text(arced, CANVAS)
    assert arc_fit.lines == flat_fit.lines
    assert arc_fit.size_px == pytest.approx(flat_fit.size_px)


def test_arc_arch_raises_the_middle_and_valley_lowers_it():
    def middle_top_and_edge_top(arc):
        slot = _slot(content="MOUNTAIN RANGE", max_lines=1, arc=arc,
                    zone=Zone(x=0.05, y=0.3, w=0.9, h=0.3))
        fit = fit_text(slot, CANVAS)
        mask = text_mask(slot, fit, CANVAS)
        left, top, right, bottom = mask.getbbox()
        mid_w = (right - left) // 5
        mid = mask.crop(((left + right) // 2 - mid_w // 2, 0,
                         (left + right) // 2 + mid_w // 2, CANVAS[1])).getbbox()
        edge = mask.crop((left, 0, left + mid_w, CANVAS[1])).getbbox()
        return mid[1], edge[1]

    flat_mid, flat_edge = middle_top_and_edge_top(0.0)
    arch_mid, arch_edge = middle_top_and_edge_top(0.3)
    valley_mid, _ = middle_top_and_edge_top(-0.3)
    assert arch_mid < flat_mid          # arch: middle ink rides higher
    assert valley_mid > flat_mid        # valley: middle ink dips lower
    assert arch_mid < arch_edge         # and higher than its own ends


def test_arc_bows_even_at_tracking_zero_and_mask_matches_ink():
    slot = _slot(content="Gull Point", max_lines=1, arc=0.25, tracking=0.0)
    fit, ink_bbox, mask_bbox = _ink_and_mask_bboxes(slot)
    assert ink_bbox == mask_bbox
    flat = _slot(content="Gull Point", max_lines=1, arc=0.0)
    flat_bbox = draw_text(_blank(), flat, fit_text(flat, CANVAS), "#ffffff",
                          None, CANVAS).getbbox()
    # The bow makes the ink's vertical extent taller than the flat line's.
    assert (ink_bbox[3] - ink_bbox[1]) > (flat_bbox[3] - flat_bbox[1]) + 5


def test_arc_chord_stays_within_the_zone_width():
    slot = _slot(content="The Lighthouse at Gull Point", arc=0.3, max_lines=2)
    fit = fit_text(slot, CANVAS)
    bbox = text_mask(slot, fit, CANVAS).getbbox()
    zone_left = round(slot.zone.x * CANVAS[0])
    zone_right = zone_left + round(slot.zone.w * CANVAS[0])
    # Glyph tangent rotation swings edge glyph corners past the chord by
    # up to roughly a glyph box at steep bows — bounded latitude, never a
    # runaway (the chord itself, which is what the fit measured, fits).
    pad = round(fit.size_px)
    assert bbox[0] >= zone_left - pad
    assert bbox[2] <= zone_right + pad


# -- rotate -------------------------------------------------------------------

def test_rotated_ink_stays_inside_the_canvas_and_matches_its_mask():
    for angle in (15.0, -15.0):
        slot = _slot(content="The Lighthouse at Gull Point", rotate=angle,
                    zone=Zone(x=0.0, y=0.0, w=1.0, h=0.35), max_lines=2)
        fit, ink_bbox, mask_bbox = _ink_and_mask_bboxes(slot)
        assert ink_bbox == mask_bbox
        assert ink_bbox[0] >= 0 and ink_bbox[1] >= 0
        assert ink_bbox[2] <= CANVAS[0] and ink_bbox[3] <= CANVAS[1]


def test_rotate_changes_the_ink_footprint_and_reanchors_per_align():
    flat = _slot(content="Gull Point", max_lines=1)
    flat_bbox = draw_text(_blank(), flat, fit_text(flat, CANVAS),
                          "#ffffff", None, CANVAS).getbbox()
    tilted = _slot(content="Gull Point", max_lines=1, rotate=12.0)
    fit = fit_text(tilted, CANVAS)
    tilted_bbox = draw_text(_blank(), tilted, fit, "#ffffff", None,
                            CANVAS).getbbox()
    # A 12° tilt makes the bbox taller than the flat line's.
    assert (tilted_bbox[3] - tilted_bbox[1]) > (flat_bbox[3] - flat_bbox[1]) + 5

    zone_left = round(tilted.zone.x * CANVAS[0])
    zone_w = round(tilted.zone.w * CANVAS[0])
    left_slot = tilted.model_copy(update={"align": "left"})
    right_slot = tilted.model_copy(update={"align": "right"})
    left_bbox = text_mask(left_slot, fit_text(left_slot, CANVAS), CANVAS).getbbox()
    right_bbox = text_mask(right_slot, fit_text(right_slot, CANVAS), CANVAS).getbbox()
    assert abs(left_bbox[0] - zone_left) <= 1          # re-anchored to the rail
    assert abs(right_bbox[2] - (zone_left + zone_w)) <= 1
    assert left_bbox[0] < right_bbox[0]


def test_rotate_line_ink_boxes_land_inside_the_rotated_placement():
    slot = _slot(content="The Long Way Down to the Sea", rotate=10.0,
                max_lines=3)
    fit = fit_text(slot, CANVAS)
    mask_bbox = text_mask(slot, fit, CANVAS).getbbox()
    boxes = [b for b in line_ink_boxes(slot, fit, CANVAS) if b is not None]
    assert boxes
    for left, top, right, bottom in boxes:
        assert left >= mask_bbox[0] - 1 and right <= mask_bbox[2] + 1
        assert top >= mask_bbox[1] - 1 and bottom <= mask_bbox[3] + 1


# -- emphasis -----------------------------------------------------------------

def _widest_gap_column(img):
    """The x at the center of the widest fully-transparent column run
    inside the ink bbox — the inter-word gap, for a two-word line."""
    left, top, right, bottom = img.getbbox()
    alpha = img.getchannel("A")
    empty = [x for x in range(left, right)
             if alpha.crop((x, top, x + 1, bottom)).getbbox() is None]
    runs: list[list[int]] = []
    for x in empty:
        if runs and x == runs[-1][-1] + 1:
            runs[-1].append(x)
        else:
            runs.append([x])
    widest = max(runs, key=len)
    return widest[len(widest) // 2]


def _colored_pixels_either_side(img, split_x, rgb):
    """Count near-opaque pixels of exactly `rgb` either side of x."""
    left = right = 0
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a > 200 and (r, g, b) == rgb:
                if x < split_x:
                    left += 1
                else:
                    right += 1
    return left, right


def test_emphasis_accent_color_styles_only_the_named_word():
    slot = _slot(content="Quiet STORM", case="as_is", max_lines=1,
                emphasis=[1], emphasis_style="accent_color")
    fit = fit_text(slot, CANVAS)
    img = draw_text(_blank(), slot, fit, "#ffffff", None, CANVAS,
                    emphasis_color="#ff0000")
    split_x = _widest_gap_column(img)
    plain, accent = _colored_pixels_either_side(img, split_x,
                                                rgb=(255, 0, 0))
    assert accent > 50        # the second word is red
    assert plain == 0         # the first word carries no red at all


def test_emphasis_accent_color_falls_back_to_the_base_ink_without_a_palette():
    slot = _slot(content="Quiet STORM", case="as_is", max_lines=1,
                emphasis=[1], emphasis_style="accent_color")
    fit = fit_text(slot, CANVAS)
    img = draw_text(_blank(), slot, fit, "#ffffff", None, CANVAS)
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a > 0:
                assert (r, g, b) == (255, 255, 255)


def test_emphasis_larger_word_is_taller_and_baseline_aligned():
    # Both words descender-free, so ink bottoms are baselines.
    plain = _slot(content="storm rises", case="as_is", max_lines=1,
                size_min=0.04, size_max=0.04)
    emph = plain.model_copy(update={"emphasis": [1],
                                    "emphasis_style": "larger"})
    emph_fit = fit_text(emph, CANVAS)
    img = draw_text(_blank(), emph, emph_fit, "#ffffff", None, CANVAS)
    bbox = img.getbbox()
    mid = _widest_gap_column(img)
    left_half = img.crop((bbox[0], 0, mid, CANVAS[1])).getbbox()
    right_half = img.crop((mid, 0, bbox[2], CANVAS[1])).getbbox()
    left_h = left_half[3] - left_half[1]
    right_h = right_half[3] - right_half[1]
    assert right_h > left_h * 1.1       # the larger word reads larger
    # Baseline-aligned: the bigger word grows UP from the shared baseline,
    # so ink bottoms agree within antialiasing rounding.
    assert abs(left_half[3] - right_half[3]) <= 2


def test_emphasis_italic_and_swap_face_change_the_glyphs():
    base = _slot(content="storm warning", case="as_is", max_lines=1,
                size_min=0.04, size_max=0.04)
    italic = base.model_copy(update={"emphasis": [1],
                                     "emphasis_style": "italic"})
    fit = fit_text(base, CANVAS)
    base_img = draw_text(_blank(), base, fit, "#ffffff", None, CANVAS)
    italic_img = draw_text(_blank(), italic, fit_text(italic, CANVAS),
                           "#ffffff", None, CANVAS)
    assert base_img.tobytes() != italic_img.tobytes()

    other_family = next(name for name in sorted(FAMILIES)
                        if name != base.font_family)
    swap = base.model_copy(update={"emphasis": [1],
                                   "emphasis_style": "swap_face",
                                   "emphasis_font": other_family})
    swap_img = draw_text(_blank(), swap, fit_text(swap, CANVAS),
                         "#ffffff", None, CANVAS)
    assert swap_img.tobytes() != base_img.tobytes()
    assert swap_img.tobytes() != italic_img.tobytes()


def test_emphasis_width_measurement_accounts_for_the_styled_words():
    # A zone sized so the line only just fits unstyled: making one word
    # 1.25x MUST shrink the fitted size (the styled width is measured, not
    # discovered at draw time) and the drawn ink must stay inside the zone.
    plain = _slot(content="EXACT MEASUREMENTS MATTER", max_lines=1,
                size_min=0.01, size_max=0.2,
                zone=Zone(x=0.05, y=0.05, w=0.9, h=0.4))
    emph = plain.model_copy(update={"emphasis": [1],
                                    "emphasis_style": "larger"})
    plain_fit, emph_fit = fit_text(plain, CANVAS), fit_text(emph, CANVAS)
    assert emph_fit.size_px < plain_fit.size_px
    bbox = text_mask(emph, emph_fit, CANVAS).getbbox()
    zone_left = round(plain.zone.x * CANVAS[0])
    zone_right = zone_left + round(plain.zone.w * CANVAS[0])
    assert bbox[0] >= zone_left - 1 and bbox[2] <= zone_right + 1


def test_emphasis_out_of_range_index_is_ignored_at_render_time():
    # The model validator rejects a bad index when content is present, but
    # a fit/draw against a hand-built FitResult must never crash on one —
    # the plan simply styles no word. (Bypass validation via construct.)
    slot = _slot(content="Quiet Storm", case="as_is", max_lines=1)
    hacked = slot.model_copy(update={"emphasis": [7]})
    fit = fit_text(hacked, CANVAS)
    img = draw_text(_blank(), hacked, fit, "#ffffff", None, CANVAS,
                    emphasis_color="#ff0000")
    assert img.getbbox() is not None


# -- moves compose together (hand-authored spec territory) --------------------

def test_moves_compose_justify_stack_plus_emphasis_and_arc_plus_rotate():
    stacked = _slot(content="The Long Way Down", fit_mode="justify_stack",
                   size_min=0.02, size_max=0.3, max_lines=3,
                   emphasis=[1], emphasis_style="larger")
    fit, ink_bbox, mask_bbox = _ink_and_mask_bboxes(stacked)
    assert ink_bbox == mask_bbox

    arcrot = _slot(content="Gull Point", max_lines=1, arc=0.2, rotate=-10.0)
    fit2, ink2, mask2 = _ink_and_mask_bboxes(arcrot)
    assert ink2 == mask2
    assert ink2[0] >= 0 and ink2[2] <= CANVAS[0]


# -- §15.12 designed line breaks (TextSlot.line_breaks) -----------------------
#
# The regression these lock down is not "a break can be forced" but WHY it
# had to be forcible: neither fit path's scorer can produce the four-line
# poster stack (long / long / short connective / long), because the uniform
# fit ranks by lowest width variance — which is exactly what a lone "AND"
# maximizes — and justify_stack ranks by least wasted height, which prefers
# whatever line count happens to fill the zone. Both are reasonable rules
# and both are wrong here, so the author overrides them.

def _title_slot(**kw):
    from docproof.cover.model import TextSlot, Zone
    return TextSlot(id="title", content="Beneath Brine and Bone",
                    zone=Zone(x=0.055, y=0.150, w=0.89, h=0.505),
                    font_family="Playfair Display SC", case="upper",
                    tracking=8, max_lines=4, size_min=0.045, size_max=0.140,
                    **kw)


def test_justify_stack_searches_to_three_lines_without_designed_breaks():
    got = fit_text(_title_slot(fit_mode="justify_stack"), (1600, 2560))
    assert got.lines == ("BENEATH", "BRINE", "AND BONE")


def test_designed_breaks_override_the_justify_stack_scorer():
    got = fit_text(_title_slot(fit_mode="justify_stack",
                               line_breaks=[1, 2, 3]), (1600, 2560))
    assert got.lines == ("BENEATH", "BRINE", "AND", "BONE")


def test_designed_breaks_apply_to_the_uniform_fit_too():
    got = fit_text(_title_slot(fit_mode="uniform",
                               line_breaks=[1, 3]), (1600, 2560))
    assert got.lines == ("BENEATH", "BRINE AND", "BONE")


def test_designed_breaks_still_size_the_stack_to_the_zone():
    """Forcing the break must not disable the size solve — every line still
    fills the measure, which is what makes this a stack and not a wrap."""
    got = fit_text(_title_slot(fit_mode="justify_stack",
                               line_breaks=[1, 2, 3]), (1600, 2560))
    assert got.fits
    assert len(got.line_sizes_px) == 4
    assert all(s > 0 for s in got.line_sizes_px)
