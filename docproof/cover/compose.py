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

from PIL import (Image, ImageChops, ImageColor, ImageDraw, ImageFilter,
                 ImageOps, ImageStat)

from . import typeset
from .archetypes import zone_px
from .imaging import has_real_alpha
from .model import (ArtSlot, CoverSpec, LayerRef, Palette, RenderReport,
                    ScrimSpec, Shadow, TextSlot, Zone)

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

# -- effects rack tuning (§7.4a) ----------------------------------------------

_POSTERIZE_LEVELS = 4
_STICKER_OUTLINE_FRACTION = 0.010     # of canvas height, the sticker ring's width
_ART_FILL_OUTLINE_FRACTION = 0.006    # of canvas height, the art_fill ring's width
_SILHOUETTE_ALPHA_THRESHOLD = 127     # 0..255; above this, a pixel is "the shape"
SCATTER_SIZE_FRACTION = 0.14          # of canvas height, each scattered copy (§7.4a)
_SCATTER_MAX_ATTEMPTS = 200           # per copy, before skipping it rather than
                                      # ever overlapping a text zone (§7.4a: "never
                                      # intersecting any text zone's padded rect" is
                                      # non-negotiable; placing fewer copies is the
                                      # fallback, not placing one wrong)
_CORNER_MARGIN_FRACTION = 0.02        # of canvas, on both axes — corners:true's
                                      # ornament sits fully inside the trim, never
                                      # flush to the edge (v2.1 BODY-fix wave)

# -- v2.1 BODY-fix wave tuning -------------------------------------------------
#
# Four live-observed defects, each fixed by a small deterministic measurement
# added to compose()'s existing passes rather than a new pass of its own —
# see this module's docstring for the general shape ("measure the CURRENT
# canvas, then decide") that all four extend.

# Title occlusion guard (fix 2): a contain-fit art slot drawn immediately
# after a text layer (the cutout_sandwich shape — _degrade_opaque_focal
# already detects this exact z-order pattern for a different reason, reused
# here) is deliberately allowed to overlap that text's zone; it is not
# allowed to bury the text's own ink. _OCCLUSION_THRESHOLD is the fraction of
# the text's ink alpha the art's positioned alpha may cover before this
# module intervenes.
_OCCLUSION_THRESHOLD = 0.30
# Tried in this fixed order — smallest nudge first, alternating sides — so
# two composes of the same spec always land on the same winning offset
# (determinism, same as every other search in this module).
_OCCLUSION_ANCHOR_OFFSETS: tuple[float, ...] = (0.10, -0.10, 0.20, -0.20, 0.25, -0.25)

# Art-vs-ground contrast floor (fix 3): silhouette/duotone both commit an art
# slot's entire visible shape to one ink color (palette.primary) with no
# check against whatever the slot actually sits on — a real render shipped a
# near-black silhouette on a near-black ground, correct by the treatment's
# own rules and invisible on the page. |ΔL| mirrors the legibility
# autopilot's own WCAG-relative-luminance measurement (§7.3), applied to art
# ink against its ground instead of text ink against a zone.
_ART_CONTRAST_FLOOR = 0.12
_ART_CONTRAST_LIGHTEN_STEP = 0.10     # HLS lightness step per bounded-loop try
_ART_CONTRAST_MAX_STEPS = 10          # hard cap — guarantees the loop ends

# Dead-band metric (fix 4): three live covers shipped with a full third of
# the canvas doing nothing at all. _DEAD_BAND_ROW_STDDEV_THRESHOLD separates
# "quiet by design" (a gradient step, a low-alpha procedural texture — both
# near-flat within any one row) from "actually empty" versus real ink (text
# glyphs, art edges, ornament) — tuned against this wave's own retuned
# templates' preview renders, per docs/cover_designer_spec.md's acceptance
# pass for this wave.
_DEAD_BAND_SAMPLE_HEIGHT = 640        # downsample rows before scanning — a
                                      # "band" spans real fractions of the
                                      # cover, so sub-pixel-row precision buys
                                      # nothing; keeps the scan O(640) rather
                                      # than O(canvas height), matching this
                                      # module's existing downsample-for-cost
                                      # discipline (_grain_layer et al.)
_DEAD_BAND_ROW_STDDEV_THRESHOLD = 0.020   # of 0..1 relative luminance
_DEAD_BAND_WARN_FRACTION = 0.28


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


