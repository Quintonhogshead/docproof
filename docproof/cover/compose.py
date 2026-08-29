"""Cover Studio's composer: CoverSpec + generated assets -> pixels, every
time the same way. This is the module that makes the spec/asset split in
docproof.cover.model actually pay off — `compose()` has no side channel to
the outside world (no network, no clock, no randomness but one fixed seed),
so archiving a CoverSpec plus its job_dir's assets really does mean the
render can be reproduced forever (docs/cover_designer_spec.md §1, §7.3).

The one piece of real judgment in here is the legibility autopilot: before
each text slot is drawn, sample the composite it is about to sit on, and if
the palette's chosen text color would not read against it, escalate that
slot's protecting scrim, then — failing that — flip to whichever of two
fixed ink colors contrasts better. That decision needs the CURRENT canvas,
which is why it lives here and not in typeset.py (measurement, fitting, and
drawing pixels — no opinions about color).

Escalating a scrim after some of the canvas has already been painted is the
one place this module works backwards from how you'd naively write it:
rather than mutate the canvas in place and try to "undo" a weaker scrim
before repainting it stronger, `compose()` replays `spec.layers` from
scratch through `render_upto()` every time a strength changes. That is
wasteful in the abstract — a handful of extra full-canvas composites per
render — but at these canvas sizes it is milliseconds, and "just render it
again" is a lot easier to trust than incremental patching of a raster.
"""
from __future__ import annotations

import colorsys
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageStat

from . import typeset
from .archetypes import zone_px
from .imaging import has_real_alpha
from .model import (ArtSlot, CoverSpec, LayerRef, Palette, RenderReport,
                    ScrimSpec, Shadow, TextSlot)

log = logging.getLogger("docproof.cover.compose")

# Canvas constants (§4): the ebook front-cover target, KDP-ideal 1:1.6.
EBOOK_W, EBOOK_H = 1600, 2560

# Thumbnail widths — the Amazon-search-result legibility check (100px) and
# the contact-sheet card display (300px). Height follows from the aspect
# ratio, so these are the only numbers a print-wrap canvas would need to
# revisit.
THUMB_LARGE = 300
THUMB_SMALL = 100

# -- legibility autopilot tuning (§7.3) --------------------------------------

_CONTRAST_THRESHOLDS = {"title": 4.5, "subtitle": 3.0, "author": 4.5, "series": 3.0}
_SCRIM_STEP = 0.15
_SCRIM_CAP = 0.85
_STDDEV_SHADOW_THRESHOLD = 0.22
_SCRIM_PAD_FRACTION = 0.04          # of canvas, on every side of a derived scrim zone
_TEXT_COLOR_FALLBACKS = ("#111111", "#f5f1e8")   # near-black / warm parchment

# -- procedural art tuning ----------------------------------------------------

# Any fixed integer works here; what matters is that it never changes, so
# the "no RNG but the grain" promise (§7.3's determinism requirement) holds
# across processes, machines, and time.
_GRAIN_SEED = 20260828
_GRAIN_SCALE = 0.25                  # generate at 25% and upsample (§7.3)
_GRADIENT_LIGHTNESS_SHIFT = 0.10     # the background gradient's 2nd stop


class ComposeError(Exception):
    """A spec that cannot be rendered as given — currently only an art asset
    on disk that will not decode. Carries a sentence for a person, the same
    convention as docproof.cover.archetypes.ArchetypeError."""


@dataclass(frozen=True)
class _ResolvedText:
    """One text slot's final answer from the legibility autopilot, cached so
    `render_upto()` can redraw an already-decided slot identically on every
    replay without re-running its fit search or its contrast measurement."""
    slot: TextSlot
    fit: typeset.FitResult
    color: str
    shadow: Shadow | None


