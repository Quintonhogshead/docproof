"""The critique pass: after a concept first composes, a vision model reviews
the finished cover the way an art director reviews a proof, before the
concept is marked ready (docs/cover_designer_spec.md §6.3). This call runs
once per round of docproof.cover.pipeline._critique_and_revise's iterating
judge loop (BRAIN wave, 2026-08-29) — the loop, its round cap, and its
identical-spec stop condition all live in pipeline.py; this module only ever
answers ONE round's question, "does this pass, and if not, what's the one
thing to fix."

The `Provider` protocol is text-only (see docproof.cover.direction's own
note on this exact point), so this call goes straight through the
`anthropic` SDK rather than through a Provider — the same "vendor SDK lives
in its own module" precedent docproof.cover.imaging sets for gpt-image-2,
just a different vendor. Unlike imaging.py's engine call, though, the actual
request shape here mirrors docproof/providers/anthropic_provider.py's own
`complete_structured` almost exactly: the same `output_config` structured-
output dialect (`{"format": {"type": "json_schema", "schema": ...}}`, an
`effort` dial gated by docproof.providers.catalog's per-model
`supports_effort`), and the same streamed call
(`client.messages.stream(**params).get_final_message()`, never a plain
`create()` — the SDK refuses a non-streaming request whose max_tokens could
run past its 10-minute ceiling; see that provider's own comment on the
identical point). anthropic_provider.py is proof this shape reaches a real
endpoint successfully today, for every OTHER Anthropic call docproof makes
in production — the one thing it does not (and cannot) prove is that an
`image` content block rides along in the same message without incident,
since none of docproof's existing Anthropic calls send one. Confirm a real
call against CRITIQUE_MODEL works before this ships.

BRAIN v2.1 (this fix wave, 2026-08-29's live-batch review): the owner
watched the judge PASS a thriller with a giant dead middle and a near-
invisible dark-on-dark silhouette — one ≤600px render hid exactly the tells
that only show up at actual shelf size. run_critique now takes a second,
optional image (`thumb_bytes`) — the job's own 100px search/shelf
thumbnail, already on disk right next to the render (see
docproof.cover.compose.save_renders's `_thumb100.png` naming) — and sends
both, each labeled, so shelf-size legibility is directly visible to the
judge rather than inferred from the large render alone. A missing
thumbnail (an old job, a test fake that only writes the primary PNG)
degrades to sending the one image; it never fails the call over that.
CritiqueResult also gains `art_defects`: the ids of art slots whose
GENERATED IMAGE ITSELF is the problem (a generation artifact, anatomical
nonsense, an illegible subject) as opposed to a slot whose art is clean but
mis-designed. A design-only revision (§6.2's machinery) can fix contrast
and placement; it can never fix a bad pixel — so
docproof.cover.pipeline._critique_and_revise reads this field to trigger
an actual, code-bounded repaint instead of just another design pass (see
that function's own comment on the one-repaint-per-concept escape hatch).

Failure posture: run_critique itself raises CritiqueError on any trouble,
the same "never guess, always raise" contract run_directions/revise_spec/
imaging.generate all share. §6.3 is explicit that a critique failure must
never block a cover from shipping — but that decision belongs to the
caller (docproof.cover.pipeline), not to this module: a function that
sometimes raises and sometimes quietly returns a fabricated "it passed"
verdict would make the two cases indistinguishable to anyone reading a call
site.
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import anthropic
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..providers import NormalizedUsage, cost_of_usage, lookup, strict_json_schema
from . import doctrine
from .model import Brief, CoverSpec

log = logging.getLogger("docproof.cover.critique")

# The workhorse model: a critique verdict is a judgment call over one already-
# composed image, the same tier of task docproof.cover.direction.
# REVISION_MODEL is priced for — mirrored as its own constant rather than
# imported, same convention direction.py itself documents about
# docproof.quest.skin.LUNA_MODEL. Module constant, overridable per call.
# The doctrine block this judge carries (docproof.cover.doctrine's `critique`
# surface): the rules a reviewer can actually NAME in a finished render, plus
# rule 14, which is the standing warning against inventing a metric. The three
# editing-session conduct rules are not here — this call has no tools and one
# question to answer. Rendered once at import; it is a constant.
_DOCTRINE = doctrine.render("critique")

CRITIQUE_MODEL = "claude-sonnet-5"

# Token cost, and the tells that matter (type crowding, weak hierarchy, a
# palette that reads wrong) are visible at 600px — finer artifacts are not
# what this pass is for (§6.3).
MAX_WIDTH = 600

# A CritiqueResult's whole answer is a bool, a short list of one-sentence
# tells, and one sentence of notes -- tiny. Generous anyway, on the same
# house lesson every other structured call in this codebase already leans
# on: max_tokens is a ceiling shared with the model's own thinking, not a
# target, and a truncated structured reply parses as nothing.
MAX_OUTPUT_TOKENS = 8000

# A transient failure (rate limit, a 5xx, a dropped connection) is worth one
# retry; anything else (a bad request, an auth failure) will fail the same
# way again unchanged, so retrying it would just waste the round trip. Same
# table shape as docproof.cover.imaging's _TRANSIENT_ERRORS, adapted to the
# anthropic SDK's own exception hierarchy (every one of these subclasses
# anthropic.APIStatusError).
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    anthropic.RateLimitError, anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


class CritiqueError(RuntimeError):
    """The critique call failed outright, or came back unusable — a bad
    reply, a refusal, or a transient failure surviving its retry. Always
    carries a sentence a person could read, the same convention as
    docproof.cover.imaging.ImagingError. docproof.cover.pipeline is what
    decides this is non-fatal (§6.3: a critique failure must never block a
    cover) — that policy lives at the call site, not here; see the module
    docstring."""


@dataclass(frozen=True)
class CritiqueResult:
    """One critique round's whole verdict. `art_defects` (BRAIN v2.1) names
    every art slot (by id, from spec.art) whose GENERATED IMAGE ITSELF is
    the problem — as opposed to `tells`/`notes`, which cover everything a
    design-only revision could actually fix. Defaulted rather than
    required so every pre-v2.1 keyword-constructed caller (pipeline.py's
    frozen-dataclass consumers, this module's own older tests) keeps
    working unchanged — the same reasoning
    docproof.cover.direction.RevisionResult.skipped already documents for
    itself."""
    passes: bool
    tells: list[str]
    notes: str
    cost: float | None
    art_defects: list[str] = field(default_factory=list)


class _CritiquePayload(BaseModel):
    """The structured reply's own shape — internal to this module.
    run_critique converts a validated instance into the public
    CritiqueResult dataclass (spec §6.3 pins CritiqueResult as a plain
    dataclass, not a pydantic model). A pydantic model here, rather than a
    hand-written JSON-schema dict, so strict_json_schema gives this call the
    same additionalProperties/required normalization every other structured
    call in this codebase relies on (docproof.quest.skin.SkinSpec,
    docproof.cover.direction.Directions/CoverSpec)."""
    model_config = ConfigDict(extra="forbid")

    passes: bool
    tells: list[str]
    notes: str
    # BRAIN v2.1: a Python default is still a required wire key
    # (strict_json_schema's own docstring) -- the model always answers with
    # SOME list, empty when no slot's art is itself defective, the same
    # convention `tells` already follows.
    art_defects: list[str] = Field(default_factory=list)


# -- image prep -----------------------------------------------------------------

def _downscale_to_base64(png_bytes: bytes, *, max_width: int = MAX_WIDTH) -> str:
    """The composed render, downscaled to at most `max_width` px wide and
    re-encoded as a BARE base64 PNG string (no `data:image/...;base64,`
    prefix) — the shape
    anthropic.types.base64_image_source_param.Base64ImageSourceParam wants:
    `data` and `media_type` are separate fields on the wire, unlike OpenAI's
    single data-URI string. Already-narrow input is re-encoded but not
    upscaled."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            height = max(1, round(img.height * (max_width / img.width)))
            img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")


# -- prompts ----------------------------------------------------------------------

def _system_prompt() -> str:
    return f"""You are the art director at a traditional press, doing a final \
proof review before a cover ships. You will be shown the finished cover render \
-- usually followed by a second image of that SAME cover at its actual \
shelf/search-thumbnail size (100px wide) -- plus a short summary of the book. \
Judge it exactly the way you would judge a proof crossing your desk: would \
this pass as a real cover on a traditionally-published shelf in this genre, \
sitting next to its competitors? Weigh both images when you have both: a \
cover can look fine large and still fail at the size a reader actually \
meets it first.

Some choices read as deliberate, well-executed design moves rather than \
flaws — judge them on execution, not on whether they are bold. In \
particular: content living inside a container shape (a light beam, a smoke \
plume, a ribbon, a doorway) that is executed cleanly, with its payload \
reading clearly inside the shape, is a strength. Do not flag a well-\
executed container device as a tell.

THE HOUSE DOCTRINE — the rules this press has learned by shipping covers \
that broke them. A tell that breaks one of these CITES its number, and rule \
2 outranks everything else on this page: a figure standing on nothing fails \
the proof no matter how good the rest is. Numbering is the house's own and \
is stable across the studio, so gaps are expected.

{_DOCTRINE}

Look for concrete, nameable problems — the kind of note an art director \
actually writes on a proof, not vague taste:
- type crowding, or text fighting the art underneath it
- a weak or unclear hierarchy (title/author/subtitle not reading in the \
right order at a glance)
- text sitting on busy art without enough contrast or scrim
- a palette that reads wrong for the genre
- visible AI-generation artifacts (extra fingers, warped objects, nonsense \
background detail, an uncanny face)
- a genre miscue (imagery that promises the wrong kind of book)
- the container device attempted but botched: a container shape too busy \
or cluttered for its payload, or the payload spilling outside its \
container's edges
- more rendered detail than the concept needs — a simple shape or \
silhouette would have hidden generation artifacts better than the detail \
actually shown
- a large empty band with nothing designed in it
- an element so low-contrast against its ground it disappears
- at thumbnail size the cover reads as a blank field with words
- the type reads pasted-on: flat, unshadowed, un-graded type sitting ON \
the image instead of living IN it, sharing none of its light or texture
- a flat, unlit composite: layers stacked with no shared light, grade, or \
atmosphere tying them into one scene
- filter soup: so many finishing effects stacked that the cover reads \
processed and muddy instead of designed
- left/right visibly unbalanced: one half of the cover carries clearly \
more visual weight. Flag this ONLY when the composer's own measurements \
(in the summary below) include a mirror-symmetry score or heavier-half \
attribution, and CITE that measured number in your tell verbatim — never \
flag imbalance from eyeballing alone when the measurements are silent.
- a near-miss alignment that survived: an element a hair off the center \
axis or off a shared rail. The composer's snap pass makes this impossible \
by construction, so if you genuinely see one, say in the tell that the \
balance pass itself appears to have missed it — this tell reports a \
pipeline bug, not a taste call.
- the typeface fights the genre's shelf: a script-titled thriller, poster \
caps on a tender romance, a blackletter romcom
- a gimmick without payoff: a type move (a stacked, arched, tilted, or \
emphasized title) or a mask that hurts legibility or hierarchy instead \
of earning its place — recommend removing it in your notes; removal is \
one patch edit

passes: true only if you would ship this exactly as-is — a real, \
essentially flaw-free proof, not merely "acceptable." false if there is at \
least one concrete tell above.

tells: every concrete problem you found, each one sentence. Empty if passes \
is true.

art_defects: the id of every art slot -- named for you, with its prompt, in \
the summary below -- whose GENERATED IMAGE ITSELF is the problem: visible \
AI-generation artifacts, anatomical or structural nonsense on an inanimate \
object, an illegible or badly warped subject. This is NOT for a slot whose \
art is clean but simply mis-designed (badly placed, badly scaled, wrong \
contrast, fighting the type) -- that belongs in tells/notes instead, since a \
design-only fix can actually address it. Only name a slot here when the \
PAINTED IMAGE ITSELF needs to be repainted from scratch, using the exact \
slot id given in the summary. Empty if every slot's art is clean.

notes: if passes is false, ONE actionable revision instruction — the kind \
of note you would hand a designer for a quick second pass: adjust an \
EXISTING design choice (palette, scale, position, contrast, tracking, a \
scrim, which existing art layer sits where, and the like). You may NOT ask \
for new art, a different archetype, or new imagery of any kind — this note \
can only request a design-only fix, never a repaint (name a slot in \
art_defects for that instead). If passes is true, notes is an empty \
string."""


def _art_slots_line(spec: CoverSpec) -> str:
    """BRAIN v2.1: names every GENERATED art slot (one with a real prompt --
    a procedural/no-prompt slot like a gradient or texture has no "generated
    image" to be defective) by its exact id, alongside the prompt that made
    it, so the judge's art_defects answer can name a real slot instead of
    guessing at one. Empty string when this concept generated no art at all
    (a big_type concept, say) -- omitted from the summary entirely then, the
    same convention the composer-warnings clause below already follows."""
    prompted = [a for a in spec.art if a.prompt]
    if not prompted:
        return ""
    listed = "; ".join(f'{a.id} ("{a.prompt}")' for a in prompted)
    return (" This concept's generated art slots, by id (name one here in "
           f"art_defects only if ITS OWN generated image is the problem): "
           f"{listed}.")


def _summary(spec: CoverSpec, brief: Brief,
            composer_warnings: Sequence[str] = ()) -> str:
    """One paragraph: brief + genre (§6.3) — not the fully labeled brief
    run_directions sends; the critique call is judging a finished image, not
    drafting one, so it needs just enough context to judge genre fit. Also
    names every generated art slot (BRAIN v2.1 — see _art_slots_line) so
    art_defects can address one by id. When the composer itself flagged
    anything (RenderReport.warnings — a contrast escalation that still fell
    short, a coverage note on a mask_from container, and the like), it
    rides along here too, clearly labeled, so the judge reasons with the
    composer's own measurements rather than re-deriving them by eye."""
    bits = [f'"{brief.title}"']
    if brief.subtitle:
        bits.append(f"({brief.subtitle})")
    bits.append(f"by {brief.author}, a {brief.genre} book.")
    sentence = " ".join(bits)
    if brief.mood:
        sentence += f" Mood: {brief.mood}."
    sentence += (f" This concept, \"{spec.concept_name}\", uses the "
                f"{spec.archetype} cover layout.")
    sentence += _art_slots_line(spec)
    if composer_warnings:
        sentence += (" The composer's own measurements flagged: "
                     + "; ".join(composer_warnings) + ".")
    return sentence


# -- request shape ------------------------------------------------------------
#
# Mirrors docproof/providers/anthropic_provider.py's own _params/
# complete_structured almost exactly (see the module docstring for the full
# reasoning): the same output_config.format.json_schema dialect, the same
# effort gating, one addition only — one or two `image` content blocks ahead
# of the text block in the one user message. Anthropic's own vision guidance
# is to put a single image before the text that refers to it, hence that
# order for the single-image (no thumbnail) case; with two images (BRAIN
# v2.1) each is preceded by its own short label so the model can address
# "the thumbnail" unambiguously in its answer, the standard multi-image
# prompting shape.

def _image_block(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": b64}}


def _request_params(*, model: str, image_b64: str, thumb_b64: str | None,
                    summary: str) -> dict[str, Any]:
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema",
                   "schema": strict_json_schema(_CritiquePayload)}}
    info = lookup(model)
    # Same gate as AnthropicProvider._params: effort is rejected outright on
    # a model that predates it, so it is catalog-gated rather than always-on.
    if info is None or info.supports_effort:
        output_config["effort"] = "low"

    # No thumbnail: exactly v1's shape (bare image, then text) -- a single
    # image was never ambiguous, so no label is added over it. A thumbnail
    # (the common case once a job has actually rendered one) labels BOTH
    # images instead, so the model can name which one it means.
    content: list[dict[str, Any]] = []
    if thumb_b64 is not None:
        content.append({"type": "text",
                        "text": "Image 1 — the full-size composed cover render:"})
    content.append(_image_block(image_b64))
    if thumb_b64 is not None:
        content.append({"type": "text", "text": (
            "Image 2 — the SAME cover at its actual shelf/search-thumbnail "
            "size (100px wide) — legibility and contrast tells often only "
            "show up here:")})
        content.append(_image_block(thumb_b64))
    content.append({"type": "text", "text": summary})

    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": _system_prompt(),
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


