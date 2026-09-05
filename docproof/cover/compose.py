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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import (Image, ImageChops, ImageColor, ImageDraw, ImageEnhance,
                 ImageFilter, ImageOps, ImageStat)

from . import balance, typeset
from .archetypes import zone_px
from .effects import (apply_adjust, apply_effect_stack, apply_mask,
                      resolve_mask,
                      composite_layer as _composite_layer,
                      lightness_band as _lightness_band,
                      luminance_band as _luminance_band,
                      srgb_to_linear as _srgb_to_linear)
from .imaging import has_real_alpha
from .model import (ArtSlot, CoverSpec, LayerRef, Palette, RenderReport,
                    ScrimSpec, Shadow, TextSlot, Zone, effect_from_shadow)
from .procedural import (FRAME_PROCEDURAL_KINDS, PROCEDURAL_SYNTHESIZERS,
                         _frame_clamp_text, _frame_inner_rect, _grain_layer,
                         _procedural_art)
from .textures import TEXTURES

log = logging.getLogger("docproof.cover.compose")

EBOOK_W, EBOOK_H = 1600, 2560

# Amazon search-result and contact-sheet widths.
THUMB_LARGE = 300
THUMB_SMALL = 100


_CONTRAST_THRESHOLDS = {"title": 4.5, "subtitle": 3.0, "author": 4.5, "series": 3.0}
_SCRIM_STEP = 0.15
_SCRIM_CAP = 0.85
_STDDEV_SHADOW_THRESHOLD = 0.22
_SCRIM_PAD_FRACTION = 0.04          # of canvas, on every side of a derived scrim zone
_TEXT_COLOR_FALLBACKS = ("#111111", "#f5f1e8")   # near-black / warm parchment


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
_CORNER_MARGIN_FRACTION = 0.02        # keeps corner ornaments inside the trim

# A contain-fit layer may overlap a text zone, but not bury its ink.
_OCCLUSION_THRESHOLD = 0.30
# Fixed order preserves deterministic placement.
_OCCLUSION_ANCHOR_OFFSETS: tuple[float, ...] = (0.10, -0.10, 0.20, -0.20, 0.25, -0.25)

# Flat-color treatments need a minimum luminance difference from their ground.
_ART_CONTRAST_FLOOR = 0.12
_ART_CONTRAST_LIGHTEN_STEP = 0.10     # HLS lightness step per bounded-loop try
_ART_CONTRAST_MAX_STEPS = 10          # hard cap — guarantees the loop ends

# Dead bands are flat rows without meaningful transitions to adjacent rows.
_DEAD_BAND_SAMPLE_HEIGHT = 640        # downsample rows before scanning — a
                                      # "band" spans real fractions of the
                                      # cover, so sub-pixel-row precision buys
                                      # nothing; keeps the scan O(640) rather
                                      # than O(canvas height), matching this
                                      # module's existing downsample-for-cost
                                      # discipline (_grain_layer et al.)
_DEAD_BAND_ROW_STDDEV_THRESHOLD = 0.020   # of 0..1 relative luminance
# Mean-row deltas keep horizontal rules from being classified as empty.
_DEAD_BAND_ROW_DELTA_THRESHOLD = 0.020    # of 0..1 relative luminance
_DEAD_BAND_WARN_FRACTION = 0.28


_LINE_GAP_FALLBACK_MARGIN_FRACTION = 0.012   # of canvas height: clearance
                                             # below the last line's own ink
                                             # when a title has no internal
                                             # gap to snap into (one line)
_LINE_GAP_CONTACT_THRESHOLD = 0.04   # fraction of the text's own ink alpha
                                     # the snapped ornament may still cover
                                     # before nudging — tighter than the
                                     # general contact guard's 8% because
                                     # this IS the archetype's own
                                     # deliberate weave-through-title move
                                     # and should read as threading the
                                     # gap cleanly, not just "mostly" clear
_LINE_GAP_NUDGE_FRACTIONS: tuple[float, ...] = (0.02, -0.02, 0.04)   # of
                                     # canvas height, tried in this fixed
                                     # order — smallest first, alternating
                                     # side, the third doubled — same
                                     # determinism reasoning as
                                     # _OCCLUSION_ANCHOR_OFFSETS

# Unplanned art-text collisions use a tighter threshold than deliberate
# sandwich overlaps.
_TEXT_ART_CONTACT_THRESHOLD = 0.08
# Shift positioned pixels so the nudge is independent of placement semantics.
_TEXT_ART_CONTACT_NUDGES: tuple[float, ...] = (0.06, -0.06, 0.12, -0.12)

# Tight padding around a frame notch's target.
_NOTCH_PAD_FRACTION = 0.015

