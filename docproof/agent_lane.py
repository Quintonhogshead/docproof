"""The Claude-Code-on-the-subscription lane, shared by everything that needs
an agent rather than a single model call.

docproof.canvas.assistant built this plumbing first (the AI box in Cover
Canvas) and docproof.cover.atelier is the second caller (the agents that
execute a director's cover assignments). It lives here rather than in either
because of ONE function: `child_env`. DocProof holds vendor API keys in the
same process that spawns these children, and a key that leaks into a child
turns a $0 subscription turn into a metered API bill with no visible symptom.
That guard must have exactly one implementation, and this is it.

Galley is the third caller, and the reason the credentials reader is here
too: the brain's `docproof` children are Bash grandchildren of a Claude Code
session, which does not pass its own OAuth token down to them. They find the
subscription by reading the agent credentials file (`~/.galley/agent.env`),
so that reader has to live below both galley and docproof.providers.

Everything else here is the same shape every caller needs: importing the SDK
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
import logging
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("docproof.agent_lane")

#: Where the Galley agent keeps the subscription token (and the rest of a
#: machine's Galley credentials). Named here rather than in galley.agent
#: because the SIFTER is what needs to read it and docproof must not import
#: galley — galley.agent imports these back the other way, which is the
#: direction the layering allows.
DEFAULT_CREDENTIALS_FILE = "~/.galley/agent.env"
#: An override for a machine that keeps the file somewhere else.
CREDENTIALS_FILE_ENV = "GALLEY_AGENT_ENV_FILE"
OAUTH_TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"


class AgentLaneUnavailable(RuntimeError):
    """The agent lane cannot run here, with a sentence saying what to do.

    Always a human sentence naming the missing piece and the command that
    fixes it: this text is rendered straight into a UI, where "no module
    named claude_agent_sdk" would be a dead end for the person it reaches."""


class CredentialsError(RuntimeError):
    """A credentials file exists but must not be read as it stands — today
    only one reason: permissions that let another account read a subscription
    token. The message names the chmod that fixes it."""


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=value lines without shell evaluation; allow export, paired
    quotes, and comment lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[key] = raw
    return out


def credentials_path(path: str | Path | None = None) -> Path:
    """The credentials file to read: the argument, else the environment
    override, else the default. `~` is expanded through `Path.home()` so a
    test that relocates home relocates this too."""
    raw = str(path) if path is not None else (
        os.environ.get(CREDENTIALS_FILE_ENV) or DEFAULT_CREDENTIALS_FILE)
    target = Path(raw)
    if target.parts and target.parts[0] == "~":
        return Path.home().joinpath(*target.parts[1:])
    return target.expanduser()


def read_credentials(path: str | Path | None = None, *,
                     stat_fn: Callable[[Path], Any] | None = None
                     ) -> dict[str, str] | None:
    """The credentials file's values, or None when there is no such file.

    Raises CredentialsError when the file is readable by group or other: it
    holds a subscription token, and a token another account can read is one
    this process should refuse rather than quietly spend."""
    target = credentials_path(path)
    if not target.is_file():
        return None
    info = (stat_fn or (lambda p: p.stat()))(target)
    mode = stat.S_IMODE(info.st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialsError(
            f"{target} is readable by other accounts (mode {mode:04o}) and it "
            f"holds a subscription token. Fix it with:\n"
            f"    chmod 600 {target}")
    return parse_env(target.read_text(encoding="utf-8"))


def file_token(path: str | Path | None = None, *,
               stat_fn: Callable[[Path], Any] | None = None) -> str:
    """The subscription token from the credentials file, or "" — never raises.

    This is the fix for the shape the first live `galley drive` died on
    (2026-09-06): the driver puts CLAUDE_CODE_OAUTH_TOKEN in the brain's
    environment, but Claude Code does not hand its own token down to the Bash
    children the brain spawns, so every `docproof` sifter saw an unset token,
    probed `claude auth status`, got loggedIn=False, and the run failed closed
    at the first Claude call. The token is on disk in the agent credentials
    file; read it there rather than depending on which launcher started us.

    A refused file (group/other-readable) is a warning and an empty string:
    the caller then fails closed with its own sentence, which is the correct
    end either way."""
    try:
        values = read_credentials(path, stat_fn=stat_fn)
    except CredentialsError as e:
        log.warning("ignoring the agent credentials file: %s", e)
        return ""
    except OSError as e:
        log.warning("could not read the agent credentials file: %s", e)
        return ""
    if not values:
        return ""
    return "".join((values.get(OAUTH_TOKEN_KEY) or "").split())


def subscription_token(*, environ: dict[str, str] | None = None,
                       path: str | Path | None = None) -> str:
    """The subscription token this machine should run on: the environment
    first (whoever exported it meant it), then the credentials file."""
    env = os.environ if environ is None else environ
    token = "".join((env.get(OAUTH_TOKEN_KEY) or "").split())
    return token or file_token(path)


def install_hint(subject: str, remedy: str) -> str:
    return (f"{subject} needs the Claude Agent SDK, which is not installed "
            f"in this environment — install it with `pip install "
            f"claude-agent-sdk` (and the Claude Code CLI it drives) and "
            f"{remedy}.")


def login_hint(subject: str, remedy: str) -> str:
    return (f"{subject} runs on your Claude subscription and this machine "
            f"is not logged in — run `claude setup-token` in a terminal and "
            f"set CLAUDE_CODE_OAUTH_TOKEN (or put it in "
            f"{DEFAULT_CREDENTIALS_FILE}, chmod 600), or run `claude` once to "
            f"sign in, then {remedy}.")


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
    if subscription_token():
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

    CLAUDE_CODE_OAUTH_TOKEN is the credential this is supposed to run on, so
    it is never blanked; it is ADDED here when this process does not have one
    but the agent credentials file does. That is the seam that makes the
    probe and the turn agree: `probe_login` runs `claude auth status` in this
    environment and `SubagentProvider._options` passes it to the session, so
    a token found on disk reaches both or neither."""
    env = {"ANTHROPIC_API_KEY": ""}
    if not os.environ.get(OAUTH_TOKEN_KEY):
        token = file_token()
        if token:
            env[OAUTH_TOKEN_KEY] = token
    return env


def sdk_tools(sdk_module: Any, specs: Any) -> list[Any]:
    """The specs, wrapped as SDK tool objects. A spec is anything carrying
    `name`, `description`, `schema` and `handler`."""
    return [sdk_module.tool(s.name, s.description, s.schema)(s.handler)
            for s in specs]


__all__ = ["AgentLaneUnavailable", "CREDENTIALS_FILE_ENV", "CredentialsError",
           "DEFAULT_CREDENTIALS_FILE", "OAUTH_TOKEN_KEY", "child_env",
           "cli_hint", "credentials_path", "file_token", "install_hint",
           "login_hint", "parse_env", "probe_login", "read_credentials",
           "require_login", "reset_login_probe", "sdk", "sdk_tools",
           "subscription_token"]
