"""Cover Studio's type-setter: turns one resolved TextSlot into pixels.

Three jobs, in order, matching docs/cover_designer_spec.md §7.3 points 2-6:

1. `fit_text` — apply case, then binary-search the largest font size in
   [size_min, size_max] whose best balanced line-breaking fits the zone.
   Never fails: a title that cannot fit even at size_min still gets a
   FitResult (rendered at the floor size with the least-bad breaking) plus a
   human-readable `.warning`, because a cover has to render for every brief.
2. `draw_text` — paint the chosen lines onto a Pillow layer: tracked runs are
   drawn glyph-by-glyph (the em/1000 tracking unit only makes sense applied
   between individual glyphs); untracked runs are drawn as one string so
   raqm, when present, can shape them. Stroke, shadow, and align/valign all
   live here.

What this module deliberately does NOT own: which color or shadow a slot
ends up drawn with. That is the legibility autopilot's call
(docproof.cover.compose), made by sampling the composite this text is about
to sit on — a decision that needs the current canvas, which this module
never sees. `draw_text` takes the autopilot's answer (`color`, `shadow`) as
plain arguments instead.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from .archetypes import zone_px
from .fonts import font_path
from .model import Shadow, TextSlot

log = logging.getLogger("docproof.cover.typeset")

# The manylinux/macOS Pillow wheels bundle libraqm (shaped text, real
# kerning); a from-source or minimal build may not have it. Covers still
# render without it — basic layout just kerns slightly worse on untracked
# runs — so this is a one-time warning, never a hard requirement (see
# docs/cover_designer_spec.md §7.3's opening paragraph).
try:
    import PIL.features
    RAQM_AVAILABLE = bool(PIL.features.check("raqm"))
except Exception:  # noqa: BLE001 - feature probing must never break import
    RAQM_AVAILABLE = False

if not RAQM_AVAILABLE:
    log.warning(
        "Pillow was built without raqm (libraqm) support; Cover Studio's "
        "text renders with Pillow's basic layout engine instead of shaped "
        "text — kerning on untracked runs will be slightly worse, but "
        "covers still render.")

# Multiplier from font size to the vertical stride between stacked lines —
# a touch looser than 1.0 so ascenders/descenders on adjacent lines never
# touch, applied uniformly to every display slot (spec §7.3 point 4).
LINE_HEIGHT = 1.08

# Binary-search steps for the fit search. 12 steps over a size range that is
# at most a few hundred px halves the gap ~4000x — far finer than a font's
# own hinting grid, so more steps would not change the rendered result.
FIT_ITERATIONS = 12

# Above this many words, the true balanced-break search (which brute-forces
# every word-boundary split for every line count) is skipped in favor of a
# plain greedy wrap. Titles are short; the combinatorial search is bounded by
# C(words-1, max_lines-1), which stays cheap for realistic word counts, but a
# hand-edited spec could pair a long title with a large max_lines and make
# that blow up. 12 keeps the worst case (max_lines up to 12) under a few
# thousand candidates while covering every realistic cover title.
MAX_BRUTE_WORDS = 12


def measure(text: str, font: ImageFont.FreeTypeFont, tracking_px: float) -> float:
    """The rendered width of one line, in px: the font's own glyph advances
    plus `tracking_px` inserted at every inter-glyph gap (never at the ends —
    an N-character line has N-1 gaps). The same formula the fit search uses
    to test candidate breaks and `draw_text` uses to position tracked glyphs,
    so measurement and rendering can never disagree about a line's width."""
    if not text:
        return 0.0
    return font.getlength(text) + tracking_px * (len(text) - 1)


# -- balanced line breaking --------------------------------------------------

@dataclass(frozen=True)
class _Break:
    lines: tuple[str, ...]
    fits: bool              # every line measures within the zone width
    variance: float         # px^2 variance of line widths — lower is more balanced


def _variance(widths: list[float]) -> float:
    if len(widths) <= 1:
        return 0.0
    mean = sum(widths) / len(widths)
    return sum((w - mean) ** 2 for w in widths) / len(widths)


def _overflow(widths: list[float], zone_w_px: float) -> float:
    return max(0.0, max(widths) - zone_w_px) if widths else 0.0