# Final contrast recovery escalates scrims, flips ink, then attenuates
# finishing layers identified by this reserved prefix.
_FX_PREFIX = "fx_"
# Geometric halving reaches this floor in bounded work.
_FX_ATTENUATION_FLOOR = 0.05


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
    adjust_by_id = {a.id: a for a in spec.adjust}
    strengths = {i: s.strength for i, s in enumerate(spec.scrims)}
    finalized: dict[str, _ResolvedText] = {}
    # Keep attenuation local so compose never mutates a reusable spec.
    fx_opacity: dict[str, float] = {}

    contrast: dict[str, float] = {}
    fitted_sizes: dict[str, float] = {}
    warnings: list[str] = []
    # Clamp before fitting so derived scrims use the same safe frame interior.
    text_by_id = _frame_clamp_text(text_by_id, spec, canvas, warnings)
    layers = _degrade_opaque_focal(spec.layers, art_by_id, job_dir, warnings)

    # Position once because replayed scrim passes cannot affect art geometry.
    text_avoid_rects = _text_zone_rects(spec.text, canvas)
    text_ink_for_masks = _text_ink_for_masks(spec, text_by_id, canvas)
    art_positions = _position_all_art(
        spec.art, layers, job_dir, canvas, spec.palette, spec.version,
        text_avoid_rects, text_by_id, text_ink_for_masks)
    positioned_art = art_positions.positioned
    layers = art_positions.layers
    warnings.extend(art_positions.warnings)

    _apply_art_contrast_floor(positioned_art, art_positions.pre_treatment,
                              layers, art_by_id, spec.palette, canvas, warnings)

    # Snap after placement guards so later measurements see final geometry.
    adjustments: list[str] = []
    if spec.axis is not None:
        _snap_positions(spec, canvas, positioned_art, layers, art_by_id,
                        text_by_id, adjustments)

    # Mask inputs are fixed before replay begins.
    adjust_masks = {
        a.id: resolve_mask(a.mask, canvas, positioned_art, text_ink_for_masks)
        for a in spec.adjust if a.mask is not None}

    def render_upto(stop: int) -> Image.Image:
        """Everything in `layers[:stop]`, freshly composited: art at its
        always-deterministic pixels (looked up from `positioned_art`, not
        recomputed), scrims at their CURRENT (possibly escalated) strength,
        adjust layers computed over the composite as it stands (§15.3), and
        any text slot already resolved drawn exactly as it was the first
        time. Never includes the text slot AT `stop` — the caller measures
        this result to decide how to draw it. Art and adjust opacities read
        through `fx_opacity` so a §15.7 attenuation replays consistently."""
        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        for layer in layers[:stop]:
            if layer.kind == "art":
                slot = art_by_id[layer.ref]
                img = _composite_layer(img, positioned_art[layer.ref],
                                       fx_opacity.get(layer.ref, slot.opacity),
                                       slot.blend)
            elif layer.kind == "scrim":
                idx = int(layer.ref)
                img = _apply_scrim(img, spec.scrims[idx], strengths[idx],
                                   text_by_id, spec.palette, canvas)
            elif layer.kind == "adjust":
                adj = adjust_by_id[layer.ref]
                img = apply_adjust(
                    img, adj, spec.palette, canvas, adjust_masks.get(layer.ref),
                    opacity=fx_opacity.get(layer.ref, adj.opacity))
            else:  # "text"
                resolved = finalized.get(layer.ref)
                if resolved is not None:
                    img = _draw_resolved_text(img, resolved, canvas,
                                              positioned_art, spec.palette)
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
        # Panel modes measure their primary-colored panel or outline.
        color_hex = (spec.palette.primary if slot.mode != "fill"
                    else spec.palette.get(slot.color_role))

        # Masked text measures contrast only inside its container.
        container_mask = (positioned_art[slot.mask_from].getchannel("A")
                          if slot.mask_from and slot.mask_from in positioned_art
                          else None)

        # Fit first so contrast scoring can ignore cells without glyph ink.
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
            # Measure once so replays do not duplicate the warning.
            text_layer = _render_text_only(slot, fit, color_hex, shadow, canvas,
                                           emphasis_color=spec.palette.accent)
            _, coverage = _clip_text_to_container(
                text_layer, positioned_art.get(slot.mask_from), canvas)
            if coverage < _TEXT_MASK_MIN_COVERAGE:
                warnings.append(
                    f"{slot.id}: only {coverage:.0%} of its ink landed "
                    f"inside '{slot.mask_from}' — move the container or "
                    f"shrink the {slot.id}.")

    final_image = render_upto(len(layers))

    def render_ground() -> Image.Image:
        """The finished stack WITHOUT the text layers — art, scrims, and
        adjust layers at their CURRENT strengths/opacities. Every final-
        composite contrast measurement below reads this rather than
        `final_image`, because measuring a zone that contains the text's
        own pixels would bias it toward the ink color and cry wolf on
        every clean cover (the long-standing safety-net reasoning). The
        one approximation that buys: an adjust op here transforms a
        text-free composite, while the shipped render's op also saw the
        glyphs — for judging INK-vs-GROUND legibility, the text-free
        reading is the honest one."""
        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        for layer in layers:
            if layer.kind == "art":
                slot = art_by_id[layer.ref]
                img = _composite_layer(img, positioned_art[layer.ref],
                                       fx_opacity.get(layer.ref, slot.opacity),
                                       slot.blend)
            elif layer.kind == "scrim":
                idx = int(layer.ref)
                img = _apply_scrim(img, spec.scrims[idx], strengths[idx],
                                   text_by_id, spec.palette, canvas)
            elif layer.kind == "adjust":
                adj = adjust_by_id[layer.ref]
                img = apply_adjust(
                    img, adj, spec.palette, canvas, adjust_masks.get(layer.ref),
                    opacity=fx_opacity.get(layer.ref, adj.opacity))
        return img

    # Finishing layers can invalidate draw-time contrast. Only invoke the
    # recovery ladder when such a layer sits above the text.
    ground = render_ground()
    ladder_changed = False
    text_positions: dict[str, int] = {}
    for i, layer in enumerate(layers):
        if layer.kind == "text" and layer.ref not in text_positions:
            text_positions[layer.ref] = i
    for ref, resolved in finalized.items():
        zl, zt, zw, zh = zone_px(resolved.slot.zone, canvas)
        rect = (zl, zt, zl + zw, zt + zh)
        container_mask = (positioned_art[resolved.slot.mask_from].getchannel("A")
                          if resolved.slot.mask_from
                          and resolved.slot.mask_from in positioned_art
                          else None)
        ink_bbox = typeset.text_mask(resolved.slot, resolved.fit, canvas).getbbox()

        def remeasure(img: Image.Image, color: str) -> float:
            return _worst_region_contrast(img, rect, ImageColor.getrgb(color),
                                          ink_bbox, container_mask)

        final_ratio = remeasure(ground, resolved.color)
        threshold = _CONTRAST_THRESHOLDS[resolved.slot.id]
        finishing_above = list(dict.fromkeys(
            layer.ref for layer in layers[text_positions.get(ref, len(layers)) + 1:]
            if layer.kind == "adjust"
            or (layer.kind == "art" and layer.ref.startswith(_FX_PREFIX))))
        if not finishing_above:
            if final_ratio < threshold and final_ratio < contrast[ref] - 0.5:
                warnings.append(
                    f"{ref}: a layer drawn later covers this text — contrast on "
                    f"the finished cover is {final_ratio:.2f} (threshold "
                    f"{threshold}, was {contrast[ref]:.2f} at draw time).")
            continue
        if final_ratio >= threshold:
            continue

        # Retry available scrim headroom against the finished ground.
        protecting = [idx for idx, s in enumerate(spec.scrims) if s.protects == ref]
        while (final_ratio < threshold
               and any(strengths[idx] < _SCRIM_CAP for idx in protecting)):
            for idx in protecting:
                strengths[idx] = min(_SCRIM_CAP, round(strengths[idx] + _SCRIM_STEP, 10))
            ground = render_ground()
            final_ratio = remeasure(ground, resolved.color)
            ladder_changed = True

        # Include the current ink so a fallback cannot lower contrast.
        if final_ratio < threshold:
            options = [(resolved.color, final_ratio)] + [
                (c, remeasure(ground, c)) for c in _TEXT_COLOR_FALLBACKS]
            best_color, best_ratio = max(options, key=lambda pair: pair[1])
            if best_color != resolved.color:
                pre_flip = final_ratio
                resolved = replace(resolved, color=best_color)
                finalized[ref] = resolved
                ladder_changed = True
                outcome = ("now passes" if best_ratio >= threshold
                           else "still falls short")
                warnings.append(
                    f"{ref}: the finishing stack pulled contrast down to "
                    f"{pre_flip:.2f} against threshold {threshold}; flipped "
                    f"text color to {best_color}, which {outcome} "
                    f"({best_ratio:.2f}).")
            final_ratio = best_ratio

        # Halve topmost finishing layers until contrast passes or the floor
        # bounds further work.
        if final_ratio < threshold:
            fx_above = [r for r in finishing_above if r.startswith(_FX_PREFIX)]

            def current_opacity(layer_ref: str) -> float:
                if layer_ref in fx_opacity:
                    return fx_opacity[layer_ref]
                owner = art_by_id.get(layer_ref) or adjust_by_id.get(layer_ref)
                return owner.opacity if owner is not None else 0.0

            while (final_ratio < threshold
                   and any(current_opacity(r) > _FX_ATTENUATION_FLOOR
                           for r in fx_above)):
                for layer_ref in reversed(fx_above):   # top-down
                    cur = current_opacity(layer_ref)
                    if cur <= _FX_ATTENUATION_FLOOR:
                        continue
                    fx_opacity[layer_ref] = round(cur / 2.0, 6)
                    ladder_changed = True
                    warnings.append(
                        f"{layer_ref} halved to {fx_opacity[layer_ref]:g} "
                        f"to keep {ref} legible.")
                    ground = render_ground()
                    final_ratio = remeasure(ground, resolved.color)
                    if final_ratio >= threshold:
                        break

        if final_ratio < threshold:
            warnings.append(
                f"{ref}: still {final_ratio:.2f} against threshold "
                f"{threshold} on the finished cover after scrim escalation, "
                f"an ink flip, and finishing attenuation.")
        contrast[ref] = round(final_ratio, 4)

    if ladder_changed:
        final_image = render_upto(len(layers))

    dead_band_frac, band_top_px, band_bottom_px = _dead_band_frac(final_image)
    if dead_band_frac >= _DEAD_BAND_WARN_FRACTION:
        ch = canvas[1]
        warnings.append(
            f"empty band from {band_top_px / ch:.0%} to "
            f"{band_bottom_px / ch:.0%} of the cover's height "
            f"({dead_band_frac:.0%} of the canvas) has no text, art, or "
            f"ornament ink crossing it.")

    # Balance checks report on the finished image and never alter pixels.
    final_rgb = final_image.convert("RGB")
    warnings.extend(_balance_measurements(spec, canvas, final_rgb,
                                          positioned_art, layers, art_by_id,
                                          finalized))

    report = RenderReport(
        contrast=contrast,
        scrim_final={idx: strengths[idx] for idx in range(len(spec.scrims))},
        fitted_sizes=fitted_sizes,
        warnings=warnings,
        occlusion=art_positions.occlusion,
        dead_band_frac=dead_band_frac,
        adjustments=adjustments)
    return final_rgb, report


def _draw_resolved_text(base: Image.Image, resolved: _ResolvedText,
                        canvas: tuple[int, int],
                        positioned_art: dict[str, Image.Image],
                        palette: Palette) -> Image.Image:
    """Dispatch one already-resolved text slot to whichever renderer its
    `mode` needs: typeset.draw_text for the normal ink-colored `fill`, or
    _draw_knockout_or_art_fill for the two panel/window modes. Both branches
    were handed the SAME `resolved.color` by the autopilot loop above (the
    escalate-then-flip machinery has no idea which mode it is tuning a color
    for), so this is the one place that color's meaning finally forks: ink,
    or panel/outline.

    A slot carrying an effect stack (§15.4 — non-empty `effects`, into
    which the model validator already folded any legacy shadow/stroke)
    takes the ONE engine path instead: _text_layer_with_effects builds the
    styled layer standalone, and the legacy branches below never see it.
    An EMPTY stack is the pre-wave spec shape, and everything below this
    guard is that byte-identical legacy path, untouched.

    When `resolved.slot.mask_from` names a container ("thing inside of
    thing" — a title inside a lighthouse beam), the fully rendered text
    layer (whichever path built it) is clipped to that container's already-
    positioned alpha before it ever reaches `base` — see
    _render_text_only/_clip_text_to_container."""
    slot = resolved.slot
    if slot.effects:
        text_layer = _text_layer_with_effects(resolved, canvas, palette)
        if slot.mask_from:
            text_layer, _coverage = _clip_text_to_container(
                text_layer, positioned_art.get(slot.mask_from), canvas)
        out = base.copy()
        out.alpha_composite(text_layer)
        return out
    if not slot.mask_from:
        if slot.mode == "fill":
            return typeset.draw_text(base, slot, resolved.fit, resolved.color,
                                     resolved.shadow, canvas,
                                     emphasis_color=palette.accent)
        return _draw_knockout_or_art_fill(base, slot, resolved.fit,
                                          resolved.color, canvas)

    text_layer = _render_text_only(slot, resolved.fit, resolved.color,
                                   resolved.shadow, canvas,
                                   emphasis_color=palette.accent)
    clipped, _coverage = _clip_text_to_container(
        text_layer, positioned_art.get(slot.mask_from), canvas)
    out = base.copy()
    out.alpha_composite(clipped)
    return out


