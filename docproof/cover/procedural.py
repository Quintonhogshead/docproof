"""Deterministic procedural artwork for cover composition.

Every synthesizer is a pure function of the canvas, palette, art slot, and
spec version. The registry is defined here so callers share one object.
"""
from __future__ import annotations

import colorsys
import math
import random

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter

from .model import ArtSlot, CoverSpec, Palette, TextSlot, Zone

_GRAIN_SEED = 20260828
_GRAIN_SCALE = 0.25
_GRADIENT_LIGHTNESS_SHIFT = 0.10


def _procedural_art(slot: ArtSlot, canvas: tuple[int, int], palette: Palette,
                    version: int) -> Image.Image | None:
    """slot.procedural, if set, names a PROCEDURAL_SYNTHESIZERS entry
    directly — any slot, any id, generatable or not (a generatable slot
    whose asset never arrived degrades to its named synthesizer instead of a
    blank layer). An UNSET (empty) `procedural` falls back to the ORIGINAL
    hardcoded-by-id behavior — "texture" -> grain, "background" -> gradient,
    anything else -> nothing — so every archetype/spec written before this
    field existed renders byte-identical pixels (v2 BODY wave)."""
    name = slot.procedural
    if not name:
        if slot.id == "texture":
            name = "grain"
        elif slot.id == "background":
            name = "gradient"
        else:
            return None
    synth = PROCEDURAL_SYNTHESIZERS.get(name)
    if synth is None:
        return None
    return synth(canvas, palette, slot, version)


def _gradient_layer(canvas: tuple[int, int], base_hex: str) -> Image.Image:
    """The big_type $0 fallback background (§5.2): a two-stop vertical
    gradient from the palette's background color to a lightness-shifted
    version of itself, for subtle depth with no generated art at all. Built
    as a 1px-wide column and stretched, so it costs O(canvas height) Python-
    level work regardless of canvas width."""
    cw, ch = canvas
    r, g, b = ImageColor.getrgb(base_hex)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l2 = max(0.0, l - _GRADIENT_LIGHTNESS_SHIFT)
    r2, g2, b2 = (round(c * 255) for c in colorsys.hls_to_rgb(h, l2, s))
    col = Image.new("RGB", (1, max(1, ch)))
    px = col.load()
    for y in range(ch):
        t = y / (ch - 1) if ch > 1 else 0.0
        px[0, y] = (round(r + (r2 - r) * t), round(g + (g2 - g) * t),
                   round(b + (b2 - b) * t))
    return col.resize((cw, ch), Image.Resampling.NEAREST).convert("RGBA")


def _grain_layer(canvas: tuple[int, int]) -> Image.Image:
    """Fixed-seed monochrome film grain: generate at `_GRAIN_SCALE` and
    upsample, per §7.3. `random.Random(_GRAIN_SEED).randbytes(...)` is the
    ONE source of randomness anywhere in this module, and it is the same
    bytes every call — the compose()-is-deterministic guarantee rests on
    this being the only exception and always reproducing identically."""
    cw, ch = canvas
    sw, sh = max(1, round(cw * _GRAIN_SCALE)), max(1, round(ch * _GRAIN_SCALE))
    raw = random.Random(_GRAIN_SEED).randbytes(sw * sh)
    small = Image.frombytes("L", (sw, sh), raw)
    grain = small.resize(canvas, Image.Resampling.BILINEAR)
    return Image.merge("RGBA", (grain, grain, grain, Image.new("L", canvas, 255)))


# Synthesizers use palette colors and either fixed geometry or a stable seed
# derived from version, slot id, and kind. Avoid Python's randomized hash so
# archived specs render the same pixels across processes.

def _synth_seed(version: int, slot_id: str, name: str) -> int:
    """Mirrors _scatter_seed's reasoning exactly (fixed integer arithmetic
    over `version` plus every character's ordinal, never hash()) with the
    synthesizer's own `name` folded in too, so two different slots asking
    for the SAME synthesizer — or one slot's `procedural` changing across a
    revision — never stamp identical noise."""
    return (version * 1_000_003 + sum(ord(c) for c in slot_id) * 97
           + sum(ord(c) for c in name) * 7)


def _synth_gradient(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                    version: int) -> Image.Image:
    return _gradient_layer(canvas, palette.background)


def _synth_grain(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                 version: int) -> Image.Image:
    return _grain_layer(canvas)


_PAPER_LINE_SPACING_FRACTION = 0.0065   # of canvas height, between laid lines
_PAPER_FIBER_BLUR_FRACTION = 0.003      # of canvas height
_PAPER_ALPHA = 90                       # of 255 - subtle fiber+laid-line tint