def compose(spec: CoverSpec, job_dir: Path,
           canvas: tuple[int, int] = (EBOOK_W, EBOOK_H)
           ) -> tuple[Image.Image, RenderReport]:
    """Render one CoverSpec deterministically. Walks `spec.layers` bottom-up;
    for each text slot, measures the composite beneath it and runs the
    legibility autopilot (escalate its protecting scrim, then flip its ink
    color, then fall back to a default shadow if the backdrop is busy)
    before drawing it — see the module docstring for why that means
    replaying earlier layers rather than patching them in place.

    Raises ComposeError only when an art slot names an asset that cannot be
    read; every other input (however extreme) renders *something* rather
    than raising, because a designer iterating on notes needs a cover back,
    not a stack trace."""
    job_dir = Path(job_dir)
    text_by_id = {t.id: t for t in spec.text}
    art_by_id = {a.id: a for a in spec.art}
    strengths = {i: s.strength for i, s in enumerate(spec.scrims)}
    finalized: dict[str, _ResolvedText] = {}

    contrast: dict[str, float] = {}
    fitted_sizes: dict[str, float] = {}
    warnings: list[str] = []
    layers = _degrade_opaque_focal(spec.layers, art_by_id, job_dir, warnings)

    def render_upto(stop: int) -> Image.Image:
        """Everything in `layers[:stop]`, freshly composited: art at its
        always-deterministic pixels, scrims at their CURRENT (possibly
        escalated) strength, and any text slot already resolved drawn
        exactly as it was the first time. Never includes the text slot AT
        `stop` — the caller measures this result to decide how to draw it."""
        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        for layer in layers[:stop]:
            if layer.kind == "art":
                img = _apply_art(img, art_by_id[layer.ref], job_dir, canvas, spec.palette)
            elif layer.kind == "scrim":
                idx = int(layer.ref)
                img = _apply_scrim(img, spec.scrims[idx], strengths[idx],
                                   text_by_id, spec.palette, canvas)
            else:  # "text"
                resolved = finalized.get(layer.ref)
                if resolved is not None:
                    img = typeset.draw_text(img, resolved.slot, resolved.fit,
                                            resolved.color, resolved.shadow, canvas)
        return img

    for i, layer in enumerate(layers):
        if layer.kind != "text":
            continue
        slot = text_by_id[layer.ref]
        if slot.optional and not slot.content.strip():
            continue

        zone_left, zone_top, zone_w_px, zone_h_px = zone_px(slot.zone, canvas)
        rect = (zone_left, zone_top, zone_left + zone_w_px, zone_top + zone_h_px)
        threshold = _CONTRAST_THRESHOLDS[slot.id]
        protecting = [idx for idx, s in enumerate(spec.scrims) if s.protects == slot.id]
        color_hex = spec.palette.get(slot.color_role)

        below = render_upto(i)
        mean_lum, stddev = _zone_stats(below, rect)
        ratio = _contrast_against_luminance(ImageColor.getrgb(color_hex), mean_lum)

        while ratio < threshold and any(strengths[idx] < _SCRIM_CAP for idx in protecting):
            for idx in protecting:
                strengths[idx] = min(_SCRIM_CAP, round(strengths[idx] + _SCRIM_STEP, 10))
            below = render_upto(i)
            mean_lum, stddev = _zone_stats(below, rect)
            ratio = _contrast_against_luminance(ImageColor.getrgb(color_hex), mean_lum)

        if ratio < threshold:
            pre_flip_ratio = ratio
            options = [(c, _contrast_against_luminance(ImageColor.getrgb(c), mean_lum))
                      for c in _TEXT_COLOR_FALLBACKS]
            color_hex, ratio = max(options, key=lambda pair: pair[1])
            outcome = "now passes" if ratio >= threshold else "still falls short"
            warnings.append(
                f"{slot.id}: contrast was {pre_flip_ratio:.2f} against "
                f"threshold {threshold} even after scrim escalation; "
                f"flipped text color to {color_hex}, which {outcome} "
                f"({ratio:.2f}).")

        shadow = slot.shadow
        if stddev > _STDDEV_SHADOW_THRESHOLD and shadow is None:
            shadow = Shadow()

        fit = typeset.fit_text(slot, canvas)
        if fit.warning:
            warnings.append(fit.warning)

        finalized[layer.ref] = _ResolvedText(slot=slot, fit=fit, color=color_hex, shadow=shadow)
        contrast[layer.ref] = round(ratio, 4)
        fitted_sizes[layer.ref] = fit.size_frac

    final_image = render_upto(len(layers))

    # Safety net: the autopilot measured each slot against what was BENEATH
    # it at draw time; a later non-text layer (a focal cutout, say) can
    # still bury text it never saw. Re-measure against the finished stack of
    # art + scrims WITHOUT the text layers — measuring the text's own pixels
    # would bias the zone toward the ink color and cry wolf on every clean
    # cover — and say so out loud when a slot lost meaningful contrast.
    ground = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for layer in layers:
        if layer.kind == "art":
            ground = _apply_art(ground, art_by_id[layer.ref], job_dir, canvas,
                                spec.palette)
        elif layer.kind == "scrim":
            idx = int(layer.ref)
            ground = _apply_scrim(ground, spec.scrims[idx], strengths[idx],
                                  text_by_id, spec.palette, canvas)
    for ref, resolved in finalized.items():
        zl, zt, zw, zh = zone_px(resolved.slot.zone, canvas)
        final_lum, _ = _zone_stats(ground, (zl, zt, zl + zw, zt + zh))
        final_ratio = _contrast_against_luminance(
            ImageColor.getrgb(resolved.color), final_lum)
        threshold = _CONTRAST_THRESHOLDS[resolved.slot.id]
        if final_ratio < threshold and final_ratio < contrast[ref] - 0.5:
            warnings.append(
                f"{ref}: a layer drawn later covers this text — contrast on "
                f"the finished cover is {final_ratio:.2f} (threshold "
                f"{threshold}, was {contrast[ref]:.2f} at draw time).")

    report = RenderReport(
        contrast=contrast,
        scrim_final={idx: strengths[idx] for idx in range(len(spec.scrims))},
        fitted_sizes=fitted_sizes,
        warnings=warnings)
    return final_image.convert("RGB"), report