def _partitions(n_words: int, n_lines: int):
    """Every way to cut `n_words` words into exactly `n_lines` contiguous,
    non-empty groups at word boundaries only (never hyphenate), as 0-based
    split points. n_lines - 1 splits chosen from the n_words - 1 gaps between
    words — brute force, per docs/cover_designer_spec.md §7.3 point 4."""
    if n_lines == 1:
        yield ()
        return
    if n_lines > n_words:
        return
    yield from itertools.combinations(range(1, n_words), n_lines - 1)


def _lines_from_splits(words: list[str], splits: tuple[int, ...]) -> tuple[str, ...]:
    bounds = (0, *splits, len(words))
    return tuple(" ".join(words[bounds[i]:bounds[i + 1]])
                for i in range(len(bounds) - 1))


def _best_break_brute(words: list[str], measure_fn, zone_w_px: float,
                      max_lines: int) -> _Break:
    """Try 1, then 2, ... lines; the first line count with any width-fitting
    split wins (fewest lines that permits the largest font), broken by
    lowest width variance among that count's fitting splits. If nothing ever
    fits, fall back to whichever candidate — at any line count — overflows
    the zone width the least (the "least-bad" breaking §7.3 point 5 renders
    at the size_min floor)."""
    best_bad: _Break | None = None
    best_bad_overflow = float("inf")
    for n_lines in range(1, max_lines + 1):
        fitting: list[_Break] = []
        for splits in _partitions(len(words), n_lines):
            lines = _lines_from_splits(words, splits)
            widths = [measure_fn(line) for line in lines]
            fits = all(w <= zone_w_px for w in widths)
            variance = _variance(widths)
            cand = _Break(lines=lines, fits=fits, variance=variance)
            if fits:
                fitting.append(cand)
            else:
                overflow = _overflow(widths, zone_w_px)
                if (overflow < best_bad_overflow - 1e-9
                        or (abs(overflow - best_bad_overflow) <= 1e-9
                            and (best_bad is None or variance < best_bad.variance))):
                    best_bad, best_bad_overflow = cand, overflow
        if fitting:
            return min(fitting, key=lambda c: c.variance)
    return best_bad if best_bad is not None else _Break(
        lines=(" ".join(words),), fits=False, variance=0.0)


def _best_break_greedy(words: list[str], measure_fn, zone_w_px: float,
                       max_lines: int) -> _Break:
    """The >MAX_BRUTE_WORDS fallback: a plain greedy word-wrap (pack a line
    until the next word would overflow, then start a new one), with any
    overflow past `max_lines` folded onto the final line. Deterministic and
    linear in word count, unlike the brute-force search above."""
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = current + [word]
        if current and measure_fn(" ".join(trial)) > zone_w_px:
            lines.append(" ".join(current))
            current = [word]
        else:
            current = trial
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        head = lines[:max_lines - 1]
        tail = " ".join(lines[max_lines - 1:])
        lines = [*head, tail]
    widths = [measure_fn(line) for line in lines]
    fits = len(lines) <= max_lines and all(w <= zone_w_px for w in widths)
    return _Break(lines=tuple(lines), fits=fits, variance=_variance(widths))


def _best_break(words: list[str], measure_fn, zone_w_px: float,
                max_lines: int) -> _Break:
    if not words:
        return _Break(lines=(), fits=True, variance=0.0)
    if len(words) <= MAX_BRUTE_WORDS:
        return _best_break_brute(words, measure_fn, zone_w_px, max_lines)
    return _best_break_greedy(words, measure_fn, zone_w_px, max_lines)


# -- fit search ---------------------------------------------------------------

@dataclass(frozen=True)
class FitResult:
    """One text slot's resolved typography: the chosen size, its line
    breaks, and the tracking (already converted to px at that size) needed
    to redraw those exact lines. `fits=False` means even size_min could not
    make the least-bad breaking fit its zone — still renderable, just with a
    `warning` for RenderReport."""
    size_px: float
    size_frac: float          # size_px / canvas height — RenderReport.fitted_sizes
    lines: tuple[str, ...]
    tracking_px: float
    fits: bool
    warning: str | None


def _apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    return text