def _synth_paper(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                 version: int) -> Image.Image:
    """Laid paper: soft mottled fiber noise plus regularly spaced horizontal
    "laid lines" — the faint ribbing real laid paper shows held to light —
    the two textures together read as "paper," not "screen." Fiber noise is
    a downsampled-then-blurred random field (like _grain_layer's, but
    blurred further so it clumps into fibers instead of reading as sharp
    per-pixel noise); the laid lines are exact, fixed-period horizontal
    rules, so the whole field tiles cleanly at that period."""
    cw, ch = canvas
    seed = _synth_seed(version, slot.id, "paper")
    sw = max(1, round(cw * _GRAIN_SCALE))
    sh = max(1, round(ch * _GRAIN_SCALE))
    raw = random.Random(seed).randbytes(sw * sh)
    fiber = (Image.frombytes("L", (sw, sh), raw)
            .resize(canvas, Image.Resampling.BILINEAR)
            .filter(ImageFilter.GaussianBlur(max(1.0, ch * _PAPER_FIBER_BLUR_FRACTION))))
    spacing = max(2, round(ch * _PAPER_LINE_SPACING_FRACTION))
    lines = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(lines)
    for y in range(0, ch, spacing):
        draw.line([(0, y), (cw, y)], fill=60)
    pattern = ImageChops.add(fiber.point(lambda v: v // 4), lines)
    rgb = ImageColor.getrgb(palette.text)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(pattern.point(lambda v: round(min(255, v) * _PAPER_ALPHA / 255)))
    return out


_HALFTONE_PERIOD_FRACTION = 0.018     # of canvas height, dot-grid pitch
_HALFTONE_MAX_ALPHA = 130
_HALFTONE_MIN_RADIUS_FACTOR = 0.25    # of the biggest dot, toward the edges
_HALFTONE_MAX_RADIUS_FACTOR = 0.42    # of the pitch, at the slot's center


def _synth_halftone(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                    version: int) -> Image.Image:
    """A classic print dot screen: a square grid of solid circles, radially
    graded so full-size dots cluster toward the slot's own center and taper
    toward the edges — a deterministic vignette built from pure geometry, no
    randomness needed (a halftone's whole visual identity IS its regular
    grid; irregular placement would just read as noise with extra steps).
    Tileable by construction: dot centers fall on an exact period-`pitch`
    grid covering the whole canvas."""
    cw, ch = canvas
    pitch = max(4, round(ch * _HALFTONE_PERIOD_FRACTION))
    max_r = pitch * _HALFTONE_MAX_RADIUS_FACTOR
    cx, cy = cw / 2, ch / 2
    max_dist = math.hypot(cx, cy) or 1.0
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for gy in range(0, ch + pitch, pitch):
        for gx in range(0, cw + pitch, pitch):
            d = 1.0 - min(1.0, math.hypot(gx - cx, gy - cy) / max_dist)
            r = max_r * (_HALFTONE_MIN_RADIUS_FACTOR
                        + (1 - _HALFTONE_MIN_RADIUS_FACTOR) * d)
            if r < 0.5:
                continue
            draw.ellipse((gx - r, gy - r, gx + r, gy + r), fill=255)
    rgb = ImageColor.getrgb(palette.primary)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _HALFTONE_MAX_ALPHA / 255)))
    return out


_WEAVE_PERIOD_FRACTION = 0.010   # of canvas height, thread pitch
_WEAVE_ALPHA = 34
_WEAVE_LINE_VALUE = 110


def _synth_canvas(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                  version: int) -> Image.Image:
    """A plain-weave canvas/linen texture: two perpendicular sets of evenly
    spaced fine rules — a cheap, fully deterministic crosshatch read at low
    alpha. Pure geometry (a fixed-pitch grid of `ImageDraw.line` calls, each
    O(canvas dimension) at the C level), so it costs nothing like a real
    per-pixel weave simulation would."""
    cw, ch = canvas
    pitch = max(3, round(ch * _WEAVE_PERIOD_FRACTION))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for y in range(0, ch, pitch):
        draw.line([(0, y), (cw, y)], fill=_WEAVE_LINE_VALUE)
    for x in range(0, cw, pitch):
        draw.line([(x, 0), (x, ch)], fill=_WEAVE_LINE_VALUE)
    rgb = ImageColor.getrgb(palette.text)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(min(255, v) * _WEAVE_ALPHA / 255)))
    return out


_SPECKLE_DENSITY_PER_MEGAPIXEL = 900    # dot count scales with canvas area
_SPECKLE_MIN_RADIUS_FRACTION = 0.0015   # of canvas height
_SPECKLE_MAX_RADIUS_FRACTION = 0.0045
_SPECKLE_ALPHA = 210


