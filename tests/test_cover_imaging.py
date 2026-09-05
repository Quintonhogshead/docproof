"""docproof/cover/imaging.py: the gpt-image-2 wrapper.

No network, no real openai.OpenAI call anywhere here — every test drives a
fake client object shaped like the bits of the SDK generate() actually
touches (client.images.generate(**params) -> an object with .data[0].b64_json).
A fake is used rather than monkeypatching the real SDK because the whole
point under test is generate()'s defensive two-shape dance: a fake can choose
exactly when to act like an SDK build that has never heard of gpt-image-2's
new keywords (TypeError) versus a server that rejects the request outright
(openai.BadRequestError) versus a transient hiccup (openai.RateLimitError and
friends) -- see docs/cover_designer_spec.md §7.2 and §11's imaging bullet.
"""
from __future__ import annotations

import base64
import io

import httpx
import openai
import pytest
from PIL import Image

from docproof.cover import imaging
from docproof.cover.imaging import (IMAGE_COST, NEGATIVE_SUFFIX, ImagingError,
                                    _edit_request_params,
                                    _refine_request_params, _request_params,
                                    edit, generate, has_real_alpha,
                                    make_client, refine,
                                    reset_capability_cache)

# -- fixtures: fake client, canned PNGs, canned SDK exceptions --------------

class _ImageData:
    def __init__(self, b64_json):
        self.b64_json = b64_json


class _Response:
    def __init__(self, b64_json):
        self.data = [_ImageData(b64_json)]


class _EmptyResponse:
    data: list = []


