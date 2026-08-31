"""Cover Studio's element-inspection kit: "visually inspect each element
and fix until pixel-perfect" as a concrete, repeatable procedure instead of
a hope. Grown out of the first real $0-lane cover (Willow On Me), where
three consecutive placements of a generated figure cutout FAILED by eye —
the operator kept reading a snow crest 20-40px wrong from zoom crops — and
every defect that actually got fixed was fixed off a measurement artifact:

- the cutout's getbbox() included ~180px of near-invisible generator haze
  under the feet, floating the figure (ink_bbox's haze report);
- the ground line was only found via a ruled coordinate grid rendered ONTO
  a crop (ruled_crop) and a per-column edge probe (surface_line);
- the seat was verified as numbers — contact-point y minus surface y —
  not as an impression (contact_gaps);
- the generated plate carried full-height vertical banding a blur cannot
  remove, found by a column-mean step scan (seam_scan).

Doctrine, in one line: **no claim about where pixels sit is made by eye;
every claim is made against a ruled artifact or a numeric probe.** The $0
art-director loop runs these between compose() and the next spec patch,
exactly the way RenderReport already answers for legibility.

Scope learned from live calibration: automated scans run on the GENERATED
PLATE, not the finished composite — a composed cover's legitimate vertical
content (glyph stems, spires, the arc) out-fires any subtle banding by an
order of magnitude, so composite hits are pure noise. And no scan here
convicts on its own: seam_scan and audit_assets return POINTERS, each
confirmed with ruled_crop / column_profile before anything is "fixed".

Like balance.py, everything here is a pure value computation over images
the caller supplies — no network, no state. It imports model/compose only
for isolate(), which reuses compose's own placement code path so an
isolated layer is positioned by the exact pixels the full render will use
(never by a re-implementation that could disagree).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .model import CoverSpec

__all__ = ["ruled_crop", "ink_bbox", "seam_scan", "column_profile",
           "surface_line", "contact_gaps", "isolate", "audit_assets",
           "opening_bbox", "containment_gaps", "placed_ink_mask",
           "containment_check"]

# Below this alpha a pixel is generator haze / anti-aliasing spill, not an
# element's real ink. 40 (of 255) is the value that separated the Willow
# figure's boots from the ~180px of near-invisible mist gpt-image left
# under them; it is a starting point for the caller to override, not a law.
INK_ALPHA_THRESHOLD = 40

# A column-mean luminance step this large (0-255 scale) across a few
# pixels of sky reads as a printed seam at full size. The Willow plate's
# two banding seams measured ~8-11; genuine soft cloud edges spread the
# same delta over 10x the distance and stay under the window comparison.
SEAM_MIN_STEP = 6.0


def ruled_crop(image: Image.Image, box: tuple[int, int, int, int], *,
               step: int = 20, scale: int = 2,
               label_every: int = 100) -> Image.Image:
    """A crop with a coordinate grid ruled onto it, labeled in SOURCE
    coordinates — the artifact you read a surface or placement from
    instead of guessing from an unruled zoom. `step` is the gridline
    spacing in source pixels; every `label_every` line carries its
    coordinate as text."""
    x0, y0, x1, y1 = box
    crop = image.crop(box).convert("RGB")
    crop = crop.resize(((x1 - x0) * scale, (y1 - y0) * scale),
                       Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(crop)
    ink, label_ink = (255, 70, 70), (255, 150, 150)
    for gx in range(x0 - (x0 % step) + step, x1, step):
        cx = (gx - x0) * scale
        draw.line((cx, 0, cx, crop.height), fill=ink, width=1)
        if gx % label_every == 0:
            draw.text((cx + 3, 3), str(gx), fill=label_ink)
    for gy in range(y0 - (y0 % step) + step, y1, step):
        cy = (gy - y0) * scale
        draw.line((0, cy, crop.width, cy), fill=ink, width=1)
        if gy % label_every == 0:
            draw.text((3, cy + 2), str(gy), fill=label_ink)
    return crop


def ink_bbox(layer: Image.Image, *, threshold: int = INK_ALPHA_THRESHOLD
             ) -> dict:
    """Where an element's REAL ink is, versus what getbbox() claims.

    Returns {"raw": box|None, "hard": box|None, "haze": (l, t, r, b)} —
    `haze` is how many pixels of sub-threshold alpha pad each side. Any
    layer seated by its raw bbox floats by its bottom haze; seat by
    `hard`. A layer with no alpha channel has raw == hard by definition."""
    rgba = layer if layer.mode == "RGBA" else layer.convert("RGBA")
    raw = rgba.getbbox()
    hard_mask = rgba.getchannel("A").point(
        lambda v: 255 if v > threshold else 0)
    hard = hard_mask.getbbox()
    if raw is None or hard is None:
        return {"raw": raw, "hard": hard, "haze": (0, 0, 0, 0)}
    haze = (hard[0] - raw[0], hard[1] - raw[1],
            raw[2] - hard[2], raw[3] - hard[3])
    return {"raw": raw, "hard": hard, "haze": haze}


def seam_scan(image: Image.Image, *, band: tuple[int, int] | None = None,
              stripe: int = 220, x_range: tuple[int, int] | None = None,
              window: int = 16, min_step: float = SEAM_MIN_STEP
              ) -> list[dict]:
    """Vertical banding-seam detector: generated plates ship with vertical
    luminance steps a blur only softens (a step stays a step; the fix is
    re-synthesis — row-wise lerp across the strip). A seam usually lives
    in a LIMITED row range, so one tall band dilutes it below detection:
    the scan sweeps horizontal stripes (`stripe` rows each, or the single
    `band` when given), compares each column's flanking window means per
    stripe, and merges hits across stripes by x-proximity.

    Returns [{"x", "step", "rows": (y0, y1)}, ...]. Real content (a glow,
    a spire) fires this too — every hit is a POINTER to confirm with
    ruled_crop, never a license to auto-fix."""
    grey = image.convert("L")
    w, h = grey.size
    px = grey.load()
    x_lo, x_hi = x_range or (window, w - window)
    stripes = ([band] if band is not None else
               [(y, min(y + stripe, h)) for y in range(0, h, stripe)])
    merged: list[dict] = []
    for y0, y1 in stripes:
        rows = range(y0, y1)
        n = max(1, len(rows))
        means: dict[int, float] = {}

        def col_mean(x: int) -> float:
            if x not in means:
                means[x] = sum(px[x, y] for y in rows) / n
            return means[x]

        hits: list[tuple[int, float]] = []
        for x in range(max(window, x_lo), min(w - window, x_hi)):
            left = sum(col_mean(x - d) for d in range(1, window + 1)) / window
            right = sum(col_mean(x + d) for d in range(1, window + 1)) / window
            step = abs(right - left)
            if step >= min_step:
                if hits and x - hits[-1][0] <= window:
                    if step > hits[-1][1]:
                        hits[-1] = (x, step)
                else:
                    hits.append((x, step))
        for x, step in hits:
            for entry in merged:
                if abs(entry["x"] - x) <= window:
                    entry["step"] = max(entry["step"], step)
                    entry["rows"] = (min(entry["rows"][0], y0),
                                     max(entry["rows"][1], y1))
                    break
            else:
                merged.append({"x": x, "step": step, "rows": (y0, y1)})
    return sorted(merged, key=lambda e: -e["step"])


def column_profile(image: Image.Image, *, y: int, x_range: tuple[int, int],
                   step: int = 10) -> list[tuple[int, int]]:
    """One row's luminance, sampled — the confirmation probe for a
    seam_scan hit (and the tool that settled every seam question on the
    first live cover). A banding seam shows as a step between flat flanks
    at the hit's x; a content gradient shows as a slope with no step; a
    glyph or spire shows as a spike. Run it at two or three y's inside the
    hit's row span before deciding anything is a defect."""
    grey = image.convert("L")
    px = grey.load()
    return [(x, px[x, y]) for x in range(x_range[0], x_range[1], step)]