def _synth_speckle(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                   version: int) -> Image.Image:
    """Sparse scattered dots at varied sizes — the "Atomic Habits" cover
    signature: a field of small solid circles at random positions and radii,
    seeded purely from (version, slot_id) so the same slot always speckles
    identically."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot.id, "speckle"))
    count = max(1, round((cw * ch) / 1_000_000 * _SPECKLE_DENSITY_PER_MEGAPIXEL))
    min_r = _SPECKLE_MIN_RADIUS_FRACTION * ch
    max_r = _SPECKLE_MAX_RADIUS_FRACTION * ch
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for _ in range(count):
        x, y = rng.uniform(0, cw), rng.uniform(0, ch)
        r = rng.uniform(min_r, max_r)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=255)
    rgb = ImageColor.getrgb(palette.primary)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _SPECKLE_ALPHA / 255)))
    return out


# The frame-containment clamp's breathing room between the inner rule and
# any text ink, as a fraction of canvas height (matching every other
# rule-frame constant's units).
_FRAME_TEXT_PAD_FRACTION = 0.015
# A clamp that would crush a zone below this share of its declared width is
# refused: microscopic type is worse than a crossed rule, and the warning
# hands the collision to the judge instead.
_FRAME_CLAMP_MIN_WIDTH = 0.40

_RULE_FRAME_INSET_FRACTION = 0.05      # of canvas height, from each edge
_RULE_FRAME_GAP_FRACTION = 0.007       # of canvas height, between the two rules
_RULE_FRAME_WIDTH_FRACTION = 0.0022    # of canvas height, each rule's stroke
_RULE_FRAME_ALPHA = 235


def _synth_rule_frame(canvas: tuple[int, int], palette: Palette, slot: ArtSlot,
                      version: int) -> Image.Image:
    """A thin, engraved double-rule frame inset from the edge — reference
    DNA item 8's "thin rule frames inset from the edge... read as craft,"
    and the same "engraved double-rule" motif the Spell & Check site's own
    chrome already uses (app/static/sc-shared.css). Pure geometry (two
    concentric inset rectangles), no randomness needed; palette.accent, so
    it reads as a considered metallic/ink accent rather than blending into
    the ground it sits on."""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    gap = max(1, round(_RULE_FRAME_GAP_FRACTION * ch))
    width = max(1, round(_RULE_FRAME_WIDTH_FRACTION * ch))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    outer = (inset, inset, cw - 1 - inset, ch - 1 - inset)
    inner = (inset + gap, inset + gap, cw - 1 - inset - gap, ch - 1 - inset - gap)
    if outer[2] > outer[0] and outer[3] > outer[1]:
        draw.rectangle(outer, outline=255, width=width)
    if inner[2] > inner[0] and inner[3] > inner[1]:
        draw.rectangle(inner, outline=255, width=width)
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


# All frame kinds share the rule-frame inset. The common inner rectangle is
# therefore conservative for sparse frames and prevents text crossing any
# frame's ink.

_FRAME_HAIRLINE_WIDTH_FRACTION = 0.0012   # of canvas height — a true
                                          # hairline, thinner than
                                          # rule_frame's own 0.0022 rule
_FRAME_THICKTHIN_OUTER_WIDTH_FRACTION = 0.0060   # heavy outer rule
_FRAME_CORNERS_ARM_FRACTION = 0.09        # of the inset rect's shorter
                                          # side, each bracket's own arm
                                          # length
_FRAME_CORNERS_WIDTH_FRACTION = 0.0030
_FRAME_DECO_SQUARE_FRACTION = 0.014       # of canvas height, each corner
                                          # square's own side length
_FRAME_OCTAGON_CUT_FRACTION = 0.09        # of the inset rect's shorter
                                          # side, how far each 45 degree
                                          # corner cut travels along both
                                          # edges it meets


def _synth_frame_hairline(canvas: tuple[int, int], palette: Palette,
                          slot: ArtSlot, version: int) -> Image.Image:
    """A single fine rule at rule_frame's own outer inset — the plainest
    member of the family, for a cover that wants "framed" without
    "engraved.\""""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    width = max(1, round(_FRAME_HAIRLINE_WIDTH_FRACTION * ch))
    mask = Image.new("L", canvas, 0)
    rect = (inset, inset, cw - 1 - inset, ch - 1 - inset)
    if rect[2] > rect[0] and rect[3] > rect[1]:
        ImageDraw.Draw(mask).rectangle(rect, outline=255, width=width)
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


def _synth_frame_thickthin(canvas: tuple[int, int], palette: Palette,
                           slot: ArtSlot, version: int) -> Image.Image:
    """Classic engraving: a heavy outer rule paired with a fine inner one,
    same inset/gap as rule_frame — asymmetric weight instead of
    rule_frame's own matched double rule."""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    gap = max(1, round(_RULE_FRAME_GAP_FRACTION * ch))
    outer_w = max(1, round(_FRAME_THICKTHIN_OUTER_WIDTH_FRACTION * ch))
    inner_w = max(1, round(_RULE_FRAME_WIDTH_FRACTION * ch))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    outer = (inset, inset, cw - 1 - inset, ch - 1 - inset)
    inner = (inset + gap, inset + gap, cw - 1 - inset - gap, ch - 1 - inset - gap)
    if outer[2] > outer[0] and outer[3] > outer[1]:
        draw.rectangle(outer, outline=255, width=outer_w)
    if inner[2] > inner[0] and inner[3] > inner[1]:
        draw.rectangle(inner, outline=255, width=inner_w)
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


