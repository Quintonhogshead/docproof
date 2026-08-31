"""Cover job -> CanvasDoc: the one-shot pipeline's output, opened as layers.

"New cover" runs the existing pipeline and the finished draft lands on the
canvas as editable layers instead of a flat PNG (docs/cover_canvas_spec.md
§1). This module is that landing: it reads a finished job's `job.json` and
its `assets/` plates and produces the editor's own document.

The conversion is deliberately ONE-WAY. Nothing here ever round-trips back
into the pipeline, so it is free to be lossy where the editor's vocabulary is
smaller than the composer's — and it is, in named places (see LOSSES below).
What it is not free to be is *wrong*: the first render of the CanvasDoc has
to look like the cover the person just approved, or the editor opens on a
different cover than the one they clicked. That is why the art frames are
computed with compose's own placement arithmetic rather than guessed at, and
why the text layers are fitted through typeset rather than seeded from
size_max.

LOSSES, each deliberate and each logged where it happens:

- **Adjust layers** (§15.3 grades, gradient maps, blooms) are dropped: they
  own no pixels and the client has no adjustment stack yet.
- **Blend modes, masks, treatments and the effects rack** on art slots are
  dropped; a plate arrives as its positioned rectangle.
- **Procedural art** with no plate on disk has no PNG to carry. The
  conventional FIELD synthesizers become a flat scrim in the palette's
  background so the canvas at least opens on the right ground; grain,
  frames and the texture shelf are dropped.
- **Vignette and halo scrims** are radial; a canvas scrim gradient is
  linear, so both arrive as their closest linear reading.
- **corners/scatter** slots stamp several copies of one plate; only the
  primary placement survives.

`source_spec` on the returned document keeps the whole CoverSpec verbatim,
so nothing listed above is *lost* — only untranslated, and available to a
later version of this vocabulary.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from docproof.cover import typeset
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.compose import EBOOK_H, EBOOK_W, _SCRIM_PAD_FRACTION
from docproof.cover.model import ArtSlot, CoverSpec, ScrimSpec, TextSlot
# The ONE prompt-assembly path, imported rather than re-implemented (the
# precedent pipeline.py itself sets by importing model._expand_recipe): a
# job dir never persists the assembled prompt, only the spec's raw
# `slot.prompt`, so the regeneration verbs' prompt has to be rebuilt the
# same way the pipeline built it the first time — archetype composition
# note, cutout directive, negative suffix and all — or a re-roll would ask
# for something subtly different from what is on screen.
from docproof.cover.pipeline import JOB_MANIFEST, _assemble_prompt

from .model import (ArtLayer, CanvasDoc, Frame, Gradient, ScrimLayer, Size,
                    Stop, TextLayer, Warp, new_layer_id)

log = logging.getLogger("docproof.canvas.ingest")


class CanvasIngestError(Exception):
    """A cover job that cannot be opened as a canvas, named precisely.

    Deliberately NOT docproof.ingest.IngestError (manuscript reading, a
    different layer entirely) — the two would be confused on sight in a
    traceback, and this one always names the file that is missing or
    unreadable."""


# TextSlot.arc's own bound (docproof.cover.model), and the divisor that maps
# it onto Warp.amount's -1..1 dial: a spec at full arc opens with the warp
# slider at full, which is the only mapping a person would guess.
_MAX_SPEC_ARC = 0.35

# An art slot with no plate on disk is synthesizing its own pixels. These
# are the synthesizers that paint a FIELD — a full-canvas ground — and so
# have a meaningful flat-color stand-in; every other synthesizer (grain,
# the frame family, the light bank) is texture or ornament, which a flat
# rectangle would misrepresent rather than approximate.
_FIELD_SYNTHS = frozenset({"gradient", "paper", "canvas"})


def ingest(job_dir: Path, *, concept: int = 0,
           canvas: tuple[int, int] | None = None) -> CanvasDoc:
    """One finished cover job's concept, as a CanvasDoc.

    A cover job holds several concepts (§8); `concept` picks which one the
    canvas opens — the index into `job.json`'s own concepts list, the same
    number the job page shows on the card and the same one that names the
    job's plates (`assets/c<n>_<slot>.png`).

    `canvas` is the reference pixel size the document's fractions are read
    against; it defaults to the composer's ebook canvas, which is what every
    job on disk today was rendered at.

    Raises CanvasIngestError, naming the file, whenever the job is missing a
    piece: no job.json, no such concept, no spec, or an art slot pointing at
    a plate that is not there."""
    job_dir = Path(job_dir)
    canvas = canvas or (EBOOK_W, EBOOK_H)
    raw = _read_manifest(job_dir)
    spec = _read_spec(job_dir, raw, concept)
    report = _concept_field(raw, concept, "report") or {}

    layers: list[Any] = []
    art_by_id = {slot.id: slot for slot in spec.art}
    text_by_id = {slot.id: slot for slot in spec.text}

    # Straight walk of spec.layers, which IS compose()'s bottom-to-top draw
    # order — background field, scrims, decorative slots, focal art, type —
    # so the canvas layer list reads in the same order as the cover it came
    # from. A ref appearing twice (legal in a spec, composited twice by the
    # composer) becomes two layers here, for the same reason.
    for ref in spec.layers:
        if ref.kind == "art":
            layer = _art_layer(art_by_id[ref.ref], spec, job_dir, canvas)
        elif ref.kind == "scrim":
            layer = _scrim_layer(spec.scrims[int(ref.ref)], int(ref.ref), spec,
                                 text_by_id, report)
        elif ref.kind == "text":
            layer = _text_layer(text_by_id[ref.ref], spec, canvas)
        else:   # "adjust" — owns no pixels, and the client has no adjustment
                # stack; the spec survives in source_spec for when it does.
            log.info("canvas ingest: adjust layer %r dropped — the editor "
                     "vocabulary has no adjustment layers yet.", ref.ref)
            layer = None
        if layer is not None:
            layers.append(layer)

    return CanvasDoc(
        job_id=str(raw.get("job_id") or job_dir.name),
        # Which cover of this job this session is. Carried on the document
        # itself so a session can say what it is without its filename having
        # to be parsed — and so the editor can put it on screen.
        concept=concept,
        canvas=Size(w=canvas[0], h=canvas[1]),
        layers=layers,
        # The cover job's own spend stays on the cover job (job.json's
        # ledger); a canvas session counts what the canvas spends.
        cost_usd=0.0,
        source_spec=spec.model_dump(mode="json"))


# -- reading the job ----------------------------------------------------------

def _read_manifest(job_dir: Path) -> dict[str, Any]:
    path = job_dir / JOB_MANIFEST
    if not path.is_file():
        raise CanvasIngestError(
            f"{path} is not there — ingest needs a finished cover job's "
            f"{JOB_MANIFEST}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CanvasIngestError(f"{path} could not be read: {e}") from e
    if not isinstance(raw, dict):
        raise CanvasIngestError(
            f"{path} is not a cover job manifest (its top level is a "
            f"{type(raw).__name__}, not an object)")
    return raw


def _concept_field(raw: dict[str, Any], concept: int, field: str) -> Any:
    concepts = raw.get("concepts") or []
    if not isinstance(concepts, list) or concept >= len(concepts):
        return None
    entry = concepts[concept]
    return entry.get(field) if isinstance(entry, dict) else None


def _read_spec(job_dir: Path, raw: dict[str, Any], concept: int) -> CoverSpec:
    """The chosen concept's CoverSpec, validated.

    Only the spec is validated, not the whole JobState: this reads a job the
    pipeline wrote and may have written months ago, and the one thing the
    canvas actually needs is the spec. Re-validating status enums and ledger
    rows it will never look at would turn "an old job opens" into "an old
    job opens if nothing else in JobState has changed since"."""
    path = job_dir / JOB_MANIFEST
    concepts = raw.get("concepts") or []
    if not isinstance(concepts, list) or not concepts:
        raise CanvasIngestError(
            f"{path} has no concepts — there is no cover in this job to open")
    if not 0 <= concept < len(concepts):
        raise CanvasIngestError(
            f"{path} has {len(concepts)} concept(s), so concept {concept} "
            f"does not exist")
    data = concepts[concept].get("spec") if isinstance(concepts[concept], dict) else None
    if not isinstance(data, dict):
        raise CanvasIngestError(
            f"{path} concept {concept} carries no spec — the job never got "
            f"far enough to have a cover")
    try:
        return CoverSpec.model_validate(data)
    except ValidationError as e:
        raise CanvasIngestError(
            f"{path} concept {concept}'s spec does not validate: {e}") from e


