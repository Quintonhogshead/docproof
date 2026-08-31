"""Cover Canvas's plate regeneration: the verbs that spend real money, and
the one measurement pass that spends none.

Re-roll ("give me another one like this") and region inpaint ("fix her hand")
are the same discipline twice, which is why they live together here rather
than inside the route module: the HTTP layer and the AI box BOTH need them
(docs/cover_canvas_spec.md §5, §6 — the assistant's `reroll`/`inpaint` tools
are the same verbs the button shelf fires), and a second implementation
behind the assistant would be a second place for the money, the plate
history and the audit trail to drift. Three more verbs have since joined
them on exactly that argument:

- `finalize` — the top of the **quality ladder** (§5, §8). gpt-image-2 has
  no seed, so a plate re-prompted at a higher tier comes back a different
  picture and the composition somebody spent an afternoon arranging the
  type against is gone. So the ladder is: roll DRAFTS at DRAFT_TIER (~3
  cents) while composing, and when a plate is KEPT, re-render THAT PLATE at
  full quality through imaging.refine — the draft itself anchors the
  composition. Every layout and type edit in between is free.
- `ground_figure` — the §15.23 shelf button, and the one verb that draws
  its OWN mask (a band across the plate's bottom) rather than taking one
  from the client. The cardinal rule it serves: a standing figure must look
  like it is standing on something.
- `rebalance` — the only $0 verb here. It measures the plate with
  docproof.cover.balance and lands a bounded exposure correction as a
  `levels` effect through ops, so it is undoable like every other edit.

Four rules all of them share, and they are the whole module:

- **A regeneration is never destructive.** The plate being replaced is pushed
  onto `layer.plate_history` with the prompt that made it, so the history
  strip can swap it back (§5) and so nothing ever writes over a file already
  on disk. Every new plate gets its own name; `assets/` only grows.
- **Cost is returned, not just accumulated.** The caller shows the price of
  the click it just paid for (§8) while `doc.cost_usd` keeps the session
  total, so this adds to the total AND hands back the one call's price. A
  verb that spends nothing (`rebalance`, anything in the fake lane) says
  $0 honestly rather than charging pretend money.
- **The audit trail is the op log.** No plate verb is expressible as a
  docproof.canvas.ops op — ops are pure document edits and these make
  network calls and write files — so each appends its own plain-dict record
  to `doc.history`, in the same shape and the same list, and the log still
  reads as one story. `rebalance` is the exception that proves it: its
  whole effect IS a pure document edit, so it goes through ops.apply and
  lands in the same history as a real op, undoable by the same mechanism.
- **The caller persists.** Nothing here saves the document. A route saves
  once after the verb; an assistant turn may run several verbs and save
  once at the end. Saving in here would make the second one lie about what
  a turn cost if it failed halfway.

Failures are RegenError with a sentence (the layer is locked, the plate is
missing, there is no prompt to roll) — the vendor's own failures stay
imaging.ImagingError, so a caller can tell "you asked for something
impossible" apart from "OpenAI said no" and answer with a different status
code.
"""
from __future__ import annotations

import copy
import io
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageStat

from docproof.cover import balance, imaging
from docproof.cover.effects import luminance_band
# The pipeline's own two answers, imported rather than restated: plates
# belong under the SAME assets/ directory the one-shot pipeline writes to
# (ingest resolves every `source` against the job dir, and a canvas plate
# living somewhere else would be a second convention), and a re-roll asks
# for the same resolution tier the plate it replaces was generated at — a
# 1K re-roll dropped into a 2K composition is a visibly softer layer.
from docproof.cover.pipeline import ASSETS_DIR, IMAGE_RESOLUTION

from . import ops
from .model import CanvasDoc, PlateVersion

log = logging.getLogger("docproof.canvas.regen")


class RegenError(RuntimeError):
    """A regeneration this document cannot do, carrying a sentence a person
    can read.

    Deliberately parallel to docproof.canvas.ops.OpError: same register, same
    contract — the text goes straight into the AI box and the browser's error
    toast, so it has to be the whole story on its own. Distinct from
    imaging.ImagingError, which means the vendor call itself failed; the
    route maps them to different status codes."""


