"""docproof/cover/subscription.py: Cover Studio's Anthropic roles on the
owner's Claude subscription.

Nothing here spawns the CLI or touches the network -- the SDK seam is one
function (`subscription._sdk`) and every test replaces it with a scripted
fake, the same discipline tests/test_canvas_assistant.py keeps.

The strongest tests in this file do not poke at the shim at all: they call
the REAL direction.py / critique.py entry points with a subscription object
in the provider/client slot and assert those modules parse the reply. That is
the only way to prove the stand-ins satisfy the interfaces their call sites
actually use, rather than the interfaces this module imagines they use.

No pytest-asyncio in this repo (see tests/test_cover_pipeline.py): the one
test that proves the sync bridge works from inside a running event loop drives
it with a plain asyncio.run().
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import types

import pytest
from PIL import Image

from docproof.cover import subscription
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.critique import CritiqueError, run_critique
from docproof.cover.direction import (DirectionError, revise_spec,
                                      run_directions)
from docproof.cover.model import Brief, Direction, Palette, build_spec
from docproof.cover.subscription import (SubscriptionAnthropicClient,
                                         SubscriptionProvider,
                                         SubscriptionUnavailable)


# -- fixtures -----------------------------------------------------------------

def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
                genre="literary", concepts=1)
    data.update(overrides)
    return Brief(**data)


def _palette() -> Palette:
    return Palette(background="#101010", primary="#f5f1e8", accent="#c9a227",
                   text="#f5f1e8", scrim="#000000")


def _direction() -> Direction:
    return Direction(concept_name="Cold Light", rationale="A test concept.",
                     archetype="big_type", palette=_palette(),
                     title_font="Playfair Display", author_font="Spectral",
                     art_prompts={}, texture=False)


def _spec():
    return build_spec(_direction(), _brief(), ARCHETYPES["big_type"])


_DIRECTIONS_REPLY = {"concepts": [{
    "concept_name": "Cold Light",
    "rationale": "Type is the hero.",
    "archetype": "big_type",
    "palette": {"background": "#101010", "primary": "#f5f1e8",
                "accent": "#c9a227", "text": "#f5f1e8", "scrim": "#000000"},
    "title_font": "Playfair Display",
    "author_font": "Spectral",
    "art_prompts": [],
    "texture": False,
    "recipe": "",
    "type_move": "",
    "emphasis_word": "",
}]}

_CRITIQUE_REPLY = {"passes": False,
                   "tells": ["The author line disappears into the ground."],
                   "notes": "Lift the author line onto the scrim.",
                   "art_defects": []}


def _png(width: int = 40, height: int = 60) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


# -- the scripted SDK ---------------------------------------------------------

class _FakeOptions:
    """ClaudeAgentOptions' stand-in: whatever it is handed, verbatim."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id


class _FakeResultMessage:
    def __init__(self, result=None, total_cost_usd=None):
        self.result = result
        self.total_cost_usd = total_cost_usd


class _FakeCLINotFound(Exception):
    pass


class _FakeProcessError(Exception):
    pass


class _FakeResultError(Exception):
    pass


def _fake_sdk(reply="{}", seen=None, raises=None):
    """A claude_agent_sdk stand-in whose `query` answers with `reply`.

    `seen` collects the options and the streamed prompt, which is how the
    billing fence, the model passthrough and the image blocks are asserted.
    `raises` is an exception class the query raises instead of answering."""
    module = types.ModuleType("claude_agent_sdk_fake")
    module.ClaudeAgentOptions = _FakeOptions
    module.AssistantMessage = _FakeAssistantMessage
    module.ResultMessage = _FakeResultMessage
    module.TextBlock = _FakeTextBlock
    module.CLINotFoundError = _FakeCLINotFound
    module.ProcessError = _FakeProcessError
    module.ResultError = _FakeResultError

    async def query(*, prompt, options):
        messages = [m async for m in prompt]
        if seen is not None:
            seen["prompt"] = messages
            seen["options"] = options
            seen.setdefault("calls", []).append(options)
        if raises is not None:
            raise raises("the CLI said no")
        yield _FakeAssistantMessage([_FakeTextBlock("working on it")])
        yield _FakeResultMessage(result=reply, total_cost_usd=0.0)

    module.query = query
    return module


def _install(monkeypatch, sdk) -> None:
    """The SDK seam plus the login pre-check, both replaced, so a construction
    never depends on whether the machine running the suite is signed in."""
    monkeypatch.setattr(subscription, "_sdk", lambda: sdk)
    monkeypatch.setattr(subscription, "_require_login", lambda: None)


def _provider(monkeypatch, reply, seen=None) -> SubscriptionProvider:
    _install(monkeypatch, _fake_sdk(json.dumps(reply) if not isinstance(
        reply, str) else reply, seen=seen))
    return SubscriptionProvider()