class _FakeImages:
    """`behavior(params, call_number)` decides what one generate() call does
    -- return a response object, or raise. call_number is 1-based and counts
    calls made on THIS fake client, so a behavior can vary by attempt."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls: list[dict] = []

    def generate(self, **params):
        self.calls.append(params)
        return self.behavior(params, len(self.calls))

    def edit(self, **params):
        # Same call-counting/behavior-dispatch shape as generate() above --
        # edit()'s tests reuse the same _FakeClient/_FakeImages fixtures,
        # just calling client.images.edit(**params) instead.
        self.calls.append(params)
        return self.behavior(params, len(self.calls))


class _FakeClient:
    def __init__(self, behavior):
        self.images = _FakeImages(behavior)


def _png_bytes(mode: str = "RGB", size: tuple[int, int] = (40, 60),
              color=(200, 50, 50)) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _transparent_border_png(size: tuple[int, int] = (60, 90)) -> bytes:
    """An RGBA image with a fully transparent margin and an opaque center --
    the shape has_real_alpha is looking for from a real cutout."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    inner = Image.new("RGBA", (w - 10, h - 10), (10, 20, 30, 255))
    img.paste(inner, (5, 5))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _opaque_rgba_png(size: tuple[int, int] = (60, 90)) -> bytes:
    img = Image.new("RGBA", size, (10, 20, 30, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/images/generations")


def _bad_request(message: str = "Unknown parameter: 'resolution'."
                 ) -> openai.BadRequestError:
    resp = httpx.Response(400, request=_REQUEST, json={"error": {"message": message}})
    return openai.BadRequestError(message, response=resp,
                                  body={"error": {"message": message}})


def _rate_limited(message: str = "Rate limit reached.") -> openai.RateLimitError:
    resp = httpx.Response(429, request=_REQUEST, json={"error": {"message": message}})
    return openai.RateLimitError(message, response=resp,
                                 body={"error": {"message": message}})


def _server_error(message: str = "Internal server error."
                  ) -> openai.InternalServerError:
    resp = httpx.Response(500, request=_REQUEST, json={"error": {"message": message}})
    return openai.InternalServerError(message, response=resp,
                                      body={"error": {"message": message}})


# -- constants ------------------------------------------------------------

def test_negative_suffix_is_exact():
    assert NEGATIVE_SUFFIX == (
        "Absolutely no text, no letters, no words, no numbers, no "
        "watermarks, no borders, no frames.")


def test_image_cost_table_has_the_three_resolution_tiers_ascending():
    assert set(IMAGE_COST) == {"1K", "2K", "4K"}
    assert IMAGE_COST["1K"] < IMAGE_COST["2K"] < IMAGE_COST["4K"]
    assert all(isinstance(v, float) for v in IMAGE_COST.values())


def test_make_client_returns_an_openai_client_with_its_own_retries_disabled():
    client = make_client("sk-test-key")
    assert isinstance(client, openai.OpenAI)
    # generate() owns its own one-retry policy; the SDK's must be off, or a
    # transient failure would be retried more than once underneath it.
    assert client.max_retries == 0


# -- _request_params: the primary vs. fallback parameter shapes -------------

def test_request_params_primary_shape_is_resolution_and_aspect_ratio():
    params = _request_params("a lighthouse at dusk", transparent=False,
                             resolution="2K", fallback=False)
    assert params["model"] == "gpt-image-2"
    assert params["prompt"] == "a lighthouse at dusk"
    assert params["resolution"] == "2K"
    assert params["aspect_ratio"] == "2:3"
    assert "size" not in params and "quality" not in params
    assert "background" not in params


def test_request_params_fallback_shape_is_size_and_quality():
    params = _request_params("a lighthouse at dusk", transparent=True,
                             resolution="2K", fallback=True)
    assert params["size"] == "1024x1536"
    assert params["quality"] == "medium"   # the 2K tier's rung
    assert "resolution" not in params and "aspect_ratio" not in params
    assert params["background"] == "transparent"    # still plumbed through


def test_request_params_omits_background_when_not_transparent():
    for fallback in (False, True):
        params = _request_params("prompt", transparent=False,
                                 resolution="2K", fallback=fallback)
        assert "background" not in params


# -- generate(): the documented shape, used when it's accepted --------------

def test_generate_uses_the_documented_shape_when_it_is_accepted():
    payload = _png_bytes()

    def behavior(params, n):
        assert params["resolution"] == "2K"
        assert params["aspect_ratio"] == "2:3"
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = generate(client, "a lighthouse at dusk, oil painting")

    assert result == payload
    assert len(client.images.calls) == 1


def test_generate_plumbs_the_resolution_argument_into_the_documented_shape():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    generate(_FakeClient(behavior), "prompt", resolution="4K")
    assert seen["resolution"] == "4K"


def test_generate_plumbs_the_transparent_flag_into_background_param():
    payload = _png_bytes(mode="RGBA")

    def behavior(params, n):
        return _Response(_b64(payload))

    seen_true = _FakeClient(behavior)
    generate(seen_true, "prompt", transparent=True)
    assert seen_true.images.calls[0]["background"] == "transparent"

    seen_false = _FakeClient(behavior)
    generate(seen_false, "prompt", transparent=False)
    assert "background" not in seen_false.images.calls[0]


# -- generate(): parameter-shape fallback ------------------------------------

def test_generate_falls_back_to_size_quality_on_typeerror():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise TypeError(
                "generate() got an unexpected keyword argument 'resolution'")
        assert params["size"] == "1024x1536"
        assert params["quality"] == "medium"   # the 2K tier's rung
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = generate(client, "a lighthouse at dusk")

    assert result == payload
    assert len(client.images.calls) == 2   # primary attempt, then fallback


def test_generate_falls_back_to_size_quality_on_bad_request_error():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = generate(client, "a lighthouse at dusk")

    assert result == payload
    assert len(client.images.calls) == 2


def test_generate_raises_imagingerror_when_both_shapes_are_rejected():
    def behavior(params, n):
        raise _bad_request(f"Unknown parameter set (call {n}).")

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="rejected both"):
        generate(client, "prompt")
    assert len(client.images.calls) == 2   # both shapes were actually tried


# -- generate(): one retry on a transient failure, never more ---------------

def test_generate_retries_once_on_a_transient_failure_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _rate_limited()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = generate(client, "prompt")

    assert result == payload
    assert len(client.images.calls) == 2


def test_generate_retries_once_on_a_5xx_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _server_error()
        return _Response(_b64(payload))

    result = generate(_FakeClient(behavior), "prompt")
    assert result == payload


def test_generate_retries_once_on_a_connection_error_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise openai.APIConnectionError(request=_REQUEST)
        return _Response(_b64(payload))

    result = generate(_FakeClient(behavior), "prompt")
    assert result == payload


def test_generate_raises_after_one_retry_on_a_persistent_transient_failure():
    def behavior(params, n):
        raise _rate_limited()

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="after a retry"):
        generate(client, "prompt")
    # exactly one retry on the primary shape -- no shape fallback was burned
    # chasing a problem that was never about the parameter shape.
    assert len(client.images.calls) == 2


