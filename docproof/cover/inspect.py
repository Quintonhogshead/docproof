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
           "surface_line", "contact_gaps", "isolate", "audit_assets"]

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