def _client(monkeypatch, reply, seen=None) -> SubscriptionAnthropicClient:
    _install(monkeypatch, _fake_sdk(json.dumps(reply) if not isinstance(
        reply, str) else reply, seen=seen))
    return SubscriptionAnthropicClient()


# -- construction is the fallback point ---------------------------------------

def test_a_machine_with_no_agent_sdk_is_told_how_to_install_it(monkeypatch):
    def no_module(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", no_module)
    with pytest.raises(SubscriptionUnavailable) as excinfo:
        SubscriptionProvider()
    assert "pip install claude-agent-sdk" in str(excinfo.value)


def test_a_machine_with_no_claude_login_is_told_to_set_a_token(monkeypatch,
                                                               tmp_path):
    monkeypatch.setattr(subscription, "_sdk", lambda: _fake_sdk())
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(SubscriptionUnavailable, match="setup-token"):
        SubscriptionProvider()
    with pytest.raises(SubscriptionUnavailable, match="setup-token"):
        SubscriptionAnthropicClient()


def test_an_oauth_token_is_login_enough(monkeypatch, tmp_path):
    monkeypatch.setattr(subscription, "_sdk", lambda: _fake_sdk())
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-whatever")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    subscription.preflight()


# -- the real direction call, answered on the subscription --------------------

def test_run_directions_parses_a_subscription_reply(monkeypatch):
    # The strongest test in the file: direction.py's own code path, unchanged,
    # with a SubscriptionProvider where AnthropicProvider normally sits.
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY)
    result = run_directions(_brief(), provider, n=1)
    assert [d.concept_name for d in result.directions] == ["Cold Light"]
    assert result.directions[0].archetype == "big_type"
    assert result.model == "claude-fable-5"


def test_a_subscription_direction_costs_nothing(monkeypatch):
    # A subscription turn bills no API dollars, and the ledger must say so
    # without inventing a figure.
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY)
    assert run_directions(_brief(), provider, n=1).cost == 0.0


def test_the_direction_model_id_rides_the_call(monkeypatch):
    seen = {}
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY, seen=seen)
    run_directions(_brief(), provider, n=1, model="claude-opus-5")
    assert seen["options"].model == "claude-opus-5"


def test_the_schema_the_caller_built_reaches_the_prompt(monkeypatch):
    # Structure comes from the prompt on this lane, so the caller's own
    # schema has to actually be in it -- and the caller's system prompt has
    # to survive intact ahead of it.
    seen = {}
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY, seen=seen)
    run_directions(_brief(), provider, n=1)
    system = seen["options"].system_prompt
    assert "senior book-cover art director" in system
    assert "concept_name" in system and "JSON Schema" in system


def test_a_reply_wrapped_in_prose_and_a_fence_still_parses(monkeypatch):
    reply = ("Here are the concepts you asked for:\n\n```json\n"
             + json.dumps(_DIRECTIONS_REPLY) + "\n```\nHope that helps.")
    provider = _provider(monkeypatch, reply)
    assert run_directions(_brief(), provider, n=1).directions[0].archetype \
        == "big_type"


def test_a_reply_with_no_json_becomes_a_readable_direction_error(monkeypatch):
    provider = _provider(monkeypatch, "I would rather not.")
    with pytest.raises(DirectionError, match="no JSON object"):
        run_directions(_brief(), provider, n=1)


def test_revise_spec_parses_a_subscription_reply(monkeypatch):
    # The second Provider-protocol call site: a patch list, applied in code.
    provider = _provider(monkeypatch, {"edits": [
        {"path": "palette.primary", "value": "\"#a83250\""}]})
    result = revise_spec(_spec(), "warmer, please", provider)
    assert result.spec.palette.primary == "#a83250"
    assert result.cost == 0.0


# -- the fences ---------------------------------------------------------------

def test_the_child_never_sees_an_api_key(monkeypatch):
    # The guard this whole module exists for: DocProof holds vendor keys in
    # this process, and a key reaching the child turns a $0 turn into a bill.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    seen = {}
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY, seen=seen)
    run_directions(_brief(), provider, n=1)
    assert seen["options"].env == {"ANTHROPIC_API_KEY": ""}


def test_the_turn_is_handed_no_tools_and_no_settings(monkeypatch):
    seen = {}
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY, seen=seen)
    run_directions(_brief(), provider, n=1)
    options = seen["options"]
    assert options.tools == [] and options.allowed_tools == []
    assert options.mcp_servers == {} and options.strict_mcp_config is True
    assert options.setting_sources == []
    assert 1 <= options.max_turns <= 2


def test_a_missing_cli_is_a_sentence_naming_the_install(monkeypatch):
    _install(monkeypatch, _fake_sdk(raises=_FakeCLINotFound))
    with pytest.raises(SubscriptionUnavailable, match="npm install"):
        SubscriptionProvider().complete_structured(
            model="claude-sonnet-5", system="s", user="u", schema={},
            schema_name="x", max_tokens=100)