def surface_line(image: Image.Image, *, x_range: tuple[int, int],
                 y_range: tuple[int, int], sample_every: int = 10
                 ) -> list[tuple[int, int]]:
    """Per-column ground line: for each sampled column, the y of the
    strongest downward dark-to-bright luminance edge inside `y_range` —
    a snow crest, a rooftop lip, a table edge. This is what a figure's
    contact points are checked against; reading it from an unruled zoom
    was wrong three times in one session."""
    grey = image.convert("L")
    px = grey.load()
    y0, y1 = y_range
    line: list[tuple[int, int]] = []
    for x in range(x_range[0], x_range[1], sample_every):
        best_y, best_gain = y0, 0
        for y in range(y0 + 2, y1 - 2):
            gain = (px[x, y + 1] + px[x, y + 2]) - (px[x, y - 1] + px[x, y - 2])
            if gain > best_gain:
                best_gain, best_y = gain, y
        line.append((x, best_y))
    return line


def contact_gaps(surface: list[tuple[int, int]],
                 contacts: list[tuple[int, int]]) -> list[dict]:
    """The seat check as numbers: for each proposed contact point, the
    surface y at its x (nearest sample) and the gap. Positive gap =
    floating above the ground by that many pixels; small negative = sunk
    in (usually wanted, a few px). An element is 'seated' when every
    contact's gap is in [-15, 2] — verify with these numbers, not by
    impression."""
    out = []
    for cx, cy in contacts:
        sx, sy = min(surface, key=lambda p: abs(p[0] - cx))
        out.append({"contact": (cx, cy), "surface_y": sy, "gap": sy - cy})
    return out


