"""The critique pass: after a concept first composes, a vision model reviews
the finished cover the way an art director reviews a proof, before the
concept is marked ready (docs/cover_designer_spec.md §6.3).

The `Provider` protocol is text-only (see docproof.cover.direction's own
note on this exact point), so this call goes straight through the `openai`
SDK — the same precedent docproof.cover.imaging's gpt-image-2 wrapper sets,
the same key. Unlike imaging.py's engine, though, this is a JUDGMENT call
with a structured, schema-shaped answer, so its request shape has more in
common with docproof.providers.openai_provider (docproof's own OpenAI text
provider) than with imaging.generate() — see run_critique's docstring for
exactly which shape and why.

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
from dataclasses import dataclass
from typing import Any

import openai
from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError

from ..providers import NormalizedUsage, cost_of_usage, strict_json_schema
from .model import Brief, CoverSpec

log = logging.getLogger("docproof.cover.critique")

# Vision-capable per spec §6.3. Mirrored as its own constant rather than
# imported from docproof.cover.direction.LUNA_MODEL — same convention, not a
# dependency, the same call direction.py itself makes about docproof.quest.
# skin.LUNA_MODEL. Module constant, overridable per call.
CRITIQUE_MODEL = "gpt-5.6-luna"

# Token cost, and the tells that matter (type crowding, weak hierarchy, a
# palette that reads wrong) are visible at 600px — finer artifacts are not
# what this pass is for (§6.3).
MAX_WIDTH = 600

# A transient failure (rate limit, a 5xx, a dropped connection) is worth one
# retry; a TypeError or BadRequestError is a request-SHAPE problem that will
# fail the same way again unchanged — see run_critique's shape fallback
# instead. Same table as docproof.cover.imaging's _TRANSIENT_ERRORS.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
)

_SCHEMA_NAME = "cover_critique"


class CritiqueError(RuntimeError):
    """The critique call failed outright, or came back unusable — every
    retry and request shape exhausted. Always carries a sentence a person
    could read, the same convention as docproof.cover.imaging.ImagingError.
    docproof.cover.pipeline is what decides this is non-fatal (§6.3: a
    critique failure must never block a cover) — that policy lives at the
    call site, not here; see the module docstring."""


@dataclass(frozen=True)
class CritiqueResult:
    passes: bool
    tells: list[str]
    notes: str
    cost: float | None


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


# -- image prep -----------------------------------------------------------------

def _downscale_to_data_uri(png_bytes: bytes, *, max_width: int = MAX_WIDTH) -> str:
    """The composed render, downscaled to at most `max_width` px wide and
    re-encoded as a base64 PNG data URI — the shape both the Responses API's
    `input_image` and chat.completions' `image_url` content parts accept
    (either a real URL or a base64 data URL; see each param's own
    docstring in the installed openai package). Already-narrow input is
    re-encoded but not upscaled."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            height = max(1, round(img.height * (max_width / img.width)))
            img = img.resize((max_width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# -- prompts ----------------------------------------------------------------------

def _system_prompt() -> str:
    return """You are the art director at a traditional press, doing a final \
proof review before a cover ships. You will be shown the finished cover render \
plus a short summary of the book. Judge it exactly the way you would judge a \
proof crossing your desk: would this pass as a real cover on a \
traditionally-published shelf in this genre, sitting next to its competitors?

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

passes: true only if you would ship this exactly as-is — a real, \
essentially flaw-free proof, not merely "acceptable." false if there is at \
least one concrete tell above.

tells: every concrete problem you found, each one sentence. Empty if passes \
is true.

notes: if passes is false, ONE actionable revision instruction — the kind \
of note you would hand a designer for a quick second pass: adjust an \
EXISTING design choice (palette, scale, position, contrast, tracking, a \
scrim, which existing art layer sits where, and the like). You may NOT ask \
for new art, a different archetype, or new imagery of any kind — this note \
can only request a design-only fix, never a repaint. If passes is true, \
notes is an empty string."""


def _summary(spec: CoverSpec, brief: Brief) -> str:
    """One paragraph: brief + genre (§6.3) — not the fully labeled brief
    run_directions sends; the critique call is judging a finished image, not
    drafting one, so it needs just enough context to judge genre fit."""
    bits = [f'"{brief.title}"']
    if brief.subtitle:
        bits.append(f"({brief.subtitle})")
    bits.append(f"by {brief.author}, a {brief.genre} book.")
    sentence = " ".join(bits)
    if brief.mood:
        sentence += f" Mood: {brief.mood}."
    return (f"{sentence} This concept, \"{spec.concept_name}\", uses the "
           f"{spec.archetype} cover layout.")


# -- request shapes -----------------------------------------------------------------
#
# INTEGRATOR, READ THIS: two request shapes follow. The PRIMARY one (Responses
# API) is verified two ways, both without live network access:
#   1. Against the installed openai==2.53.0 package's own type stubs —
#      openai.types.responses.response_input_image_param.ResponseInputImageParam
#      (type="input_image", image_url=<url or base64 data URL>) and
#      openai.types.responses.response_text_config_param.ResponseTextConfigParam
#      (format={"type": "json_schema", "name", "schema", "strict"}).
#   2. Against THIS repo's own docproof/providers/openai_provider.py, which
#      already calls client.responses.create(instructions=..., input=...,
#      text={"format": {"type": "json_schema", ...}}) for EVERY OpenAI text
#      call docproof makes in production — the identical text.format.json_schema
#      shape, just without an image part. That file is proof this shape reaches
#      a real endpoint successfully today, not merely that the SDK's type
#      stubs allow constructing it.
# What neither of those two things can verify: whether "gpt-5.6-luna"
# SPECIFICALLY accepts an input_image content part. Vision support is a
# runtime, per-model capability — nothing in the SDK's static types encodes
# it, and this repo's model catalog (docproof/providers/catalog.py) carries
# no vision flag either. CONFIRM A REAL CALL AGAINST THIS MODEL WORKS before
# this ships; if it 400s specifically on the image part, the fix is the
# `detail` value or the image content-part shape below, not the overall
# Responses-API-first design (openai_provider.py's precedent for THAT stands).
#
# The FALLBACK (chat.completions with response_format json_schema + an
# image_url content part) is verified only against the installed package's
# type stubs (openai.types.chat.chat_completion_content_part_image_param,
# completion_create_params.ResponseFormat) — not against a real call, and not
# against any existing in-repo precedent (nothing else in docproof calls
# chat.completions). It exists purely as a safety net for an SDK/gateway that
# rejects the Responses API shape outright; if IT is what ends up firing in
# production, something about the primary shape needs fixing, not this one.

def _responses_request(*, model: str, image_data_uri: str, summary: str) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": _system_prompt(),
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": summary},
                # "low" detail deliberately: the image is already downscaled
                # to <=600px wide for token cost (§6.3); asking for "auto" or
                # "high" would let the server re-tile/upscale its own
                # analysis and spend the tokens we just avoided.
                {"type": "input_image", "image_url": image_data_uri, "detail": "low"},
            ],
        }],
        "text": {"format": {"type": "json_schema", "name": _SCHEMA_NAME,
                            "schema": strict_json_schema(_CritiquePayload),
                            "strict": True}},
    }