def _text_layer_with_effects(resolved: _ResolvedText, canvas: tuple[int, int],
                             palette: Palette) -> Image.Image:
    """One text slot's pixels through the effects engine (§15.4's single
    code path): the bare fill — glyphs at the autopilot's color, WITHOUT
    the legacy shadow or stroke, both of which now live in the stack via
    the model's fold — run through apply_effect_stack.

    The one draw-time composition left to do here is the autopilot's own
    auto-Shadow: it keeps writing to `resolved.shadow` (never `effects` —
    §15.4 says so outright), so when it invented one (`slot.shadow` is
    None but the resolved shadow is not), that shadow joins the FRONT of
    the stack exactly as the fold would have put it. A shadow the slot
    declared itself is already folded in — prepending it again would
    double it."""
    slot = resolved.slot
    # model_copy, not re-validation: stripping stroke/shadow off a copy for
    # the bare-fill render must not re-trigger the fold (which would see
    # effects non-empty and try to help).
    bare = slot.model_copy(update={"shadow": None, "stroke": None})
    fill = _render_text_only(bare, resolved.fit, resolved.color, None, canvas,
                             emphasis_color=palette.accent)
    stack = list(slot.effects)
    if resolved.shadow is not None and slot.shadow is None:
        stack.insert(0, effect_from_shadow(resolved.shadow))
    return apply_effect_stack(fill, stack, palette, canvas)


def _render_text_only(slot: TextSlot, fit: typeset.FitResult, color: str,
                      shadow: Shadow | None, canvas: tuple[int, int],
                      emphasis_color: str | None = None) -> Image.Image:
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
        return typeset.draw_text(blank, slot, fit, color, shadow, canvas,
                                 emphasis_color=emphasis_color)
    return _draw_knockout_or_art_fill(blank, slot, fit, color, canvas)


# Text masks preserve glyph antialiasing instead of applying the hard
# threshold used for art masks.
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


def _mirror_if_asked(slot: ArtSlot, img: Image.Image) -> Image.Image:
    """`ArtSlot.mirror` as a plain horizontal flip of the RAW plate, before
    any fit or placement — so every anchor, offset, scatter and mask
    downstream sees the flipped pixels and needs no special case. Which way
    a generated cutout's severed stem points is whatever the model happened
    to draw, and that decides which trim edge the cut can be carried out
    through; this is the knob that fixes a plate pointing the wrong way
    without regenerating it."""
    return ImageOps.mirror(img) if slot.mirror else img


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
    canvas-space pixels.

    v2.2 wave, deliverable 5: `texture_file` is a third tier, checked after
    `asset` and before `procedural` — a real generated/uploaded asset always
    wins (unchanged priority), but a stocked shelf plate is a more
    deliberate choice than the generic hardcoded-by-id procedural fallback,
    so it is tried first when there is no `asset`. Falls through to
    `procedural` (not a raised ComposeError) if the named plate somehow
    fails to load — CoverSpec validation already refused an unknown name at
    spec-build time, so a load failure here means the shipped file itself
    went missing or corrupt after that check, and this module's own
    convention (see the module docstring) is that every input renders
    SOMETHING rather than raising, except a genuinely unreadable `asset`."""
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
    if slot.texture_file:
        textured = _load_texture(slot, canvas)
        if textured is not None:
            return textured
    return _procedural_art(slot, canvas, palette, version)


def _load_texture(slot: ArtSlot, canvas: tuple[int, int]) -> Image.Image | None:
    """slot.texture_file's own pixels as a canvas-sized RGBA image (v2.2
    wave, deliverable 5) — `texture_fit="cover"` scales the plate to fill
    the canvas exactly the way _fit_cover does for a generated asset
    (centered: a stocked plate has no book-specific focal point to anchor
    on), `"tile"` repeats the plate at its own native size across the whole
    canvas, wrapping at both edges.

    Returns None if the name is somehow not on the (possibly test-swapped)
    shelf, or the file fails to decode — defensive; CoverSpec validation
    (ArtSlot._known_texture) already refuses an unknown name long before a
    real spec ever reaches compose()."""
    path = TEXTURES.get(slot.texture_file)
    if path is None:
        return None
    try:
        plate = Image.open(path)
        plate.load()
        plate = plate.convert("RGBA")
    except Exception:  # noqa: BLE001 - a corrupt shelf file is the same story
        return None
    if slot.texture_fit == "cover":
        return _fit_cover(plate, canvas, (0.5, 0.5), 1.0, (0.0, 0.0))
    # "tile": repeat the plate's own native pixels across the canvas,
    # wrapping at both edges — a plain nested paste loop, the same cost
    # class as _grain_layer's own downsample-then-resize approach (at most
    # a few dozen alpha_composite calls for a realistic plate/canvas ratio,
    # never a per-pixel Python loop).
    cw, ch = canvas
    pw, ph = plate.size
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    for y in range(0, ch, max(1, ph)):
        for x in range(0, cw, max(1, pw)):
            out.alpha_composite(plate, dest=(x, y))
    return out


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
                   scale: float, flip_vertical: bool = False) -> Image.Image:
    """§7.4a's mirrored corner frame: the ornament, resized so its height is
    `scale` of the canvas height (there is no cover/contain baseline to zoom
    from here, unlike _fit_cover/_fit_contain's own reading of `scale` — so
    `scale` is read directly against the canvas, the same convention
    SCATTER_SIZE_FRACTION's fixed 14% uses below), placed inset
    _CORNER_MARGIN_FRACTION from the top-left corner, then mirrored into the
    other three. Every copy is an exact Pillow ImageOps.mirror/flip of the
    same resized source, so the four corners are byte-for-byte symmetric —
    the "exact symmetry is the tell generators can't produce" effect the
    spec calls for, not an approximation of one.

    v2.2 wave, deliverable 1 (gravity-safe corners): `flip_vertical=False`
    (the new default) keeps EVERY copy upright — only the right-side copies
    get horizontally mirrored, top-left and bottom-left stay byte-identical
    to `source` itself, top-right and bottom-right byte-identical to each
    other. A live render's mirrored honey-drip ornament pointed UP on both
    bottom corners under the old unconditional ImageOps.flip: gravity has an
    opinion most ornaments' own weight already encodes (a drip, a hanging
    charm, anything bottom-heavy), and a v-flip inverts it. `flip_vertical=
    True` restores the ORIGINAL full-mirror-into-all-four behavior (bottom
    copies also vertically flipped) for a genuinely top/bottom-symmetric
    ornament that wants the fuller kaleidoscope effect on purpose.

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
    if flip_vertical:
        bottom_left = ImageOps.flip(resized)
        bottom_right = ImageOps.flip(mirrored_h)
    else:
        bottom_left = resized
        bottom_right = mirrored_h

    slack_x, slack_y = max(0, cw - target_w), max(0, ch - target_h)
    inset_x = min(round(_CORNER_MARGIN_FRACTION * cw), slack_x // 2)
    inset_y = min(round(_CORNER_MARGIN_FRACTION * ch), slack_y // 2)
    left_x, top_y = inset_x, inset_y
    right_x, bottom_y = slack_x - inset_x, slack_y - inset_y

    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(resized, dest=(left_x, top_y))
    out.alpha_composite(mirrored_h, dest=(right_x, top_y))
    out.alpha_composite(bottom_left, dest=(left_x, bottom_y))
    out.alpha_composite(bottom_right, dest=(right_x, bottom_y))
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


# ArtSlot.mask_from folds into the same hard-threshold mask path.


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


# Whole-word coverage can hide a fully buried glyph, so readability uses a
# separate per-glyph limit.
_GLYPH_OCCLUSION_THRESHOLD = 0.50

# Ink below this is antialiasing, not a glyph. Projecting without a threshold
# merges neighbouring letters through their own soft edges, which would hand
# the measurement back exactly the blind spot it exists to close.
_GLYPH_INK_FLOOR = 32


def _runs(projection: Sequence[int]) -> list[tuple[int, int]]:
    """Contiguous (start, end-exclusive) runs of truth in a 0/1 projection."""
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(projection):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(projection)))
    return runs


def _glyph_boxes(ink_mask: Image.Image) -> list[tuple[int, int, int, int]]:
    """`ink_mask` cut into one box per glyph: row runs first (the lines), then
    column runs inside each line (the letters).

    Pillow has no connected-components, and it does not need one here —
    set type on a horizontal baseline separates into columns with no ink
    between them, which is what a projection finds for free in C. Two glyphs
    that genuinely touch (a tight kern, a ligature) come back as one box,
    which is the safe direction to be wrong in: a merged pair measures as a
    bigger unit and is therefore harder, not easier, to declare buried.

    Returns [] for an empty mask, so every caller's max() needs a default."""
    solid = ink_mask.point(lambda v: 255 if v >= _GLYPH_INK_FLOOR else 0)
    _, rows = solid.getprojection()
    boxes: list[tuple[int, int, int, int]] = []
    for top, bottom in _runs(rows):
        line = solid.crop((0, top, solid.width, bottom))
        cols, _ = line.getprojection()
        for left, right in _runs(cols):
            boxes.append((left, top, right, bottom))
    return boxes


def _worst_glyph_occlusion(ink_mask: Image.Image, art_alpha: Image.Image,
                           boxes: Sequence[tuple[int, int, int, int]]) -> float:
    """The most-buried single glyph's occlusion fraction — the number that
    decides whether a word is still a word. `boxes` comes from _glyph_boxes
    and is computed ONCE per text slot by the caller: the glyphs do not move
    when the art does, and recomputing them per search candidate would pay
    for the same projection six times."""
    worst = 0.0
    for box in boxes:
        ink = ink_mask.crop(box)
        total = ImageStat.Stat(ink).sum[0]
        if total <= 0:
            continue
        covered = ImageChops.multiply(ink, art_alpha.crop(box))
        worst = max(worst, ImageStat.Stat(covered).sum[0] / total)
    return worst


