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


# --- Georgis (2026-09-04): availability asks the CLI, not just the disk --------

def _probe_returning(monkeypatch, stdout, *, which="/usr/local/bin/claude",
                     raise_exc=None):
    import subprocess
    agent_lane.reset_login_probe()
    monkeypatch.setattr(agent_lane.shutil, "which", lambda name: which)
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if raise_exc:
            raise raise_exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(agent_lane.subprocess, "run", run)
    return calls


def test_availability_is_false_when_the_cli_says_not_logged_in(monkeypatch,
                                                                tmp_path):
    """~/.claude.json existed, the file check said yes, --engine auto chose
    the lane, and the first turn died "Not logged in". The CLI's own word
    decides now, and the refusal names `claude setup-token`."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(agent_lane, "sdk", lambda hint: object())
    calls = _probe_returning(monkeypatch,
                             '{"loggedIn": false, "authMethod": "none"}')
    ok, why = subagent.availability()
    assert ok is False
    assert "claude setup-token" in why and "loggedIn=False" in why
    cmd, kw = calls[0]
    assert cmd[1:] == ["auth", "status", "--json"]
    assert kw["env"]["ANTHROPIC_API_KEY"] == ""          # the billing fence
    # cached: a second ask does not spawn again
    subagent.availability()
    assert len(calls) == 1
    agent_lane.reset_login_probe()


def test_availability_is_true_when_the_cli_is_signed_in(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(agent_lane, "sdk", lambda hint: object())
    _probe_returning(monkeypatch,
                     '{"loggedIn": true, "authMethod": "oauth_token"}')
    ok, why = subagent.availability()
    assert ok is True and "oauth_token" in why
    assert subagent.available() is True
    agent_lane.reset_login_probe()


def test_an_inconclusive_probe_defers_to_the_file_check(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(agent_lane, "sdk", lambda hint: object())
    _probe_returning(monkeypatch, "", which=None)          # no CLI on PATH
    ok, why = subagent.availability()
    assert ok is True and "not on PATH" in why
    agent_lane.reset_login_probe()
    _probe_returning(monkeypatch, "garbage")               # unparseable
    ok, _why = subagent.availability()
    assert ok is True
    agent_lane.reset_login_probe()


def test_a_not_logged_in_turn_names_setup_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")

    class _ErrResult(_Result):
        is_error = True
        subtype = "error_during_execution"
    sdk = _fake_sdk([_ErrResult("Not logged in · Please run /login")], [])
    prov = subagent.SubagentProvider(sdk=sdk, cwd=tmp_path)
    import pytest
    with pytest.raises(agent_lane.AgentLaneUnavailable) as ei:
        prov.complete_structured(model="opus", system="s", user="u",
                                 schema={}, schema_name="x", max_tokens=1)
    assert "claude setup-token" in str(ei.value)


# ===========================================================================
# A dead session must say why
# ===========================================================================

def _failing_sdk(seen, stderr_lines):
    """A fake whose query() writes to the stderr sink, then dies the way the
    real CLI did on Fly: ProcessError with no detail of its own."""
    sdk = _fake_sdk([], seen)

    async def query(*, prompt, options):
        async for _ in prompt:
            pass
        sink = options.get("stderr")
        for line in stderr_lines:
            sink(line)
        raise sdk.ProcessError(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details")
        yield  # pragma: no cover - never reached, keeps this a generator
    sdk.query = query
    return sdk


def test_a_dead_session_reports_what_the_cli_actually_wrote(monkeypatch):
    """The Fly failure of 2026-09-07: the lane raised 'Command failed with
    exit code 1 / Error output: Check stderr output for details' and told the
    operator to sign in — on a machine that was signed in. The CLI's own
    account is the thing worth having."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seen = []
    sdk = _failing_sdk(seen, ["Error: ENOSPC: no space left on device",
                              "    at Object.mkdirSync (node:fs:1394:26)"])
    provider = subagent.SubagentProvider(sdk=sdk)

    try:
        provider.complete_structured(
            model="fable", system="s", user="u", schema={"type": "object"},
            schema_name="reply", max_tokens=100)
    except agent_lane.AgentLaneUnavailable as e:
        message = str(e)
    else:                                                # pragma: no cover
        raise AssertionError("the dead session did not raise")

    assert "ENOSPC: no space left on device" in message
    assert "mkdirSync" in message
    # It must NOT blame a login when the CLI said something else.
    assert "setup-token" not in message
    assert seen and seen[0]["stderr"] is not None


def test_a_silent_death_still_suggests_the_login(monkeypatch):
    """With nothing on stderr there is no evidence, so the old guess is the
    best available — but only then."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    sdk = _failing_sdk([], [])
    provider = subagent.SubagentProvider(sdk=sdk)

    try:
        provider.complete_structured(
            model="fable", system="s", user="u", schema={"type": "object"},
            schema_name="reply", max_tokens=100)
    except agent_lane.AgentLaneUnavailable as e:
        message = str(e)
    else:                                                # pragma: no cover
        raise AssertionError("the dead session did not raise")

    assert "nothing to stderr" in message
    assert "setup-token" in message


def test_stderr_capture_is_bounded(monkeypatch):
    """A chatty CLI must not grow the exception without limit."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    sdk = _failing_sdk([], [f"line {n}" for n in range(500)])
    provider = subagent.SubagentProvider(sdk=sdk)

    try:
        provider.complete_structured(
            model="fable", system="s", user="u", schema={"type": "object"},
            schema_name="reply", max_tokens=100)
    except agent_lane.AgentLaneUnavailable as e:
        message = str(e)
    else:                                                # pragma: no cover
        raise AssertionError("the dead session did not raise")

    assert "line 0" in message
    assert f"line {subagent._STDERR_KEEP}" not in message


def test_the_fenced_turn_never_asks_to_bypass_permissions(monkeypatch):
    """The blocker that stopped every paid read on Fly (2026-09-07).

    bypassPermissions reaches the CLI as --dangerously-skip-permissions, which
    it refuses as root; the Galley agent runs as uid 0, so all ten detector
    calls on the probe chunk died with "cannot be used with root/sudo
    privileges". The turn is fenced to no tools, so the bypass bought nothing
    and cost the lane.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    seen = []
    sdk = _fake_sdk([_Result('{"ok": true}')], seen)
    provider = subagent.SubagentProvider(sdk=sdk)
    provider.complete_structured(
        model="fable", system="s", user="u", schema={"type": "object"},
        schema_name="reply", max_tokens=100)

    opts = seen[0]
    assert opts["permission_mode"] != "bypassPermissions"
    # The fence is what makes that safe — if these ever open up, the
    # permission mode has to be reconsidered with them.
    assert opts["tools"] == []
    assert opts["allowed_tools"] == []
    assert opts["setting_sources"] == []
    assert opts["strict_mcp_config"] is True
