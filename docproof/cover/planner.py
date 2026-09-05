"""The frontier composition planner (docs/cover_designer_spec.md §15.16):
the one step that turns N independently-prompted image generations into ONE
planned composition, run per concept AFTER direction chooses the concept and
BEFORE any image dollar is spent. Good covers are several layers and several
generations conceived together; a designer plans that combination before
making anything — this module is that planning mind, on a frontier reasoner.

Two calls live here, both via the `anthropic` SDK directly — the same
"vendor SDK lives in its own module" precedent docproof.cover.critique set
(and imaging.py before it, for the `openai` SDK), and the same request
shape critique.py already mirrors from docproof/providers/
anthropic_provider.py's complete_structured: the output_config
structured-output dialect (`{"format": {"type": "json_schema", "schema":
...}}`, an `effort` dial gated by docproof.providers.catalog's per-model
`supports_effort`), and a STREAMED call
(`client.messages.stream(**params).get_final_message()`, never a plain
`create()` — the SDK refuses a non-streaming request whose max_tokens could
run past its 10-minute ceiling; docproof's Anthropic-streaming house rule).

- plan_composition: brief + spec + archetype + manuscript sample -> a
  CompositionPlan — the shared lighting contract, palette anchors, depth
  planes, negative space, generation order, conditioning, the unify bind,
  and a rewritten prompt per slot, every one ending in the plan's own
  consistency suffix (light + palette + era + medium). Text-only.
- review_stage: mid-generation, ONE structured vision call per staged slot —
  the prior stage's ACTUAL render (≤600px, critique.py's exact downscale
  discipline) plus the plan and the pending slot's draft prompt -> that
  slot's final prompt and placement (anchor/scale/offset, plus an optional
  linear gradient-mask angle), so the focal is prompted and positioned
  against where the background's negative space and horizon REALLY landed,
  not where the plan hoped.

Neither call is the breadth call: run_directions stays cheap and N-wide
(§15.16's "direction proposes; the planner engineers the winner"), and the
whole module sits behind docproof.cover.pipeline's COVER_PLANNER gate — off
means today's spontaneous path, byte-for-byte, zero calls into this file.

Failure posture: everything here raises PlannerError with a sentence a
person can read — the run_directions/revise_spec/run_critique "never guess,
always raise" contract. §15.16 is equally explicit that a planner failure
must NEVER cost a cover (log, ledger note, fall back to the spontaneous
path) — but that decision belongs to the caller (docproof.cover.pipeline),
not here, for exactly the reason critique.py's docstring already gives: a
function that sometimes raises and sometimes fabricates a plan would make
the two indistinguishable at the call site.

Model & cost (§15.16's "cite current pricing in a comment"): PLANNER_MODEL
is claude-fable-5 at $10/$50 per MTok in/out, falling back to claude-opus-5
at $5/$25 — both per docproof/providers/catalog.py, whose prices were
verified against the vendors' published pages 2026-08-04 (that file's own
header). A plan call reads a few thousand tokens (brief + spec summary +
manuscript sample) and writes ~1-4K including the model's own thinking;
each stage review adds one ≤600px image (~a thousand tokens) and a small
structured reply — landing the whole per-concept spend in §15.16's quoted
~$0.10–0.30 band, priced for the ledger via the same cost_of_usage helper
critique.py uses.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any, Literal, Sequence

import anthropic
from PIL import Image
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

from ..providers import NormalizedUsage, cost_of_usage, lookup, strict_json_schema
from . import doctrine
from .archetypes import Archetype
from .model import Brief, CoverSpec
from .recipes import describe_recipes

log = logging.getLogger("docproof.cover.planner")

# The frontier reasoner §15.16 pins for planning, with its named fallback —
# tried ONLY on a model-not-found error (the id retired, or an account that
# can't see it), never on an ordinary failure: any other error would fail
# identically on the second model and just double the round trip.
PLANNER_MODEL = "claude-fable-5"
PLANNER_FALLBACK_MODEL = "claude-opus-5"

# Planning is the one place in this pipeline where reasoning depth pays for
# itself (it steers every image dollar downstream); a stage review is a
# bounded placement question over one image. Both dials are catalog-gated
# exactly like critique.py's, just set deeper than its "low".
# The doctrine block both calls in this file carry (docproof.cover.doctrine's
# `plan` surface): the arrangement rules — the grounding stack, depth bands by
# value, ground-contact agreement — because this is the one call that decides
# how plates will relate to each other before a dollar is spent. Rendered once
# at import, not per call: it is a constant, and building it inside an f-string
# in each prompt function is how the two would drift.
_DOCTRINE = doctrine.render("plan")

PLAN_EFFORT = "high"
REVIEW_EFFORT = "medium"

# critique.py's exact discipline: composition tells are visible at 600px,
# and a stage review is judging where negative space and the horizon landed,
# not fine pixels.
MAX_WIDTH = 600

# Generous on the same house lesson every structured call here leans on
# (§15.16 says 8000+ outright): max_tokens is a ceiling SHARED with the
# model's own thinking, not a target, and a truncated structured reply
# parses as nothing — silent loss.
MAX_OUTPUT_TOKENS = 16000

# §15.16's staging bounds, owned here so pipeline.py and the tests read the
# same numbers: at most 3 sequential stages per concept, at most 1 vision
# review per stage. (Enforcement lives in the pipeline's stage builder; the
# planner model is TOLD the bounds but never trusted with them.)
MAX_STAGES = 3
MAX_REVIEWS_PER_STAGE = 1

# Same transient table as critique.py, adapted from imaging.py's original:
# worth exactly one retry; anything else fails the same way again unchanged.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    anthropic.RateLimitError, anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


class PlannerError(RuntimeError):
    """A planning or stage-review call failed outright, or came back
    unusable — a bad reply, a refusal, a truncated answer, or a transient
    failure surviving its retry. Always carries a sentence a person could
    read (the ImagingError/CritiqueError convention).
    docproof.cover.pipeline is what decides this is non-fatal (§15.16: a
    planner failure must never block a cover) — that policy lives at the
    call site, not here."""


# Defined HERE, not in model.py: the plan is planner vocabulary, not spec
# vocabulary — a CoverSpec renders identically with or without one, and
# keeping it out of model.py keeps the archival spec contract untouched.
# Everything is strict-schema-safe the way the rest of the wire already is
# (flat fields; lists, never tuples or open dicts — OpenAI's strict mode
# rejects tuple prefixItems and open keys, and strict_json_schema normalizes
# for both vendors), with nested list-item models only, the CoverSpec shape.

class PlanDepth(BaseModel):
    """One slot's place in the depth stack: its plane, and where negative
    space must fall — computed FROM the text zones by code (the planner is
    shown them), but restated by the planner in prompt language the image
    model obeys (§15.16)."""
    model_config = ConfigDict(extra="forbid")

    slot: str
    plane: Literal["far", "mid", "near"]
    negative_space: str


class PlanConditioning(BaseModel):
    """One staged dependency: before `slot` is generated, the planner
    reviews `review`'s ACTUAL render (vision) and finalizes `slot`'s prompt
    and placement against it."""
    model_config = ConfigDict(extra="forbid")

    slot: str
    review: str


class PlanPrompt(BaseModel):
    """One slot's rewritten generation prompt — always ending in the plan's
    shared consistency suffix (plan_composition appends it if the model
    forgot; a guarantee, not a hope). The pipeline still layers the
    archetype's composition note and imaging.NEGATIVE_SUFFIX on top at
    generation time, exactly as it does for a spontaneous prompt."""
    model_config = ConfigDict(extra="forbid")

    slot: str
    prompt: str


class _PlanPayload(BaseModel):
    """The structured reply's own wire shape — what the model answers with.
    CompositionPlan below extends it with bookkeeping the wire never
    carries, the same split _CritiquePayload/CritiqueResult draw."""
    model_config = ConfigDict(extra="forbid")

    # One shared lighting contract — key-light direction, quality, time of
    # day — injected verbatim into EVERY art prompt (§15.16: the single
    # biggest "these layers belong together" lever).
    light: str
    # The exact hexes each generation must name, drawn from the spec's
    # palette — each entry one "role #rrggbb" string.
    palette_anchors: list[str]
    depth: list[PlanDepth]
    # Shared horizon line as a fraction of canvas height (0 = top). Clamped
    # rather than range-validated: a plan lost over a 1.02 would be a plan
    # lost over nothing (AdjustLayer's forgiving-but-validated doctrine).
    horizon_y: float
    # Slot ids as sequential stages, plate-first; [] = no staging (paint
    # everything in one bounded gather, exactly the spontaneous path).
    generation_order: list[str]
    conditioning: list[PlanConditioning]
    # The finishing bind: a recipe name from the shelf ("" = none), and/or
    # 2-3 gradient_map stops (palette role names or #rrggbb hexes, dark to
    # light; [] = none) the planner wants over the assembled stack.
    unify_recipe: str = ""
    unify_stops: list[str] = Field(default_factory=list)
    # The shared tail (light + palette + era + medium) every rewritten
    # prompt ends with, layered on top of imaging's NEGATIVE_SUFFIX.
    consistency_suffix: str
    prompts: list[PlanPrompt]


class CompositionPlan(_PlanPayload):
    """§15.16's whole plan, plus what it cost and which model wrote it —
    both defaulted so the wire payload (which never carries them) lifts
    straight into this type, and so plan.json round-trips completely
    (model_validate_json of a dumped plan is the same plan, bookkeeping
    included — the replayability guarantee)."""
    cost: float | None = None
    model: str = ""

    def prompt_for(self, slot_id: str) -> str | None:
        """The rewritten prompt for one slot, or None when the plan never
        addressed it (the pipeline then leaves the direction's own prompt
        alone — spontaneous for that slot)."""
        return next((p.prompt for p in self.prompts if p.slot == slot_id), None)

    def review_source_for(self, slot_id: str) -> str:
        """Which earlier slot's render conditions this slot, or "" when the
        plan declared no review for it."""
        return next((c.review for c in self.conditioning if c.slot == slot_id), "")

    def judge_lines(self) -> list[str]:
        """The plan restated for the §6.3 judge, riding the same
        composer_warnings channel the composer's own measurements use — so
        plan-vs-render drift is a nameable tell (§15.16), reasoned from the
        plan's own words rather than re-derived by eye. The unify line only
        exists when the plan actually declared a bind."""
        lines = [f"the composition plan's lighting contract, which every "
                 f"layer was generated under and the render should honor: "
                 f"{self.light}"]
        if self.unify_recipe or self.unify_stops:
            bind = []
            if self.unify_recipe:
                bind.append(f"recipe {self.unify_recipe}")
            if self.unify_stops:
                bind.append(f"gradient_map stops {', '.join(self.unify_stops)}")
            lines.append("the composition plan's unify bind over the "
                         f"assembled stack: {'; '.join(bind)}")
        return lines


class _ReviewPayload(BaseModel):
    """One stage review's wire shape: the pending slot's final prompt plus
    its placement fields — the exact ArtSlot fields the plan is allowed to
    position (§15.16: anchor, scale, offset, mask). Pairs are lists, never
    tuples, the ArtSlot wire rule."""
    model_config = ConfigDict(extra="forbid")

    prompt: str
    anchor: list[float]
    scale: float
    offset: list[float]
    # Optional soft edge: a linear gradient-mask angle in degrees (y-down;
    # 90 = top-transparent -> bottom-opaque, GradientMask's own reading),
    # or null for no mask. The one mask move a placement review may make —
    # richer masking stays direction/revision vocabulary.
    mask_angle: float | None = None

    @field_validator("anchor", "offset")
    @classmethod
    def _pair(cls, value: list[float]) -> list[float]:
        """Mirrors ArtSlot's own pair validators in shape; the [-2, 2]
        bounds are deliberately left to the pipeline's ArtSlot
        re-validation at apply time, the single place placement legality
        is already defined."""
        if len(value) != 2:
            raise ValueError("anchor/offset must be exactly [x, y]")
        return value


class StageReview(_ReviewPayload):
    """A finished stage review: the wire payload plus cost/model
    bookkeeping, same split as CompositionPlan."""
    cost: float | None = None
    model: str = ""


def _downscale_to_base64(png_bytes: bytes, *, max_width: int = MAX_WIDTH) -> str:
    """A prior stage's render, downscaled to at most `max_width` px wide and
    re-encoded as a BARE base64 PNG string (no data-URI prefix) — the shape
    the anthropic SDK's Base64ImageSourceParam wants, `data` and
    `media_type` as separate wire fields. Copied from critique.py rather
    than imported: this module is deliberately self-contained (§15.16), and
    the helper is eleven lines."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            height = max(1, round(img.height * (max_width / img.width)))
            img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")