def isolate(spec: CoverSpec, slot_id: str, job_dir: Path,
            canvas: tuple[int, int] = (1600, 2560)) -> Image.Image:
    """One layer rendered alone through compose's own placement path —
    the honest way to look at a single element, since re-implementing
    placement could disagree with the render. Keeps only this slot (plus
    the background slot when the target isn't it, so blend modes have
    ground to act on) and strips text, scrims, and adjust layers."""
    from .compose import compose  # local import: compose imports are heavy

    if not any(a.id == slot_id for a in spec.art):
        raise ValueError(f"slot {slot_id!r} is not in this spec's art list")
    solo = spec.model_copy(deep=True)
    keep = {slot_id}
    if any(a.id == "background" for a in solo.art) and slot_id != "background":
        keep.add("background")
    solo.layers = [ref for ref in solo.layers
                   if ref.kind == "art" and ref.ref in keep]
    solo.adjust = []
    solo.scrims = []
    if not solo.layers:
        raise ValueError(f"slot {slot_id!r} is not in this spec's layers")
    image, _report = compose(solo, job_dir, canvas=canvas)
    return image


def _ink_mask(layer: Image.Image, *, threshold: int = INK_ALPHA_THRESHOLD
              ) -> Image.Image:
    """An element's hard ink as an "L" mask (ink=255, open=0). An "L" input
    is taken as an already-built mask (any value > 127 is ink)."""
    if layer.mode == "L":
        return layer.point(lambda v: 255 if v > 127 else 0)
    rgba = layer if layer.mode == "RGBA" else layer.convert("RGBA")
    return rgba.getchannel("A").point(
        lambda v: 255 if v > threshold else 0)