# -- art ----------------------------------------------------------------------

def _art_layer(slot: ArtSlot, spec: CoverSpec, job_dir: Path,
               canvas: tuple[int, int]) -> Any:
    """One art slot as a layer, or None when it has nothing this vocabulary
    can carry (see LOSSES)."""
    if not slot.asset:
        return _field_stand_in(slot, spec)
    path = job_dir / slot.asset
    if not path.is_file():
        raise CanvasIngestError(
            f"art slot {slot.id!r} names the plate {slot.asset!r}, which is "
            f"not in the job directory ({path} does not exist)")
    try:
        with Image.open(path) as plate:
            image_size = plate.size
    except (OSError, UnidentifiedImageError) as e:
        raise CanvasIngestError(f"{path} could not be read as an image: {e}") from e

    if slot.corners or slot.scatter:
        log.info("canvas ingest: art slot %r stamps multiple copies "
                 "(corners=%s, scatter=%s); only its primary placement "
                 "becomes a layer.", slot.id, slot.corners, slot.scatter)

    archetype = ARCHETYPES.get(spec.archetype)
    if slot.prompt and archetype is not None:
        prompt = _assemble_prompt(slot, archetype)
    else:
        # A procedural/uploaded slot has no prompt at all, and a spec whose
        # archetype has since left the shelf can still be opened — with the
        # raw prompt, which is the honest half of the assembly.
        prompt = slot.prompt
        if slot.prompt and archetype is None:
            log.info("canvas ingest: archetype %r is not on the shelf, so "
                     "art slot %r carries its raw prompt without the "
                     "composition note.", spec.archetype, slot.id)

    return ArtLayer(
        id=new_layer_id(), name=slot.id, opacity=slot.opacity,
        frame=_placed_frame(slot, image_size, canvas),
        source=slot.asset, prompt=prompt, transparent=slot.transparent,
        # cover/contain ride across unchanged. At ingest the frame IS the
        # plate's own rectangle, so all three fits draw identically; the
        # value only starts to matter once a person resizes the box, and
        # the spec's own intent is the right thing for it to mean then.
        fit=slot.fit)


