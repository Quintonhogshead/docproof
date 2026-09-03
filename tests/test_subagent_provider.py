"""docproof/providers/subagent.py — the subscription lane as a Provider. The
SDK is a fake module here: what is under test is the fence (options), the
prompt contract, and the reply parsing, none of which need a session."""
from __future__ import annotations

import types

from docproof import agent_lane
from docproof.providers import subagent


class _Text:
    def __init__(self, text):
        self.text = text


class _Assistant:
    def __init__(self, *texts):
        self.content = [_Text(t) for t in texts]
        self.parent_tool_use_id = None


class _Result:
    def __init__(self, result, cost=0.0, usage=None):
        self.result = result
        self.total_cost_usd = cost
        self.usage = usage or {"input_tokens": 120, "output_tokens": 30}


def _fake_sdk(messages, seen):
    sdk = types.SimpleNamespace()
    sdk.TextBlock = _Text
    sdk.AssistantMessage = _Assistant
    sdk.ResultMessage = _Result
    sdk.CLINotFoundError = type("CLINotFoundError", (Exception,), {})
    sdk.ProcessError = type("ProcessError", (Exception,), {})
    sdk.ResultError = type("ResultError", (Exception,), {})

    def ClaudeAgentOptions(**kw):
        seen.append(kw)
        return kw
    sdk.ClaudeAgentOptions = ClaudeAgentOptions

    async def query(*, prompt, options):
        async for _ in prompt:            # consume the prompt like the SDK does
            pass
        for m in messages:
            yield m
    sdk.query = query
    return sdk


def test_extract_json_handles_fences_prose_and_nothing():
    assert subagent.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert subagent.extract_json('Sure. {"problems": []} done') == {"problems": []}
    assert subagent.extract_json("no json here") is None
    assert subagent.extract_json("[1, 2]") is None          # not an object


def test_resolve_model_maps_doctrine_aliases(monkeypatch):
    assert subagent.resolve_model("opus") == "claude-opus-5"
    assert subagent.resolve_model("claude-sonnet-5") == "claude-sonnet-5"
    monkeypatch.setenv(subagent.MODEL_ENV, "sonnet")
    assert subagent.resolve_model(None) == "claude-sonnet-5"
    monkeypatch.delenv(subagent.MODEL_ENV)
    assert subagent.resolve_model(None) == subagent.DEFAULT_MODEL
    assert subagent.is_subagent_model("gpt-5.6-luna") is False
    assert subagent.is_subagent_model("fable") is True


def test_complete_structured_runs_one_fenced_turn_and_parses_the_reply(
        monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seen = []
    sdk = _fake_sdk([_Assistant("thinking…"),
                     _Result('{"problems": [{"index": 1}]}', cost=0.0)], seen)
    prov = subagent.SubagentProvider(model="opus", sdk=sdk, cwd=tmp_path)
    res = prov.complete_structured(
        model="claude-opus-5", system="SYS", user="USER",
        schema={"type": "object"}, schema_name="problems", max_tokens=100)
    assert res.stop_reason == "ok", res.error
    assert res.parsed == {"problems": [{"index": 1}]}
    assert res.usage.input_tokens == 120 and res.usage.output_tokens == 30
    assert prov.calls == 1 and prov.cost_usd == 0.0
    opts = seen[0]
    # the fence: no tools, no settings, keys blanked, one turn, our cwd
    assert opts["tools"] == [] and opts["allowed_tools"] == []
    assert opts["setting_sources"] == [] and opts["max_turns"] == 1
    assert opts["env"] == agent_lane.child_env()
    assert opts["cwd"] == str(tmp_path) and opts["model"] == "claude-opus-5"
    assert opts["system_prompt"] == "SYS"


def test_a_non_claude_model_name_routes_to_the_providers_own_model(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seen = []
    sdk = _fake_sdk([_Result('{"findings": []}')], seen)
    prov = subagent.SubagentProvider(model="sonnet", sdk=sdk, cwd=tmp_path)
    prov.complete_structured(model="gpt-5.6-luna", system="s", user="u",
                             schema={}, schema_name="findings", max_tokens=1)
    assert seen[0]["model"] == "claude-sonnet-5"


def test_a_reply_without_json_is_a_loss_not_a_half_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    sdk = _fake_sdk([_Assistant("I found nothing worth reporting."),
                     _Result("All clean!")], [])
    prov = subagent.SubagentProvider(sdk=sdk, cwd=tmp_path)
    res = prov.complete_structured(model="opus", system="s", user="u",
                                   schema={}, schema_name="x", max_tokens=1)
    assert res.stop_reason == "error" and res.parsed is None


def test_refuses_before_spawning_when_not_logged_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    sdk = _fake_sdk([_Result("{}")], [])
    prov = subagent.SubagentProvider(sdk=sdk, cwd=tmp_path)
    import pytest
    with pytest.raises(agent_lane.AgentLaneUnavailable):
        prov.complete_structured(model="opus", system="s", user="u",
                                 schema={}, schema_name="x", max_tokens=1)


def test_extract_json_tolerates_raw_newlines_and_trailing_prose():
    raw = '{"findings": [{"para_id": "p1", "quote": "a\nb", "problem": "x"}]}'
    assert subagent.extract_json(raw)["findings"][0]["quote"] == "a\nb"
    assert subagent.extract_json('{"a": 1} Hope this helps!') == {"a": 1}


def test_extract_json_salvages_a_truncated_array():
    cut = ('{"findings": [{"para_id": "p1", "quote": "teh", "problem": "sp", '
           '"suggestion": "the", "severity": "high"}, {"para_id": "p2", '
           '"quote": "recieve", "problem": "sp", "suggestion": "receive", '
           '"severity": "high"}, {"para_id": "p3", "quote": "half a ro')
    obj = subagent.extract_json(cut)
    assert obj is not None
    assert [r["para_id"] for r in obj["findings"]] == ["p1", "p2"]
    assert subagent.extract_json('{"findings": [{"para_id"') is None