def _read_message(message: Any) -> tuple[str, NormalizedUsage]:
    """(json_text, usage) from an Anthropic Message — the same walk
    docproof.providers.anthropic_provider._to_result/_usage make for the
    identical response shape, since this call goes through the exact SDK
    method (messages.stream(...).get_final_message()) that provider already
    uses in production."""
    usage = _usage(message.usage)
    if message.stop_reason == "refusal":
        raise CritiqueError("The critique model declined to review this cover.")
    if message.stop_reason == "max_tokens":
        raise CritiqueError(
            "The critique model's answer was cut off before it finished.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text:
        raise CritiqueError("The critique model returned no usable text.")
    return text, usage


def _call_with_retry(client: Any, params: dict[str, Any]) -> Any:
    """One real attempt, with a single retry for a transient failure only —
    see _TRANSIENT_ERRORS. Streamed, not a plain create(): see the module
    docstring's note on why (the SDK's own 10-minute non-streaming guard);
    get_final_message() returns the same Message object a create() would
    have, so _read_message reads it unchanged either way."""
    try:
        with client.messages.stream(**params) as stream:
            return stream.get_final_message()
    except _TRANSIENT_ERRORS as e:
        log.warning("critique.run_critique: transient failure (%s); "
                   "retrying once.", e)
        try:
            with client.messages.stream(**params) as stream:
                return stream.get_final_message()
        except _TRANSIENT_ERRORS as e2:
            raise CritiqueError(
                f"The critique call failed after a retry: {e2}") from e2


def run_critique(png_bytes: bytes, thumb_bytes: bytes | None, spec: CoverSpec,
                 brief: Brief, client: Any, *, model: str = CRITIQUE_MODEL,
                 composer_warnings: Sequence[str] = ()) -> CritiqueResult:
    """Review one composed cover the way an art director reviews a proof
    (§6.3). `png_bytes` is the full-size composed render; `thumb_bytes` is
    the job's own 100px shelf/search thumbnail (BRAIN v2.1 —
    docproof.cover.compose.save_renders's `_thumb100.png` companion file,
    read straight off disk by the caller) or None when there isn't one (an
    old job, a caller that never rendered one) — this function does its own
    downscale on whichever of the two it was given, and degrades to sending
    just `png_bytes` when `thumb_bytes` is None; a missing thumbnail is
    never a reason to fail this call. `composer_warnings` is the composer's
    OWN RenderReport.warnings for this exact render (a contrast escalation
    that still fell short, a container-coverage note, and the like) — passed
    through so the judge reasons with the composer's measurements instead of
    re-deriving them by eye; empty when the caller has none to offer, which
    is the same as never having passed the argument.

    Raises CritiqueError on any failure — a bad reply, a refusal, a
    truncated answer, or a transient failure surviving its retry. Never
    returns a fabricated verdict; see the module docstring for why that's a
    hard line. `client` is an `anthropic.Anthropic` instance (see
    app/routes/cover.py's client construction) — this module never builds
    one itself, the same division imaging.py draws with make_client()."""
    image_b64 = _downscale_to_base64(png_bytes)
    thumb_b64 = _downscale_to_base64(thumb_bytes) if thumb_bytes is not None else None
    summary = _summary(spec, brief, composer_warnings)
    params = _request_params(model=model, image_b64=image_b64,
                             thumb_b64=thumb_b64, summary=summary)

    try:
        message = _call_with_retry(client, params)
        text, usage = _read_message(message)
    except CritiqueError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise CritiqueError(f"The critique call failed: {e}") from e

    try:
        payload = _CritiquePayload.model_validate_json(text)
    except ValidationError as e:
        raise CritiqueError(
            f"The critique model's answer did not match the expected "
            f"schema: {e}") from e

    return CritiqueResult(
        passes=payload.passes, tells=list(payload.tells), notes=payload.notes,
        cost=cost_of_usage(usage, fallback_model=model),
        art_defects=list(payload.art_defects))


__all__ = ["CRITIQUE_MODEL", "MAX_WIDTH", "CritiqueError", "CritiqueResult",
          "run_critique"]