def _placed_frame(slot: ArtSlot, image_size: tuple[int, int],
                  canvas: tuple[int, int]) -> Frame:
    """Where this plate's pixels actually landed, as a frame box.

    This mirrors compose._fit_cover / _fit_contain's arithmetic exactly —
    the same fill/fit scale, the same anchor and offset formulas, the same
    crop clamp — because the whole point of the canvas is that it opens on
    the cover the person approved. What compose needs is the pixels; what
    this needs is the rectangle they occupy, which is the same computation
    stopped one step early.

    The result is the plate's own rectangle at its own aspect ratio, which
    is why the layer's `fit` is visually inert at ingest time (see
    _art_layer)."""
    cw, ch = canvas
    iw, ih = image_size
    ax, ay = slot.anchor
    ox, oy = slot.offset
    scale = max(slot.scale, 1e-6)

    if slot.fit == "cover":
        fill = max(cw / iw, ch / ih) * scale
        new_w = max(1, round(iw * fill))
        new_h = max(1, round(ih * fill))
        # compose crops the resized plate at (left, top) and pastes the crop
        # at the canvas origin, so the plate's own top-left sits at -left,
        # -top in canvas pixels.
        left = round((new_w - cw) * ax - ox * cw)
        top = round((new_h - ch) * ay - oy * ch)
        left = max(0, min(left, max(0, new_w - cw)))
        top = max(0, min(top, max(0, new_h - ch)))
        x0, y0 = -left, -top
    else:                                       # "contain"
        fit_scale = min(cw / iw, ch / ih) * scale
        new_w = max(1, round(iw * fit_scale))
        new_h = max(1, round(ih * fit_scale))
        x0 = round(ax * (cw - new_w) + ox * cw)
        y0 = round(ay * (ch - new_h) + oy * ch)

    return Frame(x=_clamp_center((x0 + new_w / 2) / cw, slot.id, "x"),
                 y=_clamp_center((y0 + new_h / 2) / ch, slot.id, "y"),
                 w=new_w / cw, h=new_h / ch)


