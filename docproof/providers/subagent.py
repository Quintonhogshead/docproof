"""A Provider that answers through a Claude Code session on the SUBSCRIPTION
instead of a vendor API — the $0 lane every Galley doctrine line assumes
("Claude models never bill — run them as session subagents"), made available
to code, not just to a practitioner typing into a terminal.

Until now that lane existed only inside an interactive session: the
practitioner spawned subagents, told each to write a file, and lost about a
fifth of them to agents that narrated instead. A headless Galley — one DocWatch
starts with no person at a keyboard — needs the same power as a plain function
call. This provider is that call: `complete_structured` runs ONE fenced turn of
the Claude Code CLI through the Agent SDK (no tools, no settings, no repo
CLAUDE.md, API keys blanked so the turn bills the subscription), asks for the
JSON the caller's schema describes, and parses the reply into the same
`ProviderResult` an API provider returns. Every caller written against the
Provider protocol — the change verifier, the finished-text walk, the settle
judge — gets the subscription lane by construction, with no prompt rewritten.

Cost is reported as the session reports it: subscription turns say 0.0, and a
0.0 is passed through rather than estimated. Token counts ride in usage so the
ledger still shows the work happened (a $0 line with nonzero tokens is a
subscription read, not the silently-didn't-run anomaly).

The SDK is imported at call time through docproof.agent_lane, so importing
this module on a server with no SDK is harmless; only USING it fails, with the
sentence agent_lane gives.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .. import agent_lane
from .base import NormalizedUsage, ProviderResult

log = logging.getLogger("docproof.providers.subagent")

DEFAULT_MODEL = "claude-opus-5"
MODEL_ENV = "DOCPROOF_SUBAGENT_MODEL"
# A structured reply is one turn: the prompt asks a question, the answer is the
# JSON. More turns would be a conversation nobody is holding.
MAX_TURNS = 1

_SUBJECT = "The Galley subagent lane"
_REMEDY = "run the verb again"
_INSTALL_HINT = agent_lane.install_hint(_SUBJECT, _REMEDY)
_LOGIN_HINT = agent_lane.login_hint(_SUBJECT, _REMEDY)
_CLI_HINT = agent_lane.cli_hint(_SUBJECT, _REMEDY)

SubagentUnavailable = agent_lane.AgentLaneUnavailable

# Short aliases the practitioner doctrine speaks in, mapped to the ids the CLI
# resolves. Anything else is passed through verbatim.
_ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5",
            "fable": "claude-fable-5-1", "haiku": "claude-haiku-4-5-20251001"}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def resolve_model(model: str | None = None) -> str:
    if model and model.strip():
        m = model.strip()
        return _ALIASES.get(m.lower(), m)
    env = os.environ.get(MODEL_ENV, "").strip()
    return _ALIASES.get(env.lower(), env) if env else DEFAULT_MODEL


def is_subagent_model(model: str | None) -> bool:
    """Whether a model name is one the subagent lane serves (a Claude id or a
    doctrine alias). Used to choose the lane automatically."""
    if not model:
        return False
    m = model.strip().lower()
    return m in _ALIASES or m.startswith("claude")


def _loads(text: str) -> dict[str, Any] | None:
    """json.loads that tolerates what a model reply actually contains: raw
    newlines/tabs inside strings (strict=False) and prose after the object
    (raw_decode stops at the object's end)."""
    try:
        obj, _end = json.JSONDecoder(strict=False).raw_decode(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _salvage_truncated(text: str) -> dict[str, Any] | None:
    """A reply cut off mid-array — `{"findings": [{...}, {...}, {"para_id":
    "body-02` — still holds every complete element before the cut. Walk back
    to each `}` that closes an element and try closing the array and the
    object there; the first that parses is the answer, minus the element the
    truncation ate (a loss of one row, not the whole read)."""
    if not text.lstrip().startswith("{"):
        return None
    end = len(text)
    for _ in range(400):
        cut = text.rfind("}", 0, end)
        if cut <= 0:
            return None
        for closer in ("]}", "}", "]}}", "]]}"):
            obj = _loads(text[:cut + 1] + closer)
            if obj is not None:
                return obj
        end = cut
    return None


def extract_json(text: str) -> dict[str, Any] | None:
    """The JSON object in a model reply: a fenced block first, then the
    outermost {...}, then a truncation salvage. None when nothing parses — the
    caller records a loss, never a half-answer of the wrong shape."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    if first != -1:
        candidates.append(text[first:])
    for c in candidates:
        c = c.strip()
        if not c.startswith("{"):
            continue
        obj = _loads(c)
        if obj is not None:
            return obj
    for c in candidates:
        c = c.strip()
        if c.startswith("{"):
            obj = _salvage_truncated(c)
            if obj is not None:
                log.warning("subagent reply was truncated; salvaged the "
                            "complete elements (%d chars)", len(c))
                return obj
    return None


def availability() -> tuple[bool, str]:
    """(can this machine run the lane, why not) — SDK importable, a login
    on disk, AND the CLI's own word that it is signed in. Never raises.

    The third check is the one the Georgis run was missing: the file check
    is generous by design, and on a machine whose ~/.claude.json existed but
    whose session had lapsed, `--engine auto` chose this lane and the first
    turn failed "Not logged in". `agent_lane.probe_login` asks `claude auth
    status` once per process; an inconclusive probe (no CLI on PATH, a
    timeout) defers to the file check rather than refusing."""
    try:
        agent_lane.sdk(_INSTALL_HINT)
        agent_lane.require_login(_LOGIN_HINT)
    except agent_lane.AgentLaneUnavailable as e:
        return False, str(e)
    logged_in, detail = agent_lane.probe_login()
    if logged_in is False:
        return False, (f"{detail} — sign this machine in with `claude "
                       f"setup-token` (and set CLAUDE_CODE_OAUTH_TOKEN) or "
                       f"`claude auth login`")
    return True, detail


def available() -> bool:
    """Whether this machine can run the lane at all. Never raises."""
    return availability()[0]


_NOT_LOGGED_IN_RE = re.compile(r"not logged in|please run /login|"
                               r"run /login", re.IGNORECASE)


def _not_logged_in(text: str) -> bool:
    """Whether a CLI result is its login refusal rather than an answer."""
    return bool(text) and bool(_NOT_LOGGED_IN_RE.search(text[:400]))


def _usage_of(msg: Any) -> NormalizedUsage:
    """Token counts off a ResultMessage, when the SDK reports them."""
    usage = getattr(msg, "usage", None) or {}
    if not isinstance(usage, dict):
        return NormalizedUsage()
    def n(key: str) -> int:
        v = usage.get(key, 0)
        return int(v) if isinstance(v, (int, float)) else 0
    return NormalizedUsage(
        input_tokens=n("input_tokens"), output_tokens=n("output_tokens"),
        cache_creation_input_tokens=n("cache_creation_input_tokens"),
        cache_read_input_tokens=n("cache_read_input_tokens"))


class SubagentProvider:
    """One fenced Claude Code turn per structured request, on the subscription.

    `sdk` may be injected (tests hand in a fake module); by default the real
    SDK is imported at first use. `cwd` is an empty scratch directory so even a
    hypothetical file tool would see nothing — the turn has no tools anyway."""

    name = "subagent"

    def __init__(self, *, model: str | None = None, sdk: Any = None,
                 cwd: str | Path | None = None, max_turns: int = MAX_TURNS):
        self.model = resolve_model(model)
        self._sdk = sdk
        self._cwd = Path(cwd) if cwd else None
        self.max_turns = max_turns
        self.calls = 0
        self.cost_usd = 0.0

    # -- the Provider protocol ------------------------------------------------

    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        sdk = self._sdk or agent_lane.sdk(_INSTALL_HINT)
        agent_lane.require_login(_LOGIN_HINT)
        target = resolve_model(model) if is_subagent_model(model) else self.model
        prompt = self._prompt(user, schema, schema_name)
        try:
            return asyncio.run(self._turn(sdk, target, system, prompt))
        except RuntimeError as e:
            # Inside a running loop (the app's async job runner) — run the
            # turn on a private loop in a thread.
            if "asyncio.run() cannot be called" not in str(e):
                raise
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(
                    lambda: asyncio.run(self._turn(sdk, target, system,
                                                   prompt))).result()

    def submit_batch(self, *, model: str, requests, **kwargs):  # pragma: no cover
        raise NotImplementedError("the subagent lane has no batch mode")

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _prompt(user: str, schema: dict[str, Any], schema_name: str) -> str:
        return (f"{user}\n\n"
                f"Reply with ONE JSON object named `{schema_name}` matching "
                f"this JSON schema exactly, and nothing else — no prose before "
                f"or after, no markdown fence:\n"
                f"{json.dumps(schema, ensure_ascii=False)}")

    def _options(self, sdk: Any, model: str, system: str) -> Any:
        cwd = self._cwd
        if cwd is None:
            cwd = Path(tempfile.mkdtemp(prefix="docproof-subagent-"))
        return sdk.ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            tools=[],
            allowed_tools=[],
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="bypassPermissions",
            max_turns=self.max_turns,
            cwd=str(cwd),
            env=agent_lane.child_env(),
        )

    async def _turn(self, sdk: Any, model: str, system: str,
                    prompt_text: str) -> ProviderResult:
        options = self._options(sdk, model, system)

        async def prompt():
            yield {"type": "user",
                   "message": {"role": "user", "content": prompt_text}}

        reply = ""
        last_text = ""
        usage = NormalizedUsage()
        try:
            async for msg in sdk.query(prompt=prompt(), options=options):
                if isinstance(msg, sdk.AssistantMessage):
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    spoken = "\n".join(
                        b.text for b in msg.content
                        if isinstance(b, sdk.TextBlock) and b.text.strip())
                    if spoken.strip():
                        last_text = spoken.strip()
                elif isinstance(msg, sdk.ResultMessage):
                    self.cost_usd += float(getattr(msg, "total_cost_usd", 0.0)
                                           or 0.0)
                    usage = _usage_of(msg)
                    result = getattr(msg, "result", None)
                    subtype = getattr(msg, "subtype", "")
                    if result and str(result).strip():
                        reply = str(result).strip()
                    if getattr(msg, "is_error", False) or (
                            subtype and subtype != "success"):
                        log.warning("subagent turn on %s ended with subtype=%s "
                                    "is_error=%s turns=%s", model, subtype,
                                    getattr(msg, "is_error", None),
                                    getattr(msg, "num_turns", None))
                        if _not_logged_in(reply):
                            # The CLI's own "Not logged in · Please run
                            # /login": /login is a slash command nobody
                            # headless can type. Say the command that works.
                            raise agent_lane.AgentLaneUnavailable(
                                f"{_SUBJECT} started a Claude session but the "
                                f"CLI is not logged in ({reply[:80]!r}). Sign "
                                f"this machine in with `claude setup-token` "
                                f"and set CLAUDE_CODE_OAUTH_TOKEN (or run "
                                f"`claude auth login`), then {_REMEDY}.")
        except agent_lane.AgentLaneUnavailable:
            raise
        except sdk.CLINotFoundError as e:
            log.error("subagent lane: Claude Code CLI not found (%s)", e)
            raise agent_lane.AgentLaneUnavailable(_CLI_HINT) from e
        except (sdk.ProcessError, sdk.ResultError) as e:
            log.error("subagent lane: the CLI session failed: %s: %s",
                      type(e).__name__, e)
            raise agent_lane.AgentLaneUnavailable(
                f"{_SUBJECT} could not hold a Claude session ({e}). Sign this "
                f"machine in once with `claude setup-token` or `claude /login`, "
                f"then {_REMEDY}.") from e
        except Exception as e:                              # noqa: BLE001
            self.calls += 1
            log.warning("subagent turn on %s raised %s: %s", model,
                        type(e).__name__, e)
            return ProviderResult(parsed=None, usage=usage,
                                  stop_reason="error",
                                  error=f"{type(e).__name__}: {e}")
        self.calls += 1
        parsed = extract_json(reply) or extract_json(last_text)
        if parsed is None:
            raw = (reply or last_text or "").strip().replace("\n", " ")
            head, tail = raw[:400], raw[-200:]
            log.warning("subagent turn on %s returned no JSON (%d chars); "
                        "reply began: %r … ended: %r", model, len(raw), head,
                        tail)
            return ProviderResult(parsed=None, usage=usage, stop_reason="error",
                                  error="the session's reply held no JSON "
                                        f"object (reply began: {head!r})")
        log.debug("subagent turn on %s ok (%d chars)", model, len(reply))
        return ProviderResult(parsed=parsed, usage=usage, stop_reason="ok")


__all__ = ["DEFAULT_MODEL", "MODEL_ENV", "SubagentProvider",
           "SubagentUnavailable", "availability", "available", "extract_json",
           "is_subagent_model", "resolve_model"]
