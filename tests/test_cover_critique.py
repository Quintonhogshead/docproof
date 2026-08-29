"""docproof/cover/critique.py: the vision critique call (§6.3).

No network, no real openai.OpenAI call anywhere here — every test drives a
fake client shaped like the bits of the SDK run_critique actually touches:
.responses.create(**kwargs) (the primary shape) and
.chat.completions.create(**kwargs) (the fallback). A `behavior(kwargs,
call_number)` callback decides what one call does -- return a canned result,
or raise -- the same idiom tests/test_cover_imaging.py's _FakeImages uses,
so a behavior can vary by attempt (retry-then-succeed) or by which endpoint
was hit (shape-rejected-then-fallback). Canned openai.* exceptions are built
the same way test_cover_imaging.py's are, since both modules share the exact
_TRANSIENT_ERRORS table.
"""
from __future__ import annotations

import base64
import io
import json

import httpx
import openai
import pytest
from PIL import Image

from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.critique import (CRITIQUE_MODEL, MAX_WIDTH, CritiqueError,
                                     CritiqueResult, run_critique)
from docproof.cover.direction import Direction
from docproof.cover.model import Brief, Palette, build_spec

# -- fixtures: brief/spec, canned PNGs, canned SDK exceptions ------------------

def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary")
    data.update(overrides)
    return Brief(**data)


def _spec():
    palette = Palette(background="#101010", primary="#f5f1e8", accent="#c9a227",
                      text="#f5f1e8", scrim="#000000")
    direction = Direction(
        concept_name="Ash and Brass", rationale="A test concept.",
        archetype="full_bleed_art", palette=palette,
        title_font="Playfair Display", author_font="Spectral",
        art_prompts={"background": "A lonely lighthouse, oil painting."},
        texture=False)
    return build_spec(direction, _brief(), ARCHETYPES["full_bleed_art"])


def _png_bytes(size: tuple[int, int] = (1600, 2560), color=(20, 20, 20)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _bad_request(message: str = "Unknown parameter: 'input_image'."
                 ) -> openai.BadRequestError:
    resp = httpx.Response(400, request=_REQUEST, json={"error": {"message": message}})
    return openai.BadRequestError(message, response=resp,
                                  body={"error": {"message": message}})


def _rate_limited(message: str = "Rate limit reached.") -> openai.RateLimitError:
    resp = httpx.Response(429, request=_REQUEST, json={"error": {"message": message}})
    return openai.RateLimitError(message, response=resp,
                                 body={"error": {"message": message}})


# -- fake client: .responses.create() / .chat.completions.create() ------------

class _FakeEndpoint:
    """`behavior(kwargs, call_number)` decides what one .create() call does
    -- return a canned result object, or raise. call_number is 1-based and
    counts calls on THIS endpoint, so a behavior can vary by attempt (the
    same idiom tests/test_cover_imaging.py's _FakeImages uses)."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behavior(kwargs, len(self.calls))


class _FakeChat:
    def __init__(self, completions: _FakeEndpoint):
        self.completions = completions


def _unexpected(kwargs, call_number):
    raise AssertionError("this endpoint should not have been called in this test")


def _once(result):
    return lambda kwargs, call_number: result


def _raises(exc: Exception):
    def behavior(kwargs, call_number):
        raise exc
    return behavior


class FakeCritiqueClient:
    """A minimal openai.OpenAI stand-in: independently scriptable
    .responses.create() and .chat.completions.create()."""

    def __init__(self, *, responses=_unexpected, chat=_unexpected):
        self.responses = _FakeEndpoint(responses)
        self.chat = _FakeChat(_FakeEndpoint(chat))


# -- canned response bodies -----------------------------------------------------

class _FakeResponsesResult:
    """Stands in for the openai Response object -- model_dump() is the only
    thing run_critique's Responses-API reader ever calls on it."""

    def __init__(self, body: dict):
        self._body = body

    def model_dump(self) -> dict:
        return self._body


def _responses_body(*, passes=True, tells=(), notes="", refusal=None,
                    input_tokens=800, output_tokens=60) -> dict:
    if refusal is not None:
        content = [{"type": "refusal", "refusal": refusal}]
    else:
        text = json.dumps({"passes": passes, "tells": list(tells), "notes": notes})
        content = [{"type": "output_text", "text": text}]
    return {"output": [{"type": "message", "content": content}],
           "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                    "input_tokens_details": {"cached_tokens": 0}}}


class _Message:
    def __init__(self, content=None, refusal=None):
        self.content = content
        self.refusal = refusal


class _Usage:
    def __init__(self, d: dict):
        self._d = d

    def model_dump(self) -> dict:
        return self._d


class _FakeChatCompletionResult:
    def __init__(self, *, passes=True, tells=(), notes="", refusal=None,
                prompt_tokens=800, completion_tokens=60):
        if refusal is not None:
            message = _Message(refusal=refusal)
        else:
            text = json.dumps({"passes": passes, "tells": list(tells), "notes": notes})
            message = _Message(content=text)
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = _Usage({"prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "prompt_tokens_details": {"cached_tokens": 0}})


# -- passes path ----------------------------------------------------------------

def test_run_critique_passes_path():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(passes=True, tells=[], notes=""))))
    result = run_critique(_png_bytes(), _spec(), _brief(), client)

    assert isinstance(result, CritiqueResult)
    assert result.passes is True
    assert result.tells == []
    assert result.notes == ""
    # feeds the pipeline's {kind: "critique", usd: ...} ledger row
    assert result.cost is not None and result.cost > 0

    call = client.responses.calls[0]
    assert call["model"] == CRITIQUE_MODEL


def test_run_critique_accepts_a_model_override():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(passes=True))))
    run_critique(_png_bytes(), _spec(), _brief(), client, model="gpt-5.6-terra")
    assert client.responses.calls[0]["model"] == "gpt-5.6-terra"


