"""gpt-image-2 wrapper: art layers only — backgrounds and focal elements with
no text in them, decoded to raw image bytes (PNG unless a caller asks for
another `output_format`). Everything about WHAT to ask for
(prompt assembly: a Direction's art_prompt plus an archetype's
composition_note plus NEGATIVE_SUFFIX) lives in docproof.cover.pipeline, not
here; this module owns exactly one thing — the vendor call itself — so it
stays engine-shaped rather than OpenAI-shaped (see the stub comment at the
bottom for the other engine this seam is meant to grow into).

gpt-image-2 was a beta-stage release with no live API reference reachable
while this file was written (see docs/cover_designer_spec.md §7.2) — its own
parameter dialect (a resolution tier, a native aspect ratio) is a documented
best guess here, not a confirmed contract. generate() tries that shape first
and falls back to gpt-image-1's long-stable size/quality shape on a TypeError
(an older SDK build that has never heard of the new keywords) or a 400 (the
server rejects the shape outright), so a parameter-name guess can never dead-
end a cover job — see _request_params's docstring for the loud caveat. Which
shape actually worked is then REMEMBERED for the life of the process
(_SHAPE_CACHE), because probing before every image is a rejected round trip
per image, forever, on exactly the deployment where the guess was wrong.
edit()
does the same two-shape dance for client.images.edit (mask-based inpainting
of one region of an existing plate) — see _edit_request_params's docstring;
its parameter names are an even blinder guess, since there was no reachable
reference for images.edit specifically, only for images.generate. refine()
is that same images.edit call with the mask left off — the whole plate
re-rendered at a higher tier with the draft anchoring it — and inherits
every one of those caveats unchanged.
Verify the primary shapes against the live gpt-image-2 reference at
integration time and simplify this once they are confirmed.

All three verbs take an optional `on_partial`: with one, the call is made
with `stream=True` and the vendor's progressive frames are handed over as
they arrive (a plate visible in seconds rather than at the end), and a
vendor that will not take those keywords quietly drops back to one finished
image and is never asked again (_STREAM_CACHE).
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
    own one-retry policy (see _with_retry), so a transient failure is
    retried exactly once — not multiplied by the SDK's own default retry
    loop running underneath it as well."""
    return openai.OpenAI(api_key=api_key, max_retries=0)


# gpt-image-1's `quality` rung for each of the three tiers, used ONLY by the
# fallback parameter shape. The fallback used to be a flat quality="high"
# whatever tier was asked for, which quietly made the entire draft ladder a
# no-op wherever the fallback is the shape that runs: a "1K draft" came back
# at the slowest, dearest rung and was still billed three cents
# (docs/cover_canvas_spec.md §5, §8). The tier is a ladder in BOTH shapes
# now, so a draft is genuinely a draft either way.
_FALLBACK_QUALITY: dict[str, str] = {"1K": "low", "2K": "medium", "4K": "high"}

# gpt-image-1's portrait size — the closest thing its size vocabulary has to
# the 2:3 archetype. The fallback shape cannot express a tier as pixels, so
# the tier lands as `quality` (above) and every fallback render is this size.
_FALLBACK_SIZE = "1024x1536"

# Which of the two parameter shapes this PROCESS has seen work, per verb
# ("generate", "edit", "refine"). Without it, a deployment where the guessed
# gpt-image-2 names are wrong pays a rejected round trip to the vendor before
# every single image, forever. The first success writes the answer down and
# later calls start there; the other shape is still tried if the remembered
# one stops working, so this can cost one extra request in a transition but
# can never dead-end a job.
_SHAPE_CACHE: dict[str, bool] = {}

# The same memo for streaming: a deployment whose SDK or server does not know
# `stream`/`partial_images` says so once, and no later call asks again.
_STREAM_CACHE: dict[str, bool] = {}

# How many progressive frames to ask for when a caller passes `on_partial`.
# Three is the API's ceiling and the whole point of the feature: something on
# screen within seconds instead of a blank wait for the finished render.
PARTIAL_IMAGES = 3


def reset_capability_cache() -> None:
    """Forget which parameter shape and whether streaming were observed to
    work. For tests, and for any process that wants the next call to probe
    the vendor again from scratch."""
    _SHAPE_CACHE.clear()
    _STREAM_CACHE.clear()