def _image_block(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": b64}}


def _plan_system_prompt() -> str:
    return f"""You are the composition planner at a traditional press's art \
department. A cover concept has been chosen; several art layers will now be \
generated as SEPARATE images by an image model and composited with the \
typography. Your job is to plan them as ONE composition BEFORE any image is \
generated — the way a designer conceives a multi-layer build before making \
anything. Every generation prompt you write is executed verbatim; nothing \
guarantees the layers belong together except your plan.

THE HOUSE DOCTRINE — the constraints your plan must satisfy, each one \
learned by shipping a cover that broke it. These are not style preferences: \
a plan that requests a standing cutout without naming the surface it stands \
on and which plate that surface comes from is a failed plan, however good \
its prompts are. Numbering is the house's own and is stable across the \
studio, so gaps are expected.

{_DOCTRINE}

Answer with:

light: one shared lighting contract — key-light direction, quality, time of \
day — as a sentence or two that will be injected verbatim into EVERY art \
prompt. This is the single biggest "these layers belong together" lever; \
commit to one light and never contradict it.

palette_anchors: the exact hexes each generation must name, drawn ONLY from \
the palette you are given — each entry one "role #rrggbb" string.

depth: one entry per slot you write a prompt for — its plane (far, mid, \
near), and where that layer's negative space must fall, restated from the \
text zones you are given in plain prompt language the image model obeys \
("leave the upper third as empty sky"). Never let generated detail land \
under a text zone.

horizon_y: the ONE shared horizon line, as a fraction of canvas height \
(0 = top edge, 1 = bottom). Every layer that implies a horizon must agree.

generation_order: the slots as sequential stages, plate first — the layer \
others must match (usually the background) generates before the layers that \
must sit in it. At most {MAX_STAGES} stages. Leave the list empty if the \
concept genuinely has no dependency worth sequencing.

conditioning: for each later-stage slot that should be finalized against \
reality rather than hope, name which earlier slot's finished render you \
want to review before that slot generates (at most \
{MAX_REVIEWS_PER_STAGE} review per stage). Typically the focal reviews the \
background.

unify_recipe / unify_stops: the finishing bind over the assembled stack — \
a recipe name from the shelf you are given (or "" for none), and/or 2-3 \
gradient_map stops (palette role names or #rrggbb hexes, dark to light; \
empty list for none). Prefer one bind, not both, unless the concept truly \
needs both.

consistency_suffix: one shared closing sentence naming the light, the \
anchor hexes, the era, and the rendering medium — the tail every prompt \
ends with.

prompts: the rewritten generation prompt for every slot you were shown, \
each ending with your consistency_suffix verbatim. Keep each prompt \
concrete and compositional (what, where in frame, what stays empty); the \
system appends its own no-text and layout suffixes afterward, so never ask \
for text, letters, or typography in an image."""


def _brief_lines(brief: Brief) -> str:
    lines = [f'Title: "{brief.title}"']
    if brief.subtitle:
        lines.append(f'Subtitle: "{brief.subtitle}"')
    lines.append(f"Author: {brief.author}")
    lines.append(f"Genre: {brief.genre}")
    if brief.mood:
        lines.append(f"Mood: {brief.mood}")
    if brief.pitch:
        lines.append(f"Pitch: {brief.pitch}")
    if brief.must_include:
        lines.append(f"Must include: {brief.must_include}")
    if brief.avoid:
        lines.append(f"Avoid: {brief.avoid}")
    return "\n".join(lines)


def _palette_lines(spec: CoverSpec) -> str:
    p = spec.palette
    return (f"background {p.background}, primary {p.primary}, accent "
            f"{p.accent}, text {p.text}, scrim {p.scrim}")


def _text_zones_line(spec: CoverSpec) -> str:
    """The text zones, code-computed (§15.16: the planner restates negative
    space FROM these, it never invents them) — x/y from the top-left, as
    percentages of the canvas."""
    parts = []
    for t in spec.text:
        z = t.zone
        parts.append(f"{t.id} x {z.x:.0%}-{z.x + z.w:.0%}, "
                     f"y {z.y:.0%}-{z.y + z.h:.0%}")
    return "; ".join(parts)


def _slots_lines(spec: CoverSpec) -> str:
    entries = []
    for a in spec.art:
        if not a.prompt:
            continue          # procedural slots are not generated, not planned
        kind = "transparent cutout" if a.transparent else "opaque"
        entries.append(f'- {a.id} ({kind}); draft prompt: "{a.prompt}"')
    return "\n".join(entries)


def _plan_user_text(brief: Brief, spec: CoverSpec, archetype: Archetype,
                    manuscript_sample: str) -> str:
    parts = [
        _brief_lines(brief),
        (f'Chosen concept: "{spec.concept_name}" — {spec.rationale} '
         f"Layout archetype: {spec.archetype} ({archetype.describe}) "
         f"Its composition note, already appended to every prompt: "
         f'"{archetype.composition_note}"'),
        f"Palette (the only hexes you may anchor to): {_palette_lines(spec)}",
        (f"Text zones the typography will occupy (fractions of the canvas, "
         f"top-left origin) — generated detail must stay out of them: "
         f"{_text_zones_line(spec)}"),
        f"Slots to plan (write one rewritten prompt per slot):\n{_slots_lines(spec)}",
        f"Finishing-recipe shelf (for unify_recipe):\n{describe_recipes()}",
    ]
    if manuscript_sample:
        parts.append("Grounding sample from the manuscript itself:\n"
                     + manuscript_sample)
    return "\n\n".join(parts)


def _review_system_prompt() -> str:
    return f"""You are the composition planner reviewing a staged cover \
build mid-generation. Earlier stages have actually been generated; you are \
shown their real renders. Finalize the PENDING slot named below against \
where the earlier layers' light, horizon, and negative space REALLY landed \
— not where the plan hoped.

THE HOUSE DOCTRINE binds this answer harder than it binds the plan, because \
this is the last moment anything can be changed before the pixels are \
bought. You are looking at the real plate: if the doctrine's ground rules \
cannot be met against what actually rendered, say so in the prompt you \
write rather than anchoring a figure onto a surface that is not there.

{_DOCTRINE}

Answer with:

prompt: the pending slot's final generation prompt — start from the draft, \
adjust it to match the reviewed render (light direction, palette, where \
empty space actually is), and end it with the plan's consistency suffix \
verbatim. Never ask for text or letters in the image.

anchor: [x, y] focal point (canvas fractions, 0-1 each) to keep in frame \
when the layer is fitted — where this layer's subject should sit against \
the render you reviewed.

scale: post-fit zoom, usually 1.0-1.4.

offset: [x, y] placement nudge as canvas fractions — small values; \
[0, 0] for none.

mask_angle: a linear gradient-mask angle in degrees to fade one edge of \
this layer softly into the stack (90 fades the top edge, feet planted; 270 \
the reverse; 0 fades the left edge), or null for no mask — null is the \
right answer unless the layer genuinely needs a soft edge."""


def _review_user_content(plan: CompositionPlan, slot_id: str,
                         prior_renders: Sequence[bytes],
                         draft_prompt: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for i, png in enumerate(prior_renders, start=1):
        content.append({"type": "text",
                        "text": f"Prior stage render {i}, already generated:"})
        content.append(_image_block(_downscale_to_base64(png)))
    depth = next((d for d in plan.depth if d.slot == slot_id), None)
    lines = [
        f"Pending slot: {slot_id}",
        f"The plan's lighting contract: {plan.light}",
        f"Palette anchors: {', '.join(plan.palette_anchors)}",
        f"Shared horizon_y: {plan.horizon_y:g}",
    ]
    if depth is not None:
        lines.append(f"Planned depth for this slot: {depth.plane} plane; "
                     f"negative space: {depth.negative_space}")
    lines.append(f'Consistency suffix every prompt must end with: '
                 f'"{plan.consistency_suffix}"')
    lines.append(f'Draft prompt for {slot_id}: "{draft_prompt}"')
    content.append({"type": "text", "text": "\n".join(lines)})
    return content


def _request_params(*, model: str, system: str, content: list[dict[str, Any]],
                    schema_model: type[BaseModel], effort: str) -> dict[str, Any]:
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema",
                   "schema": strict_json_schema(schema_model)}}
    info = lookup(model)
    # Same gate as AnthropicProvider._params and critique._request_params:
    # effort is rejected outright on a model that predates it.
    if info is None or info.supports_effort:
        output_config["effort"] = effort
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": output_config,
    }