def _synth_frame_corners(canvas: tuple[int, int], palette: Palette,
                         slot: ArtSlot, version: int) -> Image.Image:
    """Corner brackets only, open sides — an "L" at each of the inset
    rect's four corners, arms running along both edges that meet there.
    No rule ever crosses the middle of any side, which is the whole point
    of this kind: a lighter, more open frame than a fully closed rule."""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    width = max(1, round(_FRAME_CORNERS_WIDTH_FRACTION * ch))
    left, top, right, bottom = inset, inset, cw - 1 - inset, ch - 1 - inset
    arm = max(1, round(_FRAME_CORNERS_ARM_FRACTION * min(right - left, bottom - top)))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    if right > left and bottom > top:
        corners = (
            ((left, top + arm), (left, top), (left + arm, top)),          # top-left
            ((right - arm, top), (right, top), (right, top + arm)),       # top-right
            ((left, bottom - arm), (left, bottom), (left + arm, bottom)), # bottom-left
            ((right - arm, bottom), (right, bottom), (right, bottom - arm)),  # bottom-right
        )
        for points in corners:
            draw.line(points, fill=255, width=width, joint="curve")
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


def _synth_frame_deco(canvas: tuple[int, int], palette: Palette,
                      slot: ArtSlot, version: int) -> Image.Image:
    """"Stepped double rule with corner squares" — the Fatal Crossing
    register (docs/cover_template_research.md): rule_frame's own double
    rule, plus a small filled square centered on each of the OUTER rule's
    four corners — the "stepped" deco accent the plain double rule alone
    doesn't have."""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    gap = max(1, round(_RULE_FRAME_GAP_FRACTION * ch))
    width = max(1, round(_RULE_FRAME_WIDTH_FRACTION * ch))
    sq = max(2, round(_FRAME_DECO_SQUARE_FRACTION * ch))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    outer = (inset, inset, cw - 1 - inset, ch - 1 - inset)
    inner = (inset + gap, inset + gap, cw - 1 - inset - gap, ch - 1 - inset - gap)
    if outer[2] > outer[0] and outer[3] > outer[1]:
        draw.rectangle(outer, outline=255, width=width)
    if inner[2] > inner[0] and inner[3] > inner[1]:
        draw.rectangle(inner, outline=255, width=width)
    if outer[2] > outer[0] and outer[3] > outer[1]:
        half = sq // 2
        for cx, cy in ((outer[0], outer[1]), (outer[2], outer[1]),
                      (outer[0], outer[3]), (outer[2], outer[3])):
            draw.rectangle((cx - half, cy - half, cx + half, cy + half), fill=255)
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


def _synth_frame_octagon(canvas: tuple[int, int], palette: Palette,
                         slot: ArtSlot, version: int) -> Image.Image:
    """Corners cut at 45 degrees — the Theo of Golden register (docs/
    cover_template_research.md): the SAME outer/inner double-rule
    rectangles rule_frame draws, each redrawn as an octagon (a rectangle
    with its four corners cut) via ImageDraw.polygon's own `width` stroke
    (Pillow >= 9.2) rather than a plain rectangle outline."""
    cw, ch = canvas
    inset = round(_RULE_FRAME_INSET_FRACTION * ch)
    gap = max(1, round(_RULE_FRAME_GAP_FRACTION * ch))
    width = max(1, round(_RULE_FRAME_WIDTH_FRACTION * ch))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for rect in ((inset, inset, cw - 1 - inset, ch - 1 - inset),
                (inset + gap, inset + gap, cw - 1 - inset - gap, ch - 1 - inset - gap)):
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            continue
        cut = max(1, round(_FRAME_OCTAGON_CUT_FRACTION * min(right - left, bottom - top)))
        octagon = [
            (left + cut, top), (right - cut, top),
            (right, top + cut), (right, bottom - cut),
            (right - cut, bottom), (left + cut, bottom),
            (left, bottom - cut), (left, top + cut),
        ]
        draw.polygon(octagon, outline=255, width=width)
    rgb = ImageColor.getrgb(palette.accent)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _RULE_FRAME_ALPHA / 255)))
    return out