def _shape_order(verb: str) -> tuple[bool, ...]:
    """The `fallback` flags to try for `verb`, best guess first. Unknown
    verb: documented shape first, exactly as this module always did."""
    remembered = _SHAPE_CACHE.get(verb)
    if remembered is None:
        return (False, True)
    return (remembered, not remembered)


def _shape_name(fallback: bool) -> str:
    return "gpt-image-1-style fallback" if fallback else "documented gpt-image-2"


def _request_params(prompt: str, *, transparent: bool, resolution: str,
                    fallback: bool, output_format: str = "png"
                    ) -> dict[str, Any]:
    """Keyword arguments for one client.images.generate(**params) call.

    fallback=False — the "documented" gpt-image-2 shape this spec describes:
    a resolution tier and a native 2:3 aspect ratio. LOUD NOTE FOR THE
    INTEGRATOR: "resolution"/"aspect_ratio" are the best-documented guess at
    gpt-image-2's real field names, written without live API access —
    verify them against the current gpt-image-2 API reference and delete
    this note once confirmed (docs/cover_designer_spec.md §7.2).

    fallback=True — gpt-image-1's long-stable, SDK-typed shape (`size`,
    `quality`); 1024x1536 is the closest portrait size to the archetype
    2:3, and the tier lands as the `quality` rung _FALLBACK_QUALITY maps it
    to, so a draft stays a draft in this shape too.

    `output_format` is the vendor's encoding of the returned image, not
    this module's: "png" is lossless and the default everything used to
    get; "webp" is a fraction of the bytes over the wire, which is real
    wall-clock on a big plate. The CALLER decides, because only the caller
    knows whether these pixels are a throwaway draft or the deliverable.

    Everything else about the request (the prompt text itself) is the
    caller's; this function only knows the dialect. Both shapes are tried
    by generate() because there is no way to be sure from here which one
    this account will accept, and the whole point of the fallback is that
    gpt-image-2's real parameter names turn out to be something else
    entirely."""
    params: dict[str, Any] = {
        "model": "gpt-image-2", "prompt": prompt, "n": 1,
        "output_format": output_format,
    }
    if fallback:
        params["size"] = _FALLBACK_SIZE
        params["quality"] = _FALLBACK_QUALITY[resolution]
    else:
        params["resolution"] = resolution           # "1K" | "2K" | "4K"
        params["aspect_ratio"] = "2:3"
    if transparent:
        params["background"] = "transparent"
    return params


def _decode_response(resp, noun: str) -> bytes:
    """The one image out of a non-streaming images.* response."""
    data = getattr(resp, "data", None) or []
    b64 = getattr(data[0], "b64_json", None) if data else None
    if not b64:
        raise ImagingError(
            f"gpt-image-2 {noun} returned a response with no image data.")
    return base64.b64decode(b64)


def _consume_stream(events, on_partial, noun: str) -> bytes:
    """Drain a streaming image response: hand every partial frame to
    `on_partial` and return the finished image's bytes.

    Written against the event SHAPE rather than exact event names. Each
    event carries a b64_json payload and says in its `type` whether it is a
    partial or the finished frame ("image_generation.partial_image" /
    ".completed", "image_edit.partial_image" / ".completed"); anything whose
    type does not say "partial" is taken as the final frame, so a rename or
    a third verb's naming degrades to "the last frame wins" rather than to
    an error.

    A callback that raises is logged and swallowed: a partial is a preview,
    and losing a render that has already been PAID for because the preview
    hook threw would be the expensive failure."""
    final: bytes | None = None
    index = 0
    for event in events:
        b64 = getattr(event, "b64_json", None)
        if not b64:
            continue
        data = base64.b64decode(b64)
        if "partial" in str(getattr(event, "type", "") or ""):
            index += 1
            try:
                on_partial(data, index)
            except Exception as e:                            # noqa: BLE001
                log.warning("imaging: a partial-image callback raised (%s); "
                            "the render itself is unaffected.", e)
        else:
            final = data
    if final is None:
        raise ImagingError(
            f"gpt-image-2 {noun} streamed no finished image, only partial "
            f"frames.")
    return final


def _rewind(params: dict[str, Any]) -> None:
    """Put any upload streams in `params` back to position 0 so the same
    dict can be sent again. images.generate carries none; images.edit
    carries an image and (sometimes) a mask, both already read once by the
    attempt that failed."""
    for stream in (params.get("image"), params.get("mask")):
        if stream is not None:
            stream.seek(0)


