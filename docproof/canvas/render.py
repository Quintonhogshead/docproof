"""The CanvasDoc drawn again, server-side: PIL where the editor has Konva.

The editor's own picture lives in a browser (app/static/canvas/engine.js), and
everything downstream of a person's last drag — a headless export, a
measurement pass that wants to know where the ink actually landed, a thumbnail
for a job card — needs that same picture without one. So the drawing math
exists twice, which is a liability unless the second copy is honest about it:
every geometric number engine.js uses is restated ONCE in the constants block
below, each with the expression it mirrors, so a change on either side shows up
as two numbers that disagree rather than a cover that quietly renders
differently in the two places.

What "parity" means here is STRUCTURAL, not per-pixel. Same box geometry, same
visible stack, same glyph placement, same gradient ramp — but Konva rasterizes
through the browser's own text shaper and canvas filters, and PIL rasterizes
through FreeType and its own resampling. Every place the two cannot agree is
named in DIVERGENCES at the bottom of this docstring rather than papered over.

The pipeline per layer, bottom to top over the visible ones, mirrors
engine.js's buildLayer node tree exactly:

    content tile  (buildArt / buildText / buildScrim / buildFrame / buildShape)
      -> effects  (applyEffects: bevel ghosts, shadow ghosts, body)
      -> levels   (the Konva.Filters pair cached on the `flip` group)
      -> opacity  (the outer group's opacity)
      -> flips    (the inner `flip` group's -1 scales, about the box centre)
      -> rotation (the outer group's rotation, about the box centre)
      -> composited at the frame centre
      -> mask     (the alpha filter beside levels on the same cached group)

An ADJUST layer (§15.3) owns no pixels and skips that pipeline entirely: it
grades what is already on the canvas, scoped by its own frame box and mask,
and hands back the new composite (buildAdjust in engine.js does the same
thing with a raster of what is drawn so far).

A tile is carried as an image PLUS its origin in box-local pixels (_Tile), so
content that overhangs its own box — a bowed arc, a blurred shadow, a stroke
straddling its path — keeps its true position through flip and rotation instead
of being cropped to the box and silently re-centred.

DIVERGENCES from engine.js, all deliberate:

- No paper. engine.js's composite() draws the white `paper` rect under the
  content, so a browser export is opaque. This returns straight RGBA with
  nothing behind the bottom layer: a headless caller can flatten onto white
  (Image.alpha_composite) but cannot recover an alpha channel that was thrown
  away, and the measurement passes this exists for need the alpha.
- Text metrics. The engine measures with the browser's canvas 2D context and
  draws with Konva; here FreeType measures and draws. Advances agree to within
  a fraction of a pixel per glyph on the vendored faces; shaping does not —
  this Pillow has no raqm, so there is no kerning and no ligatures, where the
  browser has both. Long tracked lines drift by at most a glyph edge.
- Faux styles. A browser synthesizes bold/italic for a face that ships
  neither; font_path refuses, so a style with no companion file falls back to
  the regular cut rather than slanting or smearing it.
- Shadow blur. Canvas's shadowBlur is twice a Gaussian standard deviation
  (SHADOW_BLUR_TO_SIGMA); Pillow's GaussianBlur radius IS that deviation. The
  conversion is exact in intent, approximate in falloff.
- Sub-pixel placement. Every tile composites at a rounded pixel position; the
  browser does not round. Worth at most half a pixel per layer.
- Corner pin. engine.js approximates the homography with a MESH x MESH grid of
  affine patches (its own comment says visually smooth beats mathematically
  exact); PIL does the true projective sample in one pass, which is what that
  mesh converges to. Its edges are also hard where the mesh's are antialiased.
- Levels rounding. Konva's two filters round through a Uint8ClampedArray twice
  and so does the LUT here, but the intermediate rounding can differ by 1/255.
- Masks and grades mid-drag. Both ride a cached Konva node, and a cached node
  cannot follow a drag — so the editor drops them while a hand is on a control
  and takes them back on release. A masked layer shows unmasked mid-drag (the
  honest preview: the mask is canvas-space and does not travel with the layer,
  so what moves under it is exactly what the person is aiming), and an adjust
  layer shows a translucent stand-in over its own box rather than a raster
  that would slide its own contents around. Nothing here has an interaction
  state, so nothing here has the divergence — a headless render is always the
  settled picture.
- Grade arithmetic. `grade`'s contrast step is Pillow's ImageEnhance.Contrast,
  which pivots about the image's OWN mean; engine.js pivots about mid-grey,
  which is that mean for the graded composites this runs on but not for an
  extreme one. Saturation and brightness are the same equations in both.
  Values agree closely, never bit-for-bit — the divergence is widest on a
  composite that is almost entirely dark or almost entirely light.
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from docproof.canvas.model import (AdjustLayer, ArtLayer, CanvasDoc, FrameLayer,
                                   Mask, ScrimLayer, ShapeLayer, TextLayer)
from docproof.cover import effects as cover_effects
from docproof.cover.fonts import FAMILIES, font_path
from docproof.cover.model import AdjustLayer as CoverAdjust
from docproof.cover.model import GradientMask as CoverGradientMask
from docproof.cover.model import MaskSpec as CoverMaskSpec
from docproof.cover.model import Palette as CoverPalette


class RenderError(RuntimeError):
    """A document this renderer cannot draw, said in one sentence.

    Only for the failures a person can act on — a plate that is not in the job
    directory, a font file the install is missing, a pin whose four corners
    have collapsed onto a line. Everything the client tolerates silently (an
    effect type it has never heard of, a style companion a face does not ship)
    is tolerated silently here too: a server that refuses a document the editor
    is happily drawing is worse than a server that draws it slightly plainly.
    """


# Every geometric constant this module needs, and the engine.js expression it
# mirrors. Nothing below this block invents a number; if a value has to change,
# it changes here and in engine.js together, and the test suite asserts these
# literals so a one-sided edit fails loudly.
#   name                        engine.js counterpart
#   MIN_BOX_FRACTION            boxOf: Math.max(1e-4, l.frame.w)
#   TEXT_MIN_FONT_PX            buildText: Math.max(1, size * H())
#   TEXT_DEFAULT_SIZE           buildText: (l.size || 0.05)
#   TEXT_DEFAULT_LINE_HEIGHT    buildText: (l.line_height || 1.15)
#   WARP_SWEEP_RAD              buildWarpedText: |amount| * Math.PI * 0.75
#   WARP_MIN_SWEEP              buildWarpedText: theta > 1e-3
#   BULGE_GAIN                  buildWarpedText: 1 + amount * 0.55 * cos(...)
#   FLAG_AMP_EMS                buildWarpedText: amount * fontSize * 0.45
#   FRAME_MIN_STROKE_PX         buildFrame: Math.max(0.4, ...)
#   FRAME_DEFAULT_STROKE_W      buildFrame: (l.stroke_w || 0.002)
#   FRAME_MIN_SIDE_PX           buildFrame: Math.max(1, bw - 2 * ins)
#   CORNER_SERIF_ARM            buildFrame: Math.min(w, h) * 0.16
#   DOUBLE_RULE_GAP_STROKES     buildFrame: Math.max(sw * 3, ...)
#   DOUBLE_RULE_GAP_FRACTION    buildFrame: Math.min(w, h) * 0.022
#   DOUBLE_RULE_INNER_STROKE    buildFrame: sw * 0.6
#   INSET_PANEL_GAP_STROKES     buildFrame: sw * 2.6
#   INSET_PANEL_INNER_STROKE    buildFrame: sw * 0.5
#   INSET_PANEL_INNER_OPACITY   buildFrame: opacity: 0.6
#   BEVEL_DEFAULT_DEPTH         applyEffects: (bevel.params?.depth || 0.3)
#   BEVEL_MIN_PX                applyEffects: Math.max(0.5, ...)
#   BEVEL_DEPTH_TO_PX           applyEffects: depth * 0.006 * W()
#   BEVEL_ALPHA                 applyEffects: alpha: 0.65
#   BEVEL_LIGHT / BEVEL_DARK    applyEffects: '#ffffff' up-left, '#000000' down
#   SHADOW_DEFAULT_ALPHA        setShadow: p.alpha === undefined ? 0.5
#   SHADOW_DEFAULT_COLOR        setShadow: p.color || '#000000'
#   SCRIM_FALLBACK_ANGLE        buildScrim: { angle: 90, ... } default
#   PIN_KINDS                   buildLayer: pinned = l.kind === 'art' && ...
#   SQUARE_TO_QUAD_EPS          squareToQuad: Math.abs(sx) > 1e-9
#   LEVELS_*                    levelsOf: the wire clamps
# The remaining constants have no engine.js counterpart because they answer a
# question a browser never asks (how big to allocate a bitmap, how far a
# Gaussian reaches); each says so on itself.

MIN_BOX_FRACTION = 1e-4
TEXT_MIN_FONT_PX = 1.0
TEXT_DEFAULT_SIZE = 0.05
# Only reachable from a doc the model would refuse (line_height is gt=0 with a
# default of 1.1), kept because engine.js's `||` fallback is 1.15 and a reader
# comparing the two files should not have to wonder which one wins.
TEXT_DEFAULT_LINE_HEIGHT = 1.15

WARP_SWEEP_RAD = math.pi * 0.75          # |amount| * 135 degrees
WARP_MIN_SWEEP = 1e-3
BULGE_GAIN = 0.55
FLAG_AMP_EMS = 0.45

FRAME_MIN_STROKE_PX = 0.4
FRAME_DEFAULT_STROKE_W = 0.002
FRAME_MIN_SIDE_PX = 1.0
CORNER_SERIF_ARM = 0.16
DOUBLE_RULE_GAP_STROKES = 3.0
DOUBLE_RULE_GAP_FRACTION = 0.022
DOUBLE_RULE_INNER_STROKE = 0.6
INSET_PANEL_GAP_STROKES = 2.6
INSET_PANEL_INNER_STROKE = 0.5
INSET_PANEL_INNER_OPACITY = 0.6

BEVEL_DEFAULT_DEPTH = 0.3
BEVEL_MIN_PX = 0.5
BEVEL_DEPTH_TO_PX = 0.006
BEVEL_ALPHA = 0.65
BEVEL_LIGHT = "#ffffff"
BEVEL_DARK = "#000000"

SHADOW_DEFAULT_ALPHA = 0.5
SHADOW_DEFAULT_COLOR = "#000000"

SCRIM_FALLBACK_ANGLE = 90.0

# engine.js pins art and nothing else (buildLayer). A pinned text or shape
# layer draws undistorted in the browser, so it draws undistorted here — the
# doc's own promise that a client which cannot draw the pin is merely
# undistorted, never misplaced (model.py Frame.corners).
PIN_KINDS = ("art",)

SQUARE_TO_QUAD_EPS = 1e-9

LEVELS_BRIGHTNESS_LIMIT = 1.0
LEVELS_CONTRAST_MIN = -0.95
LEVELS_CONTRAST_MAX = 4.0


# Canvas defines shadowBlur as twice the Gaussian standard deviation; Pillow's
# GaussianBlur radius IS the standard deviation.
SHADOW_BLUR_TO_SIGMA = 0.5
# How far out to allocate for a blur before calling it black. Three sigma holds
# 99.7% of the kernel, and the missing tail is under one alpha step.
SHADOW_EXTENT_SIGMAS = 3.0
# One pixel of slack around measured ink, for the antialiasing that a getbbox
# in whole pixels does not report.
INK_PAD = 1.0
# The scrim ramp is rasterized once as a strip and projected across the box;
# more samples than this buys nothing an 8-bit alpha channel can show.
GRADIENT_RAMP_MAX = 4096
# Guard columns at each end of that strip, holding the terminal stop's value,
# so a rounding overshoot at the box's corner samples the clamped colour the
# way a canvas gradient does instead of falling off into transparency.
GRADIENT_RAMP_GUARD = 2

_ROTATE_RESAMPLE = Image.Resampling.BICUBIC
_SCALE_RESAMPLE = Image.Resampling.LANCZOS
_WARP_RESAMPLE = Image.Resampling.BICUBIC

_DEG = math.pi / 180.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _rgb(hex_color: str | None, fallback: str = "#000000"
         ) -> tuple[int, int, int]:
    """A #rrggbb string as an (r, g, b) triple. The model validates every
    colour it owns, so this is a parser and not a gate; `fallback` covers the
    optional fields (a shape's stroke, a shadow's colour off the wire) the way
    engine.js's `l.color || '#000000'` does."""
    text = (hex_color or fallback).lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