# Must stay in lockstep with model.PROCEDURAL_KINDS.
def _frame_inner_rect(canvas: tuple[int, int]) -> tuple[float, float, float, float]:
    """The rule frame's inner content boundary as zone fractions (x, y, w,
    h), derived from the SAME constants _synth_rule_frame draws with — inset
    + both rule strokes + the gap between them + breathing room — so the
    clamp and the drawn frame can never disagree. All the frame constants
    are fractions of canvas HEIGHT (matching the synthesizer's px math), so
    the x fraction re-scales by the aspect ratio."""
    cw, ch = canvas
    boundary_px = ch * (_RULE_FRAME_INSET_FRACTION
                        + 2 * _RULE_FRAME_WIDTH_FRACTION
                        + _RULE_FRAME_GAP_FRACTION
                        + _FRAME_TEXT_PAD_FRACTION)
    fx = boundary_px / cw
    fy = boundary_px / ch
    return fx, fy, 1.0 - 2 * fx, 1.0 - 2 * fy


# Every member uses the inset geometry returned by _frame_inner_rect.
FRAME_PROCEDURAL_KINDS: frozenset[str] = frozenset({
    "rule_frame", "frame_hairline", "frame_thickthin", "frame_corners",
    "frame_deco", "frame_octagon"})


def _frame_clamp_text(text_by_id: dict[str, TextSlot], spec: CoverSpec,
                      canvas: tuple[int, int],
                      warnings: list[str]) -> dict[str, TextSlot]:
    """Intersect every text zone with the frame's inner rect (when the spec
    declares ANY frame-family procedural slot — see FRAME_PROCEDURAL_KINDS)
    so type can never cross a frame the cover promised. A clamp that would
    crush a zone below _FRAME_CLAMP_MIN_WIDTH of its declared width refuses
    instead — the warning hands that collision to the judge, because
    microscopic type is the worse failure."""
    if not any(a.procedural in FRAME_PROCEDURAL_KINDS for a in spec.art):
        return text_by_id
    fx, fy, fw, fh = _frame_inner_rect(canvas)
    out: dict[str, TextSlot] = {}
    for slot_id, slot in text_by_id.items():
        z = slot.zone
        nx, ny = max(z.x, fx), max(z.y, fy)
        nx2, ny2 = min(z.x + z.w, fx + fw), min(z.y + z.h, fy + fh)
        nw, nh = nx2 - nx, ny2 - ny
        if nw <= 0 or nh <= 0 or (nx, ny, nw, nh) == (z.x, z.y, z.w, z.h):
            out[slot_id] = slot
            continue
        if nw < z.w * _FRAME_CLAMP_MIN_WIDTH:
            warnings.append(
                f"{slot_id}: its zone barely fits inside the rule frame — "
                f"clamping would crush it below "
                f"{_FRAME_CLAMP_MIN_WIDTH:.0%} of its width, so it was "
                f"left as declared and may cross the frame.")
            out[slot_id] = slot
            continue
        out[slot_id] = slot.model_copy(
            update={"zone": Zone(x=nx, y=ny, w=nw, h=nh)})
    return out


# Atmosphere generators read existing anchor and scale fields. Smooth fields
# render small and upscale; crisp particles render at full resolution. Their
# colors come from the palette so they track palette revisions.

_ATMO_WHITE_BLEND = 0.60         # accent -> white, the "warmed toward white" mix
_ATMO_WARM_SHIFT = 12            # +R/-B nudge on top of it (8-bit steps)
_ATMO_STAR_WHITE_BLEND = 0.85    # stars sit nearly at white — hot points, not paint
_FOG_WHITE_BLEND = 0.40          # background -> white, the "lightened" mix

_RADIAL_GLOW_MAX_ALPHA = 200
_RADIAL_GLOW_RADIUS_FRACTION = 0.55   # of canvas height, at scale=1.0

_LIGHT_LEAK_MAX_ALPHA = 170
_LIGHT_LEAK_ANGLE_DEG = 32.0     # the band's fixed tilt (y-down coordinates)
_LIGHT_LEAK_WIDTH_FRACTION = 0.16     # of canvas height, at scale=1.0

_FOG_MAX_ALPHA = 150
_FOG_BAND_HEIGHT_FRACTION = 0.22      # gaussian half-height, of canvas height

_RAYS_COUNT = 7
_RAYS_MAX_ALPHA = 150
_RAYS_SHARPNESS = 3                   # cos^n lobe exponent
_RAYS_EXTENT_FRACTION = 0.95          # of canvas height, at scale=1.0

_BOKEH_BASE_COUNT = 14
_BOKEH_MIN_RADIUS_FRACTION = 0.020    # of canvas height
_BOKEH_MAX_RADIUS_FRACTION = 0.060
_BOKEH_MIN_ALPHA, _BOKEH_MAX_ALPHA = 30, 70
_BOKEH_SOFTEN_FRACTION = 0.006        # of canvas height, the soft-focus blur

