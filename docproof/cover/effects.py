"""Cover Studio's pixel-effects bench: pure image math, no opinions.

The deep-stack wave (docs/cover_designer_spec.md §15) grows the composer
three capabilities that all reduce to the same kind of code — deterministic
Pillow arithmetic over already-positioned RGBA layers:

- the shared **blend-mode table** (§15.1) — one implementation that art
  layers, adjust layers, and (in a later PR) clipped effect overlays all
  dispatch through, replacing compose.py's old inline if/elif;
- **mask synthesis and resolution** (§15.2) — turning a MaskSpec into one
  canvas-sized 'L' image (multiply-combined sources, invert last) and
  applying it to a layer's alpha;
- the six **adjust-layer ops** (§15.3) — grade / gradient_map / color_wash /
  vignette / bloom / blur, each a small function over the current composite.

Layering rule, same as typeset.py's: this module imports docproof.cover.model
and NOTHING else from the cover package — never compose.py. compose.py owns
every opinion (when to run an op, what to measure afterwards, what to warn
about); this module owns only "given these pixels and these parameters,
produce exactly these pixels." Everything here is deterministic: no RNG, no
clock, no I/O — the same inputs produce the same bytes on every machine,
which is what lets compose()'s replay machinery (render_upto) call any of it
any number of times without drift, and what the golden-bytes back-compat
test leans on.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from PIL import (Image, ImageChops, ImageColor, ImageEnhance, ImageFilter,
                 ImageMath, ImageOps)

from .model import AdjustLayer, GradientMask, MaskSpec, Palette, PaletteRole

# Masks are smooth by definition — synthesize at quarter scale and Lanczos-
# upsample, the same generate-small-then-resize discipline compose.py's
# _GRAIN_SCALE applies to every smooth procedural texture (§15.2: "the
# _GRAIN_SCALE discipline"). The constant is repeated here rather than
# imported because compose.py is off-limits to this module (layering rule
# above); a divergence would only ever make masks slightly smoother or
# slightly cheaper, never wrong.
MASK_SCALE = 0.25

# §15.3's `temperature` warm/cool shift: a linear R+/B- point LUT, scaled so
# |temperature| = 1 moves each of R and B by exactly this many 8-bit steps.
# 24 is the implementer's-choice constant the spec asks to be documented: it
# is ~9% of full scale — a strong, clearly visible cast at the extreme, but
# still far from clipping midtones into flat orange/teal, and small enough
# that the default grade nudges (|t| ≈ 0.3, an ~7-step shift) read as
# "warmed," not "tinted."
TEMPERATURE_MAX_SHIFT = 24


# -- WCAG relative luminance --------------------------------------------------
#
# Moved here verbatim from compose.py (which now imports these back) so
# bloom's "relative luminance above which pixels glow" threshold and the
# composer's legibility autopilot measure brightness with the SAME curve —
# one implementation, per this module's whole reason to exist.

def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_SRGB_LUT = [round(srgb_to_linear(i / 255) * 255) for i in range(256)]


def luminance_band(rgb_img: Image.Image) -> Image.Image:
    """An 'L' image whose value at each pixel is that pixel's WCAG relative
    luminance (0-255 scale). Built from three point()-applied LUTs and two
    weighted Image.blend() calls — both pure-C Pillow operations — so this
    costs the same whether the crop is 100 or 100,000 pixels."""
    r, g, b = rgb_img.split()[:3]
    r_lin, g_lin, b_lin = r.point(_SRGB_LUT), g.point(_SRGB_LUT), b.point(_SRGB_LUT)
    # Image.blend(a, b, alpha) = a*(1-alpha) + b*alpha. Chaining two blends
    # with alphas derived from the WCAG weights (0.2126/0.7152/0.0722)
    # reaches the weighted sum without a three-way blend Pillow doesn't have.
    mixed = Image.blend(r_lin, g_lin, 0.7152 / (0.2126 + 0.7152))
    return Image.blend(mixed, b_lin, 0.0722)


# -- the blend-mode table (§15.1) ---------------------------------------------

def _color_dodge(base_rgb: Image.Image, source_rgb: Image.Image) -> Image.Image:
    """The "light pops" blend — glows, leaks, foil glints. No ImageChops op
    exists for it, so it runs per band through ImageMath (§15.1's exact
    formula): result = min(255, base * 256 / (256 - source)), where the
    division is integer division on 'I'-mode operands. `source` can never be
    256, so the denominator is always ≥ 1 — no division-by-zero branch
    needed. Cost is three band evals at canvas size: milliseconds."""
    bands = []
    for a_band, b_band in zip(base_rgb.split(), source_rgb.split()):
        bands.append(ImageMath.lambda_eval(
            lambda args: args["convert"](
                args["min"](args["a"] * 256 / (256 - args["b"]), 255), "L"),
            a=a_band, b=b_band))
    return Image.merge("RGB", tuple(bands))


# Every non-"normal" blend a spec may name (model.BLEND_MODES is the wire
# source of truth; a unit test asserts the two stay in lockstep), mapped to a
# function (base RGB, source RGB) -> blended RGB. "normal" is deliberately
# absent: it is not a pixel formula but plain alpha-over, handled by
# composite_layer directly. multiply/overlay/soft_light are the pre-wave
# trio, unchanged — the golden-bytes guarantee rides on them dispatching to
# the very same ImageChops functions as before. hue/color/luminosity are
# deferred with the Literal itself (see model.BLEND_MODES).
BLEND_TABLE = {
    "multiply": ImageChops.multiply,
    "overlay": ImageChops.overlay,
    "soft_light": ImageChops.soft_light,
    "screen": ImageChops.screen,
    "add": ImageChops.add,
    "lighten": ImageChops.lighter,
    "darken": ImageChops.darker,
    "color_dodge": _color_dodge,
}


def blend_rgb(base_rgb: Image.Image, source_rgb: Image.Image, mode: str) -> Image.Image:
    """`source_rgb` blended over `base_rgb` by `mode`'s table entry — the one
    dispatch point every blending caller shares. Raises KeyError for a mode
    not in the table (exactly what the old inline dict did): a CoverSpec that
    validated can never get here with one, so an unknown mode is a
    programming error, not a render-time surprise to paper over."""
    return BLEND_TABLE[mode](base_rgb, source_rgb)


def composite_layer(base: Image.Image, source: Image.Image, opacity: float,
                    blend: str) -> Image.Image:
    """Alpha-composite `source` (RGBA, canvas-sized) onto `base`. `normal`
    is a plain "over"; every other blend mode runs on RGB via the table
    against the CURRENT backdrop (so multiply/overlay/screen/… really do
    react to what's beneath them), then that blended result is composited
    back using `source`'s own alpha × `opacity` as the mix factor —
    otherwise a 6% "overlay" texture would fully replace the pixels under it
    instead of barely tinting them. (Moved verbatim from compose.py so art
    layers, adjust layers, and later clipped overlays share one
    implementation — §15.1.)"""
    if opacity <= 0:
        return base
    r, g, b, src_a = source.split()
    if blend == "normal":
        blended_rgb = Image.merge("RGB", (r, g, b))
    else:
        blended_rgb = blend_rgb(base.convert("RGB"), Image.merge("RGB", (r, g, b)), blend)
    if opacity < 1.0:
        src_a = src_a.point(lambda v: round(v * opacity))
    layer = Image.merge("RGBA", (*blended_rgb.split(), src_a))
    out = base.copy()
    out.alpha_composite(layer)
    return out


# -- mask synthesis & resolution (§15.2) --------------------------------------

def gradient_mask(gradient: GradientMask, canvas: tuple[int, int]) -> Image.Image:
    """One GradientMask as a canvas-sized 'L' image, 0 = fully masked out,
    255 = fully kept. Synthesized at MASK_SCALE and Lanczos-upsampled —
    gradients are smooth by definition, so quarter-scale synthesis is
    visually identical at a sixteenth of the per-pixel cost (the same
    reasoning as compose.py's grain/vignette painters).

    `linear`: alpha ramps along the direction vector (cos angle, sin angle)
    in y-down image coordinates, so the default angle=90 is the documented
    "top-transparent → bottom-opaque" and angle=0 ramps left→right. The ramp
    is normalized over the canvas's own projected extent, then remapped by
    `start`/`end` (alpha is 0 until `start`, 1.0 from `end` on — how a mask
    holds a plate fully solid for its lower third and fades only the top).

    `radial`: alpha ramps with distance from `center` (canvas fractions),
    reaching 1.0 at the farthest canvas corner — transparent core, opaque
    rim, the "grade only the outside" scope a designer's radial mask means.
    """
    cw, ch = canvas
    sw = max(1, round(cw * MASK_SCALE))
    sh = max(1, round(ch * MASK_SCALE))
    small = Image.new("L", (sw, sh))
    px = small.load()
    span = max(gradient.end - gradient.start, 1e-9)   # start<end validated; defensive
    corners = ((0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1))
    if gradient.kind == "linear":
        ux = math.cos(math.radians(gradient.angle))
        uy = math.sin(math.radians(gradient.angle))
        projections = [x * ux + y * uy for x, y in corners]
        p_min = min(projections)
        p_range = max(max(projections) - p_min, 1e-9)
        for y in range(sh):
            for x in range(sw):
                t = ((x * ux + y * uy) - p_min) / p_range
                frac = min(1.0, max(0.0, (t - gradient.start) / span))
                px[x, y] = round(255 * frac)
    else:   # "radial"
        cx = gradient.center[0] * (sw - 1)
        cy = gradient.center[1] * (sh - 1)
        max_dist = max(math.hypot(x - cx, y - cy) for x, y in corners) or 1.0
        for y in range(sh):
            for x in range(sw):
                t = math.hypot(x - cx, y - cy) / max_dist
                frac = min(1.0, max(0.0, (t - gradient.start) / span))
                px[x, y] = round(255 * frac)
    return small.resize(canvas, Image.Resampling.LANCZOS)


def resolve_mask(mask: MaskSpec, canvas: tuple[int, int],
                 art_pixels: Mapping[str, Image.Image],
                 text_ink: Mapping[str, Image.Image]) -> Image.Image | None:
    """One MaskSpec as a single canvas-sized 'L' image, or None when nothing
    resolves (validation makes that unreachable for a real spec — defensive,
    the same posture as compose's own dangling-reference guards). Sources,
    each optional, multiply-combined in documented order (multiplication
    commutes, so the order is documentation rather than semantics), with
    `invert` applied last — §15.2's combination rule exactly:

    - `from_layer`: the named art slot's POSITIONED alpha, hard-thresholded
      at 50% into a binary stencil — the *same* stencil math the legacy
      ArtSlot.mask_from path always used, which is what makes the
      model-level mask_from→mask fold byte-identical.
    - `gradient`: synthesized by gradient_mask above (soft by design).
    - `luminance_of`: the named art slot's positioned luminance ('L' of its
      RGB) gated by its own alpha, so a cutout's empty surround masks to 0
      rather than to whatever black the transparent RGB happens to hold.
    - `from_text` (§15.13): the named text slot's fitted glyph coverage,
      handed in by the caller (compose resolves text ink before positioning
      art — the occlusion guard already depends on that ordering).

    `art_pixels` maps slot id → positioned canvas-space RGBA; `text_ink`
    maps text slot id → canvas-sized 'L' glyph mask."""
    combined: Image.Image | None = None

    def fold_in(source: Image.Image) -> None:
        nonlocal combined
        combined = source if combined is None else ImageChops.multiply(combined, source)

    if mask.from_layer:
        ref = art_pixels.get(mask.from_layer)
        if ref is not None:
            fold_in(ref.getchannel("A").point(lambda v: 255 if v > 127 else 0))
    if mask.gradient is not None:
        fold_in(gradient_mask(mask.gradient, canvas))
    if mask.luminance_of:
        ref = art_pixels.get(mask.luminance_of)
        if ref is not None:
            fold_in(ImageChops.multiply(ref.convert("L"), ref.getchannel("A")))
    if mask.from_text:
        ink = text_ink.get(mask.from_text)
        if ink is not None:
            fold_in(ink)
    if combined is None:
        return None
    return ImageChops.invert(combined) if mask.invert else combined


def apply_mask(img: Image.Image, mask_img: Image.Image | None) -> Image.Image:
    """`img` (canvas-sized RGBA) with its alpha multiplied by `mask_img` —
    the one way a resolved mask ever touches a pixel-owning layer. The
    layer's own soft edges (its anti-aliasing, its treatment) survive
    wherever the mask allows them through, exactly the multiply semantics
    the legacy mask_from path established. None passes through untouched."""
    if mask_img is None:
        return img
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r, g, b, ImageChops.multiply(a, mask_img)))


# -- adjust-layer ops (§15.3) -------------------------------------------------

def _resolve_color(value: str, palette: Palette, default_role: PaletteRole) -> str:
    """An AdjustLayer color reference as a #rrggbb hex: "" falls back to
    `default_role`, a PaletteRole name reads the palette, and anything else
    already validated as a literal hex passes through. This is the only
    color indirection the adjust ops do — every choice stays visible in the
    spec, per the palette's own "point here by role" doctrine."""
    if not value:
        return palette.get(default_role)
    role_names = {role.value for role in PaletteRole}
    return palette.get(value) if value in role_names else value


def _with_alpha_of(base: Image.Image, rgb: Image.Image) -> Image.Image:
    """`rgb` re-merged with `base`'s own alpha channel, untouched. Every
    adjust op works on the RGB bands only and passes alpha straight through:
    an adjust layer grades what the composite LOOKS like, never how much of
    it exists — and ImageEnhance's degenerate images would otherwise fade
    alpha along with color on an RGBA input."""
    return Image.merge("RGBA", (*rgb.split(), base.getchannel("A")))


def _op_grade(base: Image.Image, adjust: AdjustLayer, palette: Palette,
              canvas: tuple[int, int]) -> Image.Image:
    """brightness/contrast/saturation via ImageEnhance at factor 1+v, then
    `temperature` as a linear R+/B- point LUT (±TEMPERATURE_MAX_SHIFT 8-bit
    steps at |1|). Applied in that fixed, documented order — enhancement
    factors do not commute with the LUT, so the order IS part of the op's
    definition. Zero-valued params are skipped entirely rather than applied
    at their identity factor, so a default-grade adjust layer is a true
    no-op on the pixels."""
    rgb = base.convert("RGB")
    if adjust.brightness:
        rgb = ImageEnhance.Brightness(rgb).enhance(1.0 + adjust.brightness)
    if adjust.contrast:
        rgb = ImageEnhance.Contrast(rgb).enhance(1.0 + adjust.contrast)
    if adjust.saturation:
        rgb = ImageEnhance.Color(rgb).enhance(1.0 + adjust.saturation)
    if adjust.temperature:
        shift = round(adjust.temperature * TEMPERATURE_MAX_SHIFT)
        r, g, b = rgb.split()
        r = r.point(lambda v: max(0, min(255, v + shift)))
        b = b.point(lambda v: max(0, min(255, v - shift)))
        rgb = Image.merge("RGB", (r, g, b))
    return _with_alpha_of(base, rgb)


def _op_gradient_map(base: Image.Image, adjust: AdjustLayer, palette: Palette,
                     canvas: tuple[int, int]) -> Image.Image:
    """The single most cover-defining move in the wave (§15.3): the
    composite's luminance re-inked through a 2- or 3-stop ramp —
    whole-composite duotones, teal-orange, sepia. Plain .convert("L") is the
    luminance here (the spec's own formula), not the WCAG band: colorize is
    a look, not a measurement. Stop count 2-3 is enforced at validation."""
    stops = [_resolve_color(s, palette, PaletteRole.text) for s in adjust.stops]
    gray = base.convert("L")
    if len(stops) == 2:
        mapped = ImageOps.colorize(gray, black=stops[0], white=stops[1])
    else:
        mapped = ImageOps.colorize(gray, black=stops[0], white=stops[2], mid=stops[1])
    return _with_alpha_of(base, mapped)


def _op_vignette(base: Image.Image, adjust: AdjustLayer, palette: Palette,
                 canvas: tuple[int, int]) -> Image.Image:
    """Full-canvas radial shade toward `color` ("" = the scrim role),
    `strength`-scaled: the same 0-at-center → strength-at-corner ramp math
    compose's vignette scrim paints, rebuilt here at canvas scope (the
    layering rule bars importing it) with the same quarter-resolution
    synthesis. The ramp drives an RGB composite toward the solid color, so
    the canvas center stays essentially untouched (a few 8-bit steps of
    quantization from the quarter-scale synthesis, same as the scrim's own
    painter) and alpha passes through."""
    cw, ch = canvas
    rgb_color = ImageColor.getrgb(_resolve_color(adjust.color, palette, PaletteRole.scrim))
    sw, sh = max(1, cw // 4), max(1, ch // 4)
    small = Image.new("L", (sw, sh))
    px = small.load()
    cx, cy = sw / 2, sh / 2
    max_dist = math.hypot(cx, cy) or 1.0
    for y in range(sh):
        for x in range(sw):
            d = math.hypot(x - cx, y - cy) / max_dist
            px[x, y] = round(255 * adjust.strength * min(1.0, d))
    ramp = small.resize((cw, ch), Image.Resampling.BILINEAR)
    solid = Image.new("RGB", (cw, ch), rgb_color)
    return _with_alpha_of(base, Image.composite(solid, base.convert("RGB"), ramp))


def _op_bloom(base: Image.Image, adjust: AdjustLayer, palette: Palette,
              canvas: tuple[int, int]) -> Image.Image:
    """The "it's lit, not flat" op: keep only pixels whose WCAG relative
    luminance clears `threshold`, Gaussian-blur that glow field at `radius`
    (a fraction of canvas height, like every size in a spec), scale it by
    `strength`, and screen it back over the composite. A composite with
    nothing above the threshold blooms into nothing — screening pure black
    is the identity, so the op degrades to a no-op instead of inventing
    light that isn't there."""
    rgb = base.convert("RGB")
    band = luminance_band(rgb)
    cut = round(adjust.threshold * 255)
    glow = band.point(lambda v: v if v >= cut else 0)
    glow = glow.filter(ImageFilter.GaussianBlur(adjust.radius * canvas[1]))
    if adjust.strength < 1.0:
        glow = glow.point(lambda v: round(v * adjust.strength))
    glow_rgb = Image.merge("RGB", (glow, glow, glow))
    return _with_alpha_of(base, ImageChops.screen(rgb, glow_rgb))


def _op_blur(base: Image.Image, adjust: AdjustLayer, palette: Palette,
             canvas: tuple[int, int]) -> Image.Image:
    """GaussianBlur at `radius` × canvas height. On its own this is soft
    focus; selected through a gradient mask by apply_adjust's normal mixing
    path (Image.composite(blurred, composite, m)) it is depth-of-field —
    §15.3 names that exact composition, and the generic mask×opacity mix
    below IS its implementation, no special case needed."""
    blurred = base.convert("RGB").filter(
        ImageFilter.GaussianBlur(adjust.radius * canvas[1]))
    return _with_alpha_of(base, blurred)


_ADJUST_OPS = {
    "grade": _op_grade,
    "gradient_map": _op_gradient_map,
    "vignette": _op_vignette,
    "bloom": _op_bloom,
    "blur": _op_blur,
    # "color_wash" is deliberately absent: it is not an op OVER the
    # composite but a solid fill composited AS a layer through the full
    # blend table — apply_adjust branches to composite_layer for it before
    # ever consulting this dict (§15.3's one exception, verbatim).
}


def apply_adjust(base: Image.Image, adjust: AdjustLayer, palette: Palette,
                 canvas: tuple[int, int], mask_img: Image.Image | None = None,
                 *, opacity: float | None = None) -> Image.Image:
    """One adjust layer applied to the current composite (§15.3's exact
    equation): result = composite × (1 − m·opacity) + op(composite) ×
    (m·opacity) — realized as Image.composite through the opacity-scaled
    mask (or Image.blend when there is no mask, which is the same equation
    with m ≡ 1). `color_wash` instead composites a solid fill of `color`
    as a layer through the full blend table, its alpha carrying the mask.

    `mask_img` is the already-resolved 'L' mask (resolve_mask), or None.
    `opacity` overrides adjust.opacity when given — the hook compose()'s
    finishing-attenuation ladder (§15.7) uses to halve an fx_ layer without
    ever mutating the spec it was handed."""
    effective = adjust.opacity if opacity is None else opacity
    if effective <= 0:
        return base
    if adjust.op == "color_wash":
        rgb = ImageColor.getrgb(_resolve_color(adjust.color, palette, PaletteRole.scrim))
        solid = Image.new("RGBA", base.size, (*rgb, 255))
        if mask_img is not None:
            solid.putalpha(mask_img)
        return composite_layer(base, solid, effective, adjust.blend)
    op_result = _ADJUST_OPS[adjust.op](base, adjust, palette, canvas)
    if mask_img is None:
        if effective >= 1.0:
            return op_result
        return Image.blend(base, op_result, effective)
    if effective < 1.0:
        mask_img = mask_img.point(lambda v: round(v * effective))
    return Image.composite(op_result, base, mask_img)


__all__ = [
    "BLEND_TABLE", "MASK_SCALE", "TEMPERATURE_MAX_SHIFT",
    "apply_adjust", "apply_mask", "blend_rgb", "composite_layer",
    "gradient_mask", "luminance_band", "resolve_mask", "srgb_to_linear",
]
