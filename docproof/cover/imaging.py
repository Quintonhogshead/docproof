"""gpt-image-2 wrapper: art layers only — backgrounds and focal elements with
no text in them, decoded to raw PNG bytes. Everything about WHAT to ask for
(prompt assembly: a Direction's art_prompt plus an archetype's
composition_note plus NEGATIVE_SUFFIX) lives in docproof.cover.pipeline, not
here; this module owns exactly one thing — the vendor call itself — so it
stays engine-shaped rather than OpenAI-shaped (see the stub comments at the
bottom for the two engines this seam is meant to grow into).

gpt-image-2 was a beta-stage release with no live API reference reachable
while this file was written (see docs/cover_designer_spec.md §7.2) — its own
parameter dialect (a resolution tier, a native aspect ratio) is a documented
best guess here, not a confirmed contract. generate() tries that shape first
and falls back to gpt-image-1's long-stable size/quality shape on a TypeError
(an older SDK build that has never heard of the new keywords) or a 400 (the
server rejects the shape outright), so a parameter-name guess can never dead-
end a cover job — see _request_params's docstring for the loud caveat.
Verify the primary shape against the live gpt-image-2 reference at
integration time and simplify this once it is confirmed.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

import openai
from PIL import Image

log = logging.getLogger("docproof.cover.imaging")

# Stated to the model verbatim, appended after a Direction's own art_prompt
# and the archetype's composition_note (docproof.cover.pipeline does that
# assembly; this is just the fixed tail every art call ends with — see
# docs/cover_designer_spec.md §7.2).
NEGATIVE_SUFFIX = ("Absolutely no text, no letters, no words, no numbers, "
                   "no watermarks, no borders, no frames.")

# Appended (before NEGATIVE_SUFFIX) to every transparent-slot prompt: the
# model's instinct on `background="transparent"` is still to paint a whole
# scene — the first live run got a full lighthouse landscape as its
# "cutout". Saying exactly what a cutout is beats hoping.
CUTOUT_SUFFIX = ("One single isolated subject only, fully visible and "
                 "complete, on a completely transparent background — no "
                 "scenery, no backdrop, no ground, no sky, nothing at all "
                 "behind or around the subject.")

# gpt-image-2 list prices, $/image by resolution tier — verify against
# https://openai.com/api/pricing/ at implementation time (the same page
# docproof/providers/catalog.py's text-model prices were checked against,
# there dated 2026-08-04). gpt-image-2 was in beta as of that date, which is
# exactly the situation where a list price moves before GA — re-check these
# three numbers before the first real job runs on them.
IMAGE_COST: dict[str, float] = {"1K": 0.03, "2K": 0.05, "4K": 0.08}

# A transient failure (rate limit, a 5xx, or a dropped connection) is worth
# retrying once — the next attempt often just works. A TypeError (this SDK
# build doesn't know a keyword we sent) or a BadRequestError (the server
# rejected the request shape) is never transient, so neither is in this
# tuple: retrying an unchanged request would just fail the same way again —
# see generate()'s parameter-shape fallback instead.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
)


class ImagingError(RuntimeError):
    """One art layer could not be generated — every retry and every
    parameter-shape fallback was exhausted. Always carries a sentence a
    person can read; docproof.cover.pipeline lands the owning concept in
    `error` state with it, while other concepts in the same job continue
    (§8's per-concept isolation)."""


def make_client(api_key: str) -> openai.OpenAI:
    """One OpenAI client for image calls. max_retries=0: generate() owns its
    own one-retry policy (see _call_with_retry), so a transient failure is
    retried exactly once — not multiplied by the SDK's own default retry
    loop running underneath it as well."""
    return openai.OpenAI(api_key=api_key, max_retries=0)


def _request_params(prompt: str, *, transparent: bool, resolution: str,
                    fallback: bool) -> dict[str, Any]:
    """Keyword arguments for one client.images.generate(**params) call.

    fallback=False — the "documented" gpt-image-2 shape this spec describes:
    a resolution tier and a native 2:3 aspect ratio. LOUD NOTE FOR THE
    INTEGRATOR: "resolution"/"aspect_ratio" are the best-documented guess at
    gpt-image-2's real field names, written without live API access —
    verify them against the current gpt-image-2 API reference and delete
    this note once confirmed (docs/cover_designer_spec.md §7.2).

    fallback=True — gpt-image-1's long-stable, SDK-typed shape (`size`,
    `quality`); 1024x1536 is the closest portrait size to the archetype
    canvas's 2:3, close enough that the pipeline never dead-ends even if
    gpt-image-2's real parameter names turn out to be something else
    entirely."""
    params: dict[str, Any] = {
        "model": "gpt-image-2", "prompt": prompt, "n": 1,
        "output_format": "png",
    }
    if fallback:
        params["size"] = "1024x1536"
        params["quality"] = "high"
    else:
        params["resolution"] = resolution           # "1K" | "2K" | "4K"
        params["aspect_ratio"] = "2:3"
    if transparent:
        params["background"] = "transparent"
    return params


def _call_once(client, params: dict[str, Any]) -> bytes:
    resp = client.images.generate(**params)
    data = getattr(resp, "data", None) or []
    b64 = getattr(data[0], "b64_json", None) if data else None
    if not b64:
        raise ImagingError("gpt-image-2 returned a response with no image data.")
    return base64.b64decode(b64)


def _call_with_retry(client, params: dict[str, Any]) -> bytes:
    """One real attempt, with a single retry for a transient failure only —
    see _TRANSIENT_ERRORS. A TypeError or BadRequestError propagates
    immediately, unretried, so generate() can try the other parameter shape
    instead of burning a retry on a request that will never succeed
    unchanged."""
    try:
        return _call_once(client, params)
    except _TRANSIENT_ERRORS as e:
        log.warning("imaging.generate: transient failure (%s); retrying once.", e)
        try:
            return _call_once(client, params)
        except _TRANSIENT_ERRORS as e2:
            raise ImagingError(
                f"Image generation failed after a retry: {e2}") from e2


def generate(client, prompt: str, *, transparent: bool = False,
            resolution: str = "2K") -> bytes:
    """One art layer as PNG bytes. `prompt` is already fully assembled (art
    prompt + composition note + NEGATIVE_SUFFIX — docproof.cover.pipeline's
    job, not this function's). `transparent` requests a cutout background
    (the cutout_sandwich archetype's `focal` slot); `resolution` is one of
    IMAGE_COST's keys.

    Tries the documented gpt-image-2 parameter shape first; on a TypeError
    (this SDK build rejects an unknown keyword) or an openai.BadRequestError
    (the server rejects the shape), falls back once to gpt-image-1's shape.
    Raises ImagingError, with a human sentence, if both shapes fail, or if a
    transient failure survives its one retry (see _call_with_retry)."""
    primary = _request_params(prompt, transparent=transparent,
                              resolution=resolution, fallback=False)
    try:
        image_bytes = _call_with_retry(client, primary)
    except (TypeError, openai.BadRequestError) as e:
        log.warning(
            "imaging.generate: the documented gpt-image-2 params "
            "(resolution=%r, aspect_ratio='2:3') were rejected (%s); "
            "falling back to gpt-image-1-style size/quality.", resolution, e)
        fallback = _request_params(prompt, transparent=transparent,
                                   resolution=resolution, fallback=True)
        try:
            image_bytes = _call_with_retry(client, fallback)
        except (TypeError, openai.BadRequestError) as e2:
            raise ImagingError(
                "Image generation failed: gpt-image-2 rejected both the "
                f"documented parameter shape and the gpt-image-1-style "
                f"fallback ({e2}).") from e2
        else:
            log.info("imaging.generate: used the gpt-image-1-style fallback "
                     "shape (size=%r, quality='high').", fallback["size"])
    else:
        log.info("imaging.generate: used the documented gpt-image-2 shape "
                 "(resolution=%r, aspect_ratio='2:3').", resolution)
    return image_bytes


def has_real_alpha(png_bytes: bytes) -> bool:
    """Whether a decoded PNG actually carries a transparent background, not
    just an alpha channel that happens to be present and uniformly opaque.
    Transparent backgrounds are a preview gpt-image-2 feature (§7.2); when
    the model ignores the request, it still returns an RGBA (or RGB) PNG, so
    "has an alpha channel" is not enough — an all-255 alpha channel is
    functionally opaque. This looks for real transparency near the image's
    edges instead (min alpha < 8 somewhere in a thin border band on each
    side) — where a cutout subject's background would be if the model
    actually cut it out. The caller (docproof.cover.pipeline) marks the slot
    opaque and degrades the composer's layer order when this is False."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        if img.mode != "RGBA":
            return False
        alpha = img.getchannel("A")
        w, h = alpha.size
        band = max(1, min(w, h) // 20)
        borders = (
            alpha.crop((0, 0, w, band)),               # top
            alpha.crop((0, max(0, h - band), w, h)),    # bottom
            alpha.crop((0, 0, band, h)),                 # left
            alpha.crop((max(0, w - band), 0, w, h)),     # right
        )
        min_alpha = min(region.getextrema()[0] for region in borders)
        if min_alpha >= 8:
            return False
        # A border sliver is not a cutout. The first live run returned a
        # full-scene "cutout" whose only transparency was a thin edge; it
        # passed the border test, got layered over the title, and buried it.
        # A real isolated subject leaves a meaningful share of the canvas
        # empty, so demand that too.
        transparent_px = sum(alpha.histogram()[:16])
        return transparent_px / (w * h) >= 0.10


# -- future engines (out of scope for v1 — stubs only, per
# docs/cover_designer_spec.md §7.2 and §14) ----------------------------------
#
# Recraft V4.x vector: native SVG output — resolution-independent focal
# elements and flat-illustration styles, appealing for the deferred print
# path and for clean cutouts without an alpha-fidelity guessing game. Would
# be a second generate()-shaped function behind the same seam
# (docproof.cover.pipeline picks an engine per slot), not a rewrite of this
# one. Not built.
#
# images.edit (mask-based inpainting): touching up one region of an
# already-generated layer without a full regeneration. A later phase. Also
# not built.

__all__ = ["IMAGE_COST", "NEGATIVE_SUFFIX", "ImagingError", "generate",
          "has_real_alpha", "make_client"]