def _invoke(client_call, verb: str, noun: str, params: dict[str, Any],
            on_partial) -> bytes:
    """One real vendor call. Streams partial frames when the caller asked
    for them and this process still believes the vendor takes `stream` —
    and when it turns out not to, drops streaming for the rest of the
    process and immediately retries the same request without it, rather
    than letting a streaming rejection be mistaken for the parameter shape
    being wrong."""
    if on_partial is not None and _STREAM_CACHE.get(verb, True):
        streamed = dict(params, stream=True, partial_images=PARTIAL_IMAGES)
        try:
            return _consume_stream(client_call(**streamed), on_partial, noun)
        except (TypeError, openai.BadRequestError) as e:
            _STREAM_CACHE[verb] = False
            log.warning(
                "imaging.%s: streaming partial images was rejected (%s); "
                "falling back to a single finished image, and not asking "
                "again this process.", verb, e)
            _rewind(params)
    return _decode_response(client_call(**params), noun)


def _with_retry(client_call, verb: str, noun: str, params: dict[str, Any],
                on_partial) -> bytes:
    """One real attempt, with a single retry for a transient failure only —
    see _TRANSIENT_ERRORS. A TypeError or BadRequestError propagates
    immediately, unretried, so the caller can try the other parameter shape
    instead of burning a retry on a request that will never succeed
    unchanged."""
    try:
        return _invoke(client_call, verb, noun, params, on_partial)
    except _TRANSIENT_ERRORS as e:
        log.warning("imaging.%s: transient failure (%s); retrying once.",
                    verb, e)
        _rewind(params)
        try:
            return _invoke(client_call, verb, noun, params, on_partial)
        except _TRANSIENT_ERRORS as e2:
            raise ImagingError(
                f"Image {noun} failed after a retry: {e2}") from e2


def _run_shapes(client_call, verb: str, noun: str, build, *, on_partial,
                detail: str) -> bytes:
    """Both parameter shapes, best-known first (see _SHAPE_CACHE), with the
    winner remembered. `build(fallback)` returns a fresh params dict for
    that shape — fresh because edit()'s streams cannot be reused across
    attempts. `detail` only names the request in the log line."""
    last: Exception | None = None
    for use_fallback in _shape_order(verb):
        params = build(use_fallback)
        try:
            image_bytes = _with_retry(client_call, verb, noun, params,
                                      on_partial)
        except (TypeError, openai.BadRequestError) as e:
            last = e
            log.warning(
                "imaging.%s: the %s parameter shape (%s) was rejected (%s); "
                "trying the other shape.", verb, _shape_name(use_fallback),
                detail, e)
            continue
        _SHAPE_CACHE[verb] = use_fallback
        log.info("imaging.%s: used the %s shape (%s).", verb,
                 _shape_name(use_fallback), detail)
        return image_bytes
    raise ImagingError(
        f"Image {noun} failed: gpt-image-2 rejected both the documented "
        f"parameter shape and the gpt-image-1-style fallback ({last}).")


def _edit_request_params(image_png: bytes, mask_png: bytes, prompt: str, *,
                         resolution: str, fallback: bool,
                         output_format: str = "png",
                         transparent: bool = False) -> dict[str, Any]:
    """Keyword arguments for one client.images.edit(**params) call.

    image and mask travel as file-like objects (io.BytesIO), not raw bytes:
    the SDK's multipart upload needs a `.name` attribute ending in `.png` on
    each stream to set that part's MIME type correctly — generate() never
    has to fuss with this since images.generate takes no file uploads at
    all. A fresh pair of streams is built on every call (each shape attempt
    gets its own), so nothing here depends on a stream still being at
    position 0 from an earlier attempt.

    fallback=False — the "documented" gpt-image-2 shape: model, prompt,
    n=1, an output format, plus a resolution tier. LOUD NOTE FOR THE
    INTEGRATOR: unlike generate()'s shape (checked against what little of
    the gpt-image-2 docs were reachable), the field names for images.edit
    on gpt-image-2 are a best guess written with no live API access at all
    — there was nothing to check this against. Verify them against the
    current gpt-image-2 API reference and delete this note once confirmed
    (docs/cover_designer_spec.md §7.2).

    fallback=True — gpt-image-1's long-stable, SDK-typed shape (`size`,
    `quality`); 1024x1536 mirrors generate()'s own fallback size and the
    tier lands as a quality rung, exactly as it does there. `model` stays
    "gpt-image-2" here too, exactly as generate()'s fallback does — the
    model name isn't part of what's being guessed at; only the
    resolution/size keyword shape is."""
    image = io.BytesIO(image_png)
    image.name = "image.png"
    mask = io.BytesIO(mask_png)
    mask.name = "mask.png"
    params: dict[str, Any] = {
        "model": "gpt-image-2", "image": image, "mask": mask,
        "prompt": prompt, "n": 1, "output_format": output_format,
    }
    # A cutout plate stays a cutout. Without this the model treats an edit
    # of a transparent PNG as licence to paint the empty background in, and
    # a focal layer comes back as a full frame that buries everything under
    # it (the exact failure the ground-the-figure button hit).
    if transparent:
        params["background"] = "transparent"
    if fallback:
        params["size"] = _FALLBACK_SIZE
        params["quality"] = _FALLBACK_QUALITY[resolution]
    else:
        params["resolution"] = resolution            # "1K" | "2K" | "4K"
    return params