# -- generate(): a response with no usable image data ------------------------

def test_generate_raises_when_the_response_carries_no_image_data():
    def behavior(params, n):
        return _Response(None)

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="no image data"):
        generate(client, "prompt")
    assert len(client.images.calls) == 1   # not a shape problem -- no fallback


def test_generate_raises_when_the_response_has_an_empty_data_list():
    def behavior(params, n):
        return _EmptyResponse()

    with pytest.raises(ImagingError, match="no image data"):
        generate(_FakeClient(behavior), "prompt")


# -- generate(): decoded bytes really are the model's own PNG ----------------

def test_generate_returns_the_exact_decoded_bytes():
    payload = _transparent_border_png()

    def behavior(params, n):
        return _Response(_b64(payload))

    result = generate(_FakeClient(behavior), "prompt")
    assert result == payload
    with Image.open(io.BytesIO(result)) as img:
        assert img.mode == "RGBA"


# -- has_real_alpha() ---------------------------------------------------------

def test_has_real_alpha_true_for_a_border_transparent_cutout():
    assert has_real_alpha(_transparent_border_png()) is True


def test_has_real_alpha_false_for_a_fully_opaque_rgba_image():
    assert has_real_alpha(_opaque_rgba_png()) is False


def test_has_real_alpha_false_for_an_rgb_image_with_no_alpha_channel():
    assert has_real_alpha(_png_bytes(mode="RGB")) is False


# -- edit(): _edit_request_params -- the primary vs. fallback parameter shapes

def test_edit_request_params_primary_shape_is_model_prompt_and_resolution():
    params = _edit_request_params(b"img", b"mask", "remove the lamp",
                                  resolution="2K", fallback=False)
    assert params["model"] == "gpt-image-2"
    assert params["prompt"] == "remove the lamp"
    assert params["n"] == 1
    assert params["output_format"] == "png"
    assert params["resolution"] == "2K"
    assert "size" not in params and "quality" not in params
    assert "aspect_ratio" not in params


def test_edit_request_params_fallback_shape_is_size_and_quality():
    params = _edit_request_params(b"img", b"mask", "remove the lamp",
                                  resolution="2K", fallback=True)
    assert params["model"] == "gpt-image-2"    # unchanged from the primary shape
    assert params["size"] == "1024x1536"
    assert params["quality"] == "medium"   # the 2K tier's rung
    assert "resolution" not in params


def test_edit_request_params_wraps_image_and_mask_as_named_bytesio_streams():
    image_bytes = _png_bytes()
    mask_bytes = _png_bytes(color=(0, 0, 0))
    params = _edit_request_params(image_bytes, mask_bytes, "prompt",
                                  resolution="2K", fallback=False)
    assert isinstance(params["image"], io.BytesIO)
    assert isinstance(params["mask"], io.BytesIO)
    assert params["image"].name.endswith(".png")
    assert params["mask"].name.endswith(".png")
    assert params["image"].getvalue() == image_bytes
    assert params["mask"].getvalue() == mask_bytes


# -- edit(): the documented shape, used when it's accepted -------------------