_DUST_DENSITY_PER_MEGAPIXEL = 220
_DUST_MAX_RADIUS_FRACTION = 0.0022    # of canvas height
_DUST_ALPHA = 55

_SCRATCH_BASE_COUNT = 26
_SCRATCH_MIN_LEN_FRACTION = 0.05      # of canvas height
_SCRATCH_MAX_LEN_FRACTION = 0.30
_SCRATCH_MAX_DRIFT = 0.06             # horizontal drift per unit length
_SCRATCH_ALPHA = 60

_STAR_DENSITY_PER_MEGAPIXEL = 380
_STAR_BRIGHT_FRACTION = 0.08          # share drawn as a 3px disc
_STAR_FLARE_FRACTION = 0.03           # share that also gets a cross flare
_STAR_FLARE_ARM_FRACTION = 0.004      # of canvas height, each flare arm


def _warmed_toward_white(hex_color: str, blend: float) -> tuple[int, int, int]:
    """`hex_color` mixed `blend` of the way to white, then nudged warm
    (+R/−B by _ATMO_WARM_SHIFT) — §15.5's "accent warmed toward white" as
    one documented formula, shared by every light-colored synth so the
    bank's lights always agree about what warm means."""
    r, g, b = ImageColor.getrgb(hex_color)
    r = round(r + (255 - r) * blend)
    g = round(g + (255 - g) * blend)
    b = round(b + (255 - b) * blend)
    return (min(255, r + _ATMO_WARM_SHIFT), g, max(0, b - _ATMO_WARM_SHIFT))


def _anchor_px(slot: ArtSlot, size: tuple[int, int]) -> tuple[float, float]:
    """slot.anchor as pixels at `size` — the one place the bank reads it,
    so every synth agrees an anchor is canvas fractions (the [-2, 2]
    latitude deliberately included: a glow centered beyond the trim is a
    legitimate design, exactly GradientMask.center's reasoning)."""
    return slot.anchor[0] * size[0], slot.anchor[1] * size[1]


def _quarter_field_to_layer(small: Image.Image, canvas: tuple[int, int],
                            rgb: tuple[int, int, int]) -> Image.Image:
    """A quarter-scale 'L' alpha field, Lanczos-upsampled and inked as a
    solid-color RGBA layer — the shared tail of every smooth synth here."""
    alpha = small.resize(canvas, Image.Resampling.LANCZOS)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(alpha)
    return out


def _synth_radial_glow(canvas: tuple[int, int], palette: Palette,
                       slot: ArtSlot, version: int) -> Image.Image:
    """A soft light source: alpha peaks at the anchor and falls off
    quadratically to nothing at `scale` × _RADIAL_GLOW_RADIUS_FRACTION of
    the canvas height — pure geometry, no seed needed. Screened (the usual
    blend) this is lamplight behind the title; through color_dodge it is
    midnight_neon's burn."""
    cw, ch = canvas
    sw, sh = max(1, round(cw * _GRAIN_SCALE)), max(1, round(ch * _GRAIN_SCALE))
    cx, cy = _anchor_px(slot, (sw, sh))
    radius = max(1.0, _RADIAL_GLOW_RADIUS_FRACTION * slot.scale * sh)
    small = Image.new("L", (sw, sh), 0)
    px = small.load()
    for y in range(sh):
        for x in range(sw):
            t = 1.0 - min(1.0, math.hypot(x - cx, y - cy) / radius)
            px[x, y] = round(_RADIAL_GLOW_MAX_ALPHA * t * t)
    return _quarter_field_to_layer(small, canvas,
                                   _warmed_toward_white(palette.accent,
                                                        _ATMO_WHITE_BLEND))


def _synth_light_leak(canvas: tuple[int, int], palette: Palette,
                      slot: ArtSlot, version: int) -> Image.Image:
    """A film leak: two parallel soft bands (one bright, one fainter
    trailing it) crossing the canvas at a fixed tilt THROUGH the anchor —
    where the leak enters is the anchor's whole meaning here. Band width
    scales with `scale`; the trailing band's offset is seeded, the
    organic-accident wobble a leak needs to not read as a ruled stripe."""
    cw, ch = canvas
    sw, sh = max(1, round(cw * _GRAIN_SCALE)), max(1, round(ch * _GRAIN_SCALE))
    cx, cy = _anchor_px(slot, (sw, sh))
    rng = random.Random(_synth_seed(version, slot.id, "light_leak"))
    width = max(1.0, _LIGHT_LEAK_WIDTH_FRACTION * slot.scale * sh)
    trail_offset = width * rng.uniform(1.4, 2.2)
    trail_strength = rng.uniform(0.35, 0.55)
    # Signed distance from the line through (cx, cy) at the fixed tilt:
    # d = (x-cx)·n_x + (y-cy)·n_y with n the unit normal.
    nx = -math.sin(math.radians(_LIGHT_LEAK_ANGLE_DEG))
    ny = math.cos(math.radians(_LIGHT_LEAK_ANGLE_DEG))
    small = Image.new("L", (sw, sh), 0)
    px = small.load()
    for y in range(sh):
        for x in range(sw):
            d = (x - cx) * nx + (y - cy) * ny
            v = math.exp(-(d / width) ** 2)
            v += trail_strength * math.exp(-((d - trail_offset) / width) ** 2)
            px[x, y] = round(_LIGHT_LEAK_MAX_ALPHA * min(1.0, v))
    return _quarter_field_to_layer(small, canvas,
                                   _warmed_toward_white(palette.accent,
                                                        _ATMO_WHITE_BLEND))