def _usage(u: Any) -> NormalizedUsage:
    return NormalizedUsage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(
            u, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)


def _read_message(message: Any, doing: str) -> tuple[str, NormalizedUsage]:
    """(json_text, usage) from an Anthropic Message — critique._read_message
    with this module's own error voice; `doing` names the call for the
    human sentence ("plan this composition" / "review this stage")."""
    usage = _usage(message.usage)
    if message.stop_reason == "refusal":
        raise PlannerError(f"The planner model declined to {doing}.")
    if message.stop_reason == "max_tokens":
        raise PlannerError(
            f"The planner model's answer was cut off before it finished "
            f"while trying to {doing}.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text:
        raise PlannerError(
            f"The planner model returned no usable text while trying to "
            f"{doing}.")
    return text, usage


def _stream_once(client: Any, params: dict[str, Any]) -> Any:
    with client.messages.stream(**params) as stream:
        return stream.get_final_message()


def _call_with_retry(client: Any, params: dict[str, Any]) -> Any:
    """One real attempt, one retry for a transient failure only — streamed,
    never a plain create() (the SDK's own 10-minute non-streaming guard;
    see the module docstring). A NotFoundError propagates untouched so
    _call_planner can try the fallback MODEL, which is a different request,
    not a retry of the same one."""
    try:
        return _stream_once(client, params)
    except _TRANSIENT_ERRORS as e:
        log.warning("planner: transient failure (%s); retrying once.", e)
        try:
            return _stream_once(client, params)
        except _TRANSIENT_ERRORS as e2:
            raise PlannerError(
                f"The planner call failed after a retry: {e2}") from e2


def _call_planner(client: Any, params: dict[str, Any]) -> tuple[Any, str]:
    """(message, model_that_answered). §15.16's model fallback, and ONLY
    that: a model-not-found error (the SDK's NotFoundError — the id
    retired, or a key that can't see the frontier model) retries the same
    request once on PLANNER_FALLBACK_MODEL. Every other failure mode
    propagates for the caller to wrap — a bad request or a refusal would
    fail identically on the fallback and just double the spend."""
    model = params["model"]
    try:
        return _call_with_retry(client, params), model
    except anthropic.NotFoundError as e:
        if model == PLANNER_FALLBACK_MODEL:
            raise PlannerError(
                f"The planner's fallback model {model!r} was not found "
                f"either: {e}") from e
        log.warning("planner: model %r not found (%s); falling back to %r.",
                    model, e, PLANNER_FALLBACK_MODEL)
        fallback = {**params, "model": PLANNER_FALLBACK_MODEL}
        return _call_with_retry(client, fallback), PLANNER_FALLBACK_MODEL


def _with_suffix(prompt: str, suffix: str) -> str:
    """`prompt`, guaranteed to end in `suffix` — appended when the model
    forgot (§15.16's consistency contract is enforced in code, not hoped
    for). Whitespace-tolerant on the existing tail."""
    if not suffix:
        return prompt
    if prompt.rstrip().endswith(suffix.rstrip()):
        return prompt
    return f"{prompt.rstrip()} {suffix.strip()}"


def _normalized_plan(payload: _PlanPayload, *, cost: float | None,
                     model: str) -> CompositionPlan:
    """The wire payload lifted into a CompositionPlan with this module's
    guarantees applied in code: every prompt ends with the consistency
    suffix, horizon_y is clamped to the canvas, and generation_order is
    de-duplicated in first-appearance order. Existence checks against the
    actual spec (unknown slot ids, the recipe shelf) deliberately live in
    the pipeline's apply step instead — the plan itself stays a faithful,
    auditable record of what the model said, minimally repaired."""
    prompts = [PlanPrompt(slot=p.slot,
                          prompt=_with_suffix(p.prompt, payload.consistency_suffix))
               for p in payload.prompts]
    return CompositionPlan(
        **{**payload.model_dump(),
           "horizon_y": min(1.0, max(0.0, payload.horizon_y)),
           "generation_order": list(dict.fromkeys(payload.generation_order)),
           "prompts": [p.model_dump() for p in prompts]},
        cost=cost, model=model)


def plan_composition(brief: Brief, spec: CoverSpec, archetype: Archetype,
                     manuscript_sample: str, client: Any, *,
                     model: str = PLANNER_MODEL) -> CompositionPlan:
    """One frontier planning call for one concept (§15.16): brief + spec +
    archetype + the job's manuscript sample -> a CompositionPlan. Text-only
    (the vision half of this module is review_stage). `client` is an
    `anthropic.Anthropic` instance the caller built — this module never
    builds one itself, the same division critique.py and imaging.py draw.

    Raises PlannerError on any failure: a call error, a refusal, a
    truncated or schema-breaking reply, or a transient failure surviving
    its retry. Falls back from `model` to PLANNER_FALLBACK_MODEL on a
    model-not-found error ONLY (see _call_planner). Never returns a
    fabricated plan; the never-block-a-cover policy is the caller's
    (docproof.cover.pipeline's planner gate), not this function's."""
    params = _request_params(
        model=model, system=_plan_system_prompt(),
        content=[{"type": "text",
                  "text": _plan_user_text(brief, spec, archetype,
                                          manuscript_sample)}],
        schema_model=_PlanPayload, effort=PLAN_EFFORT)
    try:
        message, used_model = _call_planner(client, params)
        text, usage = _read_message(message, "plan this composition")
    except PlannerError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise PlannerError(f"The composition-planning call failed: {e}") from e

    try:
        payload = _PlanPayload.model_validate_json(text)
    except ValidationError as e:
        raise PlannerError(
            f"The planner model's plan did not match the expected schema: "
            f"{e}") from e
    return _normalized_plan(
        payload, cost=cost_of_usage(usage, fallback_model=used_model),
        model=used_model)


def review_stage(plan: CompositionPlan, slot_id: str,
                 prior_renders: list[bytes], draft_prompt: str, client: Any,
                 *, model: str = PLANNER_MODEL) -> StageReview:
    """One staged-generation review (§15.16): the prior stages' ACTUAL
    renders (downscaled to ≤600px each, critique.py's discipline) + the
    plan + the pending slot's draft prompt -> that slot's final prompt and
    placement fields (anchor/scale/offset, plus an optional linear
    gradient-mask angle). One structured vision call; the pipeline applies
    the answer to the ArtSlot before generating it.

    Raises PlannerError on any failure, same contract and same
    model-fallback rule as plan_composition. `prior_renders` must be
    non-empty — reviewing nothing is a caller bug, not a model question."""
    if not prior_renders:
        raise PlannerError(
            f"A stage review for {slot_id!r} was requested with no prior "
            f"renders to review.")
    params = _request_params(
        model=model, system=_review_system_prompt(),
        content=_review_user_content(plan, slot_id, prior_renders, draft_prompt),
        schema_model=_ReviewPayload, effort=REVIEW_EFFORT)
    try:
        message, used_model = _call_planner(client, params)
        text, usage = _read_message(message, f"review the {slot_id} stage")
    except PlannerError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise PlannerError(f"The stage-review call failed: {e}") from e

    try:
        payload = _ReviewPayload.model_validate_json(text)
    except ValidationError as e:
        raise PlannerError(
            f"The planner model's stage review did not match the expected "
            f"schema: {e}") from e
    return StageReview(
        **{**payload.model_dump(),
           "prompt": _with_suffix(payload.prompt, plan.consistency_suffix)},
        cost=cost_of_usage(usage, fallback_model=used_model),
        model=used_model)


__all__ = ["MAX_REVIEWS_PER_STAGE", "MAX_STAGES", "MAX_WIDTH",
           "PLANNER_FALLBACK_MODEL", "PLANNER_MODEL", "CompositionPlan",
           "PlanConditioning", "PlanDepth", "PlanPrompt", "PlannerError",
           "StageReview", "plan_composition", "review_stage"]
