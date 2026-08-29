"""docproof/cover/critique.py: the vision critique call (§6.3), rebuilt on
the `anthropic` SDK (BRAIN wave, 2026-08-29 — it used to call the `openai`
SDK; see the module's own docstring for the whole story).

No network, no real anthropic.Anthropic call anywhere here — every test
drives a fake client shaped like the bits of the SDK run_critique actually
touches: `.messages.stream(**kwargs)`, which returns a context manager whose
`get_final_message()` gives back a Message-shaped object (`.content`,
`.stop_reason`, `.usage`). A `behavior(kwargs, call_number)` callback decides
what one call does — return a canned Message, or raise — the same idiom
tests/test_cover_imaging.py's _FakeImages uses, so a behavior can vary by
attempt (retry-then-succeed). Canned anthropic.* exceptions are built the
same way tests/test_cover_imaging.py's openai.* ones are.
"""
from __future__ import annotations

import base64
import io
import json

import anthropic
import httpx
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


_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _rate_limited(message: str = "Rate limit reached.") -> anthropic.RateLimitError:
    resp = httpx.Response(429, request=_REQUEST, json={"error": {"message": message}})
    return anthropic.RateLimitError(message, response=resp,
                                    body={"error": {"message": message}})


# -- fake client: .messages.stream() ------------------------------------------

class _FakeUsage:
    def __init__(self, input_tokens: int = 800, output_tokens: int = 60):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    """Stands in for anthropic.types.Message — .content/.stop_reason/.usage
    are the only attributes run_critique's reader (_read_message) touches."""

    def __init__(self, *, content=None, stop_reason: str = "end_turn",
                usage: _FakeUsage | None = None):
        self.content = content or []
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()


def _reply(*, passes: bool = True, tells=(), notes: str = "",
          input_tokens: int = 800, output_tokens: int = 60) -> _FakeMessage:
    text = json.dumps({"passes": passes, "tells": list(tells), "notes": notes})
    return _FakeMessage(content=[_FakeTextBlock(text)],
                        usage=_FakeUsage(input_tokens, output_tokens))


class _FakeStreamManager:
    """Stands in for anthropic's MessageStreamManager: a context manager
    whose __enter__ is where a real network/API error would actually fire
    (the SDK builds the manager lazily; the request only goes out once
    entered) — an exception scripted as the "message" raises there, exactly
    matching what `with client.messages.stream(**p) as stream:` sees from
    the real SDK on a failed call."""

    def __init__(self, message_or_exc):
        self._m = message_or_exc

    def __enter__(self):
        if isinstance(self._m, BaseException):
            raise self._m
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._m


class FakeCritiqueClient:
    """A minimal anthropic.Anthropic stand-in: independently scriptable
    .messages.stream(**kwargs). `behavior(kwargs, call_number)` decides what
    one call does — return a canned Message, or return an exception INSTANCE
    to be raised on __enter__. call_number is 1-based and counts calls on
    this client, so a behavior can vary by attempt (retry-then-succeed)."""

    def __init__(self, behavior):
        self._behavior = behavior
        self.calls: list[dict] = []

        class _Messages:
            def stream(inner_self, **kwargs):
                self.calls.append(kwargs)
                return _FakeStreamManager(self._behavior(kwargs, len(self.calls)))

        self.messages = _Messages()


def _once(message_or_exc):
    return lambda kwargs, call_number: message_or_exc


def _raises(exc: BaseException):
    return lambda kwargs, call_number: exc


def _first_call_raises_then(exc: BaseException, message: _FakeMessage):
    def behavior(kwargs, call_number):
        return exc if call_number == 1 else message
    return behavior


# -- passes path ----------------------------------------------------------------

def test_run_critique_passes_path():
    client = FakeCritiqueClient(_once(_reply(passes=True, tells=[], notes="")))
    result = run_critique(_png_bytes(), _spec(), _brief(), client)

    assert isinstance(result, CritiqueResult)
    assert result.passes is True
    assert result.tells == []
    assert result.notes == ""
    # feeds the pipeline's {kind: "critique", usd: ...} ledger row
    assert result.cost is not None and result.cost > 0

    call = client.calls[0]
    assert call["model"] == CRITIQUE_MODEL


def test_run_critique_accepts_a_model_override():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client, model="claude-haiku-4-5")
    assert client.calls[0]["model"] == "claude-haiku-4-5"


# -- fails path: usable tells + a revision note for the pipeline --------------

def test_run_critique_fails_path_returns_tells_and_a_revision_note():
    client = FakeCritiqueClient(_once(_reply(
        passes=False,
        tells=["type crowding against the title", "palette reads horror, not literary"],
        notes="Increase the title scrim strength and lighten the accent hex.")))
    result = run_critique(_png_bytes(), _spec(), _brief(), client)

    assert result.passes is False
    assert result.tells == ["type crowding against the title",
                           "palette reads horror, not literary"]
    assert "scrim" in result.notes
    assert result.cost is not None


# -- request shape: output_config, model, effort -------------------------------

def test_run_critique_request_uses_structured_output_config():
    # Mirrors docproof/providers/anthropic_provider.py's own _params exactly
    # (see critique.py's module docstring) -- json_schema format, no OpenAI-
    # style schema_name/strict keys, effort gated by catalog.supports_effort.
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client)
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "schema" in call["output_config"]["format"]
    assert call["output_config"]["effort"] == "low"
    assert "max_tokens" in call
    assert call["system"]