# A layer id becomes part of a filename, and a layer id arrives from the wire
# (a PUT of a whole document, an assistant's add_layer). model.new_layer_id
# only ever mints "ly_" + hex, but nothing in the model FORCES that, so the
# one place an id reaches the filesystem checks rather than trusts.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Two testing knobs, both env vars because they are about the MACHINE the
# canvas is running on, not about any one document. FAKE_ENV short-circuits
# the vendor entirely: both verbs write a locally painted stand-in plate and
# charge $0, so the whole loop — plate files, history strips, cost math, the
# UI refresh, the assistant's reroll tool — exercises for free. RESOLUTION_ENV
# picks the imaging.IMAGE_COST tier real calls roll at ("1K" is the 3-cent
# draft; the shipped default stays the pipeline's own IMAGE_RESOLUTION).
FAKE_ENV = "DOCPROOF_CANVAS_FAKE_IMAGING"
RESOLUTION_ENV = "DOCPROOF_CANVAS_IMAGE_RESOLUTION"


def fake_active() -> bool:
    """Whether the $0 stand-in lane is on. Callers that build a vendor
    client eagerly (the routes, the assistant's tool) check this first so a
    machine with no image key at all can still run the whole loop."""
    return os.environ.get(FAKE_ENV, "") not in ("", "0")


def _resolution() -> str:
    """The tier real calls roll at: RESOLUTION_ENV if set, else the
    pipeline's own. An unknown tier is refused with the valid names rather
    than falling back — a silently ignored knob reads as a pricing bug."""
    tier = os.environ.get(RESOLUTION_ENV, "").strip() or IMAGE_RESOLUTION
    if tier not in imaging.IMAGE_COST:
        raise RegenError(
            f"{RESOLUTION_ENV}={tier!r} is not an image tier this engine "
            f"prices — use one of {sorted(imaging.IMAGE_COST)}.")
    return tier


# -- the quality ladder (§5, §8) ----------------------------------------------
# A canvas session rolls plates cheap while the composition is still moving
# and pays for detail exactly once, on the plate that is kept:
#
#   "draft"   — force DRAFT_TIER, the ~3-cent rung. What a person clicks
#               twenty times while deciding what the cover even is.
#   "final"   — force the machine's real tier. What finalize() re-renders a
#               KEPT plate at, through imaging.refine.
#   "session" — whatever this machine is set to (_resolution()) — which is
#               what every call did before the ladder existed, and is
#               therefore the default: a caller that never heard of
#               `quality` behaves today exactly as it did yesterday.
QUALITY_LEVELS: tuple[str, ...] = ("draft", "final", "session")
DRAFT_TIER = "1K"


def _tier(quality: str) -> str:
    """The imaging.IMAGE_COST tier one call rolls at, from its rung of the
    ladder.

    "draft" answers DRAFT_TIER without consulting RESOLUTION_ENV at all:
    that knob names what a FULL-quality call rolls at, and a draft is by
    definition the cheap rung — so a machine whose knob is set to a tier
    this engine cannot price can still roll drafts, and only the calls that
    would actually have used the knob are refused. "final" and "session"
    both resolve through _resolution() and inherit its refusal; they stay
    separate names because they mean different things to the caller even
    where they agree on today's number — "final" is a promise about the
    plate, "session" is a shrug about the machine."""
    if quality == "draft":
        return DRAFT_TIER
    if quality in ("final", "session"):
        return _resolution()
    raise RegenError(
        f"quality {quality!r} is not a rung of this ladder — use one of "
        f"{', '.join(QUALITY_LEVELS)}.")