@dataclass
class _Tile:
    """One rasterized piece of a layer, plus where it sits.

    `x`/`y` are the tile's top-left in BOX-LOCAL pixels, where (0, 0) is the
    layer box's own top-left — the coordinate space engine.js's body group
    works in (it is translated to -bw/2, -bh/2). Keeping the origin beside the
    bitmap is what lets content overhang its box: a bowed arc, a shadow, a
    stroke centred on its path all carry negative origins and stay in the right
    place through flip, rotation and compositing."""
    img: Image.Image
    x: float = 0.0
    y: float = 0.0


def _blank(width: float, height: float,
           color: tuple[int, int, int, int] = (0, 0, 0, 0)) -> Image.Image:
    return Image.new("RGBA", (max(1, math.ceil(width)),
                              max(1, math.ceil(height))), color)


def _composite(base: Image.Image, tile: Image.Image,
               x: float, y: float) -> None:
    """alpha_composite `tile` onto `base` at a SIGNED, fractional position.

    Pillow's own alpha_composite refuses a negative destination and a source
    that runs off the edge, which are both the normal case here — a layer half
    off the trim is a real design (model.py Frame). The overhang is cropped and
    the position rounded, which is this renderer's one systematic sub-pixel
    divergence from the browser."""
    left, top = int(round(x)), int(round(y))
    crop_x, crop_y = max(0, -left), max(0, -top)
    if crop_x >= tile.width or crop_y >= tile.height:
        return
    if crop_x or crop_y:
        tile = tile.crop((crop_x, crop_y, tile.width, tile.height))
        left += crop_x
        top += crop_y
    if left >= base.width or top >= base.height:
        return
    over_x = min(tile.width, base.width - left)
    over_y = min(tile.height, base.height - top)
    if over_x <= 0 or over_y <= 0:
        return
    if over_x != tile.width or over_y != tile.height:
        tile = tile.crop((0, 0, over_x, over_y))
    base.alpha_composite(tile, dest=(left, top))


def _rotate(img: Image.Image, degrees_ccw: float) -> Image.Image:
    """Rotate about the image centre, expanded so nothing clips.

    The premultiplied round-trip is typeset._rotate_layer's rule and matters
    for the same reason: transparent pixels carry arbitrary RGB, and resampling
    straight RGBA smears it into every antialiased edge as a dark fringe."""
    return (img.convert("RGBa")
            .rotate(degrees_ccw, resample=_ROTATE_RESAMPLE, expand=True)
            .convert("RGBA"))


def _scale_alpha(img: Image.Image, factor: float) -> Image.Image:
    if factor >= 1.0:
        return img
    alpha = img.getchannel("A").point(
        lambda v: int(round(v * max(0.0, factor))))
    out = img.copy()
    out.putalpha(alpha)
    return out