def _occlusion_severity(whole: float, worst_glyph: float,
                        limit: float = _OCCLUSION_THRESHOLD) -> float:
    """The two limits as one comparable number: 1.0 is exactly at the limit,
    over 1.0 is a violation, and whichever limit is closer to breaking is the
    one that speaks. Lets a search rank candidates by "how bad" without
    having to decide up front which kind of bad it is looking for.

    `limit` is the caller's own whole-word threshold, because the three
    guards that ask this question tolerate very different amounts of overlap
    (30% for a sandwich that is SUPPOSED to overlap, 8% for incidental
    contact, 4% for something nudged into a line gap). The per-glyph limit
    is deliberately NOT scaled with it: a half-buried letter is an
    unreadable letter whichever guard is asking. On the tight guards the
    glyph term will rarely be the one that binds — one letter of nine gone
    is already ~11% of the word — which is exactly right. The blind spot
    this closes lives at the loose end, where a word can lose a whole letter
    and still measure 15%.""" 
    return max(whole / limit, worst_glyph / _GLYPH_OCCLUSION_THRESHOLD)


def _place_contain_clear_of_text(source: Image.Image, canvas: tuple[int, int],
                                 slot: ArtSlot, palette: Palette, has_alpha: bool,
                                 text_slot: TextSlot
                                 ) -> tuple[Image.Image, float, float, bool]:
    """The title occlusion guard's search (fix 2, v2.1 BODY-fix wave):
    `slot`'s normal contain-fit placement at its own declared anchor, if
    that already covers at most _OCCLUSION_THRESHOLD of `text_slot`'s
    fitted ink; otherwise, nudge the anchor's x by each of
    _OCCLUSION_ANCHOR_OFFSETS in turn (clamped to [0, 1] — an anchor is a
    fraction of the canvas, same convention as everywhere else
    _fit_contain reads one) and take the first that clears the threshold.
    If none of them does, place the slot back at its DECLARED anchor and let
    the caller do the z-order swap — see the comment on that return.

    Returns (the UNTREATED placed image at the winning anchor, its
    whole-word occlusion fraction, its WORST SINGLE GLYPH's occlusion
    fraction, whether every offset still failed — the caller's cue to
    degrade to drawing this art BELOW the text instead, per §5.2.3). Both
    fractions come back because either one can be the reason a placement was
    rejected, and a warning that quotes the wrong one sends a reader
    looking for a crowding problem that is really a buried letter.
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
    # Once, not per candidate: the glyphs do not move when the art does.
    boxes = _glyph_boxes(ink_mask)

    def measure(anchor: tuple[float, float]
                ) -> tuple[Image.Image, float, float]:
        placed = _fit_contain(source, canvas, anchor, slot.scale,
                              slot.offset, slot.keep_whole)
        treated, _warning = _apply_treatment(placed, slot.treatment, palette,
                                             slot.id, has_alpha)
        alpha = treated.getchannel("A")
        return (placed, _occlusion_fraction(ink_mask, alpha),
                _worst_glyph_occlusion(ink_mask, alpha, boxes))

    ax, ay = slot.anchor
    home_img, home_ratio, home_glyph = measure((ax, ay))
    if _occlusion_severity(home_ratio, home_glyph) <= 1.0:
        return home_img, home_ratio, home_glyph, False

    for dx in _OCCLUSION_ANCHOR_OFFSETS:
        trial_img, trial_ratio, trial_glyph = measure(
            (min(1.0, max(0.0, ax + dx)), ay))
        if _occlusion_severity(trial_ratio, trial_glyph) <= 1.0:
            return trial_img, trial_ratio, trial_glyph, False
        # The z-order fallback clears occlusion without moving a deliberately
        # overset edge back inside the trim.
    return home_img, home_ratio, home_glyph, True


def _place_contain_at_center(source: Image.Image, canvas: tuple[int, int],
                             scale: float, offset: tuple[float, float],
                             center: tuple[float, float]) -> Image.Image:
    """Like _fit_contain, but placed by its own CENTER at an exact px point
    rather than derived from an `anchor` fraction against the whole canvas
    — the line-gap snap (v2.2 wave, deliverable 3) knows exactly where it
    wants the ornament's middle to land (the biggest real gap between two
    of a title's own fitted lines), and that point moves depending on how
    many lines the title's own fit search produced — no fixed `anchor`
    fraction could express it. `offset` still nudges by a fraction of the
    canvas, same convention as every other fit function here."""
    cw, ch = canvas
    iw, ih = source.size
    fit_scale = min(cw / iw, ch / ih) * max(scale, 1e-6)
    new_w = max(1, round(iw * fit_scale))
    new_h = max(1, round(ih * fit_scale))
    resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
    cx, cy = center
    ox, oy = offset
    dest_x = round(cx - new_w / 2 + ox * cw)
    dest_y = round(cy - new_h / 2 + oy * ch)
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(resized, dest=(dest_x, dest_y))
    return out


def _snap_to_line_gap(source: Image.Image, canvas: tuple[int, int],
                      slot: ArtSlot, palette: Palette, has_alpha: bool,
                      text_slot: TextSlot) -> tuple[Image.Image, float, str | None]:
    """ArtSlot.snap == "line_gap" (v2.2 wave, deliverable 3): center a
    contain-fit ornament in the biggest REAL whitespace between two of
    `text_slot`'s own fitted lines, rather than at a fixed anchor point
    that has no idea where the glyphs actually landed — the woven_emblem
    `weave` slot's own complaint (a vine/ornament grazing letterforms it
    was meant to thread between, on two live covers).

    `text_slot` is fitted once more here (typeset.fit_text is pure and
    cheap — the same low-cost re-run _place_contain_clear_of_text already
    does for the same reason: _position_all_art runs strictly before
    compose()'s own per-text loop, so nothing has fitted this slot yet).
    typeset.line_ink_boxes gives every fitted line's own real ink bbox; the
    largest gap between two CONSECUTIVE lines' ink (bottom of one to top of
    the next) wins, by `max()` — a uniform-line-height title has every
    nominal gap the same size, so ties resolve to the first (topmost) one,
    deterministically. A one-line title has no internal gap at all: fall
    back to just below that single line's own ink, per this deliverable's
    own spec.

    Returns (the UNTREATED placed image at the winning position, the
    contact fraction achieved, a warning sentence or None) — mirrors
    _place_contain_clear_of_text's own "return untreated, let the caller's
    single uniform _apply_treatment call do it for real" contract, so
    `pre_treatment` still gets populated correctly for the art-vs-ground
    contrast floor (fix 3)."""
    fit = typeset.fit_text(text_slot, canvas)
    ink_boxes = [b for b in typeset.line_ink_boxes(text_slot, fit, canvas)
                if b is not None]
    cw, ch = canvas
    cx = slot.anchor[0] * cw

    if len(ink_boxes) >= 2:
        gaps = [(t2 - b1, (b1 + t2) / 2.0)
               for (_, _, _, b1), (_, t2, _, _) in zip(ink_boxes, ink_boxes[1:])]
        _, cy = max(gaps, key=lambda g: g[0])
    elif ink_boxes:
        cy = ink_boxes[-1][3] + _LINE_GAP_FALLBACK_MARGIN_FRACTION * ch
    else:
        cy = float(zone_px(text_slot.zone, canvas)[1])   # no ink at all —
                                                          # degenerate; the
                                                          # zone's own top

    ink_mask = typeset.text_mask(text_slot, fit, canvas)
    boxes = _glyph_boxes(ink_mask)

    def measure(center_y: float) -> tuple[Image.Image, float, float]:
        placed = _place_contain_at_center(source, canvas, slot.scale,
                                          tuple(slot.offset), (cx, center_y))
        treated, _warning = _apply_treatment(placed, slot.treatment, palette,
                                             slot.id, has_alpha)
        alpha = treated.getchannel("A")
        return (placed, _occlusion_fraction(ink_mask, alpha),
                _worst_glyph_occlusion(ink_mask, alpha, boxes))

    def severity(whole: float, glyph: float) -> float:
        return _occlusion_severity(whole, glyph, _LINE_GAP_CONTACT_THRESHOLD)

    best_img, best_ratio, best_glyph = measure(cy)
    if severity(best_ratio, best_glyph) <= 1.0:
        return best_img, best_ratio, None

    for frac in _LINE_GAP_NUDGE_FRACTIONS:
        trial_img, trial_ratio, trial_glyph = measure(cy + frac * ch)
        if severity(trial_ratio, trial_glyph) < severity(best_ratio, best_glyph):
            best_img, best_ratio, best_glyph = (trial_img, trial_ratio,
                                                trial_glyph)
        if severity(trial_ratio, trial_glyph) <= 1.0:
            return trial_img, trial_ratio, None

    warning = (
        f"{slot.id}: still touches {best_ratio:.0%} of '{text_slot.id}''s "
        f"ink after nudging into its line gap (limit "
        f"{_LINE_GAP_CONTACT_THRESHOLD:.0%}).")
    return best_img, best_ratio, warning


def _text_ink_for_masks(spec: CoverSpec, text_by_id: dict[str, TextSlot],
                        canvas: tuple[int, int]) -> dict[str, Image.Image]:
    """Fitted glyph coverage ('L', canvas-sized, via typeset.text_mask) for
    every text slot some MaskSpec in this spec clips to (mask.from_text,
    §15.13) — computed once, up front, because text ink depends on nothing
    the art walk produces (fit_text is a pure function of slot + canvas;
    the occlusion guard already leans on exactly that ordering). The fit
    runs again inside compose()'s own text loop later — deterministic, so
    the glyph alpha a mask clips to is pixel-identical to the glyphs
    eventually drawn — and any fit *warning* is deliberately left to that
    later loop, so it lands in the report exactly once. `text_by_id` must
    be the frame-clamped dict compose built, for the same identical-fit
    reason."""
    needed: set[str] = set()
    for slot in spec.art:
        if slot.mask is not None and slot.mask.from_text:
            needed.add(slot.mask.from_text)
    for layer in spec.adjust:
        if layer.mask is not None and layer.mask.from_text:
            needed.add(layer.mask.from_text)
    ink: dict[str, Image.Image] = {}
    for text_id in sorted(needed):
        slot = text_by_id.get(text_id)
        if slot is None:   # dangling ref can't survive validation — defensive
            continue
        fit = typeset.fit_text(slot, canvas)
        ink[text_id] = typeset.text_mask(slot, fit, canvas)
    return ink


def _apply_art_masks(positioned: dict[str, Image.Image], layers: list[LayerRef],
                     art_by_id: dict[str, ArtSlot], canvas: tuple[int, int],
                     text_ink: dict[str, Image.Image],
                     warnings: list[str]) -> None:
    """Second positioning pass (§15.2): every art slot's MaskSpec — the
    legacy mask_from fold included — resolved against the already-
    positioned pixels and multiplied into that slot's alpha, in place.

    Runs in z-order so a chained from_layer reference (A masked by B, B
    masked by C) always reads the referenced slot's FINAL, already-masked
    pixels — the _masks_resolve ordering rule guarantees a from_layer
    reference precedes its user, so walking `layers` forward preserves the
    exact semantics the old single-pass mask_from application had. The
    order-free sources are unaffected by z: gradient/from_text depend on no
    art at all, and luminance_of (existence-only by design) reads whatever
    the referenced slot's pixels are at this moment — for a LATER slot
    that is its pre-mask placement, deterministic either way."""
    masked: set[str] = set()
    for layer in layers:
        if layer.kind != "art" or layer.ref in masked or layer.ref not in positioned:
            continue
        masked.add(layer.ref)
        slot = art_by_id[layer.ref]
        if slot.mask is None:
            continue
        if slot.mask.from_text:
            ink = text_ink.get(slot.mask.from_text)
            if ink is None or ImageStat.Stat(ink).sum[0] <= 0:
                warnings.append(
                    f"{slot.id}: mask.from_text={slot.mask.from_text!r} "
                    f"resolved to a text slot with no ink (empty optional "
                    f"slot?) — the art is fully masked out.")
        mask_img = resolve_mask(slot.mask, canvas, positioned, text_ink)
        if mask_img is not None:
            positioned[layer.ref] = apply_mask(positioned[layer.ref], mask_img)


def _position_all_art(art_slots: list[ArtSlot], layers: list[LayerRef],
                      job_dir: Path, canvas: tuple[int, int], palette: Palette,
                      version: int, text_avoid_rects: list[tuple[int, int, int, int]],
                      text_by_id: dict[str, TextSlot],
                      text_ink: dict[str, Image.Image]) -> _ArtPositions:
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
    # Preserve pairs already resolved under deliberate sandwich thresholds.
    sandwich_pairs: set[tuple[str, str]] = set()
    layers_out = list(layers)
    for i, layer in enumerate(layers_out):
        if layer.kind != "art" or layer.ref in positioned:
            continue
        slot = art_by_id[layer.ref]
        source = _load_or_synthesize(slot, job_dir, canvas, palette, version)
        if source is None:
            positioned[slot.id] = Image.new("RGBA", canvas, (0, 0, 0, 0))
            continue
        source = _mirror_if_asked(slot, source)
        source = _crop_to_ink(slot, source)

        has_alpha = _has_transparency(source)
        sandwich_text = None
        if slot.fit == "contain" and i > 0 and layers_out[i - 1].kind == "text":
            candidate = text_by_id.get(layers_out[i - 1].ref)
            if candidate is not None and not (candidate.optional
                                              and not candidate.content.strip()):
                sandwich_text = candidate

        if slot.corners and has_alpha:
            img = _place_corners(source, canvas, slot.scale,
                                 slot.corners_flip_vertical)
        elif slot.scatter and has_alpha:
            img, placed = _place_scatter(source, canvas, slot.scatter, version,
                                         slot.id, text_avoid_rects)
            if placed < slot.scatter:
                warnings.append(
                    f"{slot.id}: only placed {placed} of {slot.scatter} "
                    f"scatter copies without overlapping a text zone.")
        elif slot.snap == "line_gap" and slot.fit == "contain" and sandwich_text is not None:
            img, ratio, snap_warning = _snap_to_line_gap(
                source, canvas, slot, palette, has_alpha, sandwich_text)
            sandwich_pairs.add((sandwich_text.id, slot.id))
            occlusion[f"{sandwich_text.id}<-{slot.id}"] = ratio
            if snap_warning:
                warnings.append(snap_warning)
        elif slot.fit == "contain" and sandwich_text is not None:
            img, ratio, glyph_ratio, degrade = _place_contain_clear_of_text(
                source, canvas, slot, palette, has_alpha, sandwich_text)
            key = f"{sandwich_text.id}<-{slot.id}"
            sandwich_pairs.add((sandwich_text.id, slot.id))
            if degrade:
                layers_out[i - 1], layers_out[i] = layers_out[i], layers_out[i - 1]
                # Name the limit that actually broke. A buried LETTER and a
                # crowded WORD are different defects with different fixes,
                # and quoting the word's number for a letter's problem sends
                # a reader (or an agent) looking in the wrong place.
                if (glyph_ratio / _GLYPH_OCCLUSION_THRESHOLD
                        > ratio / _OCCLUSION_THRESHOLD):
                    why = (f"buried one whole letter of "
                           f"'{sandwich_text.id}' ({glyph_ratio:.0%} of a "
                           f"single glyph, over the "
                           f"{_GLYPH_OCCLUSION_THRESHOLD:.0%} limit; the "
                           f"word as a whole was only {ratio:.0%})")
                else:
                    why = (f"still covered {ratio:.0%} of "
                           f"'{sandwich_text.id}''s ink (over the "
                           f"{_OCCLUSION_THRESHOLD:.0%} limit)")
                warnings.append(
                    f"{slot.id}: even after searching anchor offsets, it "
                    f"{why}; drew '{sandwich_text.id}' on top of it instead "
                    f"of underneath.")
                occlusion[key] = 0.0   # text now draws on top — the
                                      # composited cover carries none of
                                      # this pair's occlusion any more
                occlusion[f"{key}#glyph"] = 0.0
            else:
                occlusion[key] = ratio
                # Reported even when it passed: an agent tuning a sandwich
                # needs to see how close the worst letter came, not only
                # that the word survived.
                occlusion[f"{key}#glyph"] = glyph_ratio
        else:
            if slot.corners and not has_alpha:
                warnings.append(
                    f"{slot.id}: corners is set but the art has no real "
                    f"transparency; drew it as a single normal layer instead.")
            if slot.scatter and not has_alpha:
                warnings.append(
                    f"{slot.id}: scatter is set but the art has no real "
                    f"transparency; drew it as a single normal layer instead.")
            if slot.snap == "line_gap":
                warnings.append(
                    f"{slot.id}: snap is 'line_gap' but this slot is not a "
                    f"contain-fit layer immediately after a non-empty text "
                    f"layer; drew it normally instead.")
            img = (_fit_cover(source, canvas, slot.anchor, slot.scale, slot.offset)
                  if slot.fit == "cover" else
                  _fit_contain(source, canvas, slot.anchor, slot.scale,
                               slot.offset, slot.keep_whole))

        if slot.treatment != "none":
            pre_treatment[slot.id] = img
        img, treatment_warning = _apply_treatment(img, slot.treatment, palette,
                                                  slot.id, has_alpha)
        if treatment_warning:
            warnings.append(treatment_warning)

        positioned[slot.id] = img

    # Resolve masks after placement because sources may appear later in z-order.
    _apply_art_masks(positioned, layers_out, art_by_id, canvas, text_ink,
                     warnings)

    _apply_frame_notches(positioned, art_by_id, canvas, warnings)

    # Effect stacks on ART slots (§15.4) run here — after fit/placement/
    # treatment, after masks and notches (so a drop shadow or glow always
    # wraps the slot's FINAL silhouette, not a shape a mask was about to
    # carve away), and before compositing/the contact guard (which then
    # nudges the finished pixels, shadow and all, as one thing — and
    # rightly counts a soft shadow burying a title as contact).
    for slot in art_slots:
        if slot.effects and slot.id in positioned:
            positioned[slot.id] = apply_effect_stack(
                positioned[slot.id], slot.effects, palette, canvas)

    layers_out = _apply_text_contact_guard(
        positioned, layers_out, art_by_id, text_by_id, canvas, warnings,
        occlusion, sandwich_pairs)

    return _ArtPositions(positioned=positioned, pre_treatment=pre_treatment,
                         layers=layers_out, warnings=warnings, occlusion=occlusion)


def _apply_frame_notches(positioned: dict[str, Image.Image],
                         art_by_id: dict[str, ArtSlot],
                         canvas: tuple[int, int], warnings: list[str]) -> None:
    """v2.2 wave, deliverable 7: for every art slot naming `notch_for`,
    erase the frame's own painted pixels wherever the target slot's
    positioned alpha actually reaches (padded _NOTCH_PAD_FRACTION on every
    side) — "the frame politely breaks around an emblem that overlaps it"
    rather than drawing straight through it. Implemented by masking the
    frame layer's alpha with the (padded, dilated-to-a-rect) target bbox.

    Runs once, here, after every slot in `positioned` already holds its
    FINAL canvas-space pixels (see _position_all_art's own docstring on why
    this has to be a second pass: a notch_for target frequently comes LATER
    in z-order than the frame itself — corner_vine/emblem draw after
    rule_frame in woven_emblem, for instance — so it is not yet in
    `positioned` during the frame's own turn in that module's single
    forward pass over `layers`).

    Mutates `positioned[frame_id]` in place for every slot with a non-empty
    `notch_for`. A `notch_for` naming a slot with no ink anywhere (an
    ungenerated focal, say) leaves the frame untouched — nothing to notch
    around, and CoverSpec validation already guarantees the name resolves
    to a real slot, so there is nothing to warn about either."""
    cw, ch = canvas
    pad_x = round(_NOTCH_PAD_FRACTION * cw)
    pad_y = round(_NOTCH_PAD_FRACTION * ch)
    for slot in art_by_id.values():
        if not slot.notch_for:
            continue
        frame_img = positioned.get(slot.id)
        target_img = positioned.get(slot.notch_for)
        if frame_img is None or target_img is None:
            continue
        bbox = target_img.getbbox()
        if bbox is None:
            continue
        left, top, right, bottom = bbox
        hole = (max(0, left - pad_x), max(0, top - pad_y),
               min(cw, right + pad_x), min(ch, bottom + pad_y))
        if hole[2] <= hole[0] or hole[3] <= hole[1]:
            continue
        mask = Image.new("L", canvas, 255)
        ImageDraw.Draw(mask).rectangle(
            (hole[0], hole[1], hole[2] - 1, hole[3] - 1), fill=0)
        r, g, b, a = frame_img.split()
        positioned[slot.id] = Image.merge(
            "RGBA", (r, g, b, ImageChops.multiply(a, mask)))


def _bleeding_side_trims(img: Image.Image, canvas: tuple[int, int]
                         ) -> frozenset[str]:
    """Which side trims this positioned layer's ink already runs off.

    A canvas-sized positioned layer has ALREADY been clipped: whatever an
    archetype's `offset` pushed past the trim is simply not in these pixels
    any more. So a plain horizontal translate of that raster — the contact
    guard's nudge, below — cannot carry the overshoot along with it. Shifting
    a plate inboard exposes the flat edge where the clip happened and leaves
    bare ground beside it, which is the "starts and stops in mid-air" defect
    romantasy_organic's rule 1 exists to prevent. Two live renders of that
    archetype put a 96px and then a 192px bar down the left of the cover —
    0.06 and 0.12 of the width, the first two _TEXT_ART_CONTACT_NUDGES — and
    sliced the front-tier rose off along a dead-straight vertical line.

    Ink touching column 0 or column cw-1 is the observable that survives the
    clip, so the guard uses it to refuse any nudge that would give up a bleed
    it currently has. Moving FURTHER out through the same trim is still fine;
    only losing an edge is disqualifying."""
    bbox = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if bbox is None:
        return frozenset()
    edges = set()
    if bbox[0] <= 0:
        edges.add("left")
    if bbox[2] >= canvas[0]:
        edges.add("right")
    return frozenset(edges)


def _apply_text_contact_guard(positioned: dict[str, Image.Image],
                              layers_out: list[LayerRef],
                              art_by_id: dict[str, ArtSlot],
                              text_by_id: dict[str, TextSlot],
                              canvas: tuple[int, int], warnings: list[str],
                              occlusion: dict[str, float],
                              sandwich_pairs: set[tuple[str, str]]
                              ) -> list[LayerRef]:
    """v2.2 wave, deliverable 4: the general case of the title occlusion
    guard above — ANY art layer drawn after a text layer, not only a
    contain-fit slot immediately following one, must not bury a
    meaningful fraction of that text's own ink. Runs once, here, as a
    finishing pass over the current z-order (after every art slot in
    `positioned` already holds its notch-adjusted, fully treated pixels —
    _apply_frame_notches runs immediately before this), checking every art
    layer against every text layer that precedes it EXCEPT a pair already
    covered by the narrow sandwich path above — those already got their
    own, more permissive, deliberate-overlap allowance
    (_OCCLUSION_THRESHOLD / _LINE_GAP_CONTACT_THRESHOLD), and this general
    _TEXT_ART_CONTACT_THRESHOLD floor must never re-litigate a
    composed-on-purpose overlap with its own much tighter one.

    For an offending pair, first tries nudging the ALREADY-positioned art
    layer sideways by each of _TEXT_ART_CONTACT_NUDGES — a plain
    pixel-space horizontal translate (reuse; widen the shift-search's
    applicability per this deliverable), deliberately NOT a re-derivation
    from source at a new anchor: that would need to know which fit
    function and anchor semantics produced these particular pixels, and
    corners/scatter/mask_from all sidestep that question by construction.
    Translating the finished raster works uniformly for every art layer
    regardless of how it was placed. If no single nudge clears every
    currently-offending text slot AT ONCE (a shift that fixes one collision
    could worsen another), degrades: moves the art layer to draw
    immediately before the EARLIEST offending text layer's own position, so
    it ends up underneath every text slot it was colliding with — the same
    "draw text on top instead" fallback _place_contain_clear_of_text's own
    degrade path uses (reuse), generalized to a non-adjacent reorder.

    Returns the (possibly reordered) layers list; mutates `positioned`,
    `warnings`, and `occlusion` in place for the layers it actually
    touches."""
    cw, ch = canvas
    text_ink: dict[str, tuple[Image.Image, int]] = {}
    for i, layer in enumerate(layers_out):
        if layer.kind == "text" and layer.ref not in text_ink:
            slot = text_by_id.get(layer.ref)
            if slot is None or (slot.optional and not slot.content.strip()):
                continue
            fit = typeset.fit_text(slot, canvas)
            text_ink[layer.ref] = (typeset.text_mask(slot, fit, canvas), i)

    # One projection per text slot, reused by every candidate of every art
    # slot below (_glyph_boxes' own note: the glyphs never move here).
    glyph_boxes = {tid: _glyph_boxes(mask) for tid, (mask, _i) in text_ink.items()}

    def measure_all(img: Image.Image, tids: list[str]) -> dict[str, float]:
        """Each text slot's occlusion as a SEVERITY-equivalent fraction: the
        whole-word ratio, or whatever whole-word ratio would be as bad as the
        worst single glyph, whichever is worse. Reported on the whole-word
        scale so the number in RenderReport.occlusion and in the warning text
        still means what it always meant, while a buried letter can no longer
        hide behind eight intact ones."""
        alpha = img.getchannel("A")
        out: dict[str, float] = {}
        for tid in tids:
            mask = text_ink[tid][0]
            whole = _occlusion_fraction(mask, alpha)
            glyph = _worst_glyph_occlusion(mask, alpha, glyph_boxes[tid])
            out[tid] = max(whole, _occlusion_severity(
                whole, glyph, _TEXT_ART_CONTACT_THRESHOLD)
                * _TEXT_ART_CONTACT_THRESHOLD)
        return out

    moves: list[tuple[str, str]] = []   # (art_id, move-before-this-text-id)
    for i, layer in enumerate(layers_out):
        if layer.kind != "art":
            continue
        art_id = layer.ref
        if art_id.startswith(_FX_PREFIX):
            # Finishing layers (§15.6) sit above the text BY DESIGN —
            # "appended above the whole stack, text included" — so a
            # full-canvas grain plate covering the title's ink is never
            # the accidental collision this guard exists to undo, and
            # nudging or reordering it would quietly dismantle the recipe.
            # Whether the text stays LEGIBLE beneath finishing is the
            # §15.7 final-composite re-check's job: it measures the
            # finished ground and attenuates fx_ opacity, ladder-style,
            # instead of touching z-order.
            continue
        slot = art_by_id.get(art_id)
        img = positioned.get(art_id)
        if slot is None or img is None:
            continue
        candidates = [tid for tid, (_mask, tidx) in text_ink.items()
                     if tidx < i and (tid, art_id) not in sandwich_pairs]
        if not candidates:
            continue
        best_img, best = img, measure_all(img, candidates)
        if max(best.values()) > _TEXT_ART_CONTACT_THRESHOLD:
            bleeds = _bleeding_side_trims(img, canvas)
            for frac in _TEXT_ART_CONTACT_NUDGES:
                dx = round(frac * cw)
                shifted = Image.new("RGBA", canvas, (0, 0, 0, 0))
                shifted.alpha_composite(img, dest=(dx, 0))
                # A nudge that gives up a trim this plate was bleeding off
                # trades an accidental text collision — which the z-order
                # move below fixes on its own, and completely — for a hard
                # cut edge and a bar of bare ground inside the cover. That
                # is the worse defect and it is never worth making.
                if not bleeds <= _bleeding_side_trims(shifted, canvas):
                    continue
                trial = measure_all(shifted, candidates)
                if max(trial.values()) < max(best.values()):
                    best_img, best = shifted, trial
                if max(trial.values()) <= _TEXT_ART_CONTACT_THRESHOLD:
                    break

        positioned[art_id] = best_img
        for tid, frac in best.items():
            occlusion[f"{tid}<-{art_id}"] = frac
        offending = {tid: frac for tid, frac in best.items()
                    if frac > _TEXT_ART_CONTACT_THRESHOLD}
        if not offending:
            continue

        earliest_tid = min(offending, key=lambda tid: text_ink[tid][1])
        moves.append((art_id, earliest_tid))
        warnings.append(
            f"{art_id}: touches {offending[earliest_tid]:.0%} of "
            f"'{earliest_tid}''s ink (limit {_TEXT_ART_CONTACT_THRESHOLD:.0%}) "
            f"even after nudging; drew '{earliest_tid}' on top of it "
            f"instead of underneath.")
        for tid in offending:
            occlusion[f"{tid}<-{art_id}"] = 0.0   # about to move below —
                                                  # the composited cover
                                                  # carries none of this
                                                  # pair's occlusion any
                                                  # more

    out = list(layers_out)
    for art_id, text_id in moves:
        art_entries = [ref for ref in out if ref.kind == "art" and ref.ref == art_id]
        out = [ref for ref in out if not (ref.kind == "art" and ref.ref == art_id)]
        target_idx = next(j for j, ref in enumerate(out)
                          if ref.kind == "text" and ref.ref == text_id)
        out[target_idx:target_idx] = art_entries
    return out


def _snap_positions(spec: CoverSpec, canvas: tuple[int, int],
                    positioned_art: dict[str, Image.Image],
                    layers: list[LayerRef], art_by_id: dict[str, ArtSlot],
                    text_by_id: dict[str, TextSlot],
                    adjustments: list[str]) -> None:
    """The balance engine's snap pass, wired into this module's own state:
    measure every eligible element's positioned ink, hand the bboxes to
    balance.plan_snaps (which owns all the geometry and every tolerance),
    then apply the returned deltas — art by translating its positioned
    pixels (the exact shift-the-finished-raster move the contact guard
    established, and for the same reason: it works uniformly regardless of
    how the pixels were placed), text by translating its ZONE (fit_text
    depends only on the zone's width/height, so a pure horizontal
    translation re-fits to the identical size and line breaks — only where
    the ink lands changes, and the scrims/legibility machinery that derive
    from the zone follow automatically). Every applied move's line lands
    in `adjustments`.

    Eligibility is decided HERE, because it is spec-level reasoning
    balance.py deliberately can't see (it imports model/effects only):

    - Text: every drawn slot participates; exempt only a slot some
      MaskSpec.from_text clips art INTO — that art's alpha was already
      multiplied by the glyphs' current position during the mask pass, and
      moving the glyphs now would strand art-in-the-letterforms where the
      letters no longer are. (A text slot's own mask_from container clip
      is fine to move: it re-derives from the container's positioned alpha
      at draw time, after this pass.)
    - Art: contain-fit only, per §15.10 — cover-fit is a bleed plate, and
      corners/scatter placements are symmetric/deliberately-random by
      construction. Also exempt anything whose pixels are already
      entangled with another layer's: a slot carrying its own mask (its
      alpha was shaped against canvas-anchored or other-layer fields), a
      slot some other mask reads (from_layer/luminance_of stencils were
      taken at its current position), and either side of a notch_for pair
      (the hole was cut where the target ink WAS). The snap pass runs
      last by design — final positions — so entangled pixels must simply
      not move.

    Mutates `positioned_art` and `text_by_id` in place; runs before
    render_upto is ever called, so every replay, scrim derivation, and
    contrast measurement sees only the snapped state."""
    cw = canvas[0]
    axis_x = balance.resolve_axis_x(spec.axis, spec.axis_x)

    mask_source_art: set[str] = set()
    from_text_refs: set[str] = set()
    for owner in (*spec.art, *spec.adjust):
        if owner.mask is None:
            continue
        if owner.mask.from_layer:
            mask_source_art.add(owner.mask.from_layer)
        if owner.mask.luminance_of:
            mask_source_art.add(owner.mask.luminance_of)
        if owner.mask.from_text:
            from_text_refs.add(owner.mask.from_text)
    notch_bound = {a.id for a in spec.art if a.notch_for}
    notch_bound |= {a.notch_for for a in spec.art if a.notch_for}

    elements: list[balance.InkElement] = []
    seen: set[tuple[str, str]] = set()
    for layer in layers:
        key = (layer.kind, layer.ref)
        if key in seen or layer.kind not in ("text", "art"):
            continue
        seen.add(key)
        if layer.kind == "text":
            slot = text_by_id.get(layer.ref)
            if slot is None or (slot.optional and not slot.content.strip()):
                continue
            # One more redundant (pure, deterministic, cheap) fit search —
            # the same trade _position_all_art's occlusion guard already
            # blesses — because the snap needs the fitted INK's bbox, not
            # the zone's.
            fit = typeset.fit_text(slot, canvas)
            bbox = typeset.text_mask(slot, fit, canvas).getbbox()
            if bbox is None:
                continue
            zone_left = round(slot.zone.x * cw)
            # The legal horizontal travel keeps the translated zone a
            # valid Zone (x ≥ 0, x + w ≤ 1) — plan_snaps skips any snap
            # these bounds can't absorb whole, so a snap is always exact
            # or absent, never partial.
            dx_max = math.floor(cw * (1.0 - slot.zone.w) + 1e-6) - zone_left
            elements.append(balance.InkElement(
                id=layer.ref, kind="text", bbox=bbox, align=slot.align,
                snappable=layer.ref not in from_text_refs,
                dx_min=-zone_left, dx_max=dx_max))
        else:
            slot = art_by_id[layer.ref]
            snappable = (slot.fit == "contain" and not slot.corners
                         and not slot.scatter and slot.mask is None
                         and layer.ref not in mask_source_art
                         and layer.ref not in notch_bound)
            if not snappable:
                continue
            img = positioned_art.get(layer.ref)
            bbox = balance.ink_bbox(img) if img is not None else None
            if bbox is None:
                continue
            elements.append(balance.InkElement(
                id=layer.ref, kind="art", bbox=bbox,
                dx_min=-bbox[0], dx_max=cw - bbox[2]))

    deltas, lines = balance.plan_snaps(elements, spec.axis, axis_x, canvas)
    for (kind, ref), dx in deltas.items():
        if kind == "art":
            positioned_art[ref] = balance.translate_x(
                positioned_art[ref], dx, canvas)
        else:
            slot = text_by_id[ref]
            zone_left = round(slot.zone.x * cw)
            # (zone_left + dx) / cw re-rounds to exactly zone_left + dx at
            # zone_px time, so the drawn ink moves by precisely the
            # planned delta — mutating zone.x by dx/cw instead could land
            # a pixel off on a half-integer rounding boundary.
            z = slot.zone
            text_by_id[ref] = slot.model_copy(update={"zone": Zone(
                x=(zone_left + dx) / cw, y=z.y, w=z.w, h=z.h)})
    adjustments.extend(lines)


def _balance_measurements(spec: CoverSpec, canvas: tuple[int, int],
                          final_rgb: Image.Image,
                          positioned_art: dict[str, Image.Image],
                          layers: list[LayerRef],
                          art_by_id: dict[str, ArtSlot],
                          finalized: dict[str, _ResolvedText]) -> list[str]:
    """§15.10's report-only tier, gathered once over the finished render:
    whole-canvas mirror-symmetry/center-of-mass warnings (via
    balance.measure_composite — a spec with no declared axis measures as
    the center composition every pre-wave archetype is), the per-element
    margin audit, and the gap-rhythm near-miss check. Elements are
    measured at their FINAL positions — finalized text redraws from its
    (possibly snapped) slot, art reads its (possibly snapped) positioned
    alpha — and cover-fit art never enters the margin audit at all: a
    bleed layer touches the trim by design."""
    out: list[str] = list(balance.measure_composite(
        final_rgb, spec.axis or "center").warnings)

    elements: list[balance.InkElement] = []
    text_elements: list[balance.InkElement] = []
    seen: set[tuple[str, str]] = set()
    for layer in layers:
        key = (layer.kind, layer.ref)
        if key in seen or layer.kind not in ("text", "art"):
            continue
        seen.add(key)
        if layer.kind == "text":
            resolved = finalized.get(layer.ref)
            if resolved is None:
                continue
            bbox = typeset.text_mask(resolved.slot, resolved.fit,
                                     canvas).getbbox()
            if bbox is None:
                continue
            element = balance.InkElement(id=layer.ref, kind="text", bbox=bbox)
            elements.append(element)
            text_elements.append(element)
        else:
            if art_by_id[layer.ref].fit == "cover":
                continue                  # bleed layer — margin-exempt
            img = positioned_art.get(layer.ref)
            bbox = balance.ink_bbox(img) if img is not None else None
            if bbox is None:
                continue
            elements.append(balance.InkElement(id=layer.ref, kind="art",
                                               bbox=bbox))
    out.extend(balance.margin_audit(elements, canvas))
    out.extend(balance.gap_rhythm_warnings(text_elements, canvas))
    return out


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
    """Map every pixel's PERCEPTUAL lightness onto a straight-line ramp
    between palette.background (darkest) and `ink` (lightest — defaults to
    palette.primary, the normal §7.4a rule). Alpha passes through untouched, so
    a transparent cutout stays exactly as transparent after its RGB is
    remapped. Every OPAQUE output pixel's color lies exactly on that
    256-step line (never off it), which is the "duotone output contains
    only ramp colors" guarantee §7.4a's test list asks for — `ink` only
    changes WHICH ramp, never that guarantee. (`ink` is the art-vs-ground
    contrast floor's escalation, v2.1 BODY-fix wave — see
    _apply_art_contrast_floor.)

    RAMP POSITION IS PERCEPTUAL, NOT PHYSICAL (fixed 2026-08-31). This used
    to read _luminance_band — WCAG relative luminance, the legibility
    autopilot's own measurement — on the reasoning that one measurement
    serving two purposes was a virtue. It was a bug: linear luminance is
    savagely compressed at the dark end, so a low-key plate (sRGB 10-60 — a
    night interior, deep water, exactly what a duotone is reached for)
    landed inside a ~6%-wide slice of the 256-step ramp and came back a flat
    rectangle. _lightness_band asks the same question of the gamma-encoded
    values instead, which is what a tone MAPPING wants. The autopilot still
    measures with _luminance_band; contrast math is the one place linear is
    right."""
    lum = _lightness_band(img)
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
    rather than by measurement.

    Buckets on PERCEPTUAL lightness for _duotone's reason above: bucketing a
    low-key plate on linear luminance dropped every pixel into band 0 and
    returned one flat colour."""
    lum = _lightness_band(img)
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