def _chat_completions_request(*, model: str, image_data_uri: str, summary: str
                              ) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": image_data_uri, "detail": "low"}},
            ]},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": _SCHEMA_NAME, "schema": strict_json_schema(_CritiquePayload),
            "strict": True}},
    }


def _usage_from_token_counts(*, input_tokens: int, output_tokens: int,
                             cached_tokens: int) -> NormalizedUsage:
    # Both OpenAI usage shapes count cached tokens inside the input total;
    # docproof reports the two separately (same subtraction
    # docproof.providers.openai_provider._usage makes for the identical
    # reason).
    return NormalizedUsage(input_tokens=max(input_tokens - cached_tokens, 0),
                           output_tokens=output_tokens,
                           cache_read_input_tokens=cached_tokens)


def _read_responses_reply(resp: Any) -> tuple[str, NormalizedUsage]:
    """(json_text, usage) from a Responses API answer — the same walk
    docproof.providers.openai_provider.result_from_response/_usage use for
    the identical response shape, since this call goes through the exact
    endpoint docproof's own OpenAI provider already uses in production."""
    body = resp.model_dump()
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                raise CritiqueError(
                    f"The critique model declined to review this cover: "
                    f"{part.get('refusal') or 'no reason given'}.")
    text = "".join(
        part.get("text", "")
        for item in body.get("output") or []
        if item.get("type") == "message"
        for part in item.get("content") or []
        if part.get("type") == "output_text")
    if not text:
        raise CritiqueError("The critique model returned no usable text.")
    u = body.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    usage = _usage_from_token_counts(
        input_tokens=u.get("input_tokens", 0) or 0,
        output_tokens=u.get("output_tokens", 0) or 0, cached_tokens=cached)
    return text, usage