@functools.lru_cache(maxsize=512)
def _face(family: str, style: str, size_px: float) -> ImageFont.FreeTypeFont:
    """One rasterizer handle per (family, style, size), cached the way
    typeset._font caches: warped text asks for a handle per glyph size and a
    bulge asks for a different one per glyph, so the same face is loaded dozens
    of times per line otherwise. Handles are only ever measured and drawn from,
    never mutated.

    A style the family does not ship falls back to its regular cut. The browser
    would synthesize a faux bold or italic; refusing to draw the layer at all
    would be the worse answer, and font_path is deliberately strict."""
    if family not in FAMILIES:
        raise RenderError(
            f"font family {family!r} is not on the shelf — known families: "
            f"{', '.join(sorted(FAMILIES))}")
    try:
        path = font_path(family, style)
    except ValueError:
        path = font_path(family, "regular")
    try:
        return ImageFont.truetype(str(path), max(TEXT_MIN_FONT_PX, size_px))
    except OSError as exc:
        raise RenderError(
            f"the {family} font file is missing from this install: "
            f"{path} ({exc})") from exc


def _plate(layer: ArtLayer, job_dir: Path) -> Image.Image:
    """This layer's plate as RGBA, or a sentence naming what is missing.

    The client draws a dashed placeholder box for a plate that has not loaded
    yet, because in a browser "not there" usually means "not there YET". A
    headless render has no later: the file is absent, and the caller needs to
    know which layer named it and where it was looked for."""
    path = Path(job_dir) / layer.source
    if not path.is_file():
        name = layer.name or layer.id
        raise RenderError(
            f"layer {layer.id!r} ({name}) names the plate {layer.source!r}, "
            f"which is not in the job directory — expected it at {path}")
    try:
        with Image.open(path) as opened:
            return opened.convert("RGBA")
    except OSError as exc:
        raise RenderError(
            f"layer {layer.id!r} names the plate {layer.source!r}, which is "
            f"not a readable image: {exc}") from exc


def _art_tile(layer: ArtLayer, box_w: float, box_h: float,
              job_dir: Path) -> _Tile:
    """buildArt's three fits, drawn into the box.

    cover scales to the larger ratio and is clipped to the box (the engine's
    clip group); contain scales to the smaller and letterboxes; stretch takes
    the box exactly. All three centre what they drew, and none can leave the
    box — so the tile IS the box, and the clip is free."""
    img = _plate(layer, job_dir)
    src_w, src_h = img.size
    if layer.fit == "stretch":
        draw_w, draw_h = box_w, box_h
    else:
        ratios = (box_w / src_w, box_h / src_h)
        scale = min(ratios) if layer.fit == "contain" else max(ratios)
        draw_w, draw_h = src_w * scale, src_h * scale
    tile = _blank(box_w, box_h)
    resized = img.resize((max(1, round(draw_w)), max(1, round(draw_h))),
                         _SCALE_RESAMPLE)
    # paste, not alpha_composite: the destination is empty and cover's offsets
    # are negative, which is exactly the crop the engine's clip group performs.
    tile.paste(resized, (round((box_w - draw_w) / 2),
                         round((box_h - draw_h) / 2)))
    return _Tile(tile)


def _x_start(align: str, box_w: float, line_w: float) -> float:
    """Where a line of width `line_w` starts in a box of width `box_w` —
    buildWarpedText's xStart, which is also what Konva's own align does to a
    plain line."""
    if align == "left":
        return 0.0
    if align == "right":
        return box_w - line_w
    return (box_w - line_w) / 2.0


def _text_tile(layer: TextLayer, box_w: float, box_h: float,
               canvas_h: int) -> _Tile:
    """buildText: the type set line by line, warped or not.

    Sizes are fractions of canvas HEIGHT and tracking is an em fraction of the
    resulting size, exactly as the doc states them; the vertical layout is
    buildPlainText's — the block of `n` lines is centred in the box and each
    line's glyphs hang off the middle of its own slot, which is where Konva's
    verticalAlign 'middle' with lineHeight 1 puts them and where the warped
    path's `midY` is."""
    font_size = max(TEXT_MIN_FONT_PX, (layer.size or TEXT_DEFAULT_SIZE)
                    * canvas_h)
    line_h = font_size * (layer.line_height or TEXT_DEFAULT_LINE_HEIGHT)
    tracking = (layer.tracking or 0.0) * font_size
    lines = str(layer.text or "").split("\n")
    top = (box_h - len(lines) * line_h) / 2.0
    warped = layer.warp.kind != "none" and layer.warp.amount != 0.0

    stamps: list[_Tile] = []
    for index, line in enumerate(lines):
        mid_y = top + index * line_h + line_h / 2.0
        if warped:
            stamps.extend(_warped_line(layer, line, box_w, mid_y,
                                       font_size, tracking))
        else:
            stamp = _plain_line(layer, line, box_w, mid_y, font_size, tracking)
            if stamp is not None:
                stamps.append(stamp)
    return _stamps_to_tile(stamps)