def opening_bbox(container: Image.Image, *,
                 threshold: int = INK_ALPHA_THRESHOLD,
                 seed: tuple[int, int] | None = None) -> dict:
    """A container's interior opening, MEASURED — never derived from its
    bbox. §15.20 rule 7's probe: an ornate frame's crests and scrollwork
    intrude far past its rail line, so placing an element by arithmetic on
    the container's bounding box shipped a clipped scoop twice (the
    Badgerbones frame debacle) before this existed.

    `container` is the container element — an RGBA cutout, or an "L" ink
    mask (ink=255) such as placed_ink_mask returns. The probe floods
    outward from `seed` (default: the ink bbox center; if that lands on
    ink, the nearest open pixel along the axes) through non-ink pixels;
    the flooded region is the opening.

    Returns {"bbox": box|None, "seed": (x, y)|None, "closed": bool,
    "area_frac": float}. `bbox` is None when there is no ink or no open
    interior pixel near the seed. `closed` is False when the flood escapes
    the container's own ink bbox — the "container" doesn't actually
    enclose its hole, and a containment verdict built on it is void."""
    mask = _ink_mask(container, threshold=threshold)
    ink_box = mask.getbbox()
    if ink_box is None:
        return {"bbox": None, "seed": None, "closed": False,
                "area_frac": 0.0}
    px = mask.load()
    w, h = mask.size
    cx = (ink_box[0] + ink_box[2]) // 2
    cy = (ink_box[1] + ink_box[3]) // 2
    if seed is None:
        seed = (cx, cy)
        if px[cx, cy]:                       # seed on ink: probe the axes
            found = None
            for d in range(1, max(w, h)):
                for sx, sy in ((cx + d, cy), (cx - d, cy),
                               (cx, cy + d), (cx, cy - d)):
                    if (ink_box[0] <= sx < ink_box[2]
                            and ink_box[1] <= sy < ink_box[3]
                            and not px[sx, sy]):
                        found = (sx, sy)
                        break
                if found:
                    break
            if not found:
                return {"bbox": None, "seed": None, "closed": False,
                        "area_frac": 0.0}
            seed = found
    if px[seed[0], seed[1]]:
        return {"bbox": None, "seed": seed, "closed": False,
                "area_frac": 0.0}
    flood = mask.copy()
    ImageDraw.floodfill(flood, seed, 128)
    hole = flood.point(lambda v: 255 if v == 128 else 0)
    box = hole.getbbox()
    # A flood that reaches any image border escaped the container — the
    # "hole" is connected to the outside and no containment verdict built
    # on it means anything.
    closed = (box is not None and box[0] > 0 and box[1] > 0
              and box[2] < w and box[3] < h)
    area = (w * h - hole.histogram()[0]) / float(w * h)
    return {"bbox": box, "seed": seed, "closed": closed,
            "area_frac": area}


def containment_gaps(opening: tuple[int, int, int, int],
                     ink: tuple[int, int, int, int]) -> dict:
    """The contained-by seat check as numbers: per-edge gap between a
    measured opening and a contained element's hard-ink bbox, in the
    boxes' shared coordinate space. Positive gap = inside by that many
    pixels; negative = crossing the container by that many. The caller's
    gate supplies the margin (§15.20: ≥1% of canvas height, undeclared
    crossings FAIL) — verify with these numbers, not by impression."""
    gaps = (ink[0] - opening[0], ink[1] - opening[1],
            opening[2] - ink[2], opening[3] - ink[3])
    return {"gaps": gaps, "min_gap": min(gaps),
            "contained": min(gaps) >= 0}