def fit_text(slot: TextSlot, canvas_size: tuple[int, int], *,
            iters: int = FIT_ITERATIONS) -> FitResult:
    """Binary-search the largest font size in [size_min, size_max] (fractions
    of canvas height) whose best balanced line-breaking fits `slot.zone`, at
    `slot.font_family`/`slot.tracking`. `slot.content` is cased first
    (§7.3 point 2) — everything downstream (measuring, breaking, and later
    `draw_text`) works on that cased text, never the raw brief string.

    A size "fits" when its best breaking's widths are all within the zone AND
    the resulting block (line count × size × LINE_HEIGHT) fits the zone's
    height — both checked, since fewer/wider lines and more/narrower lines
    trade one constraint for the other."""
    canvas_w, canvas_h = canvas_size
    text = _apply_case(slot.content, slot.case)
    words = text.split()
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)

    if not words:
        # Nothing to place — e.g. an optional slot the caller chose not to
        # skip, or a required slot left blank. Report the ceiling size so a
        # caller inspecting `fitted_sizes` sees "no constraint hit", not a
        # misleadingly tiny number; draw_text no-ops on empty `lines` anyway.
        return FitResult(size_px=slot.size_max * canvas_h,
                         size_frac=slot.size_max, lines=(),
                         tracking_px=0.0, fits=True, warning=None)

    size_min_px = slot.size_min * canvas_h
    size_max_px = slot.size_max * canvas_h
    font_file = font_path(slot.font_family)

    def resolve_at(size_px: float) -> tuple[_Break, float]:
        tracking_px = slot.tracking / 1000.0 * size_px
        font = ImageFont.truetype(font_file, max(1.0, size_px))
        br = _best_break(words, lambda s: measure(s, font, tracking_px),
                         zone_w_px, slot.max_lines)
        return br, tracking_px

    def fits_zone(br: _Break, size_px: float) -> bool:
        if not br.fits:
            return False
        block_h = len(br.lines) * size_px * LINE_HEIGHT
        return block_h <= zone_h_px + 1e-6

    lo, hi = size_min_px, size_max_px
    best: tuple[float, _Break, float] | None = None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        br, tracking_px = resolve_at(mid)
        if fits_zone(br, mid):
            best = (mid, br, tracking_px)
            lo = mid
        else:
            hi = mid

    if best is not None:
        size_px, br, tracking_px = best
        return FitResult(size_px=size_px, size_frac=size_px / canvas_h,
                         lines=br.lines, tracking_px=tracking_px,
                         fits=True, warning=None)

    br, tracking_px = resolve_at(size_min_px)
    warning = (f"{slot.id}: even size_min "
              f"({slot.size_min:.3f}×canvas height) does not fit its "
              f"zone within {slot.max_lines} line(s); rendered anyway at "
              f"the floor size with the least-bad line breaks.")
    return FitResult(size_px=size_min_px, size_frac=size_min_px / canvas_h,
                     lines=br.lines, tracking_px=tracking_px,
                     fits=False, warning=warning)


# -- rendering ------------------------------------------------------------

def _render_line(line: str, font: ImageFont.FreeTypeFont, tracking_px: float,
                 color: tuple[int, int, int], zone_left: float, zone_w: float,
                 row_center_y: float, align: str, stroke_w_px: int,
                 stroke_rgb: tuple[int, int, int] | None,
                 canvas_size: tuple[int, int]) -> Image.Image:
    """One line, drawn onto its own canvas-sized transparent layer at its
    final (x, y) — vertically anchored to its row's mid-line via Pillow's
    "m" anchor rather than hand-rolled ascent/descent math, so untracked and
    tracked lines land on the identical baseline grid."""
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    if not line:
        return layer
    draw = ImageDraw.Draw(layer)
    fill = (*color, 255)
    stroke_fill = (*stroke_rgb, 255) if stroke_rgb else None
    if tracking_px:
        # Tracking only means something applied glyph-by-glyph — draw each
        # character with `font.getlength` advances plus the tracking gap,
        # starting from whichever x makes the WHOLE tracked run land at the
        # requested alignment (measure() gives that total width).
        total_w = measure(line, font, tracking_px)
        if align == "left":
            x = zone_left
        elif align == "right":
            x = zone_left + zone_w - total_w
        else:
            x = zone_left + (zone_w - total_w) / 2.0
        for i, ch in enumerate(line):
            draw.text((x, row_center_y), ch, font=font, fill=fill, anchor="lm",
                      stroke_width=stroke_w_px, stroke_fill=stroke_fill)
            x += font.getlength(ch)
            if i < len(line) - 1:
                x += tracking_px
    else:
        # No tracking: one draw call for the whole line, so raqm (when
        # present) shapes it — real kerning, ligatures, the works.
        anchor_h = {"left": "l", "center": "m", "right": "r"}[align]
        if align == "left":
            x = zone_left
        elif align == "right":
            x = zone_left + zone_w
        else:
            x = zone_left + zone_w / 2.0
        draw.text((x, row_center_y), line, font=font, fill=fill,
                 anchor=f"{anchor_h}m", stroke_width=stroke_w_px,
                 stroke_fill=stroke_fill)
    return layer