# Apply grain before duotone so every output color stays on the palette ramp.

_PHOTO_SOFT_BLUR_FRACTION = 0.004     # of canvas height, gaussian blur radius
_PHOTO_SOFT_CONTRAST_FACTOR = 1.15    # slight lift so a blurred, desaturated
                                      # photo doesn't read flat/muddy
_PHOTO_SOFT_GRAIN_STRENGTH = 0.12     # 0..1, how much the grain field
                                      # perturbs luminance before the ramp


def _photo_soft(img: Image.Image, palette: Palette) -> Image.Image:
    """Canvas size is read off `img.size` itself (every art image reaching
    a treatment is already canvas-sized by this module's own invariant —
    the same convention `_sticker` above already relies on) rather than
    threading a separate `canvas` parameter through _apply_treatment's
    whole call chain."""
    canvas = img.size
    ch = canvas[1]
    blur_px = max(0.0, _PHOTO_SOFT_BLUR_FRACTION * ch)
    blurred = img.filter(ImageFilter.GaussianBlur(blur_px)) if blur_px > 0 else img
    gray = ImageOps.grayscale(blurred)
    lifted = ImageEnhance.Contrast(gray).enhance(_PHOTO_SOFT_CONTRAST_FACTOR)
    grain = _grain_layer(canvas).getchannel("R")
    mixed = Image.blend(lifted, grain, _PHOTO_SOFT_GRAIN_STRENGTH)
    grayscale_rgba = Image.merge("RGBA", (mixed, mixed, mixed, img.getchannel("A")))
    return _duotone(grayscale_rgba, palette)