def _synth_fog_gradient(canvas: tuple[int, int], palette: Palette,
                        slot: ArtSlot, version: int) -> Image.Image:
    """A horizontal fog bank: a gaussian band centered on the anchor's y
    (its x is ignored — fog has no left or right), half-height `scale` ×
    _FOG_BAND_HEIGHT_FRACTION, inked as the background lightened toward
    white. Alpha depends on y alone, so it builds as a one-pixel column and
    stretches — _gradient_layer's own O(height) trick — with no seed: fog
    is fog."""
    cw, ch = canvas
    _, band_y = _anchor_px(slot, canvas)
    half = max(1.0, _FOG_BAND_HEIGHT_FRACTION * slot.scale * ch)
    col = Image.new("L", (1, max(1, ch)), 0)
    px = col.load()
    for y in range(ch):
        px[0, y] = round(_FOG_MAX_ALPHA * math.exp(-((y - band_y) / half) ** 2))
    alpha = col.resize(canvas, Image.Resampling.NEAREST)
    # Fog lightens the background without the warm light-source shift.
    r, g, b = ImageColor.getrgb(palette.background)
    rgb = (round(r + (255 - r) * _FOG_WHITE_BLEND),
           round(g + (255 - g) * _FOG_WHITE_BLEND),
           round(b + (255 - b) * _FOG_WHITE_BLEND))
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(alpha)
    return out


def _synth_rays(canvas: tuple[int, int], palette: Palette,
                slot: ArtSlot, version: int) -> Image.Image:
    """Light shafts: _RAYS_COUNT cos^n lobes fanning from the anchor,
    fading with distance to `scale` × _RAYS_EXTENT_FRACTION of the canvas
    height. The fan's rotation is seeded — which way the shafts lean is
    the one degree of freedom that keeps two ray slots from ever
    registering as the same stamp."""
    cw, ch = canvas
    sw, sh = max(1, round(cw * _GRAIN_SCALE)), max(1, round(ch * _GRAIN_SCALE))
    cx, cy = _anchor_px(slot, (sw, sh))
    rng = random.Random(_synth_seed(version, slot.id, "rays"))
    phase = rng.uniform(0.0, math.tau)
    extent = max(1.0, _RAYS_EXTENT_FRACTION * slot.scale * sh)
    small = Image.new("L", (sw, sh), 0)
    px = small.load()
    for y in range(sh):
        for x in range(sw):
            dist = math.hypot(x - cx, y - cy)
            fall = max(0.0, 1.0 - dist / extent)
            if fall <= 0.0:
                continue
            theta = math.atan2(y - cy, x - cx)
            lobe = 0.5 + 0.5 * math.cos(_RAYS_COUNT * theta + phase)
            px[x, y] = round(_RAYS_MAX_ALPHA * (lobe ** _RAYS_SHARPNESS) * fall)
    return _quarter_field_to_layer(small, canvas,
                                   _warmed_toward_white(palette.accent,
                                                        _ATMO_WHITE_BLEND))