def _stamps_to_tile(stamps: list[_Tile]) -> _Tile:
    """A pile of positioned bitmaps flattened into one tile that just contains
    them. Tight rather than box-sized on purpose: the tile is what every later
    stage resamples and blurs, and a title's ink is a fraction of its box."""
    if not stamps:
        return _Tile(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    left = math.floor(min(s.x for s in stamps))
    top = math.floor(min(s.y for s in stamps))
    right = math.ceil(max(s.x + s.img.width for s in stamps))
    bottom = math.ceil(max(s.y + s.img.height for s in stamps))
    tile = _blank(right - left, bottom - top)
    for stamp in stamps:
        _composite(tile, stamp.img, stamp.x - left, stamp.y - top)
    return _Tile(tile, float(left), float(top))


def _plain_line(layer: TextLayer, line: str, box_w: float, mid_y: float,
                font_size: float, tracking: float) -> _Tile | None:
    """One un-warped line, drawn around its own pen point.

    Konva measures a line for alignment as the whole string's width plus one
    tracking gap per INTERIOR glyph, then draws glyph by glyph when tracking is
    non-zero; both halves of that are reproduced here, including the mismatch
    it implies between the measured width and the sum of the advances. With no
    tracking the whole line goes down in one call so the rasterizer does
    whatever shaping it can."""
    if not line:
        return None
    font = _face(layer.family, layer.style, font_size)
    count = len(line)
    line_w = font.getlength(line) + tracking * (count - 1)
    start_x = _x_start(layer.align, box_w, line_w)

    if tracking:
        boxes = []
        pen = 0.0
        for index, char in enumerate(line):
            left, top, right, bottom = font.getbbox(char, anchor="lm")
            boxes.append((pen + left, top, pen + right, bottom))
            pen += font.getlength(char)
            if index < count - 1:
                pen += tracking
        ink = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    else:
        ink = font.getbbox(line, anchor="lm")

    # The tile spans the em box AND the ink, so a line's tile always covers the
    # type's own slot even when the glyphs (a lone comma, a run of spaces) sit
    # well inside it.
    half = font_size / 2.0
    left = min(0.0, ink[0]) - INK_PAD
    top = min(-half, ink[1]) - INK_PAD
    right = max(line_w, ink[2]) + INK_PAD
    bottom = max(half, ink[3]) + INK_PAD
    img = _blank(right - left, bottom - top)
    draw = ImageDraw.Draw(img)
    fill = (*_rgb(layer.color, "#ffffff"), 255)
    pen_x, pen_y = -left, -top
    if tracking:
        for index, char in enumerate(line):
            draw.text((pen_x, pen_y), char, font=font, fill=fill, anchor="lm")
            pen_x += font.getlength(char)
            if index < count - 1:
                pen_x += tracking
    else:
        draw.text((pen_x, pen_y), line, font=font, fill=fill, anchor="lm")
    return _Tile(img, start_x + left, mid_y + top)


def _warped_line(layer: TextLayer, line: str, box_w: float, mid_y: float,
                 font_size: float, tracking: float) -> list[_Tile]:
    """buildWarpedText, glyph by glyph.

    `amount` is the whole knob:
      arc / arch — a circular baseline whose sweep is |amount| * 135 degrees.
                   arc rotates each glyph to the tangent; arch keeps them
                   upright, which is the distinction the two names carry in
                   every type tool.
      flag       — one sine period across the line, glyphs tilted to the slope.
      bulge      — a per-glyph scale peaking mid-line, with the advances scaled
                   along with it so the letters do not collide.

    Each glyph is drawn into a tile centred on ITS OWN advance box, because
    that is the point Konva rotates and scales a per-character Text node
    about (offsetX/offsetY = width/2, height/2)."""
    chars = list(line)
    if not chars:
        return []
    font = _face(layer.family, layer.style, font_size)
    kind, amount = layer.warp.kind, layer.warp.amount
    widths = [font.getlength(char) for char in chars]
    advances = [width + tracking for width in widths]
    raw_w = sum(advances) - tracking

    scales = [1.0] * len(chars)
    if kind == "bulge":
        run = 0.0
        for index, width in enumerate(widths):
            along = (run + width / 2.0) / raw_w if raw_w > 0 else 0.5
            scales[index] = 1.0 + amount * BULGE_GAIN * math.cos(
                math.pi * (along - 0.5))
            run += advances[index]
        advances = [a * s for a, s in zip(advances, scales)]

    line_w = sum(advances) - tracking
    start_x = _x_start(layer.align, box_w, line_w)
    theta = abs(amount) * WARP_SWEEP_RAD
    sign = 1.0 if amount >= 0 else -1.0
    radius = line_w / theta if theta > WARP_MIN_SWEEP else math.inf

    stamps: list[_Tile] = []
    run = 0.0
    for index, char in enumerate(chars):
        along_px = run + advances[index] / 2.0
        run += advances[index]
        if char == " ":
            continue
        along = along_px / line_w if line_w > 0 else 0.5
        offset_x, offset_y, rotation = along_px, 0.0, 0.0
        if kind in ("arc", "arch") and math.isfinite(radius):
            phi = (along_px - line_w / 2.0) / radius
            offset_x = line_w / 2.0 + radius * math.sin(phi)
            offset_y = sign * (radius - radius * math.cos(phi))
            rotation = math.degrees(sign * phi) if kind == "arc" else 0.0
        elif kind == "flag":
            amp = amount * font_size * FLAG_AMP_EMS
            offset_y = amp * math.sin(2 * math.pi * along)
            slope = (amp * 2 * math.pi * math.cos(2 * math.pi * along) / line_w
                     if line_w > 0 else 0.0)
            rotation = math.degrees(math.atan(slope))
        glyph = _glyph(layer, char, font_size * scales[index], rotation)
        stamps.append(_Tile(glyph,
                            start_x + offset_x - glyph.width / 2.0,
                            mid_y + offset_y - glyph.height / 2.0))
    return stamps


def _glyph(layer: TextLayer, char: str, size_px: float,
           rotation: float) -> Image.Image:
    """One character, rasterized so that the CENTRE of the returned bitmap is
    the centre of the character's own advance box — the node origin Konva
    rotates and scales about.

    A bulge is rasterized at the scaled size rather than drawn once and
    resampled: Konva scales a vector glyph and stays crisp, and re-rendering at
    the target size is the only way PIL can say the same thing."""
    font = _face(layer.family, layer.style, size_px)
    advance = font.getlength(char)
    ink_l, ink_t, ink_r, ink_b = font.getbbox(char, anchor="lm")
    half = size_px / 2.0
    left = min(0.0, ink_l) - INK_PAD
    right = max(advance, ink_r) + INK_PAD
    top = min(-half, ink_t) - INK_PAD
    bottom = max(half, ink_b) + INK_PAD
    # Grown to be symmetric about the box centre, so a rotate-with-expand
    # about the bitmap's centre is a rotate about the glyph's own origin.
    centre_x = advance / 2.0
    half_w = max(centre_x - left, right - centre_x)
    half_h = max(-top, bottom)
    img = _blank(2 * half_w, 2 * half_h)
    ImageDraw.Draw(img).text(
        (img.width / 2.0 - centre_x, img.height / 2.0), char, font=font,
        fill=(*_rgb(layer.color, "#ffffff"), 255), anchor="lm")
    if abs(rotation) > 1e-9:
        # Konva rotates clockwise in a y-down space; PIL rotates the other way.
        img = _rotate(img, -rotation)
    return img


def _ramp_alpha(stops: list[Any], along: float) -> float:
    """The gradient's alpha at `along` (0..1), clamped at both ends the way a
    canvas gradient clamps to its terminal stops."""
    if along <= stops[0].at:
        return stops[0].alpha
    for first, second in zip(stops, stops[1:]):
        if along <= second.at:
            span = second.at - first.at
            if span <= 0:
                return second.alpha
            ratio = (along - first.at) / span
            return first.alpha + (second.alpha - first.alpha) * ratio
    return stops[-1].alpha


def _scrim_tile(layer: ScrimLayer, box_w: float, box_h: float) -> _Tile:
    """buildScrim: one ink at a varying alpha, ramped across the box.

    The gradient's start and end points span the box ALONG the ramp direction
    (|bw*cos| + |bh*sin|), which is what puts stop 0 and stop 1 exactly on the
    box's edges at any angle. Angle 0 ramps left-to-right and 90 top-to-bottom,
    y-down, as model.Gradient says.

    The ramp is rasterized once as a two-row strip and projected across the box
    by an affine transform, rather than evaluated per pixel: a full-bleed scrim
    on a 1600x2560 cover is four million pixels, and this is the same answer in
    one resample."""
    angle = layer.gradient.angle if layer.gradient else SCRIM_FALLBACK_ANGLE
    theta = angle * _DEG
    dir_x, dir_y = math.cos(theta), math.sin(theta)
    length = abs(box_w * dir_x) + abs(box_h * dir_y)
    if length <= 0:
        length = max(box_w, box_h, 1.0)
    start_x = box_w / 2.0 - dir_x * length / 2.0
    start_y = box_h / 2.0 - dir_y * length / 2.0

    stops = sorted(layer.gradient.stops, key=lambda s: s.at)
    samples = max(2, min(GRADIENT_RAMP_MAX, math.ceil(length)))
    guard = GRADIENT_RAMP_GUARD
    strip = Image.new("L", (samples + 2 * guard, 2), 0)
    pixels = strip.load()
    for index in range(strip.width):
        along = _clamp((index - guard) / (samples - 1), 0.0, 1.0)
        value = int(round(255 * _clamp(_ramp_alpha(stops, along), 0.0, 1.0)))
        pixels[index, 0] = value
        pixels[index, 1] = value

    scale = (samples - 1) / length
    coeffs = (dir_x * scale, dir_y * scale,
              guard - (start_x * dir_x + start_y * dir_y) * scale,
              0.0, 0.0, 0.5)
    mask = strip.transform(
        (max(1, math.ceil(box_w)), max(1, math.ceil(box_h))),
        Image.Transform.AFFINE, coeffs,
        resample=Image.Resampling.BILINEAR)
    tile = _blank(box_w, box_h, (*_rgb(layer.color), 0))
    tile.putalpha(mask)
    return _Tile(tile)


def _stroke_px(width: float) -> int:
    """A stroke wide enough to survive rasterization. Canvas draws a 0.3px rule
    as a faint one; PIL cannot draw less than a pixel, so a hairline reads
    slightly heavier here than in the editor."""
    return max(1, int(round(width)))


def _rect_xy(left: float, top: float, right: float,
             bottom: float) -> tuple[float, float, float, float]:
    """PIL's rectangle coordinates are inclusive at both ends, so a box from 0
    to 10 is eleven pixels wide. Trimming the far edge keeps a drawn rule the
    width the geometry asked for."""
    return left, top, max(left, right - 1), max(top, bottom - 1)


def _frame_tile(layer: FrameLayer, box_w: float, box_h: float,
                canvas_w: int) -> _Tile:
    """buildFrame's four presets.

    `inset` is a fraction of the box's SHORT side so the margin stays visually
    even on a tall box; `stroke_w` is a canvas-WIDTH fraction, as the doc says.
    Canvas centres a stroke on its path while PIL draws an outline inside the
    box it is given, so every rule here is drawn from a box grown by half a
    stroke — that half-stroke is also why the tile is padded."""
    stroke_w = max(FRAME_MIN_STROKE_PX,
                   (layer.stroke_w or FRAME_DEFAULT_STROKE_W) * canvas_w)
    inset = (layer.inset or 0.0) * min(box_w, box_h)
    left, top = inset, inset
    width = max(FRAME_MIN_SIDE_PX, box_w - 2 * inset)
    height = max(FRAME_MIN_SIDE_PX, box_h - 2 * inset)
    pad = math.ceil(stroke_w) + 1
    tile = _blank(box_w + 2 * pad, box_h + 2 * pad)
    draw = ImageDraw.Draw(tile)
    stroke = (*_rgb(layer.stroke, "#ffffff"), 255)
    left += pad
    top += pad
    half = stroke_w / 2.0

    if layer.preset == "corner_serifs":
        arm = min(width, height) * CORNER_SERIF_ARM
        right, bottom = left + width, top + height
        # Two axis-aligned bars per corner. Canvas's square line cap extends a
        # segment by half its width at each end and the miter join fills the
        # corner, which is exactly the union of these two rectangles.
        for corner_x, corner_y, arm_x, arm_y in (
                (left, top, arm, arm),
                (right, top, -arm, arm),
                (right, bottom, -arm, -arm),
                (left, bottom, arm, -arm)):
            draw.rectangle(_rect_xy(
                min(corner_x, corner_x + arm_x) - half, corner_y - half,
                max(corner_x, corner_x + arm_x) + half, corner_y + half),
                fill=stroke)
            draw.rectangle(_rect_xy(
                corner_x - half, min(corner_y, corner_y + arm_y) - half,
                corner_x + half, max(corner_y, corner_y + arm_y) + half),
                fill=stroke)
        return _Tile(tile, -pad, -pad)

    if layer.fill:
        draw.rectangle(_rect_xy(left, top, left + width, top + height),
                       fill=(*_rgb(layer.fill), 255))
    draw.rectangle(
        _rect_xy(left - half, top - half,
                 left + width + half, top + height + half),
        outline=stroke, width=_stroke_px(stroke_w))

    if layer.preset in ("double_rule", "inset_panel"):
        if layer.preset == "double_rule":
            gap = max(stroke_w * DOUBLE_RULE_GAP_STROKES,
                      min(width, height) * DOUBLE_RULE_GAP_FRACTION)
            inner_w = stroke_w * DOUBLE_RULE_INNER_STROKE
            opacity = 1.0
        else:
            gap = stroke_w * INSET_PANEL_GAP_STROKES
            inner_w = stroke_w * INSET_PANEL_INNER_STROKE
            opacity = INSET_PANEL_INNER_OPACITY
        inner = _blank(tile.width, tile.height)
        inner_half = inner_w / 2.0
        ImageDraw.Draw(inner).rectangle(
            _rect_xy(left + gap - inner_half, top + gap - inner_half,
                     left + max(FRAME_MIN_SIDE_PX, width - 2 * gap) + gap
                     + inner_half,
                     top + max(FRAME_MIN_SIDE_PX, height - 2 * gap) + gap
                     + inner_half),
            outline=stroke, width=_stroke_px(inner_w))
        tile.alpha_composite(_scale_alpha(inner, opacity))
    return _Tile(tile, -pad, -pad)


def _shape_tile(layer: ShapeLayer, box_w: float, box_h: float,
                canvas_w: int) -> _Tile:
    """buildShape: a rect (with an optional corner radius, a fraction of the
    box's shorter side) or an ellipse inscribed in the box. Stroke width is a
    canvas-WIDTH fraction and, as in the engine, is centred on the path — hence
    the half-stroke growth and the padded tile."""
    stroke_w = (layer.stroke_w or 0.0) * canvas_w
    half = stroke_w / 2.0
    pad = math.ceil(half) + 1
    tile = _blank(box_w + 2 * pad, box_h + 2 * pad)
    draw = ImageDraw.Draw(tile)
    fill = (*_rgb(layer.fill), 255) if layer.fill else None
    stroke = (*_rgb(layer.stroke), 255) if layer.stroke else None
    left, top = float(pad), float(pad)
    right, bottom = pad + box_w, pad + box_h

    if layer.shape == "ellipse":
        if fill:
            draw.ellipse(_rect_xy(left, top, right, bottom), fill=fill)
        if stroke and stroke_w > 0:
            draw.ellipse(_rect_xy(left - half, top - half,
                                  right + half, bottom + half),
                         outline=stroke, width=_stroke_px(stroke_w))
    else:
        radius = (layer.radius or 0.0) * min(box_w, box_h)
        if fill:
            draw.rounded_rectangle(_rect_xy(left, top, right, bottom),
                                   radius=radius, fill=fill)
        if stroke and stroke_w > 0:
            draw.rounded_rectangle(
                _rect_xy(left - half, top - half, right + half, bottom + half),
                radius=radius + half, outline=stroke,
                width=_stroke_px(stroke_w))
    return _Tile(tile, -pad, -pad)


@dataclass(frozen=True)
class _Shadow:
    """setShadow's parameters, resolved to pixels. `dxPx`/`dyPx`/`blurPx` are
    the engine's own internal spelling (the bevel ghosts pass them); a shadow
    off the wire states dx/dy/blur as fractions of canvas WIDTH."""
    dx: float
    dy: float
    sigma: float
    color: tuple[int, int, int]
    alpha: float


def _shadow_spec(params: dict[str, Any], canvas_w: int) -> _Shadow:
    def px(pixel_key: str, fraction_key: str) -> float:
        if params.get(pixel_key) is not None:
            return float(params[pixel_key])
        return float(params.get(fraction_key) or 0.0) * canvas_w

    alpha = params.get("alpha")
    return _Shadow(
        dx=px("dxPx", "dx"), dy=px("dyPx", "dy"),
        sigma=max(0.0, px("blurPx", "blur")) * SHADOW_BLUR_TO_SIGMA,
        color=_rgb(params.get("color"), SHADOW_DEFAULT_COLOR),
        alpha=SHADOW_DEFAULT_ALPHA if alpha is None else float(alpha))


def _shadow_image(source: Image.Image, spec: _Shadow, pad: int
                  ) -> Image.Image:
    """The silhouette of `source`, blurred and tinted — a canvas shadow.

    Built at the padded size before blurring so the halo has room; a blur taken
    at the tile's own size would clip its own falloff at the edges."""
    silhouette = Image.new("L", (source.width + 2 * pad,
                                 source.height + 2 * pad), 0)
    silhouette.paste(source.getchannel("A"), (pad, pad))
    if spec.sigma > 0:
        silhouette = silhouette.filter(ImageFilter.GaussianBlur(spec.sigma))
    if spec.alpha != 1.0:
        silhouette = silhouette.point(
            lambda v: int(round(v * _clamp(spec.alpha, 0.0, 1.0))))
    shadow = Image.new("RGBA", silhouette.size, (*spec.color, 0))
    shadow.putalpha(silhouette)
    return shadow


def _effects(layer: Any, tile: _Tile, canvas_w: int) -> _Tile:
    """applyEffects: bevel ghosts, then shadow ghosts, then the body.

    Konva carries exactly one shadow per shape, so a stack of them needs one
    GHOST COPY of the whole content per extra shadow — and those copies are
    reproduced here rather than shortcut into "all the shadows, then the
    content once", because on a semi-transparent layer the stacked copies
    accumulate alpha and the shortcut would not.

    A bevel is an emboss: a light ghost up-left and a dark one down-right, at a
    depth measured against canvas width. Unknown effect types fall through
    untouched, exactly as they do in the client."""
    shadows = [e for e in layer.effects if e.type == "drop_shadow"]
    bevel = next((e for e in layer.effects if e.type == "bevel"), None)
    if not shadows and not bevel:
        return tile

    ghosts: list[dict[str, Any]] = []
    if bevel is not None:
        depth = max(BEVEL_MIN_PX,
                    abs(float(bevel.params.get("depth") or BEVEL_DEFAULT_DEPTH))
                    * BEVEL_DEPTH_TO_PX * canvas_w)
        ghosts.append({"dxPx": -depth, "dyPx": -depth, "blurPx": depth,
                       "color": BEVEL_LIGHT, "alpha": BEVEL_ALPHA})
        ghosts.append({"dxPx": depth, "dyPx": depth, "blurPx": depth,
                       "color": BEVEL_DARK, "alpha": BEVEL_ALPHA})
    ghosts.extend(dict(e.params) for e in shadows[:-1])
    body = _shadow_spec(dict(shadows[-1].params), canvas_w) if shadows else None
    specs = [_shadow_spec(g, canvas_w) for g in ghosts]

    reach = specs + ([body] if body else [])
    pad = math.ceil(max(
        max(abs(s.dx), abs(s.dy)) + SHADOW_EXTENT_SIGMAS * s.sigma
        for s in reach)) + 1

    out = _blank(tile.img.width + 2 * pad, tile.img.height + 2 * pad)
    for spec in specs:
        _composite(out, _shadow_image(tile.img, spec, pad), spec.dx, spec.dy)
        _composite(out, tile.img, pad, pad)
    if body is not None:
        _composite(out, _shadow_image(tile.img, body, pad), body.dx, body.dy)
    _composite(out, tile.img, pad, pad)
    return _Tile(out, tile.x - pad, tile.y - pad)


@functools.lru_cache(maxsize=64)
def _levels_lut(brightness: float, contrast: float) -> tuple[int, ...]:
    """The levels contract, as a 256-entry table.

    out = (v + brightness - 0.5) * (1 + contrast) + 0.5, on normalized
    luminance (regen.plan_correction states it; levelsOf translates it into the
    two dials Konva's filters take). The intermediate clamp is not decoration:
    Konva's Brighten writes into a Uint8ClampedArray before Contrast reads it,
    so a brightness that pushes a channel past white cannot be pulled back by a
    negative contrast."""
    table = []
    for value in range(256):
        brightened = _clamp(value + brightness * 255.0, 0.0, 255.0) / 255.0
        out = (brightened - 0.5) * (1.0 + contrast) + 0.5
        table.append(int(_clamp(round(out * 255.0), 0, 255)))
    return tuple(table)


def _levels(layer: Any, img: Image.Image) -> Image.Image:
    """levelsOf, applied to the layer's own composited pixels — the engine
    caches the `flip` group and filters that, so a levels effect grades the
    content AND whatever shadows sit under it, which is what happens here."""
    effect = next((e for e in layer.effects if e.type == "levels"), None)
    if effect is None:
        return img
    brightness = _clamp(float(effect.params.get("brightness") or 0.0),
                        -LEVELS_BRIGHTNESS_LIMIT, LEVELS_BRIGHTNESS_LIMIT)
    contrast = _clamp(float(effect.params.get("contrast") or 0.0),
                      LEVELS_CONTRAST_MIN, LEVELS_CONTRAST_MAX)
    if not brightness and not contrast:
        return img
    lut = list(_levels_lut(brightness, contrast))
    red, green, blue, alpha = img.split()
    return Image.merge("RGBA", (red.point(lut), green.point(lut),
                                blue.point(lut), alpha))


def _square_to_quad(quad: list[tuple[float, float]]) -> tuple[float, ...]:
    """squareToQuad, restated: the projective map taking (0,0), (1,0), (1,1),
    (0,1) onto TL, TR, BR, BL. Returned as the nine entries of a 3x3 matrix
    (a, b, c, d, e, f, g, h, 1) in the engine's own naming."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = quad
    sum_x = x0 - x1 + x2 - x3
    sum_y = y0 - y1 + y2 - y3
    g = h = 0.0
    # Both sums are zero exactly when the quad is a parallelogram, which has no
    # projective term — the affine branch, and the one a freshly pinned (still
    # rectangular) layer takes.
    if abs(sum_x) > SQUARE_TO_QUAD_EPS or abs(sum_y) > SQUARE_TO_QUAD_EPS:
        dx1, dx2 = x1 - x2, x3 - x2
        dy1, dy2 = y1 - y2, y3 - y2
        den = dx1 * dy2 - dx2 * dy1
        if abs(den) > SQUARE_TO_QUAD_EPS:
            g = (sum_x * dy2 - dx2 * sum_y) / den
            h = (dx1 * sum_y - sum_x * dy1) / den
    return (x1 - x0 + g * x1, x3 - x0 + h * x3, x0,
            y1 - y0 + g * y1, y3 - y0 + h * y3, y0,
            g, h, 1.0)


def _invert3(m: tuple[float, ...]) -> tuple[float, ...] | None:
    """A 3x3 inverse by adjugate — no numpy, and this module stays PIL-only.
    None when the matrix is singular, which is a quad whose corners have
    collapsed onto a line."""
    a, b, c, d, e, f, g, h, i = m
    a11 = e * i - f * h
    a12 = -(d * i - f * g)
    a13 = d * h - e * g
    det = a * a11 + b * a12 + c * a13
    if abs(det) < SQUARE_TO_QUAD_EPS:
        return None
    return (a11 / det, -(b * i - c * h) / det, (b * f - c * e) / det,
            a12 / det, (a * i - c * g) / det, -(a * f - c * d) / det,
            a13 / det, -(a * h - b * g) / det, (a * e - b * d) / det)


def _pinned_art(layer: ArtLayer, canvas_w: int, canvas_h: int,
                job_dir: Path) -> Image.Image:
    """A pinned plate, mapped onto its four canvas-fraction corners.

    Drawn in CANVAS coordinates rather than in the layer's box, which is the
    short way round to the engine's answer, not a different one: pinLocal walks
    each absolute corner BACKWARDS through the group's translate-rotate-flip so
    that the group can then re-apply it, which means the frame's rotation and
    flips cancel exactly and the pixels land on the absolute points the doc
    named. Going straight there skips one resample and one accounting error.
    `fit` has no meaning while pinned and is ignored — the quad IS the
    destination — which is the engine's rule too.

    engine.js draws this as a 12x12 grid of affine patches whose corners sit on
    the true homography, because a browser cannot cheaply do better. PIL can:
    this is the exact projective map the mesh approximates."""
    plate = _plate(layer, job_dir)
    quad = [(u * canvas_w, v * canvas_h) for u, v in layer.frame.corners]
    forward = _square_to_quad(quad)
    inverse = _invert3(forward)
    if inverse is None:
        raise RenderError(
            f"layer {layer.id!r} is pinned to four corners that do not form a "
            f"quadrilateral ({layer.frame.corners}) — three of them are "
            f"collinear, so there is no map from the plate onto them")
    # The inverse takes canvas pixels to unit-square coordinates; scaling its
    # first two rows by the plate's own size takes them to plate pixels, which
    # is the destination-to-source map PIL's PERSPECTIVE transform wants.
    src_w, src_h = plate.size
    scaled = (inverse[0] * src_w, inverse[1] * src_w, inverse[2] * src_w,
              inverse[3] * src_h, inverse[4] * src_h, inverse[5] * src_h,
              inverse[6], inverse[7], inverse[8])
    if abs(scaled[8]) < SQUARE_TO_QUAD_EPS:
        raise RenderError(
            f"layer {layer.id!r} is pinned to a degenerate quad "
            f"({layer.frame.corners})")
    coeffs = tuple(value / scaled[8] for value in scaled[:8])
    warped = plate.transform((canvas_w, canvas_h), Image.Transform.PERSPECTIVE,
                             coeffs, resample=_WARP_RESAMPLE,
                             fillcolor=(0, 0, 0, 0))
    # Outside the quad the projective map samples nonsense (and, past the
    # vanishing line, mirrors the plate), so the quad itself is the clip.
    mask = Image.new("L", (canvas_w, canvas_h), 0)
    ImageDraw.Draw(mask).polygon([(round(x), round(y)) for x, y in quad],
                                 fill=255)
    warped.putalpha(ImageChops.multiply(warped.getchannel("A"), mask))
    return warped


# Both go through docproof.cover.effects rather than being reimplemented here,
# which is a deliberate exception to this module's usual "mirror engine.js"
# habit. A mask's threshold and a grade's curve are not geometry — they are the
# same pixel math the composer runs when it builds a finished cover, and a
# canvas whose grade disagreed with the studio's would make every plate look
# one way in the editor and another in the delivered file. The geometry around
# them (which box, which order, which alpha) is this module's own and is
# mirrored in engine.js the usual way.
# The one adaptation is naming: effects.resolve_mask addresses its sources by
# ART SLOT id, and a canvas layer id is not required to be a legal slot id
# (`ly_ab12` happens to be, a layer somebody renamed may not). So references
# are rewritten to synthetic slot ids at the boundary and the real images are
# handed over under those names — full reuse, no id-shape coupling.

# An adjust layer's rotation and flips are inert: the graded region is the
# axis-aligned frame box. Documented as a constant so engine.js's mirror of
# this decision has something to point at, and so the "validated but inert"
# rule that governs every other cross-op field is stated once for geometry too.
ADJUST_BOX_IS_AXIS_ALIGNED = True


def _synthetic_mask(mask: Mask, drawn: dict[str, Image.Image]
                    ) -> tuple[CoverMaskSpec, dict[str, Image.Image]]:
    """One canvas Mask as the cover MaskSpec effects.resolve_mask takes, plus
    the slot-id-keyed pixel map to resolve it against.

    Names are rewritten (`ly_ab12` -> `s0`) rather than passed through: the
    cover model validates slot ids against its own slug rule, and a canvas
    layer id is only conventionally shaped. The rewrite is local to this call
    and nothing downstream ever sees either name."""
    names: dict[str, str] = {}
    pixels: dict[str, Image.Image] = {}

    def slot_for(ref: str) -> str:
        if ref not in names:
            names[ref] = f"s{len(names)}"
            pixels[names[ref]] = drawn[ref]
        return names[ref]

    gradient = None
    if mask.gradient is not None:
        gradient = CoverGradientMask(**mask.gradient.model_dump())
    return (CoverMaskSpec(
        from_layer=slot_for(mask.from_layer) if mask.from_layer else "",
        luminance_of=(slot_for(mask.luminance_of)
                      if mask.luminance_of else ""),
        gradient=gradient, invert=mask.invert), pixels)


def _mask_alpha(layer: Any, canvas: tuple[int, int],
                drawn: dict[str, Image.Image]) -> Image.Image | None:
    """A layer's resolved mask as one canvas-sized 'L' field, or None.

    A missing source is impossible for a document that validated (the
    model's earlier-layer rule), so a KeyError here would mean the caller
    skipped a layer it should have drawn — which is exactly why hidden
    layers are still rasterized when something masks through them."""
    mask = getattr(layer, "mask", None)
    if mask is None:
        return None
    spec, pixels = _synthetic_mask(mask, drawn)
    return cover_effects.resolve_mask(spec, canvas, pixels, {})


def _box_alpha(canvas: tuple[int, int], centre_x: float, centre_y: float,
               box_w: float, box_h: float) -> Image.Image | None:
    """An adjust layer's frame as an alpha rectangle, or None when the box
    covers the whole canvas.

    None rather than a field of 255 on purpose: a full-canvas adjust layer
    is the common case (it is what ingest writes for every §15.3 layer it
    translates), and handing effects.apply_adjust no mask at all takes its
    Image.blend path instead of building and compositing through a
    255-everywhere image."""
    width, height = canvas
    left, top = centre_x - box_w / 2.0, centre_y - box_h / 2.0
    right, bottom = left + box_w, top + box_h
    if left <= 0 and top <= 0 and right >= width and bottom >= height:
        return None
    box = Image.new("L", (width, height), 0)
    ImageDraw.Draw(box).rectangle(
        [round(left), round(top), round(right) - 1, round(bottom) - 1],
        fill=255)
    return box


def _apply_adjust(out: Image.Image, layer: AdjustLayer,
                  canvas: tuple[int, int], centre_x: float, centre_y: float,
                  box_w: float, box_h: float,
                  drawn: dict[str, Image.Image]) -> Image.Image:
    """One adjust layer over everything composited so far (§15.3's equation,
    run by effects.apply_adjust).

    Scoped by TWO alpha fields multiplied together — the layer's own mask,
    and its frame box — so a grade dragged over the left half of a cover and
    a grade clipped into a silhouette are the same mechanism. The op itself
    always computes over the FULL canvas image, never a crop, because every
    op's geometry is canvas-relative (a vignette's falloff, a blur's radius
    as a fraction of canvas height): cropping first would silently change
    what those numbers mean, and a designer who drags a vignette layer
    smaller expects a smaller window onto the same vignette, not a tighter
    one."""
    scope = _mask_alpha(layer, canvas, drawn)
    box = _box_alpha(canvas, centre_x, centre_y, box_w, box_h)
    if box is not None:
        scope = box if scope is None else ImageChops.multiply(scope, box)
    adjust = CoverAdjust(
        # The cover model validates this id as an art-slot slug and nothing
        # downstream of apply_adjust reads it; a canvas layer id is not
        # required to be one, so it is not offered. Same boundary rewrite
        # _synthetic_mask makes, for the same reason.
        id="a", op=layer.op, blend=layer.blend,
        brightness=layer.brightness, contrast=layer.contrast,
        saturation=layer.saturation, temperature=layer.temperature,
        stops=list(layer.stops), color=layer.color, strength=layer.strength,
        radius=layer.radius, threshold=layer.threshold)
    # A canvas document has no palette — ingest resolved every role to its
    # hex at the boundary — so this stands in only to satisfy the signature.
    # effects._resolve_color passes a literal hex straight through and never
    # reads it; the one field it could fall back to is `scrim`, and
    # AdjustLayer.color defaults to a real hex rather than "" so that path
    # is unreachable from here.
    palette = CoverPalette(background="#000000", primary="#000000",
                           accent="#000000", text="#000000", scrim="#000000")
    return cover_effects.apply_adjust(out, adjust, palette, canvas, scope,
                                      opacity=layer.opacity)


def _flip(layer: Any, tile: _Tile, box_w: float, box_h: float) -> _Tile:
    """The inner `flip` group's -1 scales. They mirror about the BOX centre,
    not about the tile's, so the tile's origin is reflected too — which is what
    keeps a shadow on a flipped layer on the correct side."""
    img, x, y = tile.img, tile.x, tile.y
    if layer.frame.flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        x = box_w - (x + img.width)
    if layer.frame.flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        y = box_h - (y + img.height)
    return _Tile(img, x, y)


def _place(out: Image.Image, tile: _Tile, box_w: float, box_h: float,
           centre_x: float, centre_y: float, rotation: float) -> None:
    """Composite one finished tile onto the canvas, rotated about the box
    centre.

    PIL rotates about a bitmap's own centre, and a tile is only centred on its
    box when nothing overhangs — so the tile is first grown symmetrically about
    the box centre, which makes the two centres the same point and the rotation
    the engine's. Rotation is degrees clockwise in the doc and counter-
    clockwise in PIL, hence the sign."""
    if abs(rotation) < 1e-9:
        _composite(out, tile.img,
                   centre_x - box_w / 2.0 + tile.x,
                   centre_y - box_h / 2.0 + tile.y)
        return
    local_x = box_w / 2.0 - tile.x
    local_y = box_h / 2.0 - tile.y
    half_w = max(local_x, tile.img.width - local_x)
    half_h = max(local_y, tile.img.height - local_y)
    centred = _blank(2 * half_w, 2 * half_h)
    _composite(centred, tile.img,
               centred.width / 2.0 - local_x, centred.height / 2.0 - local_y)
    spun = _rotate(centred, -rotation)
    _composite(out, spun, centre_x - spun.width / 2.0,
               centre_y - spun.height / 2.0)


def _content(layer: Any, box_w: float, box_h: float, canvas_w: int,
             canvas_h: int, job_dir: Path) -> _Tile:
    """One layer's own pixels, before anything is done TO them."""
    if isinstance(layer, ArtLayer):
        return _art_tile(layer, box_w, box_h, job_dir)
    if isinstance(layer, TextLayer):
        return _text_tile(layer, box_w, box_h, canvas_h)
    if isinstance(layer, ScrimLayer):
        return _scrim_tile(layer, box_w, box_h)
    if isinstance(layer, FrameLayer):
        return _frame_tile(layer, box_w, box_h, canvas_w)
    if isinstance(layer, ShapeLayer):
        return _shape_tile(layer, box_w, box_h, canvas_w)
    raise RenderError(
        f"layer {getattr(layer, 'id', '?')!r} is a "
        f"{getattr(layer, 'kind', type(layer).__name__)!r} layer, which this "
        f"renderer has never heard of")


def render(doc: CanvasDoc, job_dir: Path, *,
           width: int | None = None) -> Image.Image:
    """The document as one RGBA image, bottom layer to top.

    `width` renders at a different pixel width and keeps the document's aspect
    ratio; everything in a CanvasDoc is a fraction of the canvas, so a scaled
    render is the same picture and not a resampled one — the type is re-set and
    the plates re-fitted at the new size rather than a full-size render being
    shrunk. Omit it for the document's own reference size.

    `job_dir` is the cover job directory every art layer's `source` is resolved
    against, and the only place this reads from: no network, no vendor call, no
    document is opened that the doc did not name.

    Hidden layers are skipped, and so is `locked` — locking is about what the
    transform tools may touch, never about what draws. What comes back has no
    paper behind it (see the module docstring): flatten it onto white to match
    a browser export.
    """
    canvas_w = int(width) if width is not None else doc.canvas.w
    if canvas_w < 1:
        raise RenderError(
            f"a render width of {width!r} draws nothing — ask for at least "
            f"one pixel")
    canvas_h = max(1, round(doc.canvas.h * canvas_w / doc.canvas.w))
    out = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    job_dir = Path(job_dir)
    canvas = (canvas_w, canvas_h)

    # Which layers something else masks through. A mask source is rasterized
    # even when it is HIDDEN, and this set is how that is known before the
    # walk reaches it: a mask is a geometric relationship, not a visual one,
    # so hiding the title must not silently un-window the plate clipped into
    # it — the plate would go full-bleed over a cover somebody thought they
    # had masked, which is the loudest possible way for `visible` to have a
    # side effect nobody asked for. Its pixels are cached, never composited.
    referenced: set[str] = set()
    for layer in doc.layers:
        mask = getattr(layer, "mask", None)
        if mask is not None:
            referenced.update(r for r in (mask.from_layer, mask.luminance_of)
                              if r)
    # layer id -> its finished, canvas-placed RGBA. Only layers something
    # masks through are kept: on a cover with no masks this stays empty and
    # the walk below is the pre-mask walk, allocation for allocation.
    drawn: dict[str, Image.Image] = {}

    for layer in doc.layers:
        needed = layer.id in referenced
        if not layer.visible and not needed:
            continue
        frame = layer.frame
        box_w = max(MIN_BOX_FRACTION, frame.w) * canvas_w
        box_h = max(MIN_BOX_FRACTION, frame.h) * canvas_h

        if isinstance(layer, AdjustLayer):
            # Owns no pixels, so there is no tile to place, nothing to cache
            # for a mask to point at, and no effects stack to run: it grades
            # what is already on the canvas and hands back the new composite.
            if layer.visible:
                out = _apply_adjust(out, layer, canvas,
                                    frame.x * canvas_w, frame.y * canvas_h,
                                    box_w, box_h, drawn)
            continue

        # A layer that is masked, or that something else masks through, is
        # drawn onto its own canvas-sized sheet first; everything else keeps
        # compositing straight onto `out` exactly as it always did. The sheet
        # is what a mask multiplies into and what `drawn` caches, and it costs
        # one RGBA allocation — paid only by the layers that need it, so a
        # document with no masks renders through the original path untouched.
        own_mask = getattr(layer, "mask", None) is not None
        sheet = (Image.new("RGBA", canvas, (0, 0, 0, 0))
                 if own_mask or needed else None)
        target = out if sheet is None else sheet

        if frame.corners and layer.kind in PIN_KINDS:
            # A pinned plate carries no shadow: Konva sets the shadow on the
            # context BEFORE a sceneFunc runs, so every one of the mesh's cells
            # would cast its own and the plate would wear a grid of them. The
            # engine skips them; so does this. Levels still apply — they are a
            # filter on the cached group, not a shape property.
            pinned = _pinned_art(layer, canvas_w, canvas_h, job_dir)
            pinned = _levels(layer, pinned)
            _composite(target, _scale_alpha(pinned, layer.opacity), 0, 0)
        else:
            tile = _content(layer, box_w, box_h, canvas_w, canvas_h, job_dir)
            tile = _effects(layer, tile, canvas_w)
            tile.img = _scale_alpha(_levels(layer, tile.img), layer.opacity)
            tile = _flip(layer, tile, box_w, box_h)
            _place(target, tile, box_w, box_h,
                   frame.x * canvas_w, frame.y * canvas_h, frame.rotation)

        if sheet is None:
            continue
        if own_mask:
            sheet = cover_effects.apply_mask(
                sheet, _mask_alpha(layer, canvas, drawn))
        if needed:
            # Cached AFTER its own mask, so a layer masking through another
            # sees it as it actually appears rather than as it was before it
            # was windowed. It is also cached after opacity, which is the one
            # place that matters: resolve_mask thresholds a stencil at 50%, so
            # masking through a layer set below half opacity yields nothing.
            # That is the honest reading of "show through this layer" and it
            # is what the browser does too, but it is worth knowing.
            drawn[layer.id] = sheet
        if layer.visible:
            out = Image.alpha_composite(out, sheet)
    return out


__all__ = ["RenderError", "render"]