def test_edit_uses_the_documented_shape_when_it_is_accepted():
    payload = _png_bytes()
    image_bytes = _png_bytes(color=(10, 10, 10))
    mask_bytes = _png_bytes(mode="RGBA", color=(0, 0, 0, 0))

    def behavior(params, n):
        assert params["model"] == "gpt-image-2"
        assert params["prompt"] == "remove the lamp; extend the wall behind it"
        assert params["resolution"] == "2K"
        assert isinstance(params["image"], io.BytesIO)
        assert isinstance(params["mask"], io.BytesIO)
        assert params["image"].name.endswith(".png")
        assert params["mask"].name.endswith(".png")
        assert params["image"].getvalue() == image_bytes
        assert params["mask"].getvalue() == mask_bytes
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = edit(client, image_bytes, mask_bytes,
                  "remove the lamp; extend the wall behind it")

    assert result == payload
    assert len(client.images.calls) == 1


def test_edit_plumbs_the_resolution_argument_into_the_documented_shape():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "prompt",
        resolution="4K")
    assert seen["resolution"] == "4K"


# -- edit(): parameter-shape fallback ------------------------------------

def test_edit_falls_back_to_size_quality_on_bad_request_error():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        assert params["size"] == "1024x1536"
        assert params["quality"] == "medium"   # the 2K tier's rung
        assert params["model"] == "gpt-image-2"
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = edit(client, _png_bytes(), _png_bytes(),
                  "remove the lamp; extend the wall behind it")

    assert result == payload
    assert len(client.images.calls) == 2   # primary attempt, then fallback


def test_edit_falls_back_to_size_quality_on_typeerror():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise TypeError(
                "edit() got an unexpected keyword argument 'resolution'")
        assert params["size"] == "1024x1536"
        assert params["quality"] == "medium"   # the 2K tier's rung
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = edit(client, _png_bytes(), _png_bytes(), "remove the lamp")

    assert result == payload
    assert len(client.images.calls) == 2


def test_edit_raises_imagingerror_when_both_shapes_are_rejected():
    def behavior(params, n):
        raise _bad_request(f"Unknown parameter set (call {n}).")

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="rejected both"):
        edit(client, _png_bytes(), _png_bytes(), "prompt")
    assert len(client.images.calls) == 2   # both shapes were actually tried


# -- edit(): one retry on a transient failure, never more ---------------

def test_edit_retries_once_on_a_transient_failure_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _rate_limited()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = edit(client, _png_bytes(), _png_bytes(), "prompt")

    assert result == payload
    assert len(client.images.calls) == 2


def test_edit_retries_once_on_a_5xx_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _server_error()
        return _Response(_b64(payload))

    result = edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "prompt")
    assert result == payload


def test_edit_retries_once_on_a_connection_error_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise openai.APIConnectionError(request=_REQUEST)
        return _Response(_b64(payload))

    result = edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "prompt")
    assert result == payload


def test_edit_raises_after_one_retry_on_a_persistent_transient_failure():
    def behavior(params, n):
        raise _rate_limited()

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="after a retry"):
        edit(client, _png_bytes(), _png_bytes(), "prompt")
    # exactly one retry on the primary shape -- no shape fallback was burned
    # chasing a problem that was never about the parameter shape.
    assert len(client.images.calls) == 2


def test_edit_rewinds_image_and_mask_streams_before_retrying():
    payload = _png_bytes()
    positions = []

    def behavior(params, n):
        positions.append((params["image"].tell(), params["mask"].tell()))
        if n == 1:
            # Simulate the SDK's own multipart upload having read (and thus
            # advanced) the streams before the transient failure surfaced.
            params["image"].read()
            params["mask"].read()
            raise _rate_limited()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = edit(client, _png_bytes(), _png_bytes(), "prompt")

    assert result == payload
    # Both attempts saw their streams positioned at 0 -- the retry rewound them.
    assert positions == [(0, 0), (0, 0)]


# -- edit(): a response with no usable image data ------------------------