def _clamp_center(value: float, slot_id: str, axis: str) -> float:
    """Frame centers live in [-2, 2] (a layer may hang off-canvas, but not
    into another postcode). A spec offset can in principle push a center
    past that; clamp and say so rather than failing the whole ingest over
    one over-enthusiastic nudge."""
    if -2.0 <= value <= 2.0:
        return value
    clamped = max(-2.0, min(2.0, value))
    log.info("canvas ingest: art slot %r's %s center %.3f is outside the "
             "frame bound; clamped to %.1f.", slot_id, axis, value, clamped)
    return clamped


def _field_stand_in(slot: ArtSlot, spec: CoverSpec) -> Any:
    """An art slot with no plate on disk, as the closest thing this
    vocabulary has — or None.

    A FIELD synthesizer (the background gradient and its paper/canvas
    siblings, or the conventional `background` id with no opinion at all)
    becomes a flat full-canvas scrim in the palette's background color, so
    the editor opens on the right ground instead of on transparency. Grain,
    frames, the light bank and the texture shelf are dropped: a flat
    rectangle is not an approximation of a texture, it is a different
    cover."""
    is_field = (slot.procedural in _FIELD_SYNTHS
                or (not slot.procedural and not slot.texture_file
                    and slot.id == "background"))
    if not is_field:
        log.info("canvas ingest: art slot %r has no plate on disk and no "
                 "field stand-in (procedural=%r, texture_file=%r); dropped.",
                 slot.id, slot.procedural, slot.texture_file)
        return None
    return ScrimLayer(
        id=new_layer_id(), name=slot.id, opacity=slot.opacity,
        frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
        color=spec.palette.background,
        gradient=Gradient(angle=90.0,
                          stops=[Stop(at=0.0, alpha=1.0),
                                 Stop(at=1.0, alpha=1.0)]))


# -- scrims -------------------------------------------------------------------

def _scrim_layer(scrim: ScrimSpec, index: int, spec: CoverSpec,
                 text_by_id: dict[str, TextSlot],
                 report: dict[str, Any]) -> Any:
    """One scrim as an alpha-gradient rectangle.

    The strength comes from the render report when the job has one, not from
    the spec: the composer's legibility autopilot escalates a scrim at render
    time and records where it landed in RenderReport.scrim_final (§7.3), and
    the escalated value is what the person actually saw. A spec-strength
    scrim would open visibly weaker than the approved cover."""
    rect = _scrim_rect(scrim, text_by_id)
    if rect is None:
        log.info("canvas ingest: scrim %d protects nothing resolvable and "
                 "has no zone; dropped.", index)
        return None
    strength = _scrim_strength(scrim, index, report)
    if strength <= 0.0:
        log.info("canvas ingest: scrim %d is at strength 0 (the composer "
                 "draws nothing for it); dropped.", index)
        return None

    left, top, right, bottom = rect
    box, stops = _scrim_ramp(scrim.kind, left, top, right, bottom, strength)
    bx0, by0, bx1, by1 = box
    return ScrimLayer(
        id=new_layer_id(), name=f"scrim {index} ({scrim.kind})",
        frame=Frame(x=(bx0 + bx1) / 2, y=(by0 + by1) / 2,
                    w=max(bx1 - bx0, 1e-6), h=max(by1 - by0, 1e-6)),
        color=spec.palette.get(scrim.color_role),
        gradient=Gradient(angle=90.0, stops=stops))


