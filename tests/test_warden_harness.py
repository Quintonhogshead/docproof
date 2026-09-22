"""app/warden/harness.py: the exact argv built for each harness kind, the
timeout derived from the tick interval, `harness: "none"` short-circuiting
without running anything, and pulling the right text out of `claude -p`'s
JSON versus `codex exec`'s plain stdout."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.warden import harness

TICK_JSON = '{"findings": [], "needs_model": [{"rule": "watch-signin-dead"}]}'


@dataclass
class FakeConfig:
    harness: str = "claude"
    harness_bin: str = ""
    harness_model: str = ""
    tick_interval_min: int = 20


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


@dataclass
class FakeRun:
    calls: list = field(default_factory=list)
    proc: FakeProc = field(default_factory=FakeProc)

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, "kwargs": kw})
        return self.proc


def _home(tmp_path) -> Path:
    (tmp_path / "tick").mkdir()
    (tmp_path / "tick" / "latest.json").write_text(TICK_JSON, "utf-8")
    return tmp_path


# --- claude argv -------------------------------------------------------

def test_claude_argv_matches_the_exact_shape(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "did a thing"})))
    text = harness.run(FakeConfig(), home, mode="tick", run=run)

    assert text == "did a thing"
    cmd = run.calls[0]["cmd"]
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert TICK_JSON in cmd[2]
    assert cmd[3] == "--bare"
    assert cmd[4:6] == ["--output-format", "json"]
    assert cmd[6] == "--allowedTools"
    assert cmd[7:10] == ["Bash(docproof-warden:*)", "Read", "Grep"]
    assert cmd[10:12] == ["--permission-mode", "acceptEdits"]
    assert cmd[12] == "--no-session-persistence"
    assert cmd[13] == "--append-system-prompt"
    assert isinstance(cmd[14], str) and cmd[14]


def test_claude_argv_honours_a_custom_binary_and_model(tmp_path):
    home = _home(tmp_path)
    cfg = FakeConfig(harness_bin="/opt/claude/claude", harness_model="opus")
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "ok"})))
    harness.run(cfg, home, mode="tick", run=run)
    cmd = run.calls[0]["cmd"]
    assert cmd[0] == "/opt/claude/claude"
    assert cmd[-2:] == ["--model", "opus"]


def test_the_prompt_carries_tick_latest_json(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "ok"})))
    harness.run(FakeConfig(), home, mode="tick", run=run)
    prompt = run.calls[0]["cmd"][2]
    assert "watch-signin-dead" in prompt


def test_question_mode_embeds_the_question_not_tick_json(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "answered"})))
    text = harness.run(FakeConfig(), home, mode="question",
                       question="is Dalton stuck?", run=run)
    assert text == "answered"
    prompt = run.calls[0]["cmd"][2]
    assert "is Dalton stuck?" in prompt
    assert TICK_JSON not in prompt


# --- codex argv --------------------------------------------------------

def test_codex_argv_matches_the_exact_shape(tmp_path):
    home = _home(tmp_path)
    cfg = FakeConfig(harness="codex")
    run = FakeRun(proc=FakeProc(stdout="codex said this"))
    text = harness.run(cfg, home, mode="tick", run=run)
    assert text == "codex said this"
    cmd = run.calls[0]["cmd"]
    assert cmd[0] == "codex"
    assert cmd[1:4] == ["exec", "--sandbox", "read-only"]
    assert cmd[4] == "-C"
    assert Path(cmd[5]).is_dir()
    assert TICK_JSON in cmd[6]


# --- timeout -------------------------------------------------------------

def test_timeout_is_tick_interval_minus_two_minutes(tmp_path):
    home = _home(tmp_path)
    cfg = FakeConfig(tick_interval_min=20)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "ok"})))
    harness.run(cfg, home, mode="tick", run=run)
    assert run.calls[0]["kwargs"]["timeout"] == 20 * 60 - 120


def test_timeout_never_drops_below_the_floor(tmp_path):
    home = _home(tmp_path)
    cfg = FakeConfig(tick_interval_min=1)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "ok"})))
    harness.run(cfg, home, mode="tick", run=run)
    assert run.calls[0]["kwargs"]["timeout"] == 30


# --- harness: none / errors -----------------------------------------------

def test_harness_none_never_runs_anything(tmp_path):
    home = _home(tmp_path)
    cfg = FakeConfig(harness="none")
    run = FakeRun()
    text = harness.run(cfg, home, mode="tick", run=run)
    assert text == ""
    assert run.calls == []


def test_a_run_failure_is_reported_not_raised(tmp_path):
    home = _home(tmp_path)

    def boom(cmd, **kw):
        raise OSError("no such binary")

    text = harness.run(FakeConfig(), home, mode="tick", run=boom)
    assert "could not run" in text


def test_non_json_stdout_from_claude_is_returned_as_is(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(proc=FakeProc(stdout="plain text, not json"))
    text = harness.run(FakeConfig(), home, mode="tick", run=run)
    assert text == "plain text, not json"


def test_code_mode_widens_the_tools_and_the_timeout(tmp_path):
    home = _home(tmp_path)
    run = FakeRun(proc=FakeProc(stdout=json.dumps({"result": "opened a PR"})))
    text = harness.run(FakeConfig(), home, mode="code",
                       question='{"rule": "x", "files": "a.py"}', run=run)

    assert text == "opened a PR"
    cmd = run.calls[0]["cmd"]
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--permission-mode")]
    assert "Edit" in tools and "Write" in tools and "Bash(git:*)" in tools
    assert not any(t.startswith("Bash(fly") for t in tools)
    assert "APPROVED" in cmd[2] and '"rule": "x"' in cmd[2]
    assert run.calls[0]["kwargs"]["timeout"] >= 45 * 60


def test_tick_and_question_modes_stay_read_only(tmp_path):
    home = _home(tmp_path)
    for mode in ("tick", "question"):
        run = FakeRun(proc=FakeProc(stdout="{}"))
        harness.run(FakeConfig(), home, mode=mode, question="q", run=run)
        cmd = run.calls[0]["cmd"]
        tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--permission-mode")]
        assert "Edit" not in tools and "Write" not in tools