def test_a_session_that_cannot_start_says_how_to_sign_in(monkeypatch):
    _install(monkeypatch, _fake_sdk(raises=_FakeResultError))
    with pytest.raises(SubscriptionUnavailable, match="setup-token"):
        SubscriptionProvider().complete_structured(
            model="claude-sonnet-5", system="s", user="u", schema={},
            schema_name="x", max_tokens=100)


def test_batching_is_refused_rather_than_faked(monkeypatch):
    _install(monkeypatch, _fake_sdk())
    with pytest.raises(NotImplementedError, match="no batch endpoint"):
        SubscriptionProvider().submit_batch(model="claude-sonnet-5",
                                            requests=[], max_tokens=100)


# -- the sync bridge ----------------------------------------------------------

def test_a_synchronous_call_works_from_inside_a_running_event_loop(monkeypatch):
    # Every call site is synchronous and pipeline.py calls them straight from
    # a coroutine on the server's loop, where asyncio.run() would raise.
    provider = _provider(monkeypatch, _DIRECTIONS_REPLY)

    async def on_the_loop():
        return run_directions(_brief(), provider, n=1)

    assert asyncio.run(on_the_loop()).directions[0].concept_name == "Cold Light"


# -- the anthropic-client shim, through the real critique call ----------------

def test_run_critique_parses_a_subscription_reply(monkeypatch):
    client = _client(monkeypatch, _CRITIQUE_REPLY)
    verdict = run_critique(_png(), _png(20, 30), _spec(), _brief(), client)
    assert verdict.passes is False
    assert verdict.tells == ["The author line disappears into the ground."]
    assert verdict.notes == "Lift the author line onto the scrim."
    assert verdict.cost == 0.0


def test_both_critique_images_reach_the_turn_as_image_blocks(monkeypatch):
    # The render AND the 100px shelf thumbnail: the judge's whole reason for
    # the second image is legibility at the size a reader meets the cover.
    seen = {}
    client = _client(monkeypatch, _CRITIQUE_REPLY, seen=seen)
    run_critique(_png(), _png(20, 30), _spec(), _brief(), client)
    content = seen["prompt"][0]["message"]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 2
    for block in images:
        assert block["source"]["type"] == "base64"
        assert base64.b64decode(block["source"]["data"])[:4] == b"\x89PNG"
    assert any("shelf/search-thumbnail" in b.get("text", "")
               for b in content if b["type"] == "text")


def test_a_critique_with_no_thumbnail_sends_the_one_image(monkeypatch):
    seen = {}
    client = _client(monkeypatch, _CRITIQUE_REPLY, seen=seen)
    run_critique(_png(), None, _spec(), _brief(), client)
    content = seen["prompt"][0]["message"]["content"]
    assert len([b for b in content if b["type"] == "image"]) == 1


def test_the_critique_model_id_rides_the_call(monkeypatch):
    seen = {}
    client = _client(monkeypatch, _CRITIQUE_REPLY, seen=seen)
    run_critique(_png(), None, _spec(), _brief(), client, model="claude-opus-5")
    assert seen["options"].model == "claude-opus-5"


def test_the_critique_child_never_sees_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-key")
    seen = {}
    client = _client(monkeypatch, _CRITIQUE_REPLY, seen=seen)
    run_critique(_png(), None, _spec(), _brief(), client)
    assert seen["options"].env == {"ANTHROPIC_API_KEY": ""}


def test_a_junk_critique_reply_is_a_readable_critique_error(monkeypatch):
    client = _client(monkeypatch, "no thanks")
    with pytest.raises(CritiqueError, match="no JSON object"):
        run_critique(_png(), None, _spec(), _brief(), client)


def test_the_shim_reads_a_cached_system_prompt_block_list(monkeypatch):
    # Both cover callers pass a plain string today, but AnthropicProvider
    # passes cache-control blocks -- reading both means a caller that starts
    # caching cannot silently lose its system prompt on this lane.
    seen = {}
    client = _client(monkeypatch, _CRITIQUE_REPLY, seen=seen)
    with client.messages.stream(
            model="claude-sonnet-5", max_tokens=8000,
            system=[{"type": "text", "text": "you are the art director",
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "judge this"}],
            output_config={"format": {"type": "json_schema",
                                      "schema": {"type": "object"}}}) as stream:
        message = stream.get_final_message()
    assert message.stop_reason == "end_turn"
    assert json.loads(message.content[0].text) == _CRITIQUE_REPLY
    assert seen["options"].system_prompt.startswith("you are the art director")


def test_the_shim_reports_zero_usage(monkeypatch):
    # No tokens, because no tokens were billed -- never an invented figure.
    client = _client(monkeypatch, _CRITIQUE_REPLY)
    with client.messages.stream(model="claude-sonnet-5", max_tokens=10,
                                system="s",
                                messages=[{"role": "user", "content": "x"}]
                                ) as stream:
        usage = stream.get_final_message().usage
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)
    assert usage.cache_read_input_tokens == 0