def _scrim_rect(scrim: ScrimSpec, text_by_id: dict[str, TextSlot]
                ) -> tuple[float, float, float, float] | None:
    """compose._scrim_rect in fractions: an explicit zone verbatim, or the
    protected text slot's zone padded on every side by the SAME 4% margin
    the composer uses (imported, not restated, so the two can never
    drift)."""
    if scrim.zone is not None:
        z = scrim.zone
        return z.x, z.y, z.x + z.w, z.y + z.h
    if scrim.protects is None:
        return None
    slot = text_by_id.get(scrim.protects)
    if slot is None:
        return None
    pad = _SCRIM_PAD_FRACTION
    return (max(0.0, slot.zone.x - pad), max(0.0, slot.zone.y - pad),
            min(1.0, slot.zone.x + slot.zone.w + pad),
            min(1.0, slot.zone.y + slot.zone.h + pad))


def _scrim_strength(scrim: ScrimSpec, index: int, report: dict[str, Any]
                    ) -> float:
    """The strength the render actually used. RenderReport.scrim_final is a
    dict keyed by scrim INDEX, which survives a JSON round trip as a string
    key — both spellings are tried, since this reads a file, not a live
    model."""
    final = report.get("scrim_final") if isinstance(report, dict) else None
    if isinstance(final, dict):
        for key in (str(index), index):
            value = final.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return scrim.strength


def _scrim_ramp(kind: str, left: float, top: float, right: float,
                bottom: float, strength: float
                ) -> tuple[tuple[float, float, float, float], list[Stop]]:
    """One scrim kind as (box, stops), all at angle 90 (top to bottom).

    - `panel` is uniform across its own rect: the composer's local panel
      feathers its edge, which a two-stop ramp cannot say, so the flat
      reading of its solid center is the honest one.
    - `gradient_down`/`gradient_up` ramp across the rect AND continue solid
      to the nearest canvas edge (§7.3), so the box is extended to that edge
      and the rect's own boundary becomes an interior stop.
    - `vignette` (clear middle, dark edges) and `halo` (dark middle, clear
      edges) are radial in the composer; a canvas gradient is linear, so
      each becomes its vertical reading, which keeps the light where the
      composer put it even though the falloff is no longer round."""
    if kind == "gradient_down":
        box = (left, top, right, 1.0)
        span = max(1.0 - top, 1e-6)
        edge = min(1.0, (bottom - top) / span)
        stops = [Stop(at=0.0, alpha=0.0), Stop(at=edge, alpha=strength)]
        if edge < 1.0:
            stops.append(Stop(at=1.0, alpha=strength))
        return box, stops
    if kind == "gradient_up":
        box = (left, 0.0, right, bottom)
        span = max(bottom, 1e-6)
        edge = max(0.0, top / span)
        stops = [Stop(at=0.0, alpha=strength)]
        if edge > 0.0:
            stops.append(Stop(at=edge, alpha=strength))
        stops.append(Stop(at=1.0, alpha=0.0))
        return box, stops
    box = (left, top, right, bottom)
    if kind == "vignette":
        return box, [Stop(at=0.0, alpha=strength), Stop(at=0.5, alpha=0.0),
                     Stop(at=1.0, alpha=strength)]
    if kind == "halo":
        return box, [Stop(at=0.0, alpha=0.0), Stop(at=0.5, alpha=strength),
                     Stop(at=1.0, alpha=0.0)]
    return box, [Stop(at=0.0, alpha=strength), Stop(at=1.0, alpha=strength)]