def test_edit_raises_when_the_response_carries_no_image_data():
    def behavior(params, n):
        return _Response(None)

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="no image data"):
        edit(client, _png_bytes(), _png_bytes(), "prompt")
    assert len(client.images.calls) == 1   # not a shape problem -- no fallback


def test_edit_raises_when_the_response_has_an_empty_data_list():
    def behavior(params, n):
        return _EmptyResponse()

    with pytest.raises(ImagingError, match="no image data"):
        edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "prompt")


# -- edit(): decoded bytes really are the model's own PNG ----------------

def test_edit_returns_the_exact_decoded_bytes():
    payload = _transparent_border_png()

    def behavior(params, n):
        return _Response(_b64(payload))

    result = edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "prompt")
    assert result == payload
    with Image.open(io.BytesIO(result)) as img:
        assert img.mode == "RGBA"


# -- refine(): _refine_request_params -- the primary vs. fallback shapes ------
#
# refine() is edit() with the mask left off, and "left off" is the entire
# feature: images.edit WITH a mask regenerates the masked region, images.edit
# with NO mask re-renders the whole frame using the supplied image as its
# composition anchor. That is what the quality ladder needs (a kept 1K draft
# re-rendered at 4K without gpt-image-2 -- which has no seed -- inventing a
# different picture), so every test below that could be satisfied by a
# fully-transparent mask asserts the absence of the key instead.

def test_refine_request_params_primary_shape_is_model_prompt_and_resolution():
    params = _refine_request_params(b"img", "re-render faithfully",
                                    resolution="4K", fallback=False)
    assert params["model"] == "gpt-image-2"
    assert params["prompt"] == "re-render faithfully"
    assert params["n"] == 1
    assert params["output_format"] == "png"
    assert params["resolution"] == "4K"
    assert "size" not in params and "quality" not in params
    assert "aspect_ratio" not in params


def test_refine_request_params_never_carry_a_mask():
    """The whole point, asserted on both shapes: a mask key at all would
    turn a full re-render into a region edit."""
    for fallback in (False, True):
        params = _refine_request_params(b"img", "prompt", resolution="4K",
                                        fallback=fallback)
        assert "mask" not in params


def test_refine_request_params_fallback_shape_is_size_and_quality():
    params = _refine_request_params(b"img", "prompt", resolution="4K",
                                    fallback=True)
    assert params["model"] == "gpt-image-2"    # unchanged from the primary shape
    assert params["size"] == "1024x1536"
    assert params["quality"] == "high"
    assert "resolution" not in params


def test_refine_request_params_wraps_the_image_as_a_named_bytesio_stream():
    image_bytes = _png_bytes()
    params = _refine_request_params(image_bytes, "prompt", resolution="4K",
                                    fallback=False)
    assert isinstance(params["image"], io.BytesIO)
    assert params["image"].name.endswith(".png")
    assert params["image"].getvalue() == image_bytes


# -- refine(): the documented shape, used when it's accepted -----------------

def test_refine_uses_the_documented_shape_when_it_is_accepted():
    payload = _png_bytes()
    image_bytes = _png_bytes(color=(10, 10, 10))

    def behavior(params, n):
        assert params["model"] == "gpt-image-2"
        assert params["prompt"] == "re-render this exactly, at full detail"
        assert params["resolution"] == "4K"
        assert "mask" not in params
        assert isinstance(params["image"], io.BytesIO)
        assert params["image"].name.endswith(".png")
        assert params["image"].getvalue() == image_bytes
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = refine(client, image_bytes, "re-render this exactly, at full detail")

    assert result == payload
    assert len(client.images.calls) == 1


def test_refine_defaults_to_the_4k_tier():
    """The default is the TOP tier, not edit()'s 2K: this call only ever
    happens on a plate somebody decided to keep."""
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    refine(_FakeClient(behavior), _png_bytes(), "prompt")
    assert seen["resolution"] == "4K"