def edit(client, image_png: bytes, mask_png: bytes, prompt: str, *,
         resolution: str = "2K", output_format: str = "png",
         transparent: bool = False, on_partial=None) -> bytes:
    """Regenerate ONLY the masked region of an existing plate, guided by
    `prompt` (an instruction like "remove the lamp; extend the wall behind
    it"), leaving the rest of `image_png` untouched. `image_png` is the
    existing plate; `mask_png` is an alpha mask the same size as the image.

    Mask convention (OpenAI's images.edit convention, not this module's
    invention): TRANSPARENT pixels in the mask mark the region to
    regenerate; opaque pixels mark the region to preserve untouched. The
    caller must rasterize whatever region it drew/selected to alpha=0 there
    and alpha=255 everywhere else before passing it in here — getting this
    inverted silently regenerates the wrong 99% of the plate.

    `output_format` is the encoding asked of the vendor (see
    _request_params); `on_partial(png_bytes, index)` receives progressive
    frames as they arrive, when the vendor supports streaming.
    `transparent` says the plate being edited is a CUTOUT and must come
    back as one — pass the layer's own flag, or the repair fills the empty
    background in and returns a full frame.

    Tries whichever parameter shape this process last saw work, then the
    other one, on a TypeError (this SDK build rejects an unknown keyword)
    or an openai.BadRequestError (the server rejects the shape). Raises
    ImagingError, with a human sentence, if both shapes fail, or if a
    transient failure survives its one retry (see _with_retry)."""
    return _run_shapes(
        client.images.edit, "edit", "edit",
        lambda fb: _edit_request_params(image_png, mask_png, prompt,
                                        resolution=resolution, fallback=fb,
                                        output_format=output_format,
                                        transparent=transparent),
        on_partial=on_partial, detail=f"resolution={resolution!r}")


def _refine_request_params(image_png: bytes, prompt: str, *, resolution: str,
                           fallback: bool, output_format: str = "png",
                           transparent: bool = False) -> dict[str, Any]:
    """Keyword arguments for one client.images.edit(**params) call that
    carries an IMAGE BUT NO MASK.

    Same shape as _edit_request_params with the `mask` key simply absent —
    and absent is the whole point, not an omission: images.edit with a mask
    regenerates the masked region, images.edit with no mask re-renders the
    entire frame with the supplied image as its anchor. The image still
    travels as an io.BytesIO with a `.name` ending in `.png`, for the same
    multipart-MIME reason edit()'s does, and a fresh stream is built on
    every call.

    fallback=False — the "documented" gpt-image-2 shape: model, prompt,
    n=1, an output format, plus a resolution tier. LOUD NOTE FOR THE
    INTEGRATOR: exactly as loud as _edit_request_params's own note, and for
    the same reason — the field names for images.edit on gpt-image-2 are a
    best guess written with no live API access at all, and this function
    guesses the same names one more time. Verify them against the current
    gpt-image-2 API reference and delete this note once confirmed
    (docs/cover_designer_spec.md §7.2).

    fallback=True — gpt-image-1's long-stable, SDK-typed shape (`size`,
    `quality`); 1024x1536 mirrors edit()'s and generate()'s own fallback
    size. NOTE that the fallback shape cannot express a tier as PIXELS, so
    a "4K" refine that falls back is a 1024x1536 render at the top quality
    rung — still a faithful re-render of the same composition, just not the
    resolution that was asked for. That is the honest failure mode of a
    guessed parameter name; the caller is charged for the tier it asked for
    because that is what it requested, and the log line says which shape
    actually ran."""
    image = io.BytesIO(image_png)
    image.name = "image.png"
    params: dict[str, Any] = {
        "model": "gpt-image-2", "image": image,
        "prompt": prompt, "n": 1, "output_format": output_format,
    }
    # As in _edit_request_params: a cutout re-rendered without this comes
    # back with its background painted in.
    if transparent:
        params["background"] = "transparent"
    if fallback:
        params["size"] = _FALLBACK_SIZE
        params["quality"] = _FALLBACK_QUALITY[resolution]
    else:
        params["resolution"] = resolution            # "1K" | "2K" | "4K"
    return params


