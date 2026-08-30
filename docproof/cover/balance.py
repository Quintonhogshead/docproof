"""Cover Studio's balance & symmetry engine (docs/cover_designer_spec.md
§15.10): the consistent live failure is covers that are *almost* right — a
title 2% off the center axis, two left-aligned blocks on slightly different
rails, one half of the canvas visibly heavier. Humans read these instantly
as amateur; no prompt fixes them reliably. So this is code, in the house
shape: **measure the current canvas, snap what's snappable, report the rest
as numbers the judge can act on.**

Three tiers, strictly ordered by how much they're trusted to act:

- *Snap* (plan_snaps): horizontal near-misses against the spec's declared
  axis, and unequal-but-nearly-equal text rails, are translated onto the
  target EXACTLY. §15.10's job is killing near-misses, not enforcing
  centering — anything off by more than the tolerance is left alone as
  intentional asymmetry. Every move is returned as an adjustment line with
  exact before→after numbers, destined for RenderReport.adjustments, so
  "why did it move" is never a mystery.
- *Warn* (gap_rhythm_warnings): vertical gap rhythm is measured and
  reported but NEVER auto-moved — vertical position interacts with zones,
  scrims, and the occlusion guards, and a wrong vertical snap is worse
  than a reported near-miss (§15.10, verbatim).
- *Measure* (measure_composite, margin_audit): mirror symmetry, visual
  center of mass, and trim margins are taste calls the judge arbitrates —
  reported as numbers into the same warnings channel §6.3's
  composer_warnings doctrine already feeds, never acted on here.

This module imports effects.py (the WCAG luminance band) only — §15.9
allows model.py too, but nothing here ever needed a spec type — and never
compose.py, typeset.py, or archetypes.py (typeset.py's own rule). It
therefore knows nothing about zones, fonts, or fit searches:
compose measures every element's positioned ink (text via its fitted glyph
mask, art via its positioned alpha) and hands the bboxes in as InkElements;
this module answers with pixel deltas and prose lines. That split is what
keeps every function here a pure, trivially testable value computation —
and what lets compose stay the only module that ever mutates a render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from PIL import Image, ImageChops, ImageOps, ImageStat

from .effects import luminance_band

# -- tuning (§15.10) ----------------------------------------------------------

# Axis and rail snaps share one tolerance: within this fraction of canvas
# WIDTH of the target but not exactly on it → translate onto it exactly;
# farther off → intentional asymmetry, leave untouched. 1.5% is the spec's
# own number — at 1600px that is 24px, comfortably wider than any glyph
# bearing wobble and comfortably narrower than any deliberate offset.
AXIS_SNAP_TOLERANCE = 0.015
RAIL_SNAP_TOLERANCE = 0.015

# Two adjacent vertical gaps within this RELATIVE fraction of each other
# (of the larger gap) but unequal read as a rhythm near-miss — warn only,
# never auto-move (§15.10's own "no auto-move in v1" rule).
GAP_RHYTHM_TOLERANCE = 0.20

# Mirror-symmetry score floor below which a center-axis cover gets its
# heavier half named (§15.10's number). The score is 1 − mean abs luminance
# difference against the horizontal flip, so 0.55 means nearly half the
# full luminance range differs on average — a genuinely lopsided canvas,
# not a slightly informal one.
SYMMETRY_WARN_FLOOR = 0.55

# Horizontal distance (fraction of canvas width) the luminance-weighted
# centroid may sit from the center axis before it's worth a warning.
MASS_OFFSET_WARN_FRACTION = 0.06

# Any element's ink closer than this (fraction of the relevant canvas
# dimension) to a trim edge gets flagged — bleed layers excepted (the
# caller filters fit="cover" art; a plate whose ink reaches all four
# margin bands is treated as bleed here too, see margin_audit).
MARGIN_WARN_FRACTION = 0.02

# Where a left/right axis rail sits when the spec declares the axis but
# not axis_x (§15.10's own defaults). "center" always means 0.5 and never
# reads axis_x at all.
AXIS_X_DEFAULTS: dict[str, float] = {"left": 0.08, "right": 0.92}

# Alpha below this (0-255) does not count as ink when measuring an art
# layer's bbox: a feathered mask or a treatment's soft edge trails off in
# single-digit alpha for many pixels, and letting that tail drag the
# measured edge around would make every snap and margin number a function
# of invisible pixels. ~10% of full alpha is where a pixel stops reading
# as part of the shape at all (well under _SILHOUETTE_ALPHA_THRESHOLD's
# 50% "this IS the shape" line — a bbox should be generous, a silhouette
# strict).
_INK_ALPHA_FLOOR = 26

# Column count the center-of-mass centroid is computed over. A warning
# threshold of 6% of width needs nothing like per-pixel precision, and a
# BOX-filter reduce keeps the whole computation in C except a 200-item
# Python sum — the same downsample-for-cost discipline compose's
# _DEAD_BAND_SAMPLE_HEIGHT already follows.
_MEASURE_COLUMNS = 200

# Float-comparison slack for the tolerance/threshold checks below: every
# boundary case worth having an opinion about (an ornament inset exactly
# 2%, an edge exactly on the tolerance line) should land on the lenient
# side of the comparison rather than flip on one ulp of division noise.
_EPS = 1e-9


@dataclass(frozen=True)
class InkElement:
    """One positioned element's measurable ink, as compose hands it over:
    a text slot's fitted glyph-mask bbox, or an art layer's positioned
    (alpha-thresholded) bbox — always canvas-space pixels, PIL bbox
    convention (left, top, right, bottom; right/bottom exclusive), or None
    for an element with no ink at all (an empty layer measures as nothing
    and is skipped everywhere).

    `snappable` is the caller's whole exemption policy folded into one
    bool — cover-fit / corners / scattered art, mask-entangled layers —
    so this module never re-derives spec-level reasoning it can't see.
    A non-snappable element still participates where standing still is
    meaningful: as a rail other slots may unify TO, and in every
    measurement. `dx_min`/`dx_max` bound the legal horizontal translation
    in pixels (a text zone must stay on-canvas; art ink shouldn't slide
    off the trim) and are read only by plan_snaps — margin/gap callers may
    leave them at 0."""
    id: str
    kind: Literal["text", "art"]
    bbox: tuple[int, int, int, int] | None
    align: Literal["left", "center", "right"] = "center"
    snappable: bool = True
    dx_min: int = 0
    dx_max: int = 0


@dataclass(frozen=True)
class BalanceMeasurements:
    """measure_composite's whole answer: the two always-computed scores,
    plus whichever warnings their thresholds earned. The scores ride along
    even when nothing warns so a caller (or a later wave's judge prompt)
    can surface them without re-measuring."""
    symmetry: float
    center_of_mass_x: float
    warnings: list[str]


def resolve_axis_x(axis: str, axis_x: float | None) -> float:
    """The axis rail as a fraction of canvas width: 0.5 for "center"
    (axis_x is ignored by design — a center axis IS the center), the
    spec's declared axis_x for "left"/"right" when set, and §15.10's
    conventional 0.08/0.92 defaults otherwise."""
    if axis == "center":
        return 0.5
    if axis_x is not None:
        return axis_x
    return AXIS_X_DEFAULTS[axis]


def ink_bbox(layer: Image.Image) -> tuple[int, int, int, int] | None:
    """An RGBA layer's ink bounding box, measured on its ALPHA channel
    thresholded at _INK_ALPHA_FLOOR — never Image.getbbox() directly,
    which scans every band and would count fully transparent pixels whose
    RGB happens to be nonzero (a treated layer's paint routinely leaves
    color under zero alpha). None when the layer holds no ink at all."""
    alpha = layer.getchannel("A")
    return alpha.point(lambda v: 255 if v >= _INK_ALPHA_FLOOR else 0).getbbox()


def translate_x(layer: Image.Image, dx: int,
                canvas: tuple[int, int]) -> Image.Image:
    """`layer` shifted `dx` pixels horizontally (positive = right) onto a
    fresh transparent canvas — paste, not alpha_composite, because on a
    blank ground a straight pixel copy IS the translation (alpha
    included), and paste handles a negative offset by cropping, which is
    exactly what sliding ink toward the trim should do."""
    out = Image.new("RGBA", canvas, (0, 0, 0, 0))
    out.paste(layer, (dx, 0))
    return out


# -- the snap pass (§15.10) ---------------------------------------------------

def _axis_feature(bbox: tuple[int, int, int, int], axis: str) -> float:
    """Which x-coordinate of an ink bbox the axis compares against: the
    ink CENTER for a center axis, the leading (left) edge for a left rail,
    the trailing (right, exclusive-boundary) edge for a right rail —
    §15.10's own reading of "measure the ink-bbox center or edge"."""
    left, _top, right, _bottom = bbox
    if axis == "center":
        return (left + right) / 2.0
    return float(left if axis == "left" else right)


_FEATURE_NAMES = {"center": "ink center", "left": "leading edge",
                  "right": "trailing edge"}


def plan_snaps(elements: Sequence[InkElement],
               axis: Literal["center", "left", "right"], axis_x: float,
               canvas: tuple[int, int]
               ) -> tuple[dict[tuple[str, str], int], list[str]]:
    """The whole snap pass as a pure plan: horizontal pixel deltas keyed
    (kind, id) — text and art ids legally collide, the same shared-slug
    namespace CoverSpec._adjust_ids_resolve polices — plus one adjustment
    line per move with exact before→after numbers. The caller applies the
    deltas (translating art pixels / text zones); nothing here touches an
    image.

    Deterministic by construction: elements are processed in the order
    given (compose passes layers-list order, the fixed order §15.10
    requires), rails are established top-down, and the only arithmetic is
    round() on pixel deltas — no search, no randomness.

    Two sub-passes, in this order:

    - *Axis snap*: every snappable element whose axis feature (see
      _axis_feature) lands within AXIS_SNAP_TOLERANCE of the rail — but
      not already on the pixel it would round to — moves onto it exactly.
      A delta the element's dx bounds cannot legally absorb is skipped
      entirely (leave untouched beats a partial snap that lands nowhere).
    - *Rail snap*: leading edges of left-aligned text, trailing edges of
      right-aligned text, measured AFTER any axis move. Walking top-down
      (bbox top, ties by input order), each slot either unifies with the
      topmost already-established rail within RAIL_SNAP_TOLERANCE, or —
      when it matches none — establishes its own edge as a new rail for
      the slots below it. "Snaps to the topmost slot's rail" (§15.10)
      falls out of first-match-wins over rails stored in establishment
      order. A non-snappable slot still establishes a rail (it is a fixed
      feature others may align to) but never moves; a near-miss it cannot
      act on establishes nothing, so it can't fork the rail it almost
      matched."""
    cw = canvas[0]
    deltas: dict[tuple[str, str], int] = {}
    lines: list[str] = []

    axis_tol_px = AXIS_SNAP_TOLERANCE * cw
    target = axis_x * cw
    for el in elements:
        if not el.snappable or el.bbox is None:
            continue
        feature = _axis_feature(el.bbox, axis)
        diff = target - feature
        if abs(diff) > axis_tol_px + _EPS:
            continue                      # intentional asymmetry — leave it
        dx = round(diff)
        if dx == 0:
            continue                      # already exact on the pixel grid
        if dx < el.dx_min or dx > el.dx_max:
            continue                      # the exact snap isn't legal — a
                                          # partial move would land on no
                                          # axis at all, so leave untouched
        deltas[(el.kind, el.id)] = dx
        axis_name = ("the center axis" if axis == "center"
                     else f"the {axis} rail at {axis_x:.1%}")
        lines.append(
            f"{el.kind} '{el.id}': {_FEATURE_NAMES[axis]} "
            f"{feature / cw:.2%} → {(feature + dx) / cw:.2%} of width — "
            f"snapped onto {axis_name} ({dx:+d}px).")

    rail_tol_px = RAIL_SNAP_TOLERANCE * cw
    order = {(el.kind, el.id): i for i, el in enumerate(elements)}
    for align, edge_index, edge_name in (("left", 0, "leading edge"),
                                         ("right", 2, "trailing edge")):
        group = [el for el in elements
                 if el.kind == "text" and el.bbox is not None
                 and el.align == align]
        if len(group) < 2:
            continue
        group.sort(key=lambda el: (el.bbox[1], order[(el.kind, el.id)]))
        rails: list[tuple[float, str]] = []   # (edge px, owner id), top-down
        for el in group:
            key = (el.kind, el.id)
            edge = float(el.bbox[edge_index] + deltas.get(key, 0))
            matched = next((rail for rail in rails
                            if abs(edge - rail[0]) <= rail_tol_px + _EPS),
                           None)
            if matched is None:
                rails.append((edge, el.id))
                continue
            rail_edge, owner = matched
            dx = round(rail_edge - edge)
            if dx == 0 or not el.snappable:
                continue
            total = deltas.get(key, 0) + dx
            if total < el.dx_min or total > el.dx_max:
                continue
            deltas[key] = total
            lines.append(
                f"text '{el.id}': {edge_name} {edge / cw:.2%} → "
                f"{(edge + dx) / cw:.2%} of width — unified with "
                f"'{owner}'s rail ({dx:+d}px).")

    # A snap that netted out to zero (an axis move a rail move later undid
    # exactly) would be a no-op translate downstream — drop it, so callers
    # can treat every entry as a real move.
    return {k: v for k, v in deltas.items() if v != 0}, lines


# -- gap rhythm (warn only, §15.10) -------------------------------------------

def gap_rhythm_warnings(text_elements: Sequence[InkElement],
                        canvas: tuple[int, int]) -> list[str]:
    """Vertical ink gaps between adjacent stacked text slots, compared
    pairwise where two gaps share a middle slot: within
    GAP_RHYTHM_TOLERANCE of each other (relative to the larger) but
    unequal → one warning naming both gaps, §15.10's own example shape
    ("title→subtitle gap 3.1%, subtitle→author 3.8% — consider
    equalizing"). NO auto-move, deliberately: vertical position interacts
    with zones, scrims, and the occlusion guards, and a wrong vertical
    snap is worse than a reported near-miss. Overlapping slots (gap ≤ 0)
    form no gap, and a comparison never bridges across one — rhythm only
    means anything between gaps that share a slot."""
    ch = canvas[1]
    stacked = sorted((el for el in text_elements if el.bbox is not None),
                     key=lambda el: el.bbox[1])
    gaps: list[tuple[str, str, int]] = []
    for above, below in zip(stacked, stacked[1:]):
        gap = below.bbox[1] - above.bbox[3]
        if gap > 0:
            gaps.append((above.id, below.id, gap))
    out: list[str] = []
    for (a, b, g1), (b2, c, g2) in zip(gaps, gaps[1:]):
        if b2 != b or g1 == g2:
            continue
        if abs(g1 - g2) <= GAP_RHYTHM_TOLERANCE * max(g1, g2) + _EPS:
            out.append(
                f"{a}→{b} gap {g1 / ch:.1%}, {b}→{c} gap {g2 / ch:.1%} — "
                f"consider equalizing.")
    return out


# -- balance measurements (reported, never auto-fixed — §15.10) ---------------

def mirror_symmetry(rgb: Image.Image) -> float:
    """Mean absolute WCAG-luminance difference between the composite and
    its horizontal flip, inverted to 0..1 — exactly 1.0 for a perfectly
    mirror-symmetric image (the flip IS the image), falling toward 0.0 as
    the halves diverge. All C-speed Pillow ops (effects.luminance_band's
    LUT/blend construction, one difference, one stat), so full canvas
    resolution costs nothing worth downsampling away."""
    lum = luminance_band(rgb)
    diff = ImageChops.difference(lum, ImageOps.mirror(lum))
    return 1.0 - ImageStat.Stat(diff).mean[0] / 255.0


def _weight_image(rgb: Image.Image) -> Image.Image:
    """Per-pixel visual weight as an 'L' image: absolute deviation of each
    pixel's WCAG luminance from the canvas's own mean. Deliberately NOT
    raw luminance — a naive luminance-weighted centroid calls the bright
    empty half of a dark-ink-on-light cover "heavy," which is exactly
    backwards. Deviation from the canvas's own mean reads visual weight
    for both polarities: ink on a light ground and glow on a dark ground
    both stand out from their surround, and standing out is what weight
    IS on a cover. The mean rounds to an int once so the whole thing is a
    single point() LUT."""
    lum = luminance_band(rgb)
    mean = round(ImageStat.Stat(lum).mean[0])
    return lum.point([abs(v - mean) for v in range(256)])


def half_weights(rgb: Image.Image) -> tuple[float, float]:
    """Total visual weight (see _weight_image) of the left and right
    halves. Odd widths drop the exact middle column from both sides —
    symmetric by construction, so the comparison can never be biased by
    which half a shared column was assigned to."""
    weight = _weight_image(rgb)
    w, h = weight.size
    half = w // 2
    left = ImageStat.Stat(weight.crop((0, 0, half, h))).sum[0]
    right = ImageStat.Stat(weight.crop((w - half, 0, w, h))).sum[0]
    return left, right


def center_of_mass_x(rgb: Image.Image) -> float:
    """The visual-weight-weighted horizontal centroid as a fraction of
    canvas width. Column weights come from one BOX-filter reduce of the
    weight image to (_MEASURE_COLUMNS × 1) — each output pixel is the mean
    weight of its column band, computed in C — leaving only a
    200-term weighted sum in Python. A canvas with no weight anywhere
    (perfectly uniform) reads as dead center: nothing is off-axis when
    nothing stands out."""
    weight = _weight_image(rgb)
    w, _h = weight.size
    columns = min(_MEASURE_COLUMNS, w)
    band = weight.resize((columns, 1), Image.Resampling.BOX)
    data = band.tobytes()          # 'L' raw bytes ARE the per-column means
    total = sum(data)
    if total <= 0:
        return 0.5
    return sum(v * (x + 0.5) for x, v in enumerate(data)) / total / columns


def measure_composite(rgb: Image.Image,
                      axis: Literal["center", "left", "right"]
                      ) -> BalanceMeasurements:
    """The two whole-canvas balance measurements over the FINAL composite
    (finishing included — these judge what ships), plus the warnings their
    thresholds earn. Both scores are always computed and returned; the
    warnings fire only for a center-axis composition (`axis` here is the
    spec's declared axis with None already resolved to "center" by the
    caller — a pre-wave spec IS a center composition, §15.10's own
    default). A left/right-rail composition is asymmetric ON PURPOSE, and
    its weight centroid sits off its rail by construction (mass spreads
    across the whole canvas; the rail is at 8%), so flagging either would
    be a permanent false alarm — the numbers still come back for the
    judge, they just don't cry wolf."""
    score = mirror_symmetry(rgb)
    com = center_of_mass_x(rgb)
    warnings: list[str] = []
    if axis == "center":
        if score < SYMMETRY_WARN_FLOOR:
            left, right = half_weights(rgb)
            total = left + right
            if total > 0:
                side, share = (("right", right / total) if right >= left
                               else ("left", left / total))
                warnings.append(
                    f"left/right balance: mirror symmetry {score:.2f} "
                    f"(floor {SYMMETRY_WARN_FLOOR}); the {side} half "
                    f"carries {share:.0%} of the visual weight.")
        offset = com - 0.5
        if abs(offset) > MASS_OFFSET_WARN_FRACTION + _EPS:
            side = "right" if offset > 0 else "left"
            warnings.append(
                f"visual center of mass sits {abs(offset):.1%} {side} of "
                f"the center axis (limit "
                f"{MASS_OFFSET_WARN_FRACTION:.0%}).")
    return BalanceMeasurements(symmetry=score, center_of_mass_x=com,
                               warnings=warnings)


def margin_audit(elements: Sequence[InkElement],
                 canvas: tuple[int, int]) -> list[str]:
    """Minimum ink distance to each trim edge, per element — one warning
    per element+edge closer than MARGIN_WARN_FRACTION (of the relevant
    canvas dimension). The caller already excludes fit="cover" art (a
    bleed layer touches the trim BY DESIGN, §15.10's own exemption); an
    ART element whose ink lands inside the margin band on all four edges
    at once is the same thing wearing a different fit — a full-bleed
    plate (a canvas-sized procedural texture on a contain slot, say) that
    no designer could inset — so it is skipped under the same reasoning
    rather than warned four times on every render forever. Text never
    gets that pass: a text slot's glyphs reaching all four trim edges is
    exactly the disaster this audit exists to name."""
    cw, ch = canvas
    out: list[str] = []
    for el in elements:
        if el.bbox is None:
            continue
        left, top, right, bottom = el.bbox
        distances = (("left", left / cw), ("top", top / ch),
                     ("right", (cw - right) / cw),
                     ("bottom", (ch - bottom) / ch))
        offending = [(edge, frac) for edge, frac in distances
                     if frac < MARGIN_WARN_FRACTION - _EPS]
        if el.kind == "art" and len(offending) == 4:
            continue                      # full-bleed plate — see docstring
        for edge, frac in offending:
            out.append(
                f"{el.kind} '{el.id}': ink {frac:.1%} from the {edge} trim "
                f"edge (limit {MARGIN_WARN_FRACTION:.0%}).")
    return out


__all__ = [
    "AXIS_SNAP_TOLERANCE", "AXIS_X_DEFAULTS", "BalanceMeasurements",
    "GAP_RHYTHM_TOLERANCE", "InkElement", "MARGIN_WARN_FRACTION",
    "MASS_OFFSET_WARN_FRACTION", "RAIL_SNAP_TOLERANCE",
    "SYMMETRY_WARN_FLOOR", "center_of_mass_x", "gap_rhythm_warnings",
    "half_weights", "ink_bbox", "margin_audit", "measure_composite",
    "mirror_symmetry", "plan_snaps", "resolve_axis_x", "translate_x",
]