def test_refine_plumbs_the_resolution_argument_into_the_documented_shape():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    refine(_FakeClient(behavior), _png_bytes(), "prompt", resolution="2K")
    assert seen["resolution"] == "2K"


def test_refine_goes_through_images_edit_not_images_generate():
    """Stated as its own test because it is the one thing a reader would
    guess wrong: a re-render is an EDIT of the draft, never a fresh
    generate() -- generate() has no way to be handed the draft at all."""
    payload = _png_bytes()
    used: list[str] = []

    class _Watched(_FakeImages):
        def generate(self, **params):
            used.append("generate")
            return super().generate(**params)

        def edit(self, **params):
            used.append("edit")
            return super().edit(**params)

    client = _FakeClient(lambda params, n: _Response(_b64(payload)))
    client.images = _Watched(lambda params, n: _Response(_b64(payload)))
    refine(client, _png_bytes(), "prompt")
    assert used == ["edit"]


# -- refine(): parameter-shape fallback --------------------------------------

def test_refine_falls_back_to_size_quality_on_bad_request_error():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        assert params["size"] == "1024x1536"
        assert params["quality"] == "high"
        assert params["model"] == "gpt-image-2"
        assert "mask" not in params
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = refine(client, _png_bytes(), "re-render this exactly")

    assert result == payload
    assert len(client.images.calls) == 2   # primary attempt, then fallback


def test_refine_falls_back_to_size_quality_on_typeerror():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise TypeError(
                "edit() got an unexpected keyword argument 'resolution'")
        assert params["size"] == "1024x1536"
        assert params["quality"] == "high"
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = refine(client, _png_bytes(), "re-render this exactly")

    assert result == payload
    assert len(client.images.calls) == 2


def test_refine_raises_imagingerror_when_both_shapes_are_rejected():
    def behavior(params, n):
        raise _bad_request(f"Unknown parameter set (call {n}).")

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="rejected both"):
        refine(client, _png_bytes(), "prompt")
    assert len(client.images.calls) == 2   # both shapes were actually tried


# -- refine(): one retry on a transient failure, never more -------------------

def test_refine_retries_once_on_a_transient_failure_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _rate_limited()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = refine(client, _png_bytes(), "prompt")

    assert result == payload
    assert len(client.images.calls) == 2


def test_refine_retries_once_on_a_5xx_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise _server_error()
        return _Response(_b64(payload))

    assert refine(_FakeClient(behavior), _png_bytes(), "prompt") == payload


def test_refine_retries_once_on_a_connection_error_then_succeeds():
    payload = _png_bytes()

    def behavior(params, n):
        if n == 1:
            raise openai.APIConnectionError(request=_REQUEST)
        return _Response(_b64(payload))

    assert refine(_FakeClient(behavior), _png_bytes(), "prompt") == payload


def test_refine_raises_after_one_retry_on_a_persistent_transient_failure():
    def behavior(params, n):
        raise _rate_limited()

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="after a retry"):
        refine(client, _png_bytes(), "prompt")
    # exactly one retry on the primary shape -- no shape fallback was burned
    # chasing a problem that was never about the parameter shape.
    assert len(client.images.calls) == 2


def test_refine_rewinds_the_image_stream_before_retrying():
    """The retry rewind loop reads its streams through .get(), so a params
    dict with no `mask` at all has simply nothing to rewind there -- this is
    the test that a missing mask cannot make the rewind path blow up."""
    payload = _png_bytes()
    positions = []

    def behavior(params, n):
        positions.append(params["image"].tell())
        assert "mask" not in params
        if n == 1:
            params["image"].read()
            raise _rate_limited()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    result = refine(client, _png_bytes(), "prompt")

    assert result == payload
    assert positions == [0, 0]


# -- refine(): a response with no usable image data --------------------------

def test_refine_raises_when_the_response_carries_no_image_data():
    def behavior(params, n):
        return _Response(None)

    client = _FakeClient(behavior)
    with pytest.raises(ImagingError, match="no image data"):
        refine(client, _png_bytes(), "prompt")
    assert len(client.images.calls) == 1   # not a shape problem -- no fallback