def test_run_critique_sends_one_user_message_with_image_before_text():
    # Anthropic's own vision guidance: a single image goes before the text
    # that refers to it.
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client)
    messages = client.calls[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "text"


# -- request content: summary + downscaled image -------------------------------

def test_run_critique_downscales_the_render_to_max_width():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(size=(1600, 2560)), _spec(), _brief(), client)

    image_part = client.calls[0]["messages"][0]["content"][0]
    assert image_part["type"] == "image"
    source = image_part["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/png"
    raw = base64.b64decode(source["data"])
    with Image.open(io.BytesIO(raw)) as sent:
        assert sent.width == MAX_WIDTH
        assert sent.width < 1600                    # actually shrunk, not just re-encoded
        assert round(sent.height / sent.width, 2) == round(2560 / 1600, 2)  # aspect kept


def test_run_critique_leaves_an_already_narrow_render_alone():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(size=(300, 480)), _spec(), _brief(), client)
    source = client.calls[0]["messages"][0]["content"][0]["source"]
    raw = base64.b64decode(source["data"])
    with Image.open(io.BytesIO(raw)) as sent:
        assert sent.width == 300


def test_run_critique_summary_mentions_title_author_and_genre_not_the_full_brief():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(),
                _brief(title="Gull Point", author="J. R. Vance", genre="literary"),
                client)
    summary = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "Gull Point" in summary
    assert "J. R. Vance" in summary
    assert "literary" in summary
    # a one-paragraph summary, not run_directions' fully labeled brief dump
    assert "Title:" not in summary and "Author:" not in summary


# -- composer warnings, passed through and clearly labeled ---------------------

def test_run_critique_summary_includes_labeled_composer_warnings_when_passed():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client,
                warnings=["title at size_min and still 2 lines over",
                         "focal art came back without real transparency"])
    summary = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "composer" in summary.lower()
    assert "title at size_min and still 2 lines over" in summary
    assert "focal art came back without real transparency" in summary


def test_run_critique_summary_omits_composer_warnings_section_when_none_passed():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client)
    summary = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "composer" not in summary.lower()


# -- critique-call failure -> CritiqueError (never a fabricated verdict) ------

def test_run_critique_raises_on_a_refusal():
    client = FakeCritiqueClient(_once(_FakeMessage(stop_reason="refusal")))
    with pytest.raises(CritiqueError, match="declined"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_truncation():
    client = FakeCritiqueClient(_once(_FakeMessage(
        content=[_FakeTextBlock("{\"passes\"")], stop_reason="max_tokens")))
    with pytest.raises(CritiqueError, match="cut off"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_empty_text():
    client = FakeCritiqueClient(_once(_FakeMessage(content=[])))
    with pytest.raises(CritiqueError, match="no usable text"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_schema_mismatch():
    bad = _FakeMessage(content=[_FakeTextBlock(json.dumps({"nope": 1}))])
    client = FakeCritiqueClient(_once(bad))
    with pytest.raises(CritiqueError, match="schema"):
        run_critique(_png_bytes(), _spec(), _brief(), client)


def test_run_critique_raises_on_unparseable_json():
    bad = _FakeMessage(content=[_FakeTextBlock("not json at all")])
    client = FakeCritiqueClient(_once(bad))
    with pytest.raises(CritiqueError):
        run_critique(_png_bytes(), _spec(), _brief(), client)


# -- retry: one transient retry, same policy as imaging.py --------------------

def test_run_critique_retries_once_on_a_transient_failure_then_succeeds():
    client = FakeCritiqueClient(
        _first_call_raises_then(_rate_limited(), _reply(passes=True)))

    result = run_critique(_png_bytes(), _spec(), _brief(), client)
    assert result.passes is True
    assert len(client.calls) == 2


def test_run_critique_raises_when_the_retry_also_fails():
    client = FakeCritiqueClient(_raises(_rate_limited()))
    with pytest.raises(CritiqueError, match="after a retry"):
        run_critique(_png_bytes(), _spec(), _brief(), client)
    assert len(client.calls) == 2


def test_run_critique_a_non_transient_failure_is_not_retried():
    # An AttributeError (a bogus/sentinel client, e.g. a route test's bypass
    # fixture) is not in _TRANSIENT_ERRORS -- it must fail on the first
    # attempt, not burn a retry on something that will fail identically.
    boom = RuntimeError("the SDK rejected this request shape")
    client = FakeCritiqueClient(_raises(boom))
    with pytest.raises(CritiqueError, match="rejected this request shape"):
        run_critique(_png_bytes(), _spec(), _brief(), client)
    assert len(client.calls) == 1


# -- system prompt content: the container device + simplicity doctrine -------

def test_run_critique_system_prompt_names_the_container_device_virtue_and_tell():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client)
    system = client.calls[0]["system"]
    assert "Do not flag a well-executed container device as a tell." in system
    assert "container device attempted but botched" in system


def test_run_critique_system_prompt_names_more_rendered_detail_tell():
    client = FakeCritiqueClient(_once(_reply(passes=True)))
    run_critique(_png_bytes(), _spec(), _brief(), client)
    system = client.calls[0]["system"]
    assert "more rendered detail than the concept needs" in system