def _block_top(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
              ) -> float:
    """Where the fitted block's own top edge lands, in canvas px — the one
    piece of geometry draw_text, text_mask, and line_boxes all need
    identically (same valign branch, same zone/line-height math), factored
    out once so the three can never quietly drift apart. `fit.lines` must
    be non-empty; callers already guard the empty case themselves (an
    empty block has no top to speak of)."""
    _, zone_top, _, zone_h_px = zone_px(slot.zone, canvas_size)
    line_h = fit.size_px * LINE_HEIGHT
    block_h = line_h * len(fit.lines)
    if slot.valign == "top":
        return float(zone_top)
    if slot.valign == "bottom":
        return zone_top + zone_h_px - block_h
    return zone_top + (zone_h_px - block_h) / 2.0


def line_boxes(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
              ) -> tuple[tuple[float, float], ...]:
    """Every fitted line's NOMINAL (top, bottom) span in canvas px — the
    line-height grid draw_text/text_mask actually lay glyphs out on, sharing
    their exact block-positioning math via _block_top so a caller reasoning
    about line position always agrees with where ink was actually drawn.
    Adjacent boxes are contiguous by construction (line i's bottom is line
    i+1's top) — this is the STRIDE grid, not real ink extent; a caller
    that wants the actual whitespace between two lines' own glyphs (as
    opposed to the nominal leading between their line-height rows) wants
    line_ink_boxes instead. Empty when `fit.lines` is empty (nothing fit —
    e.g. an empty optional slot)."""
    if not fit.lines:
        return ()
    line_h = fit.size_px * LINE_HEIGHT
    top = _block_top(slot, fit, canvas_size)
    return tuple((top + line_h * i, top + line_h * (i + 1))
                for i in range(len(fit.lines)))


def line_ink_boxes(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]
                   ) -> tuple[tuple[int, int, int, int] | None, ...]:
    """Every fitted line's OWN glyph ink bbox (left, top, right, bottom) in
    canvas px — rendered exactly like text_mask's own per-line loop, but
    kept separate rather than unioned into one mask, so a caller (the
    line-gap snap, v2.2 wave deliverable 3) can measure the REAL
    whitespace between two consecutive lines' actual ink — which varies
    line to line with whether either has ascenders/descenders — rather than
    the nominal, uniform line-height stride line_boxes describes. One entry
    per fitted line, in order; None for any line whose own render has no
    ink at all (a blank line, which _best_break shouldn't produce but
    draw_text/text_mask both defensively skip too)."""
    canvas_w, canvas_h = canvas_size
    if not fit.lines:
        return ()
    zone_left, _, zone_w_px, _ = zone_px(slot.zone, canvas_size)
    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    top = _block_top(slot, fit, canvas_size)

    stroke_w_px = 0
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))

    white = (255, 255, 255)
    boxes: list[tuple[int, int, int, int] | None] = []
    for i, line in enumerate(fit.lines):
        if not line:
            boxes.append(None)
            continue
        row_center_y = top + line_h * (i + 0.5)
        layer = _render_line(line, font, fit.tracking_px, white, zone_left,
                             zone_w_px, row_center_y, slot.align, stroke_w_px,
                             white if stroke_w_px else None, canvas_size)
        boxes.append(layer.getchannel("A").getbbox())
    return tuple(boxes)