def test_refine_raises_when_the_response_has_an_empty_data_list():
    def behavior(params, n):
        return _EmptyResponse()

    with pytest.raises(ImagingError, match="no image data"):
        refine(_FakeClient(behavior), _png_bytes(), "prompt")


# -- refine(): decoded bytes really are the model's own PNG ------------------

def test_refine_returns_the_exact_decoded_bytes():
    payload = _transparent_border_png()

    def behavior(params, n):
        return _Response(_b64(payload))

    result = refine(_FakeClient(behavior), _png_bytes(), "prompt")
    assert result == payload
    with Image.open(io.BytesIO(result)) as img:
        assert img.mode == "RGBA"


# -- the tier ladder survives the fallback shape -----------------------------
#
# The fallback used to be a flat quality="high" whatever tier was asked for,
# which made the whole draft ladder a no-op on any deployment where the
# fallback is the shape that runs: a 1K draft rolled at the dearest, slowest
# rung and was still billed three cents (docs/cover_canvas_spec.md §5, §8).

def test_fallback_shape_maps_each_tier_to_its_own_quality_rung():
    rungs = {tier: _request_params("prompt", transparent=False,
                                   resolution=tier, fallback=True)["quality"]
             for tier in IMAGE_COST}
    assert rungs == {"1K": "low", "2K": "medium", "4K": "high"}


def test_every_builder_ladders_the_tier_the_same_way():
    for tier in IMAGE_COST:
        gen = _request_params("p", transparent=False, resolution=tier,
                              fallback=True)
        ed = _edit_request_params(b"i", b"m", "p", resolution=tier,
                                  fallback=True)
        ref = _refine_request_params(b"i", "p", resolution=tier, fallback=True)
        assert gen["quality"] == ed["quality"] == ref["quality"]


# -- the parameter shape is remembered, so it is probed once -----------------

def test_generate_remembers_the_fallback_shape_and_stops_re_probing():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    generate(client, "prompt")
    assert len(client.images.calls) == 2      # probed the documented shape once

    generate(client, "another prompt")
    # ...and never again: the second roll goes straight to what works.
    assert len(client.images.calls) == 3
    assert "size" in client.images.calls[-1]


def test_a_remembered_shape_that_stops_working_still_falls_back():
    """The memo can only cost one request in a transition, never dead-end a
    job — so a vendor that changes its mind mid-process is still served."""
    payload = _png_bytes()
    state = {"reject_fallback": False}

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        if state["reject_fallback"]:
            raise _bad_request("Unknown parameter: 'size'.")
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    generate(client, "prompt")                       # learns: fallback shape
    state["reject_fallback"] = True

    def later(params, n):
        if "size" in params:
            raise _bad_request("Unknown parameter: 'size'.")
        return _Response(_b64(payload))

    client.images.behavior = later
    assert generate(client, "prompt") == payload


def test_reset_capability_cache_makes_the_next_call_probe_again():
    payload = _png_bytes()

    def behavior(params, n):
        if "resolution" in params:
            raise _bad_request()
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    generate(client, "prompt")
    reset_capability_cache()
    generate(client, "prompt")
    assert len(client.images.calls) == 4          # two probes, two renders


def test_edit_and_generate_remember_separately():
    """One verb's answer is not evidence about another's — images.edit's
    parameter names are a blinder guess than images.generate's."""
    payload = _png_bytes()

    def behavior(params, n):
        if "image" in params and "resolution" in params:
            raise _bad_request()                  # edit rejects the new shape
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    edit(client, _png_bytes(), _png_bytes(), "fix it")
    generate(client, "prompt")
    assert "resolution" in client.images.calls[-1]   # generate still probes new


# -- streaming partial frames ------------------------------------------------

class _Event:
    def __init__(self, type_, b64_json):
        self.type = type_
        self.b64_json = b64_json