def placed_ink_mask(spec: CoverSpec, slot_id: str, job_dir: Path, *,
                    base_id: str = "background",
                    canvas: tuple[int, int] = (1600, 2560),
                    diff_threshold: int = 8) -> Image.Image:
    """One art slot's UNOCCLUDED placed ink as an "L" mask in cover
    coordinates, computed through compose's own placement path (the
    isolate() doctrine — never a re-implementation that could disagree).
    Renders the base slot alone, then base + target, and takes the pixel
    difference: whatever the target visibly painted is its placed ink.
    `diff_threshold` is on the channel-mean difference — a layer that
    paints near-identically to the base (a black cutout on a black
    ground) needs a lower threshold or an isolate() look instead."""
    from PIL import ImageChops

    from .compose import compose  # local import: compose imports are heavy

    ids = {a.id for a in spec.art}
    for needed in (base_id, slot_id):
        if needed not in ids:
            raise ValueError(f"slot {needed!r} is not in this spec's art "
                             f"list")
    if base_id == slot_id:
        raise ValueError("base_id and slot_id must differ")

    def solo(keep: set[str]) -> Image.Image:
        sub = spec.model_copy(deep=True)
        sub.layers = [ref for ref in sub.layers
                      if ref.kind == "art" and ref.ref in keep]
        sub.adjust = []
        sub.scrims = []
        image, _report = compose(sub, job_dir, canvas=canvas)
        return image

    base = solo({base_id})
    pair = solo({base_id, slot_id})
    diff = ImageChops.difference(base.convert("RGB"), pair.convert("RGB"))
    return diff.convert("L").point(
        lambda v: 255 if v > diff_threshold else 0)


def containment_check(spec: CoverSpec, job_dir: Path, *, container: str,
                      contained: str, base_id: str = "background",
                      canvas: tuple[int, int] = (1600, 2560),
                      margin_frac: float = 0.01,
                      diff_threshold: int = 8) -> dict:
    """§15.20 rule 7's gate, end to end: is `contained`'s placed ink fully
    inside `container`'s measured opening, with margin? Both layers are
    placed by compose's own path (placed_ink_mask); the opening is flooded
    from the container's interior (opening_bbox); the verdict is numbers
    (containment_gaps). `margin_frac` is of canvas height — §15.20 says
    1%. Returns {"opening", "ink", "gaps", "min_gap", "margin_px",
    "closed", "contained"}; `contained` is the gate (False = FAIL unless
    the spec's rationale declares a deliberate breakout), and a not-
    `closed` container voids the verdict — look with ruled crops."""
    margin_px = round(margin_frac * canvas[1])
    frame_mask = placed_ink_mask(spec, container, job_dir, base_id=base_id,
                                 canvas=canvas,
                                 diff_threshold=diff_threshold)
    opening = opening_bbox(frame_mask)
    ink_box = placed_ink_mask(spec, contained, job_dir, base_id=base_id,
                              canvas=canvas,
                              diff_threshold=diff_threshold).getbbox()
    result: dict = {"opening": opening["bbox"], "ink": ink_box,
                    "closed": opening["closed"], "margin_px": margin_px,
                    "gaps": None, "min_gap": None, "contained": False}
    if opening["bbox"] is None or ink_box is None:
        return result
    gaps = containment_gaps(opening["bbox"], ink_box)
    result["gaps"] = gaps["gaps"]
    result["min_gap"] = gaps["min_gap"]
    result["contained"] = (opening["closed"]
                           and gaps["min_gap"] >= margin_px)
    return result


def audit_assets(spec: CoverSpec, job_dir: Path, *,
                 threshold: int = INK_ALPHA_THRESHOLD) -> list[dict]:
    """The per-element pass over every asset-backed art slot: its ink_bbox
    haze report, flagged when the haze is big enough to mis-seat the
    element (the exact bug that floated the Willow figure). Returns one
    dict per asset slot; callers render isolates / ruled crops for any
    flagged entry rather than trusting their eyes."""
    findings = []
    for slot in spec.art:
        if not slot.asset:
            continue
        path = Path(job_dir) / slot.asset
        entry: dict = {"slot": slot.id, "asset": slot.asset}
        try:
            with Image.open(path) as img:
                report = ink_bbox(img.convert("RGBA"), threshold=threshold)
        except OSError as e:
            entry["error"] = str(e)
            findings.append(entry)
            continue
        entry.update(report)
        haze = report["haze"]
        entry["flag"] = (max(haze) > 24)
        if entry["flag"]:
            entry["note"] = (
                f"{max(haze)}px of sub-threshold haze pads this asset "
                f"(l,t,r,b={haze}); seat it by the 'hard' bbox or it "
                f"will float/overhang.")
        findings.append(entry)
    return findings