def _read_chat_completion_reply(resp: Any) -> tuple[str, NormalizedUsage]:
    choice = resp.choices[0]
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise CritiqueError(
            f"The critique model declined to review this cover: {refusal}.")
    text = choice.message.content or ""
    if not text:
        raise CritiqueError("The critique model returned no usable text.")
    u = resp.usage.model_dump() if getattr(resp, "usage", None) is not None else {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    usage = _usage_from_token_counts(
        input_tokens=u.get("prompt_tokens", 0) or 0,
        output_tokens=u.get("completion_tokens", 0) or 0, cached_tokens=cached)
    return text, usage


def _call_with_retry(fn, *args, **kwargs):
    """One retry for a transient failure only — same policy as
    docproof.cover.imaging._call_with_retry. A TypeError/BadRequestError
    propagates immediately, unretried, so run_critique can try the other
    request shape instead of burning a retry on one that will never
    succeed unchanged."""
    try:
        return fn(*args, **kwargs)
    except _TRANSIENT_ERRORS as e:
        log.warning("critique.run_critique: transient failure (%s); "
                   "retrying once.", e)
        try:
            return fn(*args, **kwargs)
        except _TRANSIENT_ERRORS as e2:
            raise CritiqueError(
                f"The critique call failed after a retry: {e2}") from e2


def run_critique(png_bytes: bytes, spec: CoverSpec, brief: Brief, client: Any,
                 *, model: str = CRITIQUE_MODEL) -> CritiqueResult:
    """Review one composed cover the way an art director reviews a proof
    (§6.3). `png_bytes` is the full-size composed render; this function does
    its own downscale before sending anything.

    Raises CritiqueError on any failure — a bad reply, a refusal, both
    request shapes rejected, or a transient failure surviving its retry.
    Never returns a fabricated verdict; see the module docstring for why
    that's a hard line."""
    image_data_uri = _downscale_to_data_uri(png_bytes)
    summary = _summary(spec, brief)

    try:
        resp = _call_with_retry(
            client.responses.create,
            **_responses_request(model=model, image_data_uri=image_data_uri,
                                 summary=summary))
        text, usage = _read_responses_reply(resp)
    except (TypeError, openai.BadRequestError) as e:
        log.warning(
            "critique.run_critique: the Responses API shape (input_image + "
            "text.format json_schema) was rejected (%s); falling back to "
            "chat.completions.", e)
        try:
            resp = _call_with_retry(
                client.chat.completions.create,
                **_chat_completions_request(model=model,
                                            image_data_uri=image_data_uri,
                                            summary=summary))
            text, usage = _read_chat_completion_reply(resp)
        except (TypeError, openai.BadRequestError) as e2:
            raise CritiqueError(
                f"The critique call failed: both the Responses API shape "
                f"and the chat.completions fallback were rejected "
                f"({e2}).") from e2
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
        cost=cost_of_usage(usage, fallback_model=model))


__all__ = ["CRITIQUE_MODEL", "MAX_WIDTH", "CritiqueError", "CritiqueResult",
          "run_critique"]