def test_generate_streams_partials_to_the_callback_and_returns_the_final():
    partial, final = _png_bytes(color=(1, 2, 3)), _png_bytes(color=(9, 9, 9))
    seen = []

    def behavior(params, n):
        assert params["stream"] is True
        assert params["partial_images"] == imaging.PARTIAL_IMAGES
        return iter([
            _Event("image_generation.partial_image", _b64(partial)),
            _Event("image_generation.partial_image", _b64(partial)),
            _Event("image_generation.completed", _b64(final)),
        ])

    result = generate(_FakeClient(behavior), "prompt",
                      on_partial=lambda data, i: seen.append((i, data)))
    assert result == final
    assert [i for i, _ in seen] == [1, 2]
    assert all(data == partial for _, data in seen)


def test_no_callback_means_no_streaming_keywords_at_all():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    generate(_FakeClient(behavior), "prompt")
    assert "stream" not in seen and "partial_images" not in seen


def test_a_vendor_that_refuses_to_stream_still_returns_the_image_once():
    """And is not asked again, and is not mistaken for a shape problem."""
    payload = _png_bytes()

    def behavior(params, n):
        if params.get("stream"):
            raise TypeError("generate() got an unexpected keyword 'stream'")
        return _Response(_b64(payload))

    client = _FakeClient(behavior)
    assert generate(client, "prompt", on_partial=lambda d, i: None) == payload
    assert len(client.images.calls) == 2          # streamed probe, then plain
    assert generate(client, "prompt", on_partial=lambda d, i: None) == payload
    assert len(client.images.calls) == 3          # no second probe
    assert "resolution" in client.images.calls[-1]   # shape was never blamed


def test_a_partial_callback_that_raises_cannot_lose_a_paid_for_render():
    final = _png_bytes(color=(9, 9, 9))

    def behavior(params, n):
        return iter([
            _Event("image_generation.partial_image", _b64(_png_bytes())),
            _Event("image_generation.completed", _b64(final)),
        ])

    def boom(data, index):
        raise RuntimeError("the browser went away")

    assert generate(_FakeClient(behavior), "prompt", on_partial=boom) == final


def test_a_stream_with_no_finished_frame_is_an_imagingerror():
    def behavior(params, n):
        return iter([_Event("image_generation.partial_image",
                            _b64(_png_bytes()))])

    with pytest.raises(ImagingError, match="no finished image"):
        generate(_FakeClient(behavior), "prompt",
                 on_partial=lambda d, i: None)


def test_edit_streams_partials_too():
    final = _png_bytes(color=(4, 4, 4))
    seen = []

    def behavior(params, n):
        assert params["stream"] is True
        return iter([
            _Event("image_edit.partial_image", _b64(_png_bytes())),
            _Event("image_edit.completed", _b64(final)),
        ])

    result = edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "fix it",
                  on_partial=lambda d, i: seen.append(i))
    assert result == final and seen == [1]


# -- the output format, and the cutout flag ----------------------------------

def test_output_format_is_png_by_default_and_plumbed_when_asked():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    generate(_FakeClient(behavior), "prompt")
    assert seen["output_format"] == "png"
    generate(_FakeClient(behavior), "prompt", output_format="webp")
    assert seen["output_format"] == "webp"


def test_editing_a_cutout_asks_for_a_transparent_background():
    """Without this the model reads a cutout's empty background as canvas to
    fill, and a repair comes back as a full frame that buries what is under
    it — the failure the ground-the-figure button hit."""
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "ground her",
         transparent=True)
    assert seen["background"] == "transparent"

    seen.clear()
    edit(_FakeClient(behavior), _png_bytes(), _png_bytes(), "ground her")
    assert "background" not in seen


def test_refining_a_cutout_asks_for_a_transparent_background_too():
    payload = _png_bytes()
    seen = {}

    def behavior(params, n):
        seen.update(params)
        return _Response(_b64(payload))

    refine(_FakeClient(behavior), _png_bytes(), "re-render", transparent=True)
    assert seen["background"] == "transparent"