def save_renders(image: Image.Image, job_dir: Path, version: int, concept: int) -> list[str]:
    """Write the four on-disk artifacts one render produces — full PNG, full
    JPG (quality 90), and the two search-result/card thumbnails — under
    `job_dir/renders/`, and return their paths relative to `job_dir` as
    posix strings, in that order (PNG, JPG, 300px thumb, 100px thumb)."""
    job_dir = Path(job_dir)
    out_dir = job_dir / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v{version}_c{concept}"
    rgb = image.convert("RGB") if image.mode != "RGB" else image

    png_path = out_dir / f"{stem}.png"
    jpg_path = out_dir / f"{stem}.jpg"
    thumb300_path = out_dir / f"{stem}_thumb300.png"
    thumb100_path = out_dir / f"{stem}_thumb100.png"

    rgb.save(png_path, format="PNG")
    rgb.save(jpg_path, format="JPEG", quality=90)
    _thumbnail(rgb, THUMB_LARGE).save(thumb300_path, format="PNG")
    _thumbnail(rgb, THUMB_SMALL).save(thumb100_path, format="PNG")

    return [p.relative_to(job_dir).as_posix()
           for p in (png_path, jpg_path, thumb300_path, thumb100_path)]


def _thumbnail(image: Image.Image, width: int) -> Image.Image:
    w, h = image.size
    height = max(1, round(h * (width / w)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


# -- the cutout_sandwich fallback (§5.2.3) -----------------------------------

def _degrade_opaque_focal(layers: list[LayerRef], art_by_id: dict[str, ArtSlot],
                          job_dir: Path, warnings: list[str]) -> list[LayerRef]:
    """One narrow, spec-mandated exception to "z-order is exactly
    `spec.layers`": a `contain`-fit art slot that asked for a transparent
    cutout (`transparent=True`) but whose generated asset came back without
    real alpha swaps places with the text layer immediately before it, so an
    opaque "focal" image never fully occludes the title it was meant to
    overlap. docproof.cover.imaging.has_real_alpha is the same border-alpha
    check imaging.py's own docstring says feeds this — re-checked here
    against the actual asset on disk (not a flag threaded through the spec)
    because compose() is a pure function of spec + job_dir and should not
    have to trust that some upstream step already got this right.

    Returns a new list; `layers`/`spec.layers` is never mutated."""
    out = list(layers)
    for i, layer in enumerate(out):
        if layer.kind != "art" or i == 0 or out[i - 1].kind != "text":
            continue
        slot = art_by_id.get(layer.ref)
        if slot is None or not slot.transparent or slot.fit != "contain" or not slot.asset:
            continue
        try:
            opaque = not has_real_alpha((job_dir / slot.asset).read_bytes())
        except Exception:  # noqa: BLE001 - any failure here is the same story
            continue   # unreadable — _apply_art raises ComposeError for real later
        if not opaque:
            continue
        text_ref = out[i - 1].ref
        out[i - 1], out[i] = out[i], out[i - 1]
        warnings.append(
            f"{slot.id}: the generated art came back without real "
            f"transparency; drew '{text_ref}' on top of it instead of "
            f"underneath, so the text stays visible.")
    return out


# -- art layers ---------------------------------------------------------------

def _apply_art(base: Image.Image, slot: ArtSlot, job_dir: Path,
               canvas: tuple[int, int], palette: Palette) -> Image.Image:
    """Load a generated asset, or synthesize one procedurally when the slot
    has none — §7.3's "reads generated art when set, synthesizes otherwise"
    rule, checked on `asset` alone (not `prompt`): by the time a real job
    reaches compose(), pipeline.py has already generated everything with a
    prompt, so an empty `asset` here always means "procedural on purpose"
    (the $0 big_type background, the grain texture) or "no art for this
    slot" (an ungenerated `focal`, which draws nothing rather than invent a
    fallback the spec never described)."""
    if slot.asset:
        path = job_dir / slot.asset
        try:
            source = Image.open(path)
            source.load()
            source = source.convert("RGBA")
        except Exception as e:  # noqa: BLE001 - any decode failure is the same story
            raise ComposeError(
                f"Could not read the generated art for the '{slot.id}' slot "
                f"at {path}: {e}") from e
    else:
        source = _procedural_art(slot, canvas, palette)
        if source is None:
            return base

    if slot.fit == "cover":
        positioned = _fit_cover(source, canvas, slot.anchor, slot.scale, slot.offset)
    else:
        positioned = _fit_contain(source, canvas, slot.anchor, slot.scale, slot.offset)
    return _composite_layer(base, positioned, slot.opacity, slot.blend)


def _procedural_art(slot: ArtSlot, canvas: tuple[int, int],
                    palette: Palette) -> Image.Image | None:
    if slot.id == "texture":
        return _grain_layer(canvas)
    if slot.id == "background":
        return _gradient_layer(canvas, palette.background)
    return None   # e.g. an ungenerated "focal" cutout: nothing to draw


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


def _fit_cover(img: Image.Image, canvas: tuple[int, int],
              anchor: tuple[float, float], scale: float,
              offset: tuple[float, float]) -> Image.Image:
    """Scale to fill `canvas` completely (the larger of the two ratios), then
    crop to exactly `canvas`, keeping `anchor` — the fraction of the SOURCE
    that should stay in frame — visible. `scale` zooms in further after the
    fill; `offset` nudges the crop window by a fraction of the canvas."""
    cw, ch = canvas
    iw, ih = img.size
    fill_scale = max(cw / iw, ch / ih) * max(scale, 1e-6)
    new_w = max(1, round(iw * fill_scale))
    new_h = max(1, round(ih * fill_scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    ax, ay = anchor
    ox, oy = offset
    left = round((new_w - cw) * ax - ox * cw)
    top = round((new_h - ch) * ay - oy * ch)
    left = max(0, min(left, max(0, new_w - cw)))
    top = max(0, min(top, max(0, new_h - ch)))
    cropped = resized.crop((left, top, left + cw, top + ch))
    if cropped.size == (cw, ch):
        return cropped
    # A pathological scale/offset combination left the crop short of the
    # canvas on some edge — pad onto a transparent canvas rather than crash;
    # _composite_layer still gets a canvas-sized RGBA image either way.
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(cropped, dest=(0, 0))
    return out


def _fit_contain(img: Image.Image, canvas: tuple[int, int],
                 anchor: tuple[float, float], scale: float,
                 offset: tuple[float, float]) -> Image.Image:
    """Scale to fit WITHIN `canvas` (the smaller ratio) preserving aspect
    ratio, then place it against the full canvas using `anchor` as the
    alignment point (0,0 = flush top-left, 1,1 = flush bottom-right, 0.5,1 =
    bottom-center — how the cutout_sandwich focal figure sits at the
    canvas's bottom edge) and `offset` as a further fractional nudge. Used
    for `focal` slots, which the model carries no independent zone for
    (§7.3): "position focal slots via anchor+scale+offset against the full
    canvas.\""""
    cw, ch = canvas
    iw, ih = img.size
    fit_scale = min(cw / iw, ch / ih) * max(scale, 1e-6)
    new_w = max(1, round(iw * fit_scale))
    new_h = max(1, round(ih * fit_scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    ax, ay = anchor
    ox, oy = offset
    dest_x = round(ax * (cw - new_w) + ox * cw)
    dest_y = round(ay * (ch - new_h) + oy * ch)
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(resized, dest=(dest_x, dest_y))
    return out


_BLEND_CHOPS = {"multiply": ImageChops.multiply, "overlay": ImageChops.overlay,
                "soft_light": ImageChops.soft_light}


def _composite_layer(base: Image.Image, source: Image.Image, opacity: float,
                     blend: str) -> Image.Image:
    """Alpha-composite `source` (RGBA, canvas-sized) onto `base`. `normal`
    is a plain "over"; the other three blend modes run on RGB via
    ImageChops against the CURRENT backdrop (so multiply/overlay/soft_light
    really do react to what's beneath them), then that blended result is
    composited back using `source`'s own alpha × `opacity` as the mix
    factor — otherwise a 6% "overlay" texture would fully replace the pixels
    under it instead of barely tinting them."""
    if opacity <= 0:
        return base
    r, g, b, src_a = source.split()
    if blend == "normal":
        blended_rgb = Image.merge("RGB", (r, g, b))
    else:
        chop = _BLEND_CHOPS[blend]
        blended_rgb = chop(base.convert("RGB"), Image.merge("RGB", (r, g, b)))
    if opacity < 1.0:
        src_a = src_a.point(lambda v: round(v * opacity))
    layer = Image.merge("RGBA", (*blended_rgb.split(), src_a))
    out = base.copy()
    out.alpha_composite(layer)
    return out


# -- scrims -------------------------------------------------------------------

def _scrim_rect(scrim: ScrimSpec, text_by_id: dict[str, TextSlot],
                canvas: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """The scrim's rectangle in px, or None when it protects nothing
    resolvable (no explicit zone and either no `protects` or a `protects`
    naming a slot this archetype doesn't have — defensive; build_spec never
    produces this, but a hand-edited/revised spec could)."""
    cw, ch = canvas
    if scrim.zone is not None:
        left, top, w, h = zone_px(scrim.zone, canvas)
        return left, top, left + w, top + h
    if scrim.protects is None:
        return None
    slot = text_by_id.get(scrim.protects)
    if slot is None:
        return None
    left, top, w, h = zone_px(slot.zone, canvas)
    pad_x = round(_SCRIM_PAD_FRACTION * cw)
    pad_y = round(_SCRIM_PAD_FRACTION * ch)
    return (max(0, left - pad_x), max(0, top - pad_y),
           min(cw, left + w + pad_x), min(ch, top + h + pad_y))


def _apply_scrim(base: Image.Image, scrim: ScrimSpec, strength: float,
                 text_by_id: dict[str, TextSlot], palette: Palette,
                 canvas: tuple[int, int]) -> Image.Image:
    if strength <= 0:
        return base
    rect = _scrim_rect(scrim, text_by_id, canvas)
    if rect is None:
        return base
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        return base
    rgb = ImageColor.getrgb(palette.get(scrim.color_role))
    overlay = Image.new("RGBA", canvas, (0, 0, 0, 0))
    if scrim.kind == "panel":
        block = Image.new("RGBA", (right - left, bottom - top),
                          (*rgb, round(255 * strength)))
        overlay.alpha_composite(block, dest=(left, top))
    elif scrim.kind in ("gradient_down", "gradient_up"):
        _paint_gradient_scrim(overlay, scrim.kind, rgb, strength,
                              left, top, right, bottom, canvas)
    elif scrim.kind == "vignette":
        _paint_vignette_scrim(overlay, rgb, strength, left, top, right, bottom)
    out = base.copy()
    out.alpha_composite(overlay)
    return out


def _paint_gradient_scrim(overlay: Image.Image, kind: str, rgb: tuple[int, int, int],
                          strength: float, left: int, top: int, right: int,
                          bottom: int, canvas: tuple[int, int]) -> None:
    """A vertical alpha ramp across the (already 4%-padded) scrim rectangle —
    0 at the leading edge to `strength` at the trailing edge, so the actual
    text zone always reaches full protecting strength — then a SOLID
    `strength` fill continuing from the trailing edge to the nearest canvas
    edge, so there is no visible seam between "protected" and "unprotected"
    art before the page actually ends (§7.3: "extended to the nearest canvas
    edge below/above\")."""
    cw, ch = canvas
    h = bottom - top
    col = Image.new("L", (1, max(1, h)))
    px = col.load()
    for y in range(h):
        t = y / (h - 1) if h > 1 else 1.0
        frac = t if kind == "gradient_down" else (1.0 - t)
        px[0, y] = round(255 * strength * frac)
    alpha = col.resize((right - left, h), Image.Resampling.NEAREST)
    block = Image.new("RGBA", (right - left, h), (*rgb, 0))
    block.putalpha(alpha)
    overlay.alpha_composite(block, dest=(left, top))
    if kind == "gradient_down" and bottom < ch:
        ext = Image.new("RGBA", (right - left, ch - bottom), (*rgb, round(255 * strength)))
        overlay.alpha_composite(ext, dest=(left, bottom))
    if kind == "gradient_up" and top > 0:
        ext = Image.new("RGBA", (right - left, top), (*rgb, round(255 * strength)))
        overlay.alpha_composite(ext, dest=(left, 0))


def _paint_vignette_scrim(overlay: Image.Image, rgb: tuple[int, int, int],
                          strength: float, left: int, top: int, right: int,
                          bottom: int) -> None:
    """A radial ramp from 0 at the rectangle's center to `strength` at its
    corner distance. Computed at a downsampled resolution and upscaled
    (like the grain texture) so cost is bounded by the ~quarter-resolution
    pixel count rather than the full rectangle — no shipped archetype uses
    `vignette`, so this path is not performance-critical, but there is no
    reason to let it scale quadratically with canvas size either."""
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return
    sw, sh = max(1, w // 4), max(1, h // 4)
    small = Image.new("L", (sw, sh))
    px = small.load()
    cx, cy = sw / 2, sh / 2
    max_dist = math.hypot(cx, cy) or 1.0
    for y in range(sh):
        for x in range(sw):
            d = math.hypot(x - cx, y - cy) / max_dist
            px[x, y] = round(255 * strength * min(1.0, d))
    alpha = small.resize((w, h), Image.Resampling.BILINEAR)
    block = Image.new("RGBA", (w, h), (*rgb, 0))
    block.putalpha(alpha)
    overlay.alpha_composite(block, dest=(left, top))


# -- legibility autopilot: measurement ---------------------------------------

# WCAG relative-luminance sRGB->linear lookup, precomputed once as an 8-bit
# LUT so Pillow's Image.point() applies it band-wise in C rather than this
# module looping over every pixel in Python.
def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_SRGB_LUT = [round(_srgb_to_linear(i / 255) * 255) for i in range(256)]


def _luminance_band(rgb_img: Image.Image) -> Image.Image:
    """An 'L' image whose value at each pixel is that pixel's WCAG relative
    luminance (0-255 scale). Built from three point()-applied LUTs and two
    weighted Image.blend() calls — both pure-C Pillow operations — so this
    costs the same whether the crop is 100 or 100,000 pixels."""
    r, g, b = rgb_img.split()[:3]
    r_lin, g_lin, b_lin = r.point(_SRGB_LUT), g.point(_SRGB_LUT), b.point(_SRGB_LUT)
    # Image.blend(a, b, alpha) = a*(1-alpha) + b*alpha. Chaining two blends
    # with alphas derived from the WCAG weights (0.2126/0.7152/0.0722)
    # reaches the weighted sum without a three-way blend Pillow doesn't have
    # — see the derivation in the PR description / commit message.
    mixed = Image.blend(r_lin, g_lin, 0.7152 / (0.2126 + 0.7152))
    return Image.blend(mixed, b_lin, 0.0722)


def _zone_stats(img: Image.Image, rect: tuple[int, int, int, int]) -> tuple[float, float]:
    """Mean and stddev of relative luminance (both 0..1) under `rect` of the
    CURRENT composite — the "how legible would text be here, and how busy is
    the backdrop" readout the autopilot bases every decision on."""
    left, top, right, bottom = rect
    left, top = max(0, left), max(0, top)
    right, bottom = min(img.width, right), min(img.height, bottom)
    if right <= left or bottom <= top:
        return 0.5, 0.0   # a degenerate/out-of-canvas zone: neutral, no data
    crop = img.convert("RGB").crop((left, top, right, bottom))
    band = _luminance_band(crop)
    stat = ImageStat.Stat(band)
    return stat.mean[0] / 255.0, stat.stddev[0] / 255.0


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * _srgb_to_linear(r / 255) + 0.7152 * _srgb_to_linear(g / 255)
           + 0.0722 * _srgb_to_linear(b / 255))


def _contrast_against_luminance(rgb: tuple[int, int, int], mean_luminance: float) -> float:
    """The WCAG contrast ratio between a color and a backdrop's mean relative
    luminance — treating that mean as if it WERE a flat backdrop color,
    which is exactly what §7.3 asks for ("contrast ratio between the slot's
    palette color and the zone's mean luminance") and sidesteps needing to
    invent a representative backdrop RGB at all."""
    l1 = _relative_luminance(rgb)
    lighter, darker = max(l1, mean_luminance), min(l1, mean_luminance)
    return (lighter + 0.05) / (darker + 0.05)


__all__ = ["EBOOK_H", "EBOOK_W", "THUMB_LARGE", "THUMB_SMALL", "ComposeError",
          "compose", "save_renders"]
