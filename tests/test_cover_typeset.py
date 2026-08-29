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


def test_size_min_floor_and_warning_when_nothing_fits():
    # A tiny zone (5% of a 400px-wide canvas = 20px) paired with a size that
    # cannot shrink (size_min == size_max) guarantees even one line of this
    # long, hard-to-break text overflows the zone width regardless of size —
    # the fit search's `best` stays None for all 12 iterations.
    slot = _slot(
        content="Supercalifragilisticexpialidocious Antidisestablishmentarianism",
        zone=Zone(x=0.0, y=0.0, w=0.05, h=0.03), max_lines=1,
        size_min=0.05, size_max=0.05)
    fit = fit_text(slot, CANVAS)
    assert fit.fits is False
    assert fit.warning is not None
    assert "title" in fit.warning
    assert fit.size_px == pytest.approx(slot.size_min * CANVAS[1])
    assert fit.lines   # still rendered — the least-bad breaking, not nothing


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