@dataclass(frozen=True)
class _ArtPositions:
    """Everything one _position_all_art() pass produces (v2.1 BODY-fix
    wave): every art slot's final canvas-space pixels (`positioned`); the
    same pixels from just before treatment was applied, for any slot whose
    treatment is not "none" (`pre_treatment` — only the art-vs-ground
    contrast floor reads this, and only for silhouette/duotone slots, so it
    can re-treat from a clean source instead of compounding a second
    treatment on top of the first); the z-order (`layers` — identical to
    what was passed in unless the title occlusion guard degraded a sandwich
    pair, the same list-mutation _degrade_opaque_focal already does for a
    different reason); the warnings raised while positioning; and the
    occlusion measured for every sandwich (a contain-fit art layer
    immediately after a text layer) pair, keyed "<text id><-<art id>"."""
    positioned: dict[str, Image.Image]
    pre_treatment: dict[str, Image.Image]
    layers: list[LayerRef]
    warnings: list[str]
    occlusion: dict[str, float]


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
    # A cover that declares a rule frame promises nothing crosses it — but
    # zones are validated against the canvas, so a generous title zone can
    # legally run right over the gold lines (a live woven_emblem cover
    # shipped exactly that). Clamp every text zone to the frame's inner
    # content rect BEFORE fitting; scrims derive their zones from these same
    # slots below, so they follow automatically.
    text_by_id = _frame_clamp_text(text_by_id, spec, canvas, warnings)
    layers = _degrade_opaque_focal(spec.layers, art_by_id, job_dir, warnings)

    # Every art layer's final, canvas-space, positioned pixels — computed
    # ONCE, here, rather than inside render_upto(): art never depends on
    # scrim strength or on which text slots have been finalized, so
    # recomputing it on every one of render_upto()'s replays (one per text
    # slot measured, more on every scrim-escalation step) would be wasted
    # work at best and, for the effects rack's warnings (a sticker/corners/
    # scatter request that silently no-ops on opaque art), duplicated
    # warnings at worst — see _position_all_art's own docstring.
    text_avoid_rects = _text_zone_rects(spec.text, canvas)
    art_positions = _position_all_art(
        spec.art, layers, job_dir, canvas, spec.palette, spec.version,
        text_avoid_rects, text_by_id)
    positioned_art = art_positions.positioned
    layers = art_positions.layers   # possibly degraded by the title
                                    # occlusion guard (fix 2) — every reader
                                    # below this point (render_upto's own
                                    # closure, the safety net's ground loop)
                                    # sees the same, single, final z-order.
    warnings.extend(art_positions.warnings)

    # Art-vs-ground contrast floor (fix 3): mutates `positioned_art` in
    # place for any silhouette/duotone slot whose ink barely differed from
    # its own ground — run once, here, after every slot has its real
    # (possibly occlusion-adjusted) position, and before render_upto ever
    # composites anything for real.
    _apply_art_contrast_floor(positioned_art, art_positions.pre_treatment,
                              layers, art_by_id, spec.palette, canvas, warnings)

    def render_upto(stop: int) -> Image.Image:
        """Everything in `layers[:stop]`, freshly composited: art at its
        always-deterministic pixels (looked up from `positioned_art`, not
        recomputed), scrims at their CURRENT (possibly escalated) strength,
        and any text slot already resolved drawn exactly as it was the
        first time. Never includes the text slot AT `stop` — the caller
        measures this result to decide how to draw it."""
        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        for layer in layers[:stop]:
            if layer.kind == "art":
                slot = art_by_id[layer.ref]
                img = _composite_layer(img, positioned_art[layer.ref],
                                       slot.opacity, slot.blend)
            elif layer.kind == "scrim":
                idx = int(layer.ref)
                img = _apply_scrim(img, spec.scrims[idx], strengths[idx],
                                   text_by_id, spec.palette, canvas)
            else:  # "text"
                resolved = finalized.get(layer.ref)
                if resolved is not None:
                    img = _draw_resolved_text(img, resolved, canvas, positioned_art)
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
        # knockout/art_fill have no ink color at all (§7.4a) — the thing
        # being tested for contrast against the backdrop is the PANEL/
        # outline color instead, which the rack fixes to the palette's
        # `primary` role (the same role every effects-rack treatment reaches
        # for by fixed rule — see _apply_treatment). Reusing the exact same
        # escalate-then-flip loop below for that color is what "autopilot
        # escalation/flip logic must not crash on them" (§7.4a) asks for:
        # the loop has no idea these two modes are different, it just has a
        # different color to test.
        color_hex = (spec.palette.primary if slot.mode != "fill"
                    else spec.palette.get(slot.color_role))

        # "Thing inside of thing" (mask_from): a title living inside a
        # lighthouse beam reads its contrast against the BEAM's own
        # interior, not the dark sky around it — _zone_stats restricts its
        # sample to wherever this container's positioned alpha is actually
        # opaque, still cropped to the slot's own rect first.
        container_mask = (positioned_art[slot.mask_from].getchannel("A")
                          if slot.mask_from and slot.mask_from in positioned_art
                          else None)

        # v2 BODY wave: the fit search runs BEFORE the legibility
        # measurement now (it used to run after) — worst-REGION scoring,
        # below, needs to know where the fitted glyphs actually land so it
        # can skip grid cells with no ink in them; fit_text has no
        # dependency on the render itself, so this reorder costs nothing.
        fit = typeset.fit_text(slot, canvas)
        if fit.warning:
            warnings.append(fit.warning)
        ink_bbox = typeset.text_mask(slot, fit, canvas).getbbox()

        below = render_upto(i)
        ratio = _worst_region_contrast(below, rect, ImageColor.getrgb(color_hex),
                                       ink_bbox, container_mask)
        _, stddev = _zone_stats(below, rect, container_mask)

        while ratio < threshold and any(strengths[idx] < _SCRIM_CAP for idx in protecting):
            for idx in protecting:
                strengths[idx] = min(_SCRIM_CAP, round(strengths[idx] + _SCRIM_STEP, 10))
            below = render_upto(i)
            ratio = _worst_region_contrast(below, rect, ImageColor.getrgb(color_hex),
                                           ink_bbox, container_mask)
            _, stddev = _zone_stats(below, rect, container_mask)

        if ratio < threshold:
            pre_flip_ratio = ratio
            options = [(c, _worst_region_contrast(below, rect, ImageColor.getrgb(c),
                                                  ink_bbox, container_mask))
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

        finalized[layer.ref] = _ResolvedText(slot=slot, fit=fit, color=color_hex, shadow=shadow)
        contrast[layer.ref] = round(ratio, 4)
        fitted_sizes[layer.ref] = fit.size_frac

        if slot.mask_from:
            # Measured once, here, at finalization — not inside render_upto,
            # which redraws every finalized slot on every later replay (see
            # the module docstring's note on that trade-off); a warning
            # appended once per replay would duplicate it N times over.
            text_layer = _render_text_only(slot, fit, color_hex, shadow, canvas)
            _, coverage = _clip_text_to_container(
                text_layer, positioned_art.get(slot.mask_from), canvas)
            if coverage < _TEXT_MASK_MIN_COVERAGE:
                warnings.append(
                    f"{slot.id}: only {coverage:.0%} of its ink landed "
                    f"inside '{slot.mask_from}' — move the container or "
                    f"shrink the {slot.id}.")

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
            slot = art_by_id[layer.ref]
            ground = _composite_layer(ground, positioned_art[layer.ref],
                                      slot.opacity, slot.blend)
        elif layer.kind == "scrim":
            idx = int(layer.ref)
            ground = _apply_scrim(ground, spec.scrims[idx], strengths[idx],
                                  text_by_id, spec.palette, canvas)
    for ref, resolved in finalized.items():
        zl, zt, zw, zh = zone_px(resolved.slot.zone, canvas)
        container_mask = (positioned_art[resolved.slot.mask_from].getchannel("A")
                          if resolved.slot.mask_from
                          and resolved.slot.mask_from in positioned_art
                          else None)
        ink_bbox = typeset.text_mask(resolved.slot, resolved.fit, canvas).getbbox()
        final_ratio = _worst_region_contrast(
            ground, (zl, zt, zl + zw, zt + zh), ImageColor.getrgb(resolved.color),
            ink_bbox, container_mask)
        threshold = _CONTRAST_THRESHOLDS[resolved.slot.id]
        if final_ratio < threshold and final_ratio < contrast[ref] - 0.5:
            warnings.append(
                f"{ref}: a layer drawn later covers this text — contrast on "
                f"the finished cover is {final_ratio:.2f} (threshold "
                f"{threshold}, was {contrast[ref]:.2f} at draw time).")

    # Dead-band metric (fix 4): one full-canvas read of the FINISHED
    # composite — text, art, and scrims all baked in — for the tallest
    # stretch of vertical real estate doing nothing at all.
    dead_band_frac, band_top_px, band_bottom_px = _dead_band_frac(final_image)
    if dead_band_frac >= _DEAD_BAND_WARN_FRACTION:
        ch = canvas[1]
        warnings.append(
            f"empty band from {band_top_px / ch:.0%} to "
            f"{band_bottom_px / ch:.0%} of the cover's height "
            f"({dead_band_frac:.0%} of the canvas) has no text, art, or "
            f"ornament ink crossing it.")

    report = RenderReport(
        contrast=contrast,
        scrim_final={idx: strengths[idx] for idx in range(len(spec.scrims))},
        fitted_sizes=fitted_sizes,
        warnings=warnings,
        occlusion=art_positions.occlusion,
        dead_band_frac=dead_band_frac)
    return final_image.convert("RGB"), report


# -- knockout / art_fill text modes (§7.4a) -----------------------------------

def _draw_resolved_text(base: Image.Image, resolved: _ResolvedText,
                        canvas: tuple[int, int],
                        positioned_art: dict[str, Image.Image]) -> Image.Image:
    """Dispatch one already-resolved text slot to whichever renderer its
    `mode` needs: typeset.draw_text for the normal ink-colored `fill`, or
    _draw_knockout_or_art_fill for the two panel/window modes. Both branches
    were handed the SAME `resolved.color` by the autopilot loop above (the
    escalate-then-flip machinery has no idea which mode it is tuning a color
    for), so this is the one place that color's meaning finally forks: ink,
    or panel/outline.

    When `resolved.slot.mask_from` names a container ("thing inside of
    thing" — a title inside a lighthouse beam), the fully rendered text
    layer (whichever mode built it) is clipped to that container's already-
    positioned alpha before it ever reaches `base` — see
    _render_text_only/_clip_text_to_container."""
    slot = resolved.slot
    if not slot.mask_from:
        if slot.mode == "fill":
            return typeset.draw_text(base, slot, resolved.fit, resolved.color,
                                     resolved.shadow, canvas)
        return _draw_knockout_or_art_fill(base, slot, resolved.fit,
                                          resolved.color, canvas)

    text_layer = _render_text_only(slot, resolved.fit, resolved.color,
                                   resolved.shadow, canvas)
    clipped, _coverage = _clip_text_to_container(
        text_layer, positioned_art.get(slot.mask_from), canvas)
    out = base.copy()
    out.alpha_composite(clipped)
    return out


def _render_text_only(slot: TextSlot, fit: typeset.FitResult, color: str,
                      shadow: Shadow | None, canvas: tuple[int, int]) -> Image.Image:
    """`slot`'s fully rendered pixels — ink/stroke/shadow for `fill`, the
    panel/window pixels for knockout/art_fill — as a STANDALONE canvas-sized
    RGBA layer, transparent everywhere else. Both typeset.draw_text and
    _draw_knockout_or_art_fill already build exactly this internally before
    compositing it onto whatever `base` they're given (neither reads a
    single pixel FROM `base` — it is only ever the accumulator they
    composite onto), so calling either against a blank transparent canvas
    returns the layer itself, unchanged. mask_from's clip and its coverage
    measurement both need the text's own pixels in isolation from the art
    beneath them, before that clip ever happens."""
    blank = Image.new("RGBA", canvas, (0, 0, 0, 0))
    if slot.mode == "fill":
        return typeset.draw_text(blank, slot, fit, color, shadow, canvas)
    return _draw_knockout_or_art_fill(blank, slot, fit, color, canvas)


# v2 BODY wave: "thing inside of thing" — a title living inside a lighthouse
# beam, an image inside a train's smoke plume. Art-inside-art already worked
# via ArtSlot.mask_from/_apply_mask_from (a hard-thresholded stencil, by
# that mechanism's own design); text-inside-art wants a SOFTER edge (the
# glyphs' own antialiasing should survive, not get re-thresholded to a hard
# stencil), so this is deliberately a separate, smaller mechanism rather
# than a shared helper.
_TEXT_MASK_FEATHER_PX = 2.0     # gaussian-soften the container's own edge,
                                # so a clipped title never looks razor-cut
_TEXT_MASK_MIN_COVERAGE = 0.85  # below this fraction of surviving ink, warn


def _clip_text_to_container(text_layer: Image.Image, container: Image.Image | None,
                            canvas: tuple[int, int]) -> tuple[Image.Image, float]:
    """`text_layer` (from _render_text_only) with its alpha multiplied by
    `container`'s own positioned alpha — lightly feathered so the container
    art's own edge (however hard-edged the source cutout is) never reads as
    a razor-cut clip — plus how much of the text's own ink alpha survived:
    1.0 when there is no container (a dangling mask_from can't happen for a
    spec that passed CoverSpec validation — defensive, mirrors
    _apply_mask_from's own None guard) or when the layer had no ink at all
    (an empty optional slot trivially "fully covers")."""
    if container is None:
        return text_layer, 1.0
    container_alpha = container.getchannel("A")
    if _TEXT_MASK_FEATHER_PX > 0:
        container_alpha = container_alpha.filter(
            ImageFilter.GaussianBlur(_TEXT_MASK_FEATHER_PX))
    r, g, b, a = text_layer.split()
    ink_total = ImageStat.Stat(a).sum[0]
    clipped_a = ImageChops.multiply(a, container_alpha)
    coverage = (ImageStat.Stat(clipped_a).sum[0] / ink_total) if ink_total > 0 else 1.0
    return Image.merge("RGBA", (r, g, b, clipped_a)), coverage


def _padded_rect(zone, canvas: tuple[int, int]) -> tuple[int, int, int, int]:
    """A fractional zone, padded _SCRIM_PAD_FRACTION on every side and
    clamped to the canvas — the same "protecting a text slot" padding
    `_scrim_rect` already applies when a ScrimSpec derives its zone from a
    TextSlot, reused here for knockout/art_fill's own panel bounds (§7.4a:
    "covering the zone (padded 4%)") so both mechanisms agree on what
    "around this text" means."""
    cw, ch = canvas
    left, top, w, h = zone_px(zone, canvas)
    pad_x = round(_SCRIM_PAD_FRACTION * cw)
    pad_y = round(_SCRIM_PAD_FRACTION * ch)
    return (max(0, left - pad_x), max(0, top - pad_y),
           min(cw, left + w + pad_x), min(ch, top + h + pad_y))


def _draw_knockout_or_art_fill(base: Image.Image, slot: TextSlot,
                               fit: typeset.FitResult, color: str,
                               canvas: tuple[int, int]) -> Image.Image:
    """§7.4a's two "the type IS the window" text modes, both built from the
    same glyph mask (typeset.text_mask) and the same padded panel rect
    (_padded_rect) `knockout`'s own spec line describes, differing only in
    which side of the glyph edge gets painted:

    `knockout` — a solid `color` panel over the whole padded zone, with the
    glyph shapes cut fully transparent (ImageChops.subtract turns "panel(255)
    minus glyph(255)" into 0), so the layers already beneath this one show
    through in letter shapes: reverse/negative-space type.

    `art_fill` — the mirror image: nothing is painted over the glyph
    INTERIOR at all (whatever is already composited there — the art+scrim
    ground — simply stays, which is what makes the title "a window into the
    art" rather than an approximation of one), and only a thin ring right at
    each glyph's edge is painted, so the letterforms still read as shapes
    against a backdrop that would otherwise swallow them. That ring is
    exactly "dilated glyph mask minus the original glyph mask" — the same
    subtract-based ring construction _sticker uses for its outline, at a
    narrower fixed width tuned for type instead of a whole cutout figure.

    Either way `fit.lines == ()` (an empty optional slot) draws nothing, the
    same short-circuit typeset.draw_text itself takes."""
    if not fit.lines:
        return base
    mask = typeset.text_mask(slot, fit, canvas)
    rgb = ImageColor.getrgb(color)
    if slot.mode == "knockout":
        left, top, right, bottom = _padded_rect(slot.zone, canvas)
        panel = Image.new("L", canvas, 0)
        if right > left and bottom > top:
            ImageDraw.Draw(panel).rectangle((left, top, right - 1, bottom - 1), fill=255)
        alpha = ImageChops.subtract(panel, mask)
    else:  # "art_fill"
        outline_px = _ART_FILL_OUTLINE_FRACTION * canvas[1]
        alpha = ImageChops.subtract(_dilate_mask(mask, outline_px), mask)
    layer = Image.new("RGBA", canvas, (*rgb, 0))
    layer.putalpha(alpha)
    out = base.copy()
    out.alpha_composite(layer)
    return out


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

def _load_or_synthesize(slot: ArtSlot, job_dir: Path, canvas: tuple[int, int],
                        palette: Palette, version: int) -> Image.Image | None:
    """Load a generated asset, or synthesize one procedurally when the slot
    has none — §7.3's "reads generated art when set, synthesizes otherwise"
    rule, checked on `asset` alone (not `prompt`): by the time a real job
    reaches compose(), pipeline.py has already generated everything with a
    prompt, so an empty `asset` here always means "procedural on purpose"
    (the $0 big_type background, the grain texture, or any slot naming a
    `procedural` synthesizer) or "no art for this slot" (an ungenerated
    `focal` with no `procedural` either, which draws nothing rather than
    invent a fallback the spec never described — signaled by returning
    None).

    Returns the RAW loaded/synthesized image, before any fit/placement or
    effects-rack treatment — _position_all_art is what turns this into
    canvas-space pixels."""
    if slot.asset:
        path = job_dir / slot.asset
        try:
            source = Image.open(path)
            source.load()
            return source.convert("RGBA")
        except Exception as e:  # noqa: BLE001 - any decode failure is the same story
            raise ComposeError(
                f"Could not read the generated art for the '{slot.id}' slot "
                f"at {path}: {e}") from e
    return _procedural_art(slot, canvas, palette, version)


def _has_transparency(img: Image.Image) -> bool:
    """Whether `img`'s own alpha channel actually varies anywhere — the same
    "don't trust the declared flag, look at the real pixels" instinct
    _degrade_opaque_focal already applies to the cutout_sandwich focal slot
    (via imaging.has_real_alpha on the raw file bytes), reapplied here to
    the DECODED image for the effects rack's three "transparent slots only"
    features (corners, scatter, sticker — §7.4a). A procedurally synthesized
    layer (the gradient background, the grain texture) is always fully
    opaque by construction, so this is also what correctly no-ops those
    features on a big_type-style $0 render."""
    return img.getchannel("A").getextrema()[0] < 255


def _text_zone_rects(text: list[TextSlot], canvas: tuple[int, int]
                     ) -> list[tuple[int, int, int, int]]:
    """Every NON-empty text slot's padded rectangle, in px — what motif
    scatter (§7.4a) must never stamp a copy inside. Padded the same
    _SCRIM_PAD_FRACTION as a protecting scrim / a knockout panel, so "clear
    of the text" means the same margin everywhere in this module. An
    optional slot with no content never renders (compose()'s own per-slot
    loop skips it identically), so scatter has no reason to avoid its zone
    either."""
    return [_padded_rect(t.zone, canvas) for t in text
           if not (t.optional and not t.content.strip())]


def _rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _scatter_seed(version: int, slot_id: str) -> int:
    """A deterministic seed from the spec version and this slot's id — NOT
    Python's built-in hash() (its str hashing is randomized per-process
    unless PYTHONHASHSEED is pinned, which would silently break the "same
    spec+assets = identical bytes" promise every other seed in this module
    already keeps — see _GRAIN_SEED). Folding the slot id in too means two
    different scatter slots in the same spec version don't stamp an
    identical arrangement on top of each other."""
    return version * 1_000_003 + sum(ord(c) for c in slot_id)


def _place_corners(source: Image.Image, canvas: tuple[int, int],
                   scale: float) -> Image.Image:
    """§7.4a's mirrored corner frame: the ornament, resized so its height is
    `scale` of the canvas height (there is no cover/contain baseline to zoom
    from here, unlike _fit_cover/_fit_contain's own reading of `scale` — so
    `scale` is read directly against the canvas, the same convention
    SCATTER_SIZE_FRACTION's fixed 14% uses below), placed inset
    _CORNER_MARGIN_FRACTION from the top-left corner, then mirrored
    horizontally, vertically, and both, into the other three. Every copy is
    an exact Pillow ImageOps.mirror/flip of the same resized source, so the
    four corners are byte-for-byte symmetric — the "exact symmetry is the
    tell generators can't produce" effect the spec calls for, not an
    approximation of one.

    v2.1 BODY-fix wave: a real render shipped ornament halves bleeding off
    all four edges — the ORIGINAL version of this function placed every copy
    flush to the trim edge (0% margin), and a generated ornament whose own
    artwork already reaches its own image bounds then reads as "chopped off"
    rather than "inset like a designer would place it." The margin is
    clamped to at most half of whatever slack (canvas minus ornament) exists
    on each axis, so an oversized ornament degrades to symmetric centered
    overlap instead of a negative destination or a left/right swap."""
    cw, ch = canvas
    iw, ih = source.size
    target_h = max(1, round(scale * ch))
    target_w = max(1, round(iw * (target_h / ih))) if ih else 1
    resized = source.resize((target_w, target_h), Image.Resampling.LANCZOS)
    mirrored_h = ImageOps.mirror(resized)
    flipped_v = ImageOps.flip(resized)
    both = ImageOps.flip(mirrored_h)

    slack_x, slack_y = max(0, cw - target_w), max(0, ch - target_h)
    inset_x = min(round(_CORNER_MARGIN_FRACTION * cw), slack_x // 2)
    inset_y = min(round(_CORNER_MARGIN_FRACTION * ch), slack_y // 2)
    left_x, top_y = inset_x, inset_y
    right_x, bottom_y = slack_x - inset_x, slack_y - inset_y

    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(resized, dest=(left_x, top_y))
    out.alpha_composite(mirrored_h, dest=(right_x, top_y))
    out.alpha_composite(flipped_v, dest=(left_x, bottom_y))
    out.alpha_composite(both, dest=(right_x, bottom_y))
    return out


def _place_scatter(source: Image.Image, canvas: tuple[int, int], count: int,
                   version: int, slot_id: str,
                   avoid_rects: list[tuple[int, int, int, int]]
                   ) -> tuple[Image.Image, int]:
    """§7.4a's motif scatter: `count` copies of `source`, each sized to
    SCATTER_SIZE_FRACTION of canvas height, stamped at positions drawn from
    a `random.Random` seeded purely from `version`/`slot_id` (see
    _scatter_seed) — the ONLY randomness in this module besides the grain
    texture's, and just as reproducible. A candidate position is retried (up
    to _SCATTER_MAX_ATTEMPTS times) until it clears every entry in
    `avoid_rects`; a copy that never finds a clear spot is simply skipped —
    a cover with fewer motifs than asked for is a far better failure than
    one that guarantees a motif buries the title. Returns the composited
    layer and how many copies actually landed, so the caller can warn about
    the shortfall exactly once."""
    cw, ch = canvas
    iw, ih = source.size
    target_h = max(1, round(SCATTER_SIZE_FRACTION * ch))
    target_w = max(1, round(iw * (target_h / ih))) if ih else 1
    resized = source.resize((target_w, target_h), Image.Resampling.LANCZOS)
    rng = random.Random(_scatter_seed(version, slot_id))
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    placed = 0
    max_x, max_y = max(0, cw - target_w), max(0, ch - target_h)
    for _ in range(count):
        for _attempt in range(_SCATTER_MAX_ATTEMPTS):
            x, y = rng.randint(0, max_x), rng.randint(0, max_y)
            rect = (x, y, x + target_w, y + target_h)
            if not any(_rects_overlap(rect, avoid) for avoid in avoid_rects):
                out.alpha_composite(resized, dest=(x, y))
                placed += 1
                break
    return out, placed


def _apply_mask_from(img: Image.Image, ref_img: Image.Image | None) -> Image.Image:
    """§7.4a's double exposure: keep `img`'s own pixels only where `ref_img`
    (another art slot's already-positioned, canvas-space image) is opaque —
    art poured inside another slot's silhouette. A hard 50% threshold turns
    the reference's alpha into a binary stencil first (a soft/graduated
    reference edge should not leave `img` partially see-through in a way
    neither slot's own alpha describes); ImageChops.multiply against that
    0/255 stencil then either passes `img`'s own alpha through unchanged
    (stencil=255) or zeroes it (stencil=0), so `img`'s own soft edges (its
    OWN anti-aliasing, its OWN treatment) still survive wherever the
    reference allows it through. `ref_img=None` (a dangling reference) can't
    happen for a spec that passed CoverSpec validation — defensive, not a
    real path."""
    if ref_img is None:
        return img
    r, g, b, a = img.split()
    ref_stencil = ref_img.split()[3].point(lambda v: 255 if v > 127 else 0)
    return Image.merge("RGBA", (r, g, b, ImageChops.multiply(a, ref_stencil)))


def _occlusion_fraction(ink_mask: Image.Image, art_alpha: Image.Image) -> float:
    """What fraction of `ink_mask`'s (a text slot's glyph-coverage 'L' mask,
    from typeset.text_mask) total weighted ink is also covered by
    `art_alpha` (an art slot's own positioned+treated alpha channel) — the
    same multiply-then-ratio idiom _clip_text_to_container already uses for
    the opposite question ("how much of the ink SURVIVES a clip"), reused
    here to ask "how much gets BURIED." 0.0 when the text slot has no ink at
    all (never happens here — the caller already skips empty optional slots
    — but degenerate-safe regardless, matching _clip_text_to_container's own
    zero-ink guard)."""
    ink_total = ImageStat.Stat(ink_mask).sum[0]
    if ink_total <= 0:
        return 0.0
    covered = ImageChops.multiply(ink_mask, art_alpha)
    return ImageStat.Stat(covered).sum[0] / ink_total


def _place_contain_clear_of_text(source: Image.Image, canvas: tuple[int, int],
                                 slot: ArtSlot, palette: Palette, has_alpha: bool,
                                 text_slot: TextSlot
                                 ) -> tuple[Image.Image, float, bool]:
    """The title occlusion guard's search (fix 2, v2.1 BODY-fix wave):
    `slot`'s normal contain-fit placement at its own declared anchor, if
    that already covers at most _OCCLUSION_THRESHOLD of `text_slot`'s
    fitted ink; otherwise, nudge the anchor's x by each of
    _OCCLUSION_ANCHOR_OFFSETS in turn (clamped to [0, 1] — an anchor is a
    fraction of the canvas, same convention as everywhere else
    _fit_contain reads one) and take the first that clears the threshold.

    Returns (the UNTREATED placed image at the winning anchor, its
    occlusion fraction, whether every offset still failed — the caller's
    cue to degrade to drawing this art BELOW the text instead, per §5.2.3).
    Measurement itself happens against each candidate's TREATED alpha (a
    throwaway copy) because silhouette's hard threshold and sticker's
    dilation both reshape alpha — scoring the untreated candidate could
    pick an anchor that only looks clear before its own treatment repaints
    back over the text. The function still returns the untreated image at
    the winning anchor so the caller's own single, uniform
    _apply_treatment call (the one that also populates `pre_treatment` for
    the art-vs-ground contrast floor) is the only place treatment is
    actually applied for real — never twice."""
    fit = typeset.fit_text(text_slot, canvas)
    ink_mask = typeset.text_mask(text_slot, fit, canvas)

    def measure(anchor: tuple[float, float]) -> tuple[Image.Image, float]:
        placed = _fit_contain(source, canvas, anchor, slot.scale, slot.offset)
        treated, _warning = _apply_treatment(placed, slot.treatment, palette,
                                             slot.id, has_alpha)
        return placed, _occlusion_fraction(ink_mask, treated.getchannel("A"))

    ax, ay = slot.anchor
    best_img, best_ratio = measure((ax, ay))
    if best_ratio <= _OCCLUSION_THRESHOLD:
        return best_img, best_ratio, False

    for dx in _OCCLUSION_ANCHOR_OFFSETS:
        trial_img, trial_ratio = measure((min(1.0, max(0.0, ax + dx)), ay))
        if trial_ratio <= _OCCLUSION_THRESHOLD:
            return trial_img, trial_ratio, False
        if trial_ratio < best_ratio:
            best_img, best_ratio = trial_img, trial_ratio
    return best_img, best_ratio, True


def _position_all_art(art_slots: list[ArtSlot], layers: list[LayerRef],
                      job_dir: Path, canvas: tuple[int, int], palette: Palette,
                      version: int, text_avoid_rects: list[tuple[int, int, int, int]],
                      text_by_id: dict[str, TextSlot]) -> _ArtPositions:
    """Every art slot's final, canvas-space RGBA image, positioned and
    effects-rack-treated exactly once, keyed by slot id. Computed as a
    single pass over `layers` (not `art_slots` directly) so a `mask_from`
    slot always finds its reference already in `positioned` — CoverSpec
    validation guarantees the reference appears earlier in `layers`, so a
    single forward pass is sufficient and no slot is ever positioned twice.

    Doing this once, up front, rather than inside compose()'s render_upto()
    (which replays the whole layer stack from scratch on every scrim
    escalation and every text slot's "what's beneath me" measurement) is
    what keeps this function's own warnings (a sticker/corners/scatter
    request that no-ops because the art has no real transparency, or a
    scatter that couldn't place every copy) from being duplicated once per
    replay — see compose()'s own docstring note on the same trade-off.

    v2.1 BODY-fix wave: also runs the title occlusion guard (fix 2). A
    contain-fit art slot immediately following a text layer in `layers`
    (the cutout_sandwich shape) is checked against that text's own fitted
    ink via _place_contain_clear_of_text — typeset.fit_text has no
    dependency on anything this function computes, so measuring it here,
    ahead of compose()'s own per-text loop, costs nothing beyond one
    redundant (pure, deterministic, cheap) fit search. The returned
    _ArtPositions.layers reflects any occlusion-driven degrade (a text/art
    swap — the same list-mutation move _degrade_opaque_focal makes for a
    different reason); every caller must read z-order from THAT list from
    this point on, not the one passed in."""
    art_by_id = {a.id: a for a in art_slots}
    positioned: dict[str, Image.Image] = {}
    pre_treatment: dict[str, Image.Image] = {}
    occlusion: dict[str, float] = {}
    warnings: list[str] = []
    layers_out = list(layers)
    for i, layer in enumerate(layers_out):
        if layer.kind != "art" or layer.ref in positioned:
            continue
        slot = art_by_id[layer.ref]
        source = _load_or_synthesize(slot, job_dir, canvas, palette, version)
        if source is None:
            positioned[slot.id] = Image.new("RGBA", canvas, (0, 0, 0, 0))
            continue

        has_alpha = _has_transparency(source)
        sandwich_text = None
        if slot.fit == "contain" and i > 0 and layers_out[i - 1].kind == "text":
            candidate = text_by_id.get(layers_out[i - 1].ref)
            if candidate is not None and not (candidate.optional
                                              and not candidate.content.strip()):
                sandwich_text = candidate

        if slot.corners and has_alpha:
            img = _place_corners(source, canvas, slot.scale)
        elif slot.scatter and has_alpha:
            img, placed = _place_scatter(source, canvas, slot.scatter, version,
                                         slot.id, text_avoid_rects)
            if placed < slot.scatter:
                warnings.append(
                    f"{slot.id}: only placed {placed} of {slot.scatter} "
                    f"scatter copies without overlapping a text zone.")
        elif slot.fit == "contain" and sandwich_text is not None:
            img, ratio, degrade = _place_contain_clear_of_text(
                source, canvas, slot, palette, has_alpha, sandwich_text)
            key = f"{sandwich_text.id}<-{slot.id}"
            if degrade:
                layers_out[i - 1], layers_out[i] = layers_out[i], layers_out[i - 1]
                warnings.append(
                    f"{slot.id}: even after searching anchor offsets, it "
                    f"still covered {ratio:.0%} of '{sandwich_text.id}''s "
                    f"ink (over the {_OCCLUSION_THRESHOLD:.0%} limit); drew "
                    f"'{sandwich_text.id}' on top of it instead of "
                    f"underneath.")
                occlusion[key] = 0.0   # text now draws on top — the
                                      # composited cover carries none of
                                      # this pair's occlusion any more
            else:
                occlusion[key] = ratio
        else:
            if slot.corners and not has_alpha:
                warnings.append(
                    f"{slot.id}: corners is set but the art has no real "
                    f"transparency; drew it as a single normal layer instead.")
            if slot.scatter and not has_alpha:
                warnings.append(
                    f"{slot.id}: scatter is set but the art has no real "
                    f"transparency; drew it as a single normal layer instead.")
            img = (_fit_cover(source, canvas, slot.anchor, slot.scale, slot.offset)
                  if slot.fit == "cover" else
                  _fit_contain(source, canvas, slot.anchor, slot.scale, slot.offset))

        if slot.treatment != "none":
            pre_treatment[slot.id] = img
        img, treatment_warning = _apply_treatment(img, slot.treatment, palette,
                                                  slot.id, has_alpha)
        if treatment_warning:
            warnings.append(treatment_warning)

        if slot.mask_from:
            img = _apply_mask_from(img, positioned.get(slot.mask_from))

        positioned[slot.id] = img
    return _ArtPositions(positioned=positioned, pre_treatment=pre_treatment,
                         layers=layers_out, warnings=warnings, occlusion=occlusion)


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
            return None   # e.g. an ungenerated "focal" cutout: nothing to draw
    synth = PROCEDURAL_SYNTHESIZERS.get(name)
    if synth is None:
        return None   # unreachable given the Literal; never crash on the unknown
    return synth(canvas, palette, slot.id, version)


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


# -- procedural texture shelf (v2 BODY wave) -----------------------------------
#
# Reference DNA item 7: "quiet paper grain unifying everything" — and item 8's
# "thin rule frames... read as craft." Every synthesizer here is a pure
# function of (canvas, palette, slot id, spec version) -> a canvas-sized RGBA
# image, drawn in ONE palette color at a baked-in low alpha (so it looks
# right blended `normal` at slot.opacity=1.0; an archetype may still dial it
# further via opacity/blend) — never a hardcoded hex, so a direction that
# "makes the palette warmer" re-tints every texture along with everything
# else. Each is built from vector draw calls or a downsampled-then-upsampled
# noise field (never a full-canvas Python pixel loop — the same performance
# discipline _grain_layer/_gradient_layer/_paint_vignette_scrim already
# apply), and each is either periodic-by-construction (a fixed pitch grid:
# halftone, canvas weave, rule_frame — trivially tileable) or seeded purely
# from (version, slot_id, name) via _synth_seed (paper, speckle) — never
# Python's process-randomized built-in hash() — so two composes of the same
# spec, on any machine, synthesize byte-identical pixels. `grain`/`gradient`
# below are thin registry wrappers around the two ORIGINAL, unchanged
# synthesizers above, so the "texture"/"background" legacy-id fallback in
# _procedural_art reaches the exact same bytes it always has.

def _synth_seed(version: int, slot_id: str, name: str) -> int:
    """Mirrors _scatter_seed's reasoning exactly (fixed integer arithmetic
    over `version` plus every character's ordinal, never hash()) with the
    synthesizer's own `name` folded in too, so two different slots asking
    for the SAME synthesizer — or one slot's `procedural` changing across a
    revision — never stamp identical noise."""
    return (version * 1_000_003 + sum(ord(c) for c in slot_id) * 97
           + sum(ord(c) for c in name) * 7)


def _synth_gradient(canvas: tuple[int, int], palette: Palette, slot_id: str,
                    version: int) -> Image.Image:
    return _gradient_layer(canvas, palette.background)


def _synth_grain(canvas: tuple[int, int], palette: Palette, slot_id: str,
                 version: int) -> Image.Image:
    return _grain_layer(canvas)


_PAPER_LINE_SPACING_FRACTION = 0.0065   # of canvas height, between laid lines
_PAPER_FIBER_BLUR_FRACTION = 0.003      # of canvas height
_PAPER_ALPHA = 90                       # of 255 - subtle fiber+laid-line tint


def _synth_paper(canvas: tuple[int, int], palette: Palette, slot_id: str,
                 version: int) -> Image.Image:
    """Laid paper: soft mottled fiber noise plus regularly spaced horizontal
    "laid lines" — the faint ribbing real laid paper shows held to light —
    the two textures together read as "paper," not "screen." Fiber noise is
    a downsampled-then-blurred random field (like _grain_layer's, but
    blurred further so it clumps into fibers instead of reading as sharp
    per-pixel noise); the laid lines are exact, fixed-period horizontal
    rules, so the whole field tiles cleanly at that period."""
    cw, ch = canvas
    seed = _synth_seed(version, slot_id, "paper")
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


def _synth_halftone(canvas: tuple[int, int], palette: Palette, slot_id: str,
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


def _synth_canvas(canvas: tuple[int, int], palette: Palette, slot_id: str,
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


def _synth_speckle(canvas: tuple[int, int], palette: Palette, slot_id: str,
                   version: int) -> Image.Image:
    """Sparse scattered dots at varied sizes — the "Atomic Habits" cover
    signature: a field of small solid circles at random positions and radii,
    seeded purely from (version, slot_id) so the same slot always speckles
    identically."""
    cw, ch = canvas
    rng = random.Random(_synth_seed(version, slot_id, "speckle"))
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


def _synth_rule_frame(canvas: tuple[int, int], palette: Palette, slot_id: str,
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


# One entry per docproof.cover.model.PROCEDURAL_KINDS name — kept in that
# exact same set (tests assert it) so a name that validates at the spec
# layer always resolves to a real synthesizer here, and vice versa.
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


def _frame_clamp_text(text_by_id: dict[str, TextSlot], spec: CoverSpec,
                      canvas: tuple[int, int],
                      warnings: list[str]) -> dict[str, TextSlot]:
    """Intersect every text zone with the rule frame's inner rect (when the
    spec declares one) so type can never cross a frame the cover promised.
    A clamp that would crush a zone below _FRAME_CLAMP_MIN_WIDTH of its
    declared width refuses instead — the warning hands that collision to
    the judge, because microscopic type is the worse failure."""
    if not any(a.procedural == "rule_frame" for a in spec.art):
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


PROCEDURAL_SYNTHESIZERS = {
    "gradient": _synth_gradient,
    "grain": _synth_grain,
    "paper": _synth_paper,
    "halftone": _synth_halftone,
    "canvas": _synth_canvas,
    "speckle": _synth_speckle,
    "rule_frame": _synth_rule_frame,
}


# -- slot treatments (§7.4a) ---------------------------------------------------
#
# All four are pure functions of (canvas-space RGBA image, Palette) -> a new
# RGBA image of the same size, applied after fit/placement and before
# compositing — none of them ever touch alpha's SHAPE except sticker (which
# is explicitly about growing it). Colors come from the palette by fixed
# rule, never a per-slot parameter (§7.4a: "no per-slot color params in
# v1") — duotone and posterize both read palette.background/primary/accent/
# text, silhouette and sticker each read exactly one role.

def _dilate_mask(mask: Image.Image, px: float) -> Image.Image:
    """Grow an 'L' mask outward by `px` (a float, rounded to the nearest odd
    kernel >= 3 — PIL.ImageFilter.MaxFilter requires an odd size). Shared by
    _sticker's cutout outline and _draw_knockout_or_art_fill's art_fill
    ring: both are "the band just outside this shape," differing only in
    how wide that band is and what shape they start from."""
    size = max(3, int(round(px)) | 1)
    return mask.filter(ImageFilter.MaxFilter(size))


def _duotone(img: Image.Image, palette: Palette,
            ink: tuple[int, int, int] | None = None) -> Image.Image:
    """Map every pixel's WCAG luminance onto a straight-line ramp between
    palette.background (darkest) and `ink` (lightest — defaults to
    palette.primary, the normal §7.4a rule), reusing the same
    _luminance_band the legibility autopilot already computes — the same
    measurement, put to a different use. Alpha passes through untouched, so
    a transparent cutout stays exactly as transparent after its RGB is
    remapped. Every OPAQUE output pixel's color lies exactly on that
    256-step line (never off it), which is the "duotone output contains
    only ramp colors" guarantee §7.4a's test list asks for — `ink` only
    changes WHICH ramp, never that guarantee. (`ink` is the art-vs-ground
    contrast floor's escalation, v2.1 BODY-fix wave — see
    _apply_art_contrast_floor.)"""
    lum = _luminance_band(img)
    bg = ImageColor.getrgb(palette.background)
    fg = ink if ink is not None else ImageColor.getrgb(palette.primary)
    bands = []
    for c in range(3):
        lut = [round(bg[c] + (fg[c] - bg[c]) * (i / 255)) for i in range(256)]
        bands.append(lum.point(lut))
    return Image.merge("RGBA", (*bands, img.getchannel("A")))


def _silhouette(img: Image.Image, palette: Palette,
                ink: tuple[int, int, int] | None = None) -> Image.Image:
    """Threshold `img`'s OWN alpha (not its luminance — a transparent
    cutout's shape is already exactly its alpha channel) at
    _SILHOUETTE_ALPHA_THRESHOLD into a hard 0/255 stencil, then fill that
    stencil flat with `ink` (defaults to palette.primary, the normal §7.4a
    rule). The result is binary by construction: every pixel is either
    fully transparent or exactly `ink` at full opacity — no in-between
    shade survives, which is what "thresholds to a flat shape" means
    literally, regardless of which color fills it. (`ink` is the
    art-vs-ground contrast floor's escalation, v2.1 BODY-fix wave — see
    _apply_art_contrast_floor.)"""
    fill = ink if ink is not None else ImageColor.getrgb(palette.primary)
    stencil = img.getchannel("A").point(
        lambda v: 255 if v > _SILHOUETTE_ALPHA_THRESHOLD else 0)
    flat = Image.new("RGBA", img.size, (*fill, 255))
    flat.putalpha(stencil)
    return flat


def _posterize(img: Image.Image, palette: Palette) -> Image.Image:
    """Bucket every pixel's luminance into _POSTERIZE_LEVELS even bands, and
    recolor each band flat with whichever of the palette's four visual
    roles (background/primary/accent/text — `scrim` is a utility overlay
    color, never a subject color, so it is excluded) is nearest in RGB space
    to that band's own midpoint gray. Alpha passes through untouched. Only
    _POSTERIZE_LEVELS distinct RGB triples ever appear in the opaque output,
    and every one of them is a real palette color — "posterize output
    contains only palette colors" from the test list, by construction
    rather than by measurement."""
    lum = _luminance_band(img)
    roles = ("background", "primary", "accent", "text")
    palette_colors = [ImageColor.getrgb(palette.get(r)) for r in roles]
    edges = [i * 256 // _POSTERIZE_LEVELS for i in range(_POSTERIZE_LEVELS + 1)]
    band_colors = []
    for lo, hi in zip(edges, edges[1:]):
        mid = (lo + hi) / 2
        band_colors.append(min(
            palette_colors,
            key=lambda c: sum((c[ch] - mid) ** 2 for ch in range(3))))
    lut = []
    for i in range(256):
        band = min(i * _POSTERIZE_LEVELS // 256, _POSTERIZE_LEVELS - 1)
        lut.append(band_colors[band])
    bands = [lum.point([color[ch] for color in lut]) for ch in range(3)]
    return Image.merge("RGBA", (*bands, img.getchannel("A")))


def _sticker(img: Image.Image, palette: Palette, canvas: tuple[int, int]) -> Image.Image:
    """Grow `img`'s alpha outward by _STICKER_OUTLINE_FRACTION of canvas
    height (see _dilate_mask) and fill the newly-grown ring — dilated alpha
    minus original alpha — with palette.text, then composite the ORIGINAL
    image back on top so its own colors are untouched inside the ring. The
    net look is the die-cut white (or, here, palette.text-colored) border a
    sticker/collage cutout gets in print, built the same subtract-a-mask way
    _draw_knockout_or_art_fill's art_fill ring is."""
    a = img.getchannel("A")
    dilated = _dilate_mask(a, _STICKER_OUTLINE_FRACTION * canvas[1])
    ring = ImageChops.subtract(dilated, a)
    text_rgb = ImageColor.getrgb(palette.text)
    out = Image.new("RGBA", img.size, (*text_rgb, 0))
    out.putalpha(ring)
    layer = out.copy()
    layer.alpha_composite(img)
    return layer


def _apply_treatment(img: Image.Image, treatment: str, palette: Palette,
                     slot_id: str, has_alpha: bool
                     ) -> tuple[Image.Image, str | None]:
    """Dispatch one ArtSlot.treatment (§7.4a) to its pure function, plus
    `sticker`'s one stateful exception: on an opaque slot (no real alpha to
    dilate — see _has_transparency) it is a documented no-op, not a crash,
    and returns a warning sentence instead of a treated image. The other
    three treatments have no such precondition — duotone/silhouette/
    posterize are well-defined on a fully opaque image too (silhouette
    just becomes one flat rectangle; not wrong, just usually not what an
    archetype author wants on a full-bleed background)."""
    if treatment == "none":
        return img, None
    if treatment == "duotone":
        return _duotone(img, palette), None
    if treatment == "silhouette":
        return _silhouette(img, palette), None
    if treatment == "posterize":
        return _posterize(img, palette), None
    if treatment == "sticker":
        if not has_alpha:
            return img, (
                f"{slot_id}: sticker treatment needs a transparent slot; "
                f"the art has no real transparency, so it was left untreated.")
        return _sticker(img, palette, img.size), None
    return img, None   # unreachable given the Literal; never crash on the unknown


# -- art-vs-ground contrast floor (fix 3, v2.1 BODY-fix wave) ----------------
#
# silhouette/duotone both commit an art slot's entire visible shape to ONE
# ink color (palette.primary) by fixed rule (§7.4a: "no per-slot color
# params"), with nothing ever checking that color against whatever the slot
# actually sits on. A live thriller render shipped a near-black silhouette
# on a near-black ground — correct by the treatment's own rule, invisible on
# the page. This mirrors the legibility autopilot's own escalate-then-
# reach-for-a-different-color shape (§7.3), reusing its exact WCAG relative-
# luminance measurement, for art ink against its ground instead of text ink
# against a zone.

def _farther_extreme(luminance: float) -> float:
    """Which luminance extreme (0.0 black, 1.0 white) sits farther from
    `luminance` — the contrast floor's escalation pushes an ink color
    toward whichever extreme maximizes its distance from the ground it
    sits on, per the fix's own "lighten/darken toward the farther extreme.\""""
    return 1.0 if luminance <= 0.5 else 0.0


def _step_toward_extreme(rgb: tuple[int, int, int], target_l: float,
                         step: float) -> tuple[int, int, int]:
    """Nudge `rgb`'s HLS lightness by `step` toward `target_l` (0.0 or 1.0
    — see _farther_extreme), clamped so it never overshoots past it. HLS
    lightness is not the same scale as the WCAG relative luminance the
    contrast floor actually measures against, but it is monotonic with it
    — the bounded loop that calls this (_apply_art_contrast_floor)
    re-measures the ACTUAL relative luminance after every step, so this is
    never trusted as more than "which direction to nudge.\""""
    h, l, s = colorsys.rgb_to_hls(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    l = min(target_l, l + step) if target_l >= l else max(target_l, l - step)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def _retreat_with_ink(img: Image.Image, treatment: str, palette: Palette,
                      ink: tuple[int, int, int]) -> Image.Image:
    """_silhouette/_duotone, re-run with `ink` standing in for
    palette.primary — the contrast floor's escalation, sharing the exact
    same treatment functions (and so the exact same "binary stencil" /
    "ramp" guarantees §7.4a's own tests already hold the normal
    palette.primary path to) rather than a parallel implementation. `img`
    is expected to be the PRE-treatment positioned image
    (_ArtPositions.pre_treatment) — re-treating from a clean source rather
    than compounding a second treatment on top of the first."""
    if treatment == "silhouette":
        return _silhouette(img, palette, ink=ink)
    return _duotone(img, palette, ink=ink)


def _apply_art_contrast_floor(positioned: dict[str, Image.Image],
                              pre_treatment: dict[str, Image.Image],
                              layers: list[LayerRef], art_by_id: dict[str, ArtSlot],
                              palette: Palette, canvas: tuple[int, int],
                              warnings: list[str]) -> None:
    """After every art slot is positioned and treated (_position_all_art),
    walk `layers` in z-order a second time, maintaining a running "ground"
    composite of the art painted so far, and for every silhouette/duotone
    slot compare its own opaque pixels' mean WCAG relative luminance
    against the ground beneath its bbox. Scrims are deliberately left out
    of `ground`: every shipped archetype's scrims start at strength 0.0 (a
    no-op — see _apply_scrim's own early exit) and only ever escalate
    later, in compose()'s text-driven autopilot loop, which has not run yet
    at this point — so the ground this function sees already matches the
    ground a silhouette/duotone slot is actually painted onto in practice.

    Mutates `positioned` (and, for a slot that needed re-treating,
    implicitly nothing else — compose() still owns compositing the result
    for real) in place; returns nothing."""
    ground = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for layer in layers:
        if layer.kind != "art":
            continue
        slot = art_by_id[layer.ref]
        img = positioned[slot.id]
        if slot.treatment in ("silhouette", "duotone"):
            bbox = img.getbbox()
            if bbox is not None:
                art_l, _ = _zone_stats(img, bbox, mask=img.getchannel("A"))
                ground_l, _ = _zone_stats(ground, bbox)
                delta = art_l - ground_l
                if abs(delta) < _ART_CONTRAST_FLOOR:
                    before = delta
                    source = pre_treatment.get(slot.id, img)
                    target_l = _farther_extreme(ground_l)
                    ink = ImageColor.getrgb(palette.accent)
                    retreated = _retreat_with_ink(source, slot.treatment, palette, ink)
                    art_l, _ = _zone_stats(retreated, bbox,
                                          mask=retreated.getchannel("A"))
                    delta = art_l - ground_l
                    steps = 0
                    while abs(delta) < _ART_CONTRAST_FLOOR and steps < _ART_CONTRAST_MAX_STEPS:
                        ink = _step_toward_extreme(ink, target_l, _ART_CONTRAST_LIGHTEN_STEP)
                        retreated = _retreat_with_ink(source, slot.treatment, palette, ink)
                        art_l, _ = _zone_stats(retreated, bbox,
                                              mask=retreated.getchannel("A"))
                        delta = art_l - ground_l
                        steps += 1
                    positioned[slot.id] = img = retreated
                    outcome = ("now clears" if abs(delta) >= _ART_CONTRAST_FLOOR
                              else "still falls short of")
                    warnings.append(
                        f"{slot.id}: {slot.treatment} ink was only "
                        f"|ΔL|={abs(before):.3f} from its own ground "
                        f"(floor {_ART_CONTRAST_FLOOR}); switched its ink "
                        f"toward {'white' if target_l == 1.0 else 'black'}, "
                        f"which {outcome} the floor (|ΔL|={abs(delta):.3f}).")
        ground = _composite_layer(ground, img, slot.opacity, slot.blend)


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
    produces this, but a hand-edited/revised spec could). The derived-from-a-
    TextSlot branch shares its 4%-padding formula with _padded_rect (the
    same "around this text" margin the knockout/art_fill panel and motif
    scatter's avoidance rects also use)."""
    if scrim.zone is not None:
        left, top, w, h = zone_px(scrim.zone, canvas)
        return left, top, left + w, top + h
    if scrim.protects is None:
        return None
    slot = text_by_id.get(scrim.protects)
    if slot is None:
        return None
    return _padded_rect(slot.zone, canvas)


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
        _paint_local_panel_scrim(overlay, rgb, strength, left, top, right,
                                 bottom, canvas)
    elif scrim.kind in ("gradient_down", "gradient_up"):
        _paint_gradient_scrim(overlay, scrim.kind, rgb, strength,
                              left, top, right, bottom, canvas)
    elif scrim.kind == "vignette":
        _paint_vignette_scrim(overlay, rgb, strength, left, top, right, bottom)
    out = base.copy()
    out.alpha_composite(overlay)
    return out


# -- local panel scrim (v2 BODY wave: "de-mute the composer") ----------------
#
# The DEFAULT `panel` scrim kind, redesigned: reference-DNA beta feedback
# named scrims washing covers out ("reads as AI image + blank space, muted,
# timid") — traced to `gradient_down`/`gradient_up`'s "extend a solid fill to
# the nearest canvas edge" behavior (§7.3), which a protecting scrim used
# unconditionally even for a small text zone. Those two kinds keep that
# exact behavior UNCHANGED below — full-bleed archetypes that deliberately
# want a dramatic sky/ground wash still get it — but `panel` (every
# ArchetypeScrim/ScrimSpec's own default `kind`, so this is what a newly
# authored or default-kind scrim gets) is now a soft-edged, rounded, Gaussian
# -feathered local patch, hard-clipped so it PROVABLY never dims a single
# pixel outside its own (already 4%-padded) rect — no wash to the canvas
# edge, ever. The legibility autopilot's escalate-then-flip contract doesn't
# change at all (compose()'s own loop above only ever raises `strength`); a
# scrim using `panel` just means escalation raises THIS local panel's
# opacity instead of a hard box's.

_PANEL_FEATHER_FRACTION = 0.028       # of canvas height, gaussian blur sigma cap
_PANEL_FEATHER_RECT_FRACTION = 0.22   # cap as a fraction of the rect's shorter side
_PANEL_CORNER_FRACTION = 0.30         # corner radius, as a fraction of the
                                      # rect's shorter side / 2


def _paint_local_panel_scrim(overlay: Image.Image, rgb: tuple[int, int, int],
                             strength: float, left: int, top: int, right: int,
                             bottom: int, canvas: tuple[int, int]) -> None:
    """A rounded rectangle exactly filling (left, top, right, bottom),
    Gaussian-blurred for a soft edge, then hard-clipped back to that same
    rect — so the panel's CENTER reads solid (any zone comfortably bigger
    than the feather radius stays at full `strength` across the text itself,
    the padding margin is what tapers) while its boundary is provably never
    crossed: multiplying by a hard binary mask of the rect zeroes anything
    the blur spread past it, by construction, not by tuning. The feather
    radius is capped as a fraction of the rect's own shorter side so a very
    small zone (a `series` eyebrow, say) still gets a visible, proportionally
    -softened panel instead of being blurred down to nothing."""
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return
    ch = canvas[1]
    feather_px = max(1.0, min(_PANEL_FEATHER_FRACTION * ch,
                              _PANEL_FEATHER_RECT_FRACTION * min(w, h)))
    radius = max(1, min(round(_PANEL_CORNER_FRACTION * min(w, h) / 2),
                        min(w, h) // 2))
    mask = Image.new("L", canvas, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (left, top, right - 1, bottom - 1), radius=radius, fill=255)
    feathered = mask.filter(ImageFilter.GaussianBlur(feather_px))
    clip = Image.new("L", canvas, 0)
    ImageDraw.Draw(clip).rectangle((left, top, right - 1, bottom - 1), fill=255)
    alpha = ImageChops.multiply(feathered, clip)
    if strength < 1.0:
        alpha = alpha.point(lambda v: round(v * strength))
    block = Image.new("RGBA", canvas, (*rgb, 0))
    block.putalpha(alpha)
    overlay.alpha_composite(block)


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


def _zone_stats(img: Image.Image, rect: tuple[int, int, int, int],
                mask: Image.Image | None = None) -> tuple[float, float]:
    """Mean and stddev of relative luminance (both 0..1) under `rect` of the
    CURRENT composite — the "how legible would text be here, and how busy is
    the backdrop" readout the autopilot bases every decision on.

    `mask` (an 'L' image the same size as `img`) restricts the sample
    further, to wherever the mask is opaque within `rect` — the "thing
    inside of thing" device (TextSlot.mask_from): a title living inside a
    lighthouse beam reads its contrast against the beam's own interior, not
    the dark sky around it. Binarized at the same threshold
    _apply_mask_from's own stencil uses, since ImageStat's mask semantics
    are a boolean selector, not a weight. Falls back to the unmasked
    reading if the mask happens to be entirely empty inside `rect` (a
    degenerate archetype where the container never actually reaches the
    text's own zone) rather than dividing by zero."""
    left, top, right, bottom = rect
    left, top = max(0, left), max(0, top)
    right, bottom = min(img.width, right), min(img.height, bottom)
    if right <= left or bottom <= top:
        return 0.5, 0.0   # a degenerate/out-of-canvas zone: neutral, no data
    crop = img.convert("RGB").crop((left, top, right, bottom))
    band = _luminance_band(crop)
    if mask is not None:
        mask_crop = mask.crop((left, top, right, bottom)).point(
            lambda v: 255 if v > _SILHOUETTE_ALPHA_THRESHOLD else 0)
        stat = ImageStat.Stat(band, mask=mask_crop)
        if stat.count[0] > 0:
            return stat.mean[0] / 255.0, stat.stddev[0] / 255.0
    stat = ImageStat.Stat(band)
    return stat.mean[0] / 255.0, stat.stddev[0] / 255.0


# -- dead-band metric (fix 4, v2.1 BODY-fix wave) -----------------------------
#
# Three live covers shipped with a full third of the canvas doing nothing at
# all. _dead_band_frac measures the tallest such stretch so a template's
# emptiness can be judged by a number, not eyeballed off a thumbnail.

def _dead_band_frac(image: Image.Image) -> tuple[float, int, int]:
    """The tallest contiguous run of near-flat rows (per-row relative-
    luminance stddev under _DEAD_BAND_ROW_STDDEV_THRESHOLD), as a fraction
    of canvas height, plus its own (top, bottom) row bounds in the ORIGINAL
    image's px — the composer's one full-canvas, post-composite read of
    "how much of this cover is doing nothing." `image` is the FINAL
    flattened composite (text, art, scrims all baked in), so nothing this
    function measures can tell "texture on a gradient" apart from "an
    actually blank stretch" except the threshold itself — which is the
    point: a deliberately quiet band (reference DNA item 7's paper grain, a
    gradient step) should still measure quiet, and a title, a silhouette,
    or an ornament crossing a row should not.

    Downsamples to _DEAD_BAND_SAMPLE_HEIGHT rows first (see that constant's
    own comment) — the returned fraction is scale-invariant either way, and
    the px bounds are scaled back up to `image`'s own size before returning."""
    cw, ch = image.size
    sample_h = min(ch, _DEAD_BAND_SAMPLE_HEIGHT)
    sample_w = max(1, round(cw * sample_h / ch)) if ch else cw
    band = _luminance_band(image)
    if (sample_w, sample_h) != (cw, ch):
        band = band.resize((sample_w, sample_h), Image.Resampling.BILINEAR)

    best_len, best_top = 0, 0
    run_len, run_top = 0, 0
    for y in range(sample_h):
        row = band.crop((0, y, sample_w, y + 1))
        row_stddev = ImageStat.Stat(row).stddev[0] / 255.0
        if row_stddev < _DEAD_BAND_ROW_STDDEV_THRESHOLD:
            if run_len == 0:
                run_top = y
            run_len += 1
            if run_len > best_len:
                best_len, best_top = run_len, run_top
        else:
            run_len = 0

    frac = best_len / sample_h if sample_h else 0.0
    top_px = round(best_top / sample_h * ch) if sample_h else 0
    bottom_px = round((best_top + best_len) / sample_h * ch) if sample_h else 0
    return frac, top_px, bottom_px


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


# -- legibility autopilot: worst-region scoring (v2 BODY wave) ---------------
#
# A mean-luminance-over-the-whole-zone score can pass a slot whose backdrop
# is HALF perfectly legible and half illegible — a real cover shipped a dark
# title straddling a busy dark border and a cream clearing that scored fine
# on average while its outer letters were unreadable. `_worst_region_contrast`
# grids the zone into _CONTRAST_GRID x _CONTRAST_GRID cells and scores the
# slot by its WORST cell instead: "readable everywhere the text actually
# sits," not "readable on average." The escalate-then-flip contract above is
# unchanged — only what `ratio` MEANS changed; every call site still just
# compares it to `threshold`.

_CONTRAST_GRID = 3   # cells per side


def _grid_cells(rect: tuple[int, int, int, int], n: int
               ) -> list[tuple[int, int, int, int]]:
    """`rect` partitioned into an n x n grid of sub-rectangles, covering it
    exactly (fractional rounding lands on whichever cell boundary it lands
    on — cells are not required to be pixel-identical in size)."""
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    cells = []
    for row in range(n):
        y0 = top + round(h * row / n)
        y1 = top + round(h * (row + 1) / n)
        for col in range(n):
            x0 = left + round(w * col / n)
            x1 = left + round(w * (col + 1) / n)
            cells.append((x0, y0, x1, y1))
    return cells


def _rects_intersect(a: tuple[int, int, int, int],
                     b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _worst_region_contrast(img: Image.Image, rect: tuple[int, int, int, int],
                           color_rgb: tuple[int, int, int],
                           ink_bbox: tuple[int, int, int, int] | None,
                           mask: Image.Image | None = None) -> float:
    """The MINIMUM WCAG contrast ratio across `rect`'s _CONTRAST_GRID x
    _CONTRAST_GRID cells, each measured the same way _zone_stats measures
    the whole zone (mean luminance, optionally further restricted to
    `mask`). `ink_bbox` (typeset.text_mask(...).getbbox(), computed once by
    the caller from the already-fitted text) skips any cell the fitted
    glyphs never reach — a large zone holding a short line of text should
    not be scored on its own empty margin — falling back to every cell when
    `ink_bbox` is None (no ink at all: an empty fit, or called before one
    exists) or when, degenerately, nothing intersects it."""
    ratios = [
        _contrast_against_luminance(color_rgb, _zone_stats(img, cell, mask)[0])
        for cell in _grid_cells(rect, _CONTRAST_GRID)
        if ink_bbox is None or _rects_intersect(cell, ink_bbox)]
    if not ratios:
        return _contrast_against_luminance(color_rgb, _zone_stats(img, rect, mask)[0])
    return min(ratios)


__all__ = ["EBOOK_H", "EBOOK_W", "PROCEDURAL_SYNTHESIZERS", "THUMB_LARGE",
          "THUMB_SMALL", "ComposeError", "compose", "save_renders"]
