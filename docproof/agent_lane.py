"""The Claude-Code-on-the-subscription lane, shared by everything that needs
an agent rather than a single model call.

docproof.canvas.assistant built this plumbing first (the AI box in Cover
Canvas) and docproof.cover.atelier is the second caller (the agents that
execute a director's cover assignments). It lives here rather than in either
because of ONE function: `child_env`. DocProof holds vendor API keys in the
same process that spawns these children, and a key that leaks into a child
turns a $0 subscription turn into a metered API bill with no visible symptom.
That guard must have exactly one implementation, and this is it.

Everything else here is the same shape both callers need: importing the SDK
at CALL time so a server with no assistant installed still starts, refusing
before spawning when the machine has no login, wrapping in-process functions
as SDK tools, and turning the SDK's three failure modes into one readable
sentence.

The hints are parameterized rather than fixed because the sentence is read by
a person in a specific place — "reopen the canvas" is wrong advice when the
thing that failed was a cover job running on a server.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class AgentLaneUnavailable(RuntimeError):
    """The agent lane cannot run here, with a sentence saying what to do.

    Always a human sentence naming the missing piece and the command that
    fixes it: this text is rendered straight into a UI, where "no module
    named claude_agent_sdk" would be a dead end for the person it reaches."""


def install_hint(subject: str, remedy: str) -> str:
    return (f"{subject} needs the Claude Agent SDK, which is not installed "
            f"in this environment — install it with `pip install "
            f"claude-agent-sdk` (and the Claude Code CLI it drives) and "
            f"{remedy}.")


def login_hint(subject: str, remedy: str) -> str:
    return (f"{subject} runs on your Claude subscription and this machine "
            f"is not logged in — run `claude setup-token` in a terminal and "
            f"set CLAUDE_CODE_OAUTH_TOKEN, or run `claude` once to sign in, "
            f"then {remedy}.")


def cli_hint(subject: str, remedy: str) -> str:
    return (f"{subject} could not find the Claude Code CLI it drives — "
            f"install it (`npm install -g @anthropic-ai/claude-code`) and "
            f"{remedy}.")


def sdk(hint: str) -> Any:
    """The agent SDK, or the sentence that says how to get it.

    Imported through importlib at CALL time, never at module import: a server
    must start, and its routes must import, on a machine that has no agent
    SDK installed — the agent feature is the only thing that should fail
    there, and it should fail into the surface the person is looking at."""
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        raise AgentLaneUnavailable(hint) from e


def require_login(hint: str) -> None:
    """Refuse before spawning anything if this machine has no Claude login.

    Checked here rather than left to the CLI because the CLI's own failure
    for this is a subprocess exit nobody in a UI can act on. The three places
    a login can live are the token env var (the Galley pattern), the
    credentials file, and the CLI's own config — any one of them is enough,
    and the check is deliberately generous: a false "you are logged in" costs
    one clear error from the CLI, a false "you are not" costs the feature."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    if (base / ".credentials.json").exists():
        return
    legacy = Path(config_dir) / ".claude.json" if config_dir \
        else Path.home() / ".claude.json"
    if legacy.exists():
        return
    raise AgentLaneUnavailable(hint)


# The cached answer of `probe_login` for this process: (logged_in, detail).
# `logged_in` is None when the probe could not be made (no CLI, a timeout, an
# unparseable reply) — "unknown", which the file check above then decides.
_LOGIN_PROBE: tuple[bool | None, str] | None = None
PROBE_TIMEOUT = 20.0


def reset_login_probe() -> None:
    """Forget the cached probe (tests; a `claude setup-token` mid-process)."""
    global _LOGIN_PROBE
    _LOGIN_PROBE = None


def probe_login(*, timeout: float = PROBE_TIMEOUT) -> tuple[bool | None, str]:
    """Ask the CLI itself whether this machine is signed in — `claude auth
    status --json`, the same binary the SDK drives, in the same fenced
    environment (`child_env`, so an API key in this process can neither
    satisfy nor confuse the check). Cached per process: it is a subprocess.

    The file check in `require_login` is deliberately generous, and on the
    Georgis run (2026-09-04) generous was wrong: ~/.claude.json existed, so
    `available()` said yes, `--engine auto` chose the subagent lane, and the
    first turn died with "Not logged in · Please run /login". Returns
    (True|False|None, detail); None means the probe could not be made and the
    caller falls back to the file check."""
    global _LOGIN_PROBE
    if _LOGIN_PROBE is not None:
        return _LOGIN_PROBE
    cli = shutil.which("claude")
    if not cli:
        _LOGIN_PROBE = (None, "the Claude Code CLI is not on PATH")
        return _LOGIN_PROBE
    env = {**os.environ, **child_env()}
    try:
        proc = subprocess.run([cli, "auth", "status", "--json"],
                              capture_output=True, text=True, timeout=timeout,
                              env=env)
    except (OSError, subprocess.SubprocessError) as e:
        _LOGIN_PROBE = (None, f"`claude auth status` could not run ({e})")
        return _LOGIN_PROBE
    text = (proc.stdout or "").strip()
    try:
        start = text.index("{")
        payload = json.loads(text[start:])
    except (ValueError, TypeError):
        _LOGIN_PROBE = (None, "`claude auth status` gave no JSON "
                        f"(exit {proc.returncode})")
        return _LOGIN_PROBE
    logged_in = payload.get("loggedIn")
    if not isinstance(logged_in, bool):
        _LOGIN_PROBE = (None, "`claude auth status` did not say loggedIn")
        return _LOGIN_PROBE
    method = str(payload.get("authMethod") or "none")
    _LOGIN_PROBE = (logged_in, f"`claude auth status`: loggedIn={logged_in} "
                               f"(authMethod {method})")
    return _LOGIN_PROBE


def child_env() -> dict[str, str]:
    """The billing guard: the spawned CLI must run on the subscription.

    ClaudeAgentOptions.env is MERGED over the parent's environment by the
    transport (it cannot delete a key), and the CLI reads ANTHROPIC_API_KEY
    with JavaScript truthiness — so blanking it to "" is how you actually
    make it absent, and the SDK's own auth probing treats the empty string as
    unset. This matters more than it looks: DocProof holds image-generation
    keys in the same process, and a key that leaks into this child turns a $0
    subscription turn into a metered API bill without any visible symptom.

    CLAUDE_CODE_OAUTH_TOKEN is deliberately left alone — it is the credential
    this is supposed to run on."""
    return {"ANTHROPIC_API_KEY": ""}


def sdk_tools(sdk_module: Any, specs: Any) -> list[Any]:
    """The specs, wrapped as SDK tool objects. A spec is anything carrying
    `name`, `description`, `schema` and `handler`."""
    return [sdk_module.tool(s.name, s.description, s.schema)(s.handler)
            for s in specs]


__all__ = ["AgentLaneUnavailable", "child_env", "cli_hint", "install_hint",
           "login_hint", "probe_login", "require_login", "reset_login_probe",
           "sdk", "sdk_tools"]