def _synth_bokeh(canvas: tuple[int, int], palette: Palette,
                 slot: ArtSlot, version: int) -> Image.Image:
    """Out-of-focus lights: seeded discs at varied radius and per-disc
    alpha, softened by one small blur so they read as camera bokeh rather
    than confetti. `scale` sets how many. Draw calls at full resolution
    (the speckle cost class), blur at a fixed small radius — crisp enough
    to keep their circular identity, soft enough to sit behind focus."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot.id, "bokeh"))
    count = max(3, round(_BOKEH_BASE_COUNT * slot.scale))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for _ in range(count):
        x, y = rng.uniform(0, cw), rng.uniform(0, ch)
        r = rng.uniform(_BOKEH_MIN_RADIUS_FRACTION,
                        _BOKEH_MAX_RADIUS_FRACTION) * ch
        value = rng.randint(_BOKEH_MIN_ALPHA, _BOKEH_MAX_ALPHA)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=value)
    mask = mask.filter(ImageFilter.GaussianBlur(_BOKEH_SOFTEN_FRACTION * ch))
    rgb = _warmed_toward_white(palette.accent, _ATMO_WHITE_BLEND)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask)
    return out


def _synth_dust(canvas: tuple[int, int], palette: Palette,
                slot: ArtSlot, version: int) -> Image.Image:
    """Drifting motes: seeded near-text-color specks at
    _DUST_ALPHA — speckle's exact mechanism an order of magnitude smaller
    and fainter, which is the whole difference between a design element
    and an atmosphere. `scale` sets density."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot.id, "dust"))
    count = max(1, round((cw * ch) / 1_000_000
                         * _DUST_DENSITY_PER_MEGAPIXEL * slot.scale))
    max_r = _DUST_MAX_RADIUS_FRACTION * ch
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for _ in range(count):
        x, y = rng.uniform(0, cw), rng.uniform(0, ch)
        r = rng.uniform(max_r * 0.3, max_r)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     fill=rng.randint(120, 255))
    rgb = ImageColor.getrgb(palette.text)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _DUST_ALPHA / 255)))
    return out


def _synth_scratches(canvas: tuple[int, int], palette: Palette,
                     slot: ArtSlot, version: int) -> Image.Image:
    """Film scratches: seeded hairline near-vertical strokes with a slight
    drift, near-text color at low alpha, drawn crisp at full resolution
    (§15.5 names this synth full-res by design — a blurred scratch is just
    a smudge). `scale` sets how many."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot.id, "scratches"))
    count = max(1, round(_SCRATCH_BASE_COUNT * slot.scale))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    for _ in range(count):
        x0 = rng.uniform(0, cw)
        y0 = rng.uniform(-0.1 * ch, 0.9 * ch)
        length = rng.uniform(_SCRATCH_MIN_LEN_FRACTION,
                             _SCRATCH_MAX_LEN_FRACTION) * ch
        drift = rng.uniform(-_SCRATCH_MAX_DRIFT, _SCRATCH_MAX_DRIFT) * length
        draw.line([(x0, y0), (x0 + drift, y0 + length)],
                  fill=rng.randint(120, 255), width=1)
    rgb = ImageColor.getrgb(palette.text)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask.point(lambda v: round(v * _SCRATCH_ALPHA / 255)))
    return out


def _synth_stars(canvas: tuple[int, int], palette: Palette,
                 slot: ArtSlot, version: int) -> Image.Image:
    """A night sky: seeded pinpoints at full resolution (§15.5's other
    named crisp synth — a star is one or two pixels or it is nothing),
    mostly single pixels at varied brightness, a bright few as small
    discs, a rare few flared with a four-arm cross. Ink is the accent
    blended nearly to white — hot points of light that still carry the
    palette. `scale` sets density."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot.id, "stars"))
    count = max(1, round((cw * ch) / 1_000_000
                         * _STAR_DENSITY_PER_MEGAPIXEL * slot.scale))
    mask = Image.new("L", canvas, 0)
    draw = ImageDraw.Draw(mask)
    arm = max(1.0, _STAR_FLARE_ARM_FRACTION * ch)
    for _ in range(count):
        x, y = rng.uniform(0, cw - 1), rng.uniform(0, ch - 1)
        value = rng.randint(120, 255)
        roll = rng.random()
        if roll < _STAR_FLARE_FRACTION:
            draw.line([(x - arm, y), (x + arm, y)], fill=value, width=1)
            draw.line([(x, y - arm), (x, y + arm)], fill=value, width=1)
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=255)
        elif roll < _STAR_FLARE_FRACTION + _STAR_BRIGHT_FRACTION:
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=value)
        else:
            draw.point((x, y), fill=value)
    rgb = _warmed_toward_white(palette.accent, _ATMO_STAR_WHITE_BLEND)
    out = Image.new("RGBA", canvas, (*rgb, 0))
    out.putalpha(mask)
    return out


PROCEDURAL_SYNTHESIZERS = {
    "gradient": _synth_gradient,
    "grain": _synth_grain,
    "paper": _synth_paper,
    "halftone": _synth_halftone,
    "canvas": _synth_canvas,
    "speckle": _synth_speckle,
    "rule_frame": _synth_rule_frame,
    "frame_hairline": _synth_frame_hairline,
    "frame_thickthin": _synth_frame_thickthin,
    "frame_corners": _synth_frame_corners,
    "frame_deco": _synth_frame_deco,
    "frame_octagon": _synth_frame_octagon,
    "radial_glow": _synth_radial_glow,
    "light_leak": _synth_light_leak,
    "fog_gradient": _synth_fog_gradient,
    "rays": _synth_rays,
    "bokeh": _synth_bokeh,
    "dust": _synth_dust,
    "scratches": _synth_scratches,
    "stars": _synth_stars,
}
