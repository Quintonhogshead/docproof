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

from docproof.cover.imaging import (IMAGE_COST, NEGATIVE_SUFFIX, ImagingError,
                                    _request_params, generate, has_real_alpha,
                                    make_client)

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
    assert params["quality"] == "high"
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
        assert params["quality"] == "high"
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