# -- text ---------------------------------------------------------------------

def _text_layer(slot: TextSlot, spec: CoverSpec,
                canvas: tuple[int, int]) -> Any:
    """One text slot as live, editable text at the size and breaks the
    composer chose.

    The slot is fitted through typeset.fit_text — the same pure fit search
    compose runs — rather than seeded from size_max, for two reasons the
    editor cannot do without: the fitted size is what the person saw, and
    the fit's LINE BREAKS are the only record of them there is. A canvas
    text layer does not auto-wrap (§4: line breaks are explicit), so a title
    the composer broke over three lines has to arrive already broken or it
    opens as one long line running off the trim."""
    if not slot.content.strip():
        log.info("canvas ingest: text slot %r is empty; the composer draws "
                 "nothing for it, so it becomes no layer.", slot.id)
        return None
    try:
        fit = typeset.fit_text(slot, canvas)
    except OSError as e:
        # The shelf's TTFs are vendored, so this is a broken install rather
        # than a bad spec. Open the cover anyway, at the slot's own ceiling
        # and its author's own line breaks — a canvas that opens slightly
        # wrong beats one that will not open.
        log.warning("canvas ingest: could not fit text slot %r (%s); falling "
                    "back to size_max and the brief's own breaks.", slot.id, e)
        lines = tuple(slot.content.splitlines()) or (slot.content,)
        size_frac, line_sizes = slot.size_max, ()
    else:
        lines, size_frac, line_sizes = fit.lines, fit.size_frac, fit.line_sizes_px

    # justify_stack fits every line at its own size (§15.12); a uniform fit
    # reports none, and every line is `size_frac` tall.
    per_line = ([px / canvas[1] for px in line_sizes] if line_sizes
                else [size_frac] * len(lines))
    block_h = max(sum(px * typeset.LINE_HEIGHT for px in per_line), 1e-6)

    return TextLayer(
        id=new_layer_id(), name=slot.id,
        frame=Frame(x=slot.zone.x + slot.zone.w / 2,
                    y=_valign_center(slot, block_h),
                    w=slot.zone.w, h=block_h,
                    # §15.12's whole-slot tilt is exactly a frame rotation.
                    rotation=slot.rotate),
        text="\n".join(lines),
        family=slot.font_family,
        size=max(size_frac, 1e-6),
        # knockout/art_fill have no ink color of their own; the composer
        # tests the PANEL/outline color, which the effects rack fixes to the
        # palette's primary role. Same rule here, so the canvas opens in the
        # color the render used.
        color=(spec.palette.primary if slot.mode != "fill"
               else spec.palette.get(slot.color_role)),
        # CoverSpec tracks in em/1000; the canvas tracks in ems.
        tracking=slot.tracking / 1000.0,
        align=slot.align,
        line_height=typeset.LINE_HEIGHT,
        warp=_warp(slot))


def _valign_center(slot: TextSlot, block_h: float) -> float:
    """The text block's own center, honoring the slot's valign.

    A canvas text layer's box IS its text block — there is no vertical
    alignment field, because a box you can drag makes one unnecessary. So
    the ingest resolves valign once, here, by placing a block-sized box
    where the composer would have placed the ink."""
    if slot.valign == "top":
        return slot.zone.y + block_h / 2
    if slot.valign == "bottom":
        return slot.zone.y + slot.zone.h - block_h / 2
    return slot.zone.y + slot.zone.h / 2


def _warp(slot: TextSlot) -> Warp:
    """TextSlot.arc (a bow as a fraction of zone height, ±0.35) as the
    canvas's arc preset and its -1..1 dial. `arch` is a separate canvas
    preset with its own geometry; the spec's single `arc` field maps to
    `arc`, the general one, in both directions."""
    if not slot.arc:
        return Warp()
    return Warp(kind="arc",
                amount=max(-1.0, min(1.0, slot.arc / _MAX_SPEC_ARC)))


__all__ = ["CanvasIngestError", "ingest"]