def _fake_plate(job_dir: Path, layer: Any, label: str,
                mask_png: bytes | None = None) -> bytes:
    """A locally painted stand-in for a vendor call: the current plate,
    visibly tinted and stamped, or a flat two-tone card when the layer's
    plate is missing. With `mask_png`, only the region the mask marks for
    regeneration (its transparent pixels, imaging.edit's convention) is
    tinted — so a fake inpaint shows exactly where a real one would land."""
    tints = ["#c2452e", "#2e6ec2", "#2ec27a", "#c2a12e", "#7a2ec2"]
    tint = tints[len(layer.plate_history) % len(tints)]
    try:
        base = Image.open(io.BytesIO(
            (Path(job_dir) / layer.source).read_bytes())).convert("RGBA")
    except OSError:
        base = Image.new("RGBA", (1024, 1536), "#3a3f4a")
    overlay = Image.new("RGBA", base.size, tint)
    if mask_png is not None:
        region = Image.open(io.BytesIO(mask_png)).convert("RGBA")
        if region.size != base.size:
            region = region.resize(base.size)
        # Tint strength where the mask says regenerate (alpha 0), nothing
        # where it says preserve — the inverse of the mask's own alpha.
        overlay.putalpha(region.getchannel("A").point(lambda a: 96 - a * 96 // 255))
    else:
        overlay.putalpha(96)
    out = Image.alpha_composite(base, overlay)
    ImageDraw.Draw(out).text((24, 24), label, fill="#ffffff")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def reroll(job_dir: Path, doc: CanvasDoc, layer_id: str, *, client,
           prompt: str | None = None, quality: str = "session") -> float:
    """Roll this art layer again and return what the call cost.

    The one-click verb of §5: same prompt, fresh call, new plate in the
    layer with the old one kept. `prompt` overrides the layer's own — that
    is "tweak-then-roll" (edit the prompt, then roll), and the override is
    PERSISTED onto the layer, because the next plain re-roll should ask for
    the tweaked thing, not silently revert to the plate before it.

    `quality` is the ladder rung this roll pays for — "draft" while you are
    still deciding, "final" when you already know, "session" (the default)
    for the machine's own tier. See _tier. A draft roll is a real plate in
    every other respect: it lands in the layer, keeps its predecessor, and
    can be finalized later without being re-prompted.

    Raises RegenError if the layer is not art, is locked, has no prompt to
    roll, or `quality` is not a rung; imaging.ImagingError if the generation
    itself fails. Does not save the document — see the module docstring."""
    layer = _art_target(doc, layer_id, "re-roll")
    text = (prompt if prompt is not None else layer.prompt or "").strip()
    if not text:
        raise RegenError(
            f"layer {layer_id!r} carries no prompt, so there is nothing to "
            f"roll — type one in first (an uploaded or procedural plate has "
            f"no prompt of its own).")

    fake = fake_active()
    tier = _tier(quality)
    if fake:
        png_bytes = _fake_plate(job_dir, layer,
                                f"FAKE ROLL {1 + len(layer.plate_history)}")
    else:
        png_bytes = imaging.generate(client, text,
                                     transparent=layer.transparent,
                                     resolution=tier)
    rel = _write_plate(job_dir, layer, png_bytes)
    log.info("canvas reroll: layer %s -> %s (%s%s)", layer_id, rel,
             "tweaked prompt" if prompt is not None else "same prompt",
             ", fake" if fake else "")
    layer.prompt = text
    doc.history.append({"op": "reroll", "layer_id": layer_id, "source": rel,
                        "prompt_overridden": prompt is not None})
    return _charge(doc, tier, fake)


def inpaint(job_dir: Path, doc: CanvasDoc, layer_id: str, *, client,
            instruction: str, mask_png: bytes,
            quality: str = "session") -> float:
    """Regenerate one masked region of this art layer's plate, and return
    what the call cost.

    `mask_png` follows imaging.edit's convention verbatim (see its
    docstring): TRANSPARENT marks the region to regenerate, opaque marks
    what to preserve, and the mask is the same pixel size as the plate. That
    is the client's job to rasterize — inverting it here to be "helpful"
    would regenerate the wrong 99% of the plate, silently.

    Unlike a re-roll this does NOT touch `layer.prompt`: the instruction is
    a local repair ("remove the lamp"), not a new description of the whole
    plate, so a later re-roll must still ask for the plate the person
    designed. The instruction is recorded in the history instead.

    `quality` is the same ladder rung reroll takes (see _tier). A repair on
    a draft plate is worth drafting too; a repair on a plate that has
    already been finalized should be asked for at "final", or the finalized
    plate comes back a rung softer than the rest of the cover.

    Raises RegenError if the layer is not art, is locked, its current plate
    is missing from the job directory, or `quality` is not a rung;
    imaging.ImagingError if the edit call fails. Does not save the
    document."""
    layer = _art_target(doc, layer_id, "inpaint")
    instruction = (instruction or "").strip()
    if not instruction:
        raise RegenError(
            "an inpaint needs an instruction saying what to change in the "
            "region you drew — \"remove the lamp\", \"fix her hand\".")
    if not mask_png:
        raise RegenError(
            f"the inpaint of layer {layer_id!r} arrived with an empty mask, "
            f"so there is no region to repair.")

    fake = fake_active()
    tier = _tier(quality)
    if fake:
        png_bytes = _fake_plate(job_dir, layer, "FAKE INPAINT",
                                mask_png=mask_png)
    else:
        image_png = _read_plate(job_dir, layer)
        png_bytes = imaging.edit(client, image_png, mask_png, instruction,
                                 resolution=tier)
    rel = _write_plate(job_dir, layer, png_bytes)
    log.info("canvas inpaint: layer %s -> %s (%r%s)", layer_id, rel,
             instruction, ", fake" if fake else "")
    doc.history.append({"op": "inpaint", "layer_id": layer_id, "source": rel,
                        "instruction": instruction})
    return _charge(doc, tier, fake)


# -- finalize: the top of the quality ladder (§5, §8) -------------------------

# What finalize() asks for, verbatim and unconditionally. Every clause here
# is load-bearing against the one failure mode a re-render has: the model
# treating "render this again" as an invitation to improve the composition.
# The draft is the plan; this call buys craft, not judgement.
FINALIZE_INSTRUCTION = (
    "Re-render this exact image faithfully, at full quality and full "
    "detail. Keep the composition unchanged: the same subjects, in the same "
    "places, at the same scale, with the same palette, the same light and "
    "the same camera. Refine only the craft of what is already here — "
    "texture, edges, materials, depth. Do not re-compose it, do not move "
    "anything, and do not add or remove any element.")


def _finalize_prompt(layer_prompt: str, extra: str | None) -> str:
    """FINALIZE_INSTRUCTION, plus the layer's own prompt as SUBJECT CONTEXT
    and the caller's `prompt` as emphasis.

    The layer's prompt is deliberately framed as context rather than as the
    request: re-stating "a lighthouse at dusk, oil painting" as an
    instruction invites a NEW lighthouse at dusk, which is the exact thing
    the image anchor exists to prevent. Naming the subject still helps —
    it tells the model what the shapes in the anchor image are, so detail
    lands on a lighthouse rather than on whatever the pixels resembled — so
    it goes in last-but-one, as description. `extra` is the person leaning
    on one part of it ("especially the water"), so it goes last, where the
    emphasis reads."""
    parts = [FINALIZE_INSTRUCTION]
    subject = (layer_prompt or "").strip()
    if subject:
        parts.append(f"For context, this image depicts: {subject}")
    emphasis = (extra or "").strip()
    if emphasis:
        parts.append(emphasis)
    return " ".join(parts)


def finalize(job_dir: Path, doc: CanvasDoc, layer_id: str, *, client,
             prompt: str | None = None) -> float:
    """Re-render this art layer's CURRENT plate at full quality, and return
    what the call cost.

    The other half of the draft→final ladder: roll cheap while composing
    (reroll(quality="draft"), ~3 cents a click, and every layout and type
    edit in between is free), then spend once on the plate you kept. It is
    imaging.refine, not imaging.generate, and that distinction is the whole
    feature — gpt-image-2 has no seed, so asking for the same words at a
    higher tier returns a different picture and the composition the type was
    arranged against is gone. Feeding the draft itself back through
    images.edit with no mask is what holds it steady.

    Unlike a re-roll this needs NO prompt of its own: the plate is the
    request. A layer with an empty `prompt` (an uploaded plate, a procedural
    one) finalizes perfectly well, where a re-roll of the same layer is
    correctly refused. `prompt` here is emphasis appended to
    FINALIZE_INSTRUCTION, never a replacement for it, and is NOT persisted
    onto the layer — finalizing is not a new description of the picture, so
    a later re-roll must still ask for the plate the person designed.

    Fidelity is composition-faithful, not pixel-identical (see
    imaging.refine): anything measured against the draft's exact pixels — a
    mask drawn on it, an anchor read off it — must be re-measured against
    the plate this returns.

    Charged at the "final" rung, which is the machine's own tier. Raises
    RegenError if the layer is not art, is locked, or its plate is missing;
    imaging.ImagingError if the re-render fails. Does not save the
    document."""
    layer = _art_target(doc, layer_id, "finalize")

    fake = fake_active()
    tier = _tier("final")
    text = _finalize_prompt(layer.prompt, prompt)
    if fake:
        png_bytes = _fake_plate(job_dir, layer, "FAKE FINAL")
    else:
        image_png = _read_plate(job_dir, layer)
        png_bytes = imaging.refine(client, image_png, text, resolution=tier)
    rel = _write_plate(job_dir, layer, png_bytes)
    log.info("canvas finalize: layer %s -> %s (%s%s)", layer_id, rel, tier,
             ", fake" if fake else "")
    doc.history.append({"op": "finalize", "layer_id": layer_id, "source": rel,
                        "tier": tier})
    return _charge(doc, tier, fake)


# -- ground the figure (§15.23's cardinal rule) -------------------------------

# How much of the PLATE's height the generated ground band covers, measured
# from the bottom edge. 18% is enough to hold a floor plane running away
# from the camera to a near horizon under a full-length figure, and little
# enough that it cannot reach up into a face. It is a fraction of the
# plate's own pixels, not of the canvas: the mask must be exactly the size
# of the image it accompanies (imaging.edit's own rule), and a plate that
# is cropped or offset inside the canvas would otherwise get a band
# measured against something it is not.
GROUND_BAND_FRACTION = 0.18

# The §15.23 recipe as one instruction. "Consistent with the scene's own
# light and palette" and "receding" are the two clauses that keep this from
# returning a flat grey slab pasted under the figure, and the contact
# shadow is term 1 of the spec's own gate — the ground is not credible
# without it, and asking for it here is far cheaper than a second pass.
GROUND_INSTRUCTION = (
    "Generate credible ground under the standing figure: a real surface "
    "that belongs to this scene, consistent with its own light, palette "
    "and materials, receding naturally into the distance in the scene's "
    "own perspective, with a soft contact shadow where the figure's feet "
    "meet it. The figure must read as standing on it.")


def _ground_mask(size: tuple[int, int]) -> bytes:
    """The band mask ground_figure draws for ITSELF, in imaging.edit's
    convention: transparent (regenerate) across the bottom
    GROUND_BAND_FRACTION of the plate, opaque (preserve) everywhere above.

    Every other masked verb takes its mask from the client, because the
    client is where somebody drew a region. This one is a recipe, not a
    gesture — §15.23's answer is always "a floor at the bottom of the
    frame" — so the server draws it, and the person clicks one button
    instead of tracing a rectangle they were going to trace the same way
    every time.

    Note the band is a hard edge, not a feather. imaging.edit blends its
    own seam; a soft mask edge would hand the model a zone it is only
    partly allowed to touch, which is how a ground band ends up as a haze
    creeping up the figure's shins."""
    w, h = size
    mask = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    band = max(1, round(h * GROUND_BAND_FRACTION))
    mask.paste((0, 0, 0, 0), (0, h - band, w, h))
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def _plate_size(png_bytes: bytes, layer: Any) -> tuple[int, int]:
    """The plate's own pixel size, named precisely when the bytes on disk
    are not an image at all."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            return img.size
    except OSError as e:
        raise RegenError(
            f"layer {layer.id!r}'s plate {layer.source!r} could not be read "
            f"as an image ({e}).") from e


def ground_figure(job_dir: Path, doc: CanvasDoc, layer_id: str, *, client,
                  instruction: str | None = None) -> float:
    """Give the figure on this plate something to stand on, and return what
    the call cost.

    §15.23's cardinal rule as one button (§5's shelf): *if a figure is
    standing, they MUST look like they are standing on something*, and the
    fix is usually a new plate rather than better shadow work. This is the
    inpaint recipe of that fix — regenerate the bottom band of the plate as
    a real surface, with the contact shadow that seats the figure on it.

    The mask is the server's own (see _ground_mask), which is what makes
    this a button rather than a drawing exercise: the band is always the
    bottom GROUND_BAND_FRACTION of the plate's own pixels, so the plate has
    to be read even in the fake lane, where inpaint reads it only for real.
    `instruction` is appended to GROUND_INSTRUCTION as the scene's own
    specifics ("a wet cobbled street"), never a replacement for it.

    Charged like an inpaint, at the session tier: this is a repair on the
    plate as it stands, so it should cost what a repair costs. Like an
    inpaint it leaves `layer.prompt` alone — a ground band is not a new
    description of the picture.

    Raises RegenError if the layer is not art, is locked, or its plate is
    missing or unreadable; imaging.ImagingError if the edit call fails. Does
    not save the document."""
    layer = _art_target(doc, layer_id, "ground the figure")
    text = GROUND_INSTRUCTION
    extra = (instruction or "").strip()
    if extra:
        text = f"{text} {extra}"

    fake = fake_active()
    tier = _tier("session")
    image_png = _read_plate(job_dir, layer)
    mask_png = _ground_mask(_plate_size(image_png, layer))
    if fake:
        png_bytes = _fake_plate(job_dir, layer, "FAKE GROUND",
                                mask_png=mask_png)
    else:
        png_bytes = imaging.edit(client, image_png, mask_png, text,
                                 resolution=tier)
    rel = _write_plate(job_dir, layer, png_bytes)
    log.info("canvas ground_figure: layer %s -> %s (band %.0f%%%s)", layer_id,
             rel, GROUND_BAND_FRACTION * 100, ", fake" if fake else "")
    doc.history.append({"op": "ground_figure", "layer_id": layer_id,
                        "source": rel, "instruction": text})
    return _charge(doc, tier, fake)


# -- rebalance: measure, nudge, report ($0) -----------------------------------

# The effect type this verb writes. One entry per layer, replaced rather
# than repeated — see plan_levels.
LEVELS_EFFECT = "levels"

# Both levels parameters are signed offsets in this band and nothing wider.
# §15.10's whole posture is that measurement licenses a NUDGE, not a
# re-grade: a cover that needs more than a 15% correction needs a different
# plate, and a button that could deliver one would quietly become a colour
# grader nobody asked for. The clamp is what keeps "rebalance" a suggestion
# a person can undo in one click.
LEVELS_CLAMP = 0.15

# Where the correction aims. Deliberately BELOW middle grey: a cover field
# sits dark on purpose far more often than it sits light, and aiming at a
# true 50% would fight the art direction on every moody plate. What the
# nudge is really for is §15.23 term 2 — "the plane is light enough for
# that shadow to register — lift it BEFORE darkening it" — and a plate
# already anywhere near this target moves by almost nothing.
TARGET_MEAN = 0.45

# Target luminance spread (standard deviation of the WCAG luminance band,
# 0-1). ~0.22 is a plate with real blacks and real highlights; a plate well
# under it is flat and the focal subject cannot be the loudest thing on a
# cover where nothing is loud.
TARGET_SPREAD = 0.22

# Below this spread a plate is flat enough that TARGET_SPREAD/spread is
# meaningless (and, at zero, undefined) — a single flat colour has no
# contrast to scale. Push contrast to the clamp and let the person look.
_FLAT_SPREAD = 0.01


@dataclass(frozen=True)
class PlateBalance:
    """What rebalance() measured on one plate.

    `symmetry`, `center_of_mass_x` and `warnings` are
    docproof.cover.balance's own numbers, passed through verbatim.
    `mean_luminance` and `spread` are NOT balance's — that module measures
    symmetry, mass and margins, and has no exposure measurement at all — so
    they are computed here off the same WCAG luminance band balance itself
    measures through (docproof.cover.effects.luminance_band), which is what
    keeps the two halves of this reading commensurable."""
    mean_luminance: float          # 0-1, mean of the WCAG luminance band
    spread: float                  # 0-1, its standard deviation
    symmetry: float                # balance.mirror_symmetry, 0-1
    center_of_mass_x: float        # fraction of plate width
    warnings: list[str] = field(default_factory=list)


def _spec_axis(doc: CanvasDoc) -> str:
    """Which vertical axis to judge this composition against, read out of
    the frozen source spec. `source_spec` is provenance and is never
    re-validated (model.py's own rule), so this reads defensively and falls
    back to "center" — which is also balance.measure_composite's documented
    default for a spec that never declared one."""
    axis = doc.source_spec.get("axis") if isinstance(doc.source_spec, dict) else None
    return axis if axis in ("center", "left", "right") else "center"


def measure_plate(job_dir: Path, doc: CanvasDoc, layer: Any) -> PlateBalance:
    """Measure one art layer's plate with the balance engine.

    **v1 measures the PLATE, not the composite.** balance.measure_composite
    is written for the finished render — "these judge what ships" — and the
    canvas's finished render lives in the browser, where the client
    composites live text over these plates. Measuring the single plate is
    what the server can do without a parity renderer, and it is the right
    input for the correction this verb actually applies: a `levels` effect
    lands on ONE layer, so the thing to measure is that layer. The
    consequence is honest and worth stating — type, scrims and every other
    layer are invisible to this reading, so the symmetry and centre-of-mass
    numbers describe the art, not the cover. When the server-side parity
    render of §7's v2 lands, this is the function that should be handed the
    composite instead.

    RGBA is flattened with .convert("RGB"), which reads a cutout's
    transparent surround as black — correct enough for a full-bleed field
    layer (the case the shelf button is for) and misleading on a cutout,
    where the surround is not part of the picture at all."""
    png_bytes = _read_plate(job_dir, layer)
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            rgb = img.convert("RGB")
    except OSError as e:
        raise RegenError(
            f"layer {layer.id!r}'s plate {layer.source!r} could not be read "
            f"as an image ({e}).") from e
    measured = balance.measure_composite(rgb, _spec_axis(doc))
    stat = ImageStat.Stat(luminance_band(rgb))
    return PlateBalance(mean_luminance=stat.mean[0] / 255.0,
                        spread=stat.stddev[0] / 255.0,
                        symmetry=measured.symmetry,
                        center_of_mass_x=measured.center_of_mass_x,
                        warnings=list(measured.warnings))


def _clamp(value: float) -> float:
    return max(-LEVELS_CLAMP, min(LEVELS_CLAMP, value))


def plan_correction(reading: PlateBalance) -> tuple[float, float]:
    """The bounded levels correction this reading earns: (brightness,
    contrast), both signed offsets clamped to ±LEVELS_CLAMP.

    Both numbers are TOTALS against the untouched plate, not deltas onto
    whatever correction is already on the layer — which is precisely what
    makes a second rebalance converge instead of stacking. The plate on
    disk never changes (a levels effect is drawn in the browser), so the
    measurement never changes, so the answer never changes: calling this
    verb twice lands the same two numbers and the second call is a no-op
    the person cannot tell from the first.

    `brightness` is added to normalized luminance; `contrast` widens
    proportionally about mid-grey (out = (v − 0.5)·(1 + contrast) + 0.5).
    That contract is stated here because the renderer is the client's and
    nothing else on the wire says what these two numbers mean."""
    brightness = _clamp(TARGET_MEAN - reading.mean_luminance)
    if reading.spread <= _FLAT_SPREAD:
        contrast = LEVELS_CLAMP
    else:
        contrast = _clamp(TARGET_SPREAD / reading.spread - 1.0)
    return brightness, contrast


def plan_levels(effects: list[dict[str, Any]], brightness: float,
                contrast: float) -> list[dict[str, Any]]:
    """This layer's effect stack with its levels entry set to these
    numbers: the existing one REPLACED IN PLACE, or a new one appended when
    the layer has none. Pure — plain dicts in, plain dicts out, no document
    anywhere near it.

    In place, not moved to the end: effect order is paint order
    (ops._op_set_effects's own rule), so re-balancing a layer must not
    quietly reshuffle the bevel that was drawn after its levels. And
    replaced, not appended: two levels entries on one layer would apply
    twice, which is how one button click becomes a plate that gets brighter
    every time somebody presses it."""
    entry = {"type": LEVELS_EFFECT,
             "params": {"brightness": brightness, "contrast": contrast}}
    out = copy.deepcopy(list(effects))
    for i, existing in enumerate(out):
        if isinstance(existing, dict) and existing.get("type") == LEVELS_EFFECT:
            out[i] = entry
            return out
    out.append(entry)
    return out


def _measured_sentence(reading: PlateBalance, brightness: float,
                       contrast: float) -> str:
    """The one line this verb reports back — every number that drove the
    decision, in the order a person reads them, so "why did it do that" is
    answered without opening anything."""
    line = (
        f"Measured on this plate alone: mean luminance "
        f"{reading.mean_luminance:.0%}, contrast spread {reading.spread:.0%}, "
        f"mirror symmetry {reading.symmetry:.2f}, visual centre of mass at "
        f"{reading.center_of_mass_x:.0%} of width — so levels were nudged "
        f"brightness {brightness:+.2f}, contrast {contrast:+.2f} (both "
        f"clamped to ±{LEVELS_CLAMP:.2f})")
    if reading.warnings:
        return f"{line}; balance also flags: {' '.join(reading.warnings)}"
    return f"{line}."


def rebalance(job_dir: Path, doc: CanvasDoc, layer_id: str) -> str:
    """Measure this art layer's plate, nudge its exposure, and report what
    was measured. Costs nothing — no vendor call happens here at all.

    §5's "rebalance values" shelf button: *run balance on the composite;
    nudge the field layer's exposure/contrast so the focal subject stays
    loudest; report what it measured in the AI box.* The nudge is bounded
    to ±LEVELS_CLAMP by construction (see plan_correction) — this button
    can make a cover slightly better or slightly worse, never unrecognizable.

    The correction lands through ops.apply as a `set_effects` op, not by
    assignment, which is the point: it appears in `doc.history`, undoes with
    ctrl-Z, and reads in the log exactly like the same change made by hand
    or by the assistant. Everything a plate verb has to do by hand
    (history record, audit trail) it gets for free by being expressible as
    an op.

    Returns the sentence — the caller puts it in the AI box. Raises
    RegenError if the layer is not art, is locked, or its plate cannot be
    read; ops.OpError if the effect stack it built would not validate
    (which would be this module's bug, not the caller's). Does not save the
    document."""
    layer = _art_target(doc, layer_id, "rebalance")
    reading = measure_plate(job_dir, doc, layer)
    brightness, contrast = plan_correction(reading)
    effects = [effect.model_dump(mode="json") for effect in layer.effects]
    ops.apply(doc, {"op": "set_effects", "layer_id": layer_id,
                    "effects": plan_levels(effects, brightness, contrast)})
    log.info("canvas rebalance: layer %s -> brightness %+.2f, contrast %+.2f",
             layer_id, brightness, contrast)
    return _measured_sentence(reading, brightness, contrast)


# -- shared plumbing ----------------------------------------------------------

def _art_target(doc: CanvasDoc, layer_id: str, verb: str) -> Any:
    """The art layer this verb addresses, with the three refusals every
    verb here shares: an id that names nothing, a layer with no plate to
    regenerate, and a locked layer.

    The lock check is ops.py's rule, restated for the verbs that do not go
    through ops: locking is how a person stops the background moving while
    they drag the title, and a $0.05 regeneration is exactly the kind of
    thing it has to hold against."""
    try:
        layer = doc.layer(layer_id)
    except KeyError as e:
        raise RegenError(str(e.args[0])) from e
    if layer.kind != "art":
        raise RegenError(
            f"{verb} was aimed at layer {layer_id!r}, which is a "
            f"{layer.kind} layer — only art layers have plates to "
            f"regenerate.")
    if layer.locked:
        raise RegenError(
            f"layer {layer_id!r} is locked, so {verb} was refused — unlock "
            f"it first.")
    return layer


def _read_plate(job_dir: Path, layer: Any) -> bytes:
    """The layer's current plate as bytes, named precisely when it is not
    there.

    `source` was validated relative-and-inside-the-job-dir by the model
    (model._validate_source), so this resolve is defense in depth rather
    than the only guard — but this function is the one place canvas code
    opens a path a document named, and one belt does not make a suspender."""
    job_dir = Path(job_dir).resolve()
    path = (job_dir / layer.source).resolve()
    if job_dir not in path.parents:
        raise RegenError(
            f"layer {layer.id!r}'s plate {layer.source!r} resolves outside "
            f"the job directory, so it was not read.")
    try:
        return path.read_bytes()
    except OSError as e:
        raise RegenError(
            f"layer {layer.id!r}'s plate {layer.source!r} could not be read "
            f"from the job directory ({e}).") from e


def _write_plate(job_dir: Path, layer: Any, png_bytes: bytes) -> str:
    """Save a fresh plate beside the pipeline's own, push the one it
    replaces onto the layer's history, and point the layer at the new file.

    The name is `assets/canvas_<layer id>_<n>.png` — the "canvas_" prefix so
    a job directory says at a glance which plates the one-shot pipeline
    painted (`c<n>_<slot>.png`) and which ones the editor did, and `n` from
    the history depth so the numbers read as a version count. `n` is then
    walked past anything already on disk: a document rolled back through its
    plate history has a history shorter than its file count, and quietly
    overwriting a plate somebody can still click back to would break the one
    promise this module makes."""
    if not _SAFE_ID.match(layer.id):
        raise RegenError(
            f"layer id {layer.id!r} cannot name a file — layer ids are short "
            f"words like 'ly_a91f' (letters, digits, '_' and '-' only).")
    assets = Path(job_dir) / ASSETS_DIR
    assets.mkdir(parents=True, exist_ok=True)
    n = 1 + len(layer.plate_history)
    while (assets / f"canvas_{layer.id}_{n}.png").exists():
        n += 1
    rel = f"{ASSETS_DIR}/canvas_{layer.id}_{n}.png"
    (Path(job_dir) / rel).write_bytes(png_bytes)
    # Pushed only now that the replacement is real bytes on disk: a failed
    # vendor call must leave the layer exactly as it was, not carrying a
    # history entry for a plate that is still its own.
    layer.plate_history.append(
        PlateVersion(source=layer.source, prompt=layer.prompt))
    layer.source = rel
    return rel


def _charge(doc: CanvasDoc, tier: str, fake: bool) -> float:
    """Add one image call to the session's running total and hand back what
    it cost, so the caller can show the price of the click that just
    happened (§8) while `cost_usd` keeps the total. One table
    (imaging.IMAGE_COST) prices both verbs; a fake roll is honestly $0 —
    charging pretend money would corrupt the one number §8 promises."""
    cost = 0.0 if fake else imaging.IMAGE_COST[tier]
    doc.cost_usd += cost
    return cost


__all__ = ["DRAFT_TIER", "FAKE_ENV", "FINALIZE_INSTRUCTION",
           "GROUND_BAND_FRACTION", "GROUND_INSTRUCTION", "LEVELS_CLAMP",
           "LEVELS_EFFECT", "QUALITY_LEVELS", "RESOLUTION_ENV",
           "PlateBalance", "RegenError", "fake_active", "finalize",
           "ground_figure", "inpaint", "measure_plate", "plan_correction",
           "plan_levels", "rebalance", "reroll"]