def _apply_treatment(img: Image.Image, treatment: str, palette: Palette,
                     slot_id: str, has_alpha: bool
                     ) -> tuple[Image.Image, str | None]:
    """Dispatch one ArtSlot.treatment (§7.4a) to its pure function, plus
    `sticker`'s one stateful exception: on an opaque slot (no real alpha to
    dilate — see _has_transparency) it is a documented no-op, not a crash,
    and returns a warning sentence instead of a treated image. The other
    treatments have no such precondition — duotone/silhouette/posterize/
    photo_soft are well-defined on a fully opaque image too (silhouette
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
    if treatment == "photo_soft":
        return _photo_soft(img, palette), None
    if treatment == "sticker":
        if not has_alpha:
            return img, (
                f"{slot_id}: sticker treatment needs a transparent slot; "
                f"the art has no real transparency, so it was left untreated.")
        return _sticker(img, palette, img.size), None
    return img, None


# Flat-color treatments reuse the text legibility luminance calculation.

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


def _crop_to_ink(slot: ArtSlot, source: Image.Image) -> Image.Image:
    """`source` cropped to its alpha bounding box when the slot asks to be
    placed by its ink (ArtSlot.place_by), else `source` untouched.

    Applied at the SOURCE stage — before corners, scatter, snap, the
    occlusion anchor search and both fits — so every downstream placement
    path inherits ink semantics from one edit rather than each growing its
    own copy of this measurement.

    A plate with no alpha at all (an opaque ground, a procedural field) has
    no ink box distinct from its frame, so it is returned unchanged: the
    crop is a no-op there, not an error. balance.ink_bbox is the measuring
    tool on purpose — it thresholds the alpha channel, where a bare
    Image.getbbox() would scan every band and count the color a treated
    layer routinely leaves lying under zero alpha, which on a soft-edged
    cutout means cropping to nothing at all."""
    if slot.place_by != "ink":
        return source
    if source.mode != "RGBA":
        return source
    box = balance.ink_bbox(source)
    if box is None or box == (0, 0, source.width, source.height):
        return source
    return source.crop(box)


def _fit_contain(img: Image.Image, canvas: tuple[int, int],
                 anchor: tuple[float, float], scale: float,
                 offset: tuple[float, float],
                 keep_whole: bool = False) -> Image.Image:
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
    if keep_whole:
        dest_x, dest_y = _clamp_inside(resized, canvas, dest_x, dest_y)
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.alpha_composite(resized, dest=(dest_x, dest_y))
    return out


def _clamp_inside(resized: Image.Image, canvas: tuple[int, int],
                  dest_x: int, dest_y: int) -> tuple[int, int]:
    """`dest` pulled back just far enough that `resized`'s INK lands entirely
    within `canvas` — ArtSlot.keep_whole, for a plate whose subject is a
    scatter of discrete whole objects rather than something with a severed end
    to carry out through the trim.

    Measured on the ink, not the plate, so it composes with place_by: under
    place_by "ink" the source is already cropped and the two agree, and under
    "frame" this still clamps the subject rather than the model's arbitrary
    transparent margin (clamping the margin would strand the subject far
    inside the trim, which is the bug place_by exists to kill).

    Only ever pulls INWARD. A plate already inside is returned untouched, and
    one larger than the canvas on an axis is left alone on that axis — there
    is no placement that keeps it whole, and silently shrinking it would be a
    different decision than the archetype asked for."""
    box = balance.ink_bbox(resized)
    if box is None:
        return dest_x, dest_y
    ix0, iy0, ix1, iy1 = box
    cw, ch = canvas
    ink_w, ink_h = ix1 - ix0, iy1 - iy0
    if ink_w <= cw:
        dest_x = min(max(dest_x, -ix0), cw - ix1)
    if ink_h <= ch:
        dest_y = min(max(dest_y, -iy0), ch - iy1)
    return dest_x, dest_y


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
    elif scrim.kind == "halo":
        _paint_halo_scrim(overlay, rgb, strength, left, top, right, bottom, canvas)
    out = base.copy()
    out.alpha_composite(overlay)
    return out


# Panel feathering is hard-clipped to its padded rectangle so it cannot wash
# out unrelated parts of the cover.
_PANEL_FEATHER_FRACTION = 0.056       # of canvas height, gaussian blur sigma cap
_PANEL_FEATHER_RECT_FRACTION = 0.44   # cap as a fraction of the rect's shorter side
_PANEL_CORNER_FRACTION = 0.55         # corner radius, as a fraction of the
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


# A large proportional blur keeps the halo edge invisible.
_HALO_SIGMA_FRACTION = 0.30


def _paint_halo_scrim(overlay: Image.Image, rgb: tuple[int, int, int],
                      strength: float, left: int, top: int, right: int,
                      bottom: int, canvas: tuple[int, int]) -> None:
    """A radial soft darkening centered on the scrim's own rect: a filled
    ellipse the size of the zone, Gaussian-blurred at a sigma proportional
    to the zone's OWN diagonal (so a small `series` eyebrow and a huge
    `title` block both get a halo that reads as atmosphere relative to
    their own size, the same "proportional, not fixed-px" reasoning
    _paint_local_panel_scrim's own feather cap already applies). Unlike the
    panel scrim, this is deliberately left UNCLIPPED to its own rect after
    blurring — hard-clipping a blurred ellipse would just reintroduce the
    "box around the text" problem this scrim kind exists to avoid; a halo
    is allowed (expected) to fade past its own nominal zone, the way real
    falloff light or shadow does."""
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return
    diagonal = math.hypot(w, h)
    sigma = max(1.0, _HALO_SIGMA_FRACTION * diagonal)
    mask = Image.new("L", canvas, 0)
    ImageDraw.Draw(mask).ellipse((left, top, right - 1, bottom - 1), fill=255)
    blurred = mask.filter(ImageFilter.GaussianBlur(sigma))
    if strength < 1.0:
        blurred = blurred.point(lambda v: round(v * strength))
    block = Image.new("RGBA", canvas, (*rgb, 0))
    block.putalpha(blurred)
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


def _dead_band_frac(image: Image.Image) -> tuple[float, int, int]:
    """The tallest contiguous run of DEAD rows — flat across themselves AND
    flat against their neighbours (see `alive` below) — as a fraction
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

    stats = []
    for y in range(sample_h):
        row = ImageStat.Stat(band.crop((0, y, sample_w, y + 1)))
        stats.append((row.stddev[0] / 255.0, row.mean[0] / 255.0))

    def alive(y: int) -> bool:
        """Whether row `y` has anything in it — measured on BOTH axes.

        Across the row: its own stddev, the original test, which catches
        glyphs, art edges and ornament. Between rows: how far its mean sits
        from its neighbours', which catches a horizontal edge — a full-bleed
        rule, a hard seam, the top of a band of colour — that is invisible to
        the first test precisely because it is uniform."""
        stddev, mean = stats[y]
        if stddev >= _DEAD_BAND_ROW_STDDEV_THRESHOLD:
            return True
        for neighbour in (y - 1, y + 1):
            if 0 <= neighbour < sample_h:
                if abs(mean - stats[neighbour][1]) >= _DEAD_BAND_ROW_DELTA_THRESHOLD:
                    return True
        return False

    best_len, best_top = 0, 0
    run_len, run_top = 0, 0
    for y in range(sample_h):
        if not alive(y):
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


# Score the worst occupied grid cell; a zone-wide average can hide an
# illegible part of the text.

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