# -- fails path: usable tells + a revision note for the pipeline --------------

def test_run_critique_fails_path_returns_tells_and_a_revision_note():
    client = FakeCritiqueClient(responses=_once(_FakeResponsesResult(_responses_body(
        passes=False,
        tells=["type crowding against the title", "palette reads horror, not literary"],
        notes="Increase the title scrim strength and lighten the accent hex."))))
    result = run_critique(_png_bytes(), _spec(), _brief(), client)

    assert result.passes is False
    assert result.tells == ["type crowding against the title",
                           "palette reads horror, not literary"]
    assert "scrim" in result.notes
    assert result.cost is not None


# -- request content: summary + downscaled image -------------------------------

def test_run_critique_downscales_the_render_to_max_width():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(passes=True))))
    run_critique(_png_bytes(size=(1600, 2560)), _spec(), _brief(), client)

    call = client.responses.calls[0]
    image_part = call["input"][0]["content"][1]
    assert image_part["type"] == "input_image"
    data_uri = image_part["image_url"]
    assert data_uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as sent:
        assert sent.width == MAX_WIDTH
        assert sent.width < 1600                    # actually shrunk, not just re-encoded
        assert round(sent.height / sent.width, 2) == round(2560 / 1600, 2)  # aspect kept


def test_run_critique_leaves_an_already_narrow_render_alone():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(passes=True))))
    run_critique(_png_bytes(size=(300, 480)), _spec(), _brief(), client)
    call = client.responses.calls[0]
    data_uri = call["input"][0]["content"][1]["image_url"]
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as sent:
        assert sent.width == 300


def test_run_critique_summary_mentions_title_author_and_genre_not_the_full_brief():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(passes=True))))
    run_critique(_png_bytes(), _spec(),
                _brief(title="Gull Point", author="J. R. Vance", genre="literary"),
                client)
    summary = client.responses.calls[0]["input"][0]["content"][0]["text"]
    assert "Gull Point" in summary
    assert "J. R. Vance" in summary
    assert "literary" in summary
    # a one-paragraph summary, not run_directions' fully labeled brief dump
    assert "Title:" not in summary and "Author:" not in summary


# -- critique-call failure -> CritiqueError (never a fabricated verdict) ------

def test_run_critique_raises_on_a_refusal():
    client = FakeCritiqueClient(responses=_once(
        _FakeResponsesResult(_responses_body(refusal="content policy"))))
    with pytest.raises(CritiqueError, match="declined"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_empty_output_text():
    client = FakeCritiqueClient(
        responses=_once(_FakeResponsesResult({"output": [], "usage": {}})))
    with pytest.raises(CritiqueError, match="no usable text"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_schema_mismatch():
    body = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"nope": 1})}]}], "usage": {}}
    client = FakeCritiqueClient(responses=_once(_FakeResponsesResult(body)))
    with pytest.raises(CritiqueError, match="schema"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_unparseable_json():
    body = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "not json at all"}]}], "usage": {}}
    client = FakeCritiqueClient(responses=_once(_FakeResponsesResult(body)))
    with pytest.raises(CritiqueError):
        run_critique(_png_bytes(), _spec(), _brief(), client)


# -- retry: one transient retry, same policy as imaging.py --------------------

def test_run_critique_retries_once_on_a_transient_failure_then_succeeds():
    def behavior(kwargs, call_number):
        if call_number == 1:
            raise _rate_limited()
        return _FakeResponsesResult(_responses_body(passes=True))
    client = FakeCritiqueClient(responses=behavior)

    result = run_critique(_png_bytes(), _spec(), _brief(), client)
    assert result.passes is True
    assert len(client.responses.calls) == 2


def test_run_critique_raises_when_the_retry_also_fails():
    client = FakeCritiqueClient(responses=_raises(_rate_limited()))
    with pytest.raises(CritiqueError, match="after a retry"):
        run_critique(_png_bytes(), _spec(), _brief(), client)
    assert len(client.responses.calls) == 2


# -- shape fallback: Responses API rejected -> chat.completions ---------------

def test_run_critique_falls_back_to_chat_completions_when_the_shape_is_rejected():
    client = FakeCritiqueClient(
        responses=_raises(_bad_request()),
        chat=_once(_FakeChatCompletionResult(
            passes=False, tells=["weak hierarchy"], notes="Enlarge the title.")))

    result = run_critique(_png_bytes(), _spec(), _brief(), client)

    assert result.passes is False
    assert result.tells == ["weak hierarchy"]
    assert result.cost is not None
    assert len(client.responses.calls) == 1
    assert len(client.chat.completions.calls) == 1
    chat_call = client.chat.completions.calls[0]
    assert chat_call["response_format"]["type"] == "json_schema"
    image_part = chat_call["messages"][1]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_run_critique_raises_when_both_shapes_are_rejected():
    client = FakeCritiqueClient(responses=_raises(_bad_request()),
                                chat=_raises(_bad_request()))
    with pytest.raises(CritiqueError, match="both"):
        run_critique(_png_bytes(), _spec(), _brief(), client)
    assert len(client.responses.calls) == 1
    assert len(client.chat.completions.calls) == 1


def test_run_critique_does_not_fall_back_on_a_refusal():
    # A refusal is a real answer from the primary shape, not evidence the
    # shape itself is unsupported -- it must not trigger the fallback.
    client = FakeCritiqueClient(
        responses=_once(_FakeResponsesResult(_responses_body(refusal="policy"))),
        chat=_unexpected)
    with pytest.raises(CritiqueError, match="declined"):
        run_critique(_png_bytes(), _spec(), _brief(), client)
    assert len(client.chat.completions.calls) == 0