def refine(client, image_png: bytes, prompt: str, *,
           resolution: str = "4K", output_format: str = "png",
           transparent: bool = False, on_partial=None) -> bytes:
    """Re-render a whole plate at `resolution`, with `image_png` anchoring
    the composition. Returns the new plate's bytes.

    This is the finalize half of the draft→final ladder
    (docs/cover_canvas_spec.md §5, §8): a person rolls plates at the 1K
    draft tier while composing, and when one is KEPT it is re-rendered at
    full quality. It cannot be a fresh generate() call — gpt-image-2 has no
    seed, so re-prompting the same words at a higher tier returns a
    DIFFERENT picture, and the composition somebody just spent an afternoon
    arranging the type against would be gone. Feeding the kept draft back
    through images.edit is what holds it steady.

    **No mask, deliberately.** edit() masks a region to say "change only
    here"; refine() supplies no mask at all, which is how images.edit is
    told to re-render the entire frame. Passing a fully-transparent mask
    instead would be the same request phrased as a coincidence — this
    function says it outright.

    **Fidelity here is composition-faithful, not pixel-identical.** The
    model re-paints; it does not upscale. Subjects, placement, palette and
    light come back recognizably the same, and brush-level detail does not:
    a hand may hold a different number of fingers, a distant window may
    move. Anything measured against the draft's exact pixels (a mask drawn
    on it, an anchor recorded from it) must be re-measured against the
    returned plate. `prompt` is what steers that re-paint — the caller
    should state "same composition, full detail, do not re-compose" rather
    than re-describing the scene from scratch.

    Shape fallback, streaming and retries are exactly edit()'s."""
    return _run_shapes(
        client.images.edit, "refine", "refine",
        lambda fb: _refine_request_params(image_png, prompt,
                                          resolution=resolution, fallback=fb,
                                          output_format=output_format,
                                          transparent=transparent),
        on_partial=on_partial,
        detail=f"resolution={resolution!r}, no mask")


def generate(client, prompt: str, *, transparent: bool = False,
            resolution: str = "2K", output_format: str = "png",
            on_partial=None) -> bytes:
    """One art layer as image bytes. `prompt` is already fully assembled
    (art prompt + composition note + NEGATIVE_SUFFIX — docproof.cover.
    pipeline's job, not this function's). `transparent` requests a cutout
    background (the cutout_sandwich archetype's `focal` slot); `resolution`
    is one of IMAGE_COST's keys; `output_format` is the vendor encoding
    ("png" lossless, "webp" far fewer bytes on the wire).

    `on_partial(png_bytes, index)`, when given, is called with each
    progressive frame the vendor streams — a coarse plate on screen in
    seconds rather than a blank wait. It is a preview hook: what it raises
    is logged and ignored, and a vendor that will not stream simply never
    calls it.

    Tries whichever parameter shape this process last saw work, then the
    other one, on a TypeError (this SDK build rejects an unknown keyword)
    or an openai.BadRequestError (the server rejects the shape). Raises
    ImagingError, with a human sentence, if both shapes fail, or if a
    transient failure survives its one retry (see _with_retry)."""
    return _run_shapes(
        client.images.generate, "generate", "generation",
        lambda fb: _request_params(prompt, transparent=transparent,
                                   resolution=resolution, fallback=fb,
                                   output_format=output_format),
        on_partial=on_partial,
        detail=f"resolution={resolution!r}")


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
__all__ = ["IMAGE_COST", "NEGATIVE_SUFFIX", "PARTIAL_IMAGES", "ImagingError",
          "edit", "generate", "has_real_alpha", "make_client", "refine",
          "reset_capability_cache"]