def draw_text(base: Image.Image, slot: TextSlot, fit: FitResult, color: str,
             shadow: Shadow | None, canvas_size: tuple[int, int]) -> Image.Image:
    """Draw one resolved text slot onto `base`, returning a new composited
    image (`base` is not mutated). `color` and `shadow` are the legibility
    autopilot's final answer (docproof.cover.compose) — they may differ from
    `slot.color_role`'s literal palette color / `slot.shadow`, which is why
    they are passed in rather than read off `slot` directly.

    Each line gets its own transparent layer, including its own shadow pass
    (blur, offset, composited under the glyphs) — per §7.3 point 6, so a
    large blur radius on one line's shadow never bleeds into a neighboring
    line the way blurring one shared multi-line layer would."""
    if not fit.lines:
        return base
    canvas_w, canvas_h = canvas_size
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)
    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    block_top = _block_top(slot, fit, canvas_size)

    stroke_w_px = 0
    stroke_rgb = None
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))
        stroke_rgb = ImageColor.getrgb(slot.stroke.color)

    color_rgb = ImageColor.getrgb(color)
    out = base
    for i, line in enumerate(fit.lines):
        if not line:
            continue
        row_center_y = block_top + line_h * (i + 0.5)
        if shadow is not None:
            shadow_rgb = ImageColor.getrgb(shadow.color)
            shadow_layer = _render_line(line, font, fit.tracking_px, shadow_rgb,
                                        zone_left, zone_w_px, row_center_y,
                                        slot.align, 0, None, canvas_size)
            blur_px = max(0.0, shadow.blur * canvas_h)
            if blur_px > 0:
                shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_px))
            r, g, b, a = shadow_layer.split()
            a = a.point(lambda v: round(v * shadow.alpha))
            shadow_layer = Image.merge("RGBA", (r, g, b, a))
            dx_px = round(shadow.dx * canvas_h)
            dy_px = round(shadow.dy * canvas_h)
            out = out.copy()
            out.alpha_composite(shadow_layer, dest=(dx_px, dy_px))
        text_layer = _render_line(line, font, fit.tracking_px, color_rgb,
                                  zone_left, zone_w_px, row_center_y,
                                  slot.align, stroke_w_px, stroke_rgb, canvas_size)
        out = out.copy()
        out.alpha_composite(text_layer)
    return out


def text_mask(slot: TextSlot, fit: FitResult, canvas_size: tuple[int, int]) -> Image.Image:
    """The fitted text's glyph coverage alone, as a canvas-sized 'L' mask —
    white where ink would land, black everywhere else. Shares draw_text's
    exact block-positioning and per-line math (same valign branch, same
    `_render_line` calls) so a mask always lines up pixel-for-pixel with
    where a normal `fill` render would have put its ink.

    This is the effects rack's (§7.4a) seam into typeset: knockout/art_fill
    text modes are not "draw colored glyphs", they are "punch glyphs out of
    a panel" / "window glyphs onto the ground beneath" — both of which need
    the glyph SHAPE as a mask, never an ink color, which is why this returns
    an 'L' image rather than taking a color and drawing RGBA like draw_text
    does. docproof.cover.compose owns what to DO with the mask (that is
    still a legibility-autopilot decision, same as draw_text's color/shadow
    are), never this module."""
    canvas_w, canvas_h = canvas_size
    if not fit.lines:
        return Image.new("L", canvas_size, 0)
    zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas_size)
    font = ImageFont.truetype(font_path(slot.font_family), max(1.0, fit.size_px))
    line_h = fit.size_px * LINE_HEIGHT
    block_top = _block_top(slot, fit, canvas_size)

    stroke_w_px = 0
    if slot.stroke is not None and slot.stroke.width > 0:
        stroke_w_px = max(1, round(slot.stroke.width * canvas_h))

    out = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    white = (255, 255, 255)
    for i, line in enumerate(fit.lines):
        if not line:
            continue
        row_center_y = block_top + line_h * (i + 0.5)
        layer = _render_line(line, font, fit.tracking_px, white, zone_left,
                             zone_w_px, row_center_y, slot.align, stroke_w_px,
                             white if stroke_w_px else None, canvas_size)
        out.alpha_composite(layer)
    return out.getchannel("A")


__all__ = ["FIT_ITERATIONS", "LINE_HEIGHT", "MAX_BRUTE_WORDS",
          "RAQM_AVAILABLE", "FitResult", "draw_text", "fit_text",
          "line_boxes", "line_ink_boxes", "measure", "text_mask"]
