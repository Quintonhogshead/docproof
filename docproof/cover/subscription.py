"""Cover Studio's Anthropic roles, answered by the owner's Claude
subscription instead of by metered API credits.

The owner ran Cover Studio on his own machine and art direction died on
"Your credit balance is too low" — a Max subscription sitting right there,
unused, while the studio reached for an API key. docproof.canvas.assistant
already solved this for the AI box: drive the Claude Code CLI through
claude_agent_sdk, which authenticates against the subscription login. This
module is that same lane, shaped to the two interfaces Cover Studio's
Anthropic call sites actually use:

- `SubscriptionProvider` implements docproof.providers.Provider, so
  direction.run_directions / direction.revise_spec / reality.distill_reality
  keep calling `complete_structured` exactly as they do against
  AnthropicProvider. It is the REAL protocol, not a parallel one.
- `SubscriptionAnthropicClient` stands in for the `anthropic.Anthropic`
  instance that critique.py and planner.py talk to directly — a shim over
  the one call shape those two modules make,
  `client.messages.stream(**params).get_final_message()`, including the
  `image` content blocks the critique and stage-review calls send.

Three things are load-bearing here:

- **The billing fence.** `_child_env` blanks ANTHROPIC_API_KEY in the spawned
  CLI's environment, the same fifteen lines assistant.py calls its most
  important guard. DocProof holds vendor keys in this very process; a key
  that leaks into the child turns a $0 subscription turn into a metered API
  bill with no visible symptom — which is the exact failure this module
  exists to prevent.
- **Structure comes from the prompt, not from the wire.** The CLI has no
  structured-output parameter, so the JSON schema each caller already builds
  is appended to its system prompt as a contract, and the reply's first JSON
  object is what gets handed back. Every caller then validates that object
  through its own pydantic model exactly as before, so a model that answers
  badly fails the same way it always did.
- **$0 is reported honestly.** Every usage figure returned here is zero,
  because a subscription turn bills no API dollars — so `cost_of_usage` over
  these calls prices at $0.00. No number is ever invented.

Construction is the fallback point: `preflight()` (and therefore both
constructors) raises SubscriptionUnavailable when the SDK is missing or this
machine has no Claude login, so app/routes/cover.py can choose the API lane
before a job starts rather than mid-run.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence

from ..providers.base import (BatchRequest, BatchStatus, NormalizedUsage,
                              ProviderResult)

log = logging.getLogger("docproof.cover.subscription")

# No tools are registered, so one turn is the whole conversation; two is
# headroom for a CLI that spends a turn on preamble, and a hard stop against
# a confused session spinning on the owner's subscription.
MAX_TURNS = 2

_INSTALL_HINT = (
    "Cover Studio's Claude subscription lane needs the Claude Agent SDK, "
    "which is not installed here — install it with `pip install "
    "claude-agent-sdk` (and the Claude Code CLI it drives), or set "
    "COVER_ANTHROPIC_LANE=api to spend API credits instead.")

_LOGIN_HINT = (
    "Cover Studio's Claude subscription lane needs this machine signed in to "
    "Claude and it is not — run `claude setup-token` in a terminal and set "
    "CLAUDE_CODE_OAUTH_TOKEN, or run `claude` once to sign in, then start the "
    "job again.")

_CLI_HINT = (
    "Cover Studio could not find the Claude Code CLI its subscription lane "
    "drives — install it (`npm install -g @anthropic-ai/claude-code`) and try "
    "again.")


class SubscriptionUnavailable(RuntimeError):
    """This machine cannot run a subscription turn, with a sentence saying
    what to do about it.

    Raised at CONSTRUCTION (see `preflight`) so a caller can pick the API
    lane before a job starts spending, and always carrying a human sentence
    naming the missing piece and the command that fixes it — "no module named
    claude_agent_sdk" is a dead end for the person looking at Cover
    Studio."""


def _sdk() -> Any:
    """The agent SDK, or the sentence that says how to get it.

    Imported through importlib at CALL time, never at module import: the
    cover routes must import on a deployment (Fly, quest) that has no agent
    SDK and no CLI login at all, where the API lane is the only lane."""
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        raise SubscriptionUnavailable(_INSTALL_HINT) from e


def _require_login() -> None:
    """Refuse before spawning anything if this machine has no Claude login.

    Adapted verbatim from docproof.canvas.assistant._require_login, including
    its known limitation: the three places a login can live are the token env
    var, the credentials file, and the CLI's own config, and any one of them
    passes this check — so a machine whose CLI is signed OUT while
    ~/.claude.json still exists passes here and fails later, at the call, as
    a readable "could not start a Claude session" sentence. The check is
    generous on purpose: a false "you are logged in" costs one clear error, a
    false "you are not" costs the lane."""
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
    raise SubscriptionUnavailable(_LOGIN_HINT)


def preflight() -> None:
    """Everything that must be true before this lane is worth choosing.

    The one place app/routes/cover.py asks "can this machine run a
    subscription turn at all", and the same check both constructors below
    make, so the route's lane decision and the objects it builds can never
    disagree."""
    _sdk()
    _require_login()


def _child_env() -> dict[str, str]:
    """The billing guard: the spawned CLI must run on the subscription.

    ClaudeAgentOptions.env is MERGED over the parent's environment by the
    transport (it cannot delete a key), and the CLI reads ANTHROPIC_API_KEY
    with JavaScript truthiness — so blanking it to "" is how you actually
    make it absent. DocProof holds this process's Anthropic and OpenAI keys
    in the same environment, and a key that leaks into this child turns a $0
    subscription turn into a metered API bill with no visible symptom, which
    is the very failure the lane exists to prevent.

    CLAUDE_CODE_OAUTH_TOKEN is deliberately left alone — it is the credential
    this is supposed to run on."""
    return {"ANTHROPIC_API_KEY": ""}


def _default_cwd() -> str:
    """Where the child process is pinned.

    The temp directory rather than the repo or a job folder: no tools are
    registered, so cwd steers nothing but the CLI's own session bookkeeping,
    and pointing it away from the source tree keeps a hypothetical future
    file tool looking at nothing interesting."""
    return tempfile.gettempdir()


def _options(sdk: Any, *, model: str, system: str, cwd: str) -> Any:
    """The child's whole world for one call: a model, a system prompt, and
    no capabilities whatsoever.

    Every field is a fence. `tools=[]` and `allowed_tools=[]` remove Claude
    Code's built-ins, so the only thing this turn can do is answer;
    `mcp_servers={}` with `strict_mcp_config` keeps the repo's own .mcp.json
    out; `setting_sources=[]` keeps its CLAUDE.md and settings out, so an art
    director is not handed a coding agent's instructions; `max_turns` bounds
    a confused session; and `env` is the billing fence above.
    `permission_mode` is set because nobody is watching this subprocess's
    stdin — a prompt would block forever — and with no tools registered there
    is nothing a permission could protect."""
    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        mcp_servers={},
        allowed_tools=[],
        tools=[],
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        cwd=cwd,
        env=_child_env(),
    )


def _user_message(content: Any) -> dict[str, Any]:
    """One streamed user message in the shape the CLI's stdin expects.

    `content` is a plain string or the caller's own content-block list —
    `{"type": "text"}` and `{"type": "image", "source": {...}}` blocks ride
    through unchanged, which is why the critique and stage-review calls can
    send their downscaled renders on this lane at all."""
    return {"type": "user", "session_id": "",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None}


async def _ask(sdk: Any, *, model: str, system: str, content: Any,
               cwd: str) -> str:
    """One subscription turn, returning whatever the model said.

    The ResultMessage's own text is preferred over the assistant blocks (it
    is the CLI's final answer); the last spoken assistant text stands in when
    a result carries none."""
    options = _options(sdk, model=model, system=system, cwd=cwd)
    message = _user_message(content)

    async def prompt():
        yield message

    reply = ""
    last_text = ""
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
                if msg.result and msg.result.strip():
                    reply = msg.result.strip()
    except sdk.CLINotFoundError as e:
        raise SubscriptionUnavailable(_CLI_HINT) from e
    except (sdk.ProcessError, sdk.ResultError) as e:
        # A CLI that exists but cannot hold a session — most commonly "Not
        # logged in", which _require_login cannot see (its own docstring says
        # so). Same remedy, said the same way.
        raise SubscriptionUnavailable(
            f"Cover Studio could not start a Claude session on your "
            f"subscription ({e}). Sign this machine in once with `claude "
            f"setup-token` or `claude /login`, then try again.") from e
    return reply or last_text


def _run_turn(*, model: str, system: str, content: Any, cwd: str) -> str:
    """`_ask` from synchronous code, wherever that code is standing.

    Every call site on this lane is synchronous (run_directions, revise_spec,
    distill_reality, run_critique, plan_composition) and every one of them is
    called DIRECTLY from a pipeline coroutine already running on the server's
    event loop — where `asyncio.run` raises "cannot be called from a running
    event loop". So the turn always gets its own loop on its own thread,
    which is correct whether or not a loop is running here, and blocks the
    caller exactly as long as the synchronous SDK call it replaces did."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = asyncio.run(
                _ask(_sdk(), model=model, system=system, content=content,
                     cwd=cwd))
        except BaseException as e:                          # noqa: BLE001
            box["error"] = e

    thread = threading.Thread(target=work, name="cover-subscription",
                              daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return str(box.get("value") or "")


def _json_contract(schema: dict[str, Any], schema_name: str) -> str:
    """The schema the caller already built, restated as a prompt-side
    contract.

    The CLI takes no `output_config`, so this is where structured output
    comes from on this lane. The schema is the caller's OWN
    `strict_json_schema(...)` dict, unchanged, so the shape asked for here is
    byte-identical to the shape the API lane asks for — and every caller
    still validates the answer through its own pydantic model, so a model
    that ignores this contract fails exactly where it always did."""
    return (
        f"\n\n## Your answer\n\n"
        f"Reply with ONE JSON object called {schema_name} and nothing else: "
        f"no prose before or after it, no markdown code fence, no "
        f"commentary. It must validate against this JSON Schema, with every "
        f"listed property present:\n\n"
        f"{json.dumps(schema, separators=(',', ':'))}")


def _first_json_object(text: str) -> str | None:
    """The first complete JSON object in a reply, or None.

    A CLI turn is chattier than a structured-output endpoint — a fence, a
    sentence of preamble — so the object is found by brace matching (string
    literals and escapes respected) rather than by trusting the whole reply
    to be JSON."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _reply_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """(the object, None) or (None, a sentence saying what came back
    instead). Never raises: both callers below have their own way of
    reporting a bad reply, and neither wants an exception from the parse."""
    if not text.strip():
        return None, "the model answered with nothing at all"
    body = _first_json_object(text)
    if body is None:
        return None, f"no JSON object in the reply: {text.strip()[:200]!r}"
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"unparseable JSON: {e}"
    if not isinstance(parsed, dict):
        return None, "the model's JSON was not an object"
    return parsed, None


def _dropped(**params: Any) -> None:
    """Log the API-only knobs this lane cannot honor.

    `max_tokens`, `effort` and the structured-output config are real
    parameters on the Messages API and have no CLI equivalent: the child
    process owns its own output ceiling and its own thinking budget. They are
    accepted and dropped rather than refused, because refusing would mean
    every call site needs a second code path for a difference none of them
    can act on — and logged rather than swallowed, so a run that behaves
    unlike its API twin says why in the log."""
    named = ", ".join(f"{k}={v!r}" for k, v in params.items() if v is not None)
    if named:
        log.debug("subscription lane: dropped API-only parameters (%s) — the "
                  "Claude CLI has no equivalent.", named)


def _zero_usage() -> NormalizedUsage:
    """No tokens, because no tokens were billed.

    A subscription turn costs no API dollars, and the CLI reports no token
    counts docproof could price anyway. Zeros make cost_of_usage answer
    $0.00, which is the truth; an estimate here would be a number nobody
    owes."""
    return NormalizedUsage()


class SubscriptionProvider:
    """docproof.providers.Provider, answered on the Claude subscription.

    Constructed per role by app/routes/cover.py exactly where
    AnthropicProvider used to be, and called through the same
    `complete_structured` the API provider implements — the model id rides
    each call, so one instance serves whichever role holds it.

    Raises SubscriptionUnavailable from `__init__` when this machine cannot
    run a subscription turn, so the route falls back to the API lane before a
    job starts rather than mid-run."""

    name = "anthropic-subscription"

    def __init__(self, *, cwd: str | Path | None = None):
        preflight()
        self.cwd = str(cwd) if cwd else _default_cwd()

    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        """One structured call, with the schema carried in the prompt.

        `max_tokens` is accepted and dropped (see `_dropped`). Failures come
        back as a ProviderResult with `stop_reason="error"` rather than as an
        exception — the same contract AnthropicProvider.complete_structured
        keeps, and the reason direction.py's "the model did not return any
        cover directions: <error>" sentence reads the same on either lane.
        SubscriptionUnavailable is the one thing raised rather than returned:
        it names a machine problem (no CLI, no login) rather than a bad
        answer, and every caller's own wrapper turns it into that call's
        readable failure with the fix still in the sentence."""
        _dropped(max_tokens=max_tokens)
        try:
            text = _run_turn(model=model,
                             system=system + _json_contract(schema,
                                                            schema_name),
                             content=user, cwd=self.cwd)
        except SubscriptionUnavailable:
            raise
        except Exception as e:                              # noqa: BLE001
            return ProviderResult(usage=_zero_usage(), stop_reason="error",
                                  error=f"the Claude subscription call "
                                        f"failed: {e}")
        parsed, problem = _reply_json(text)
        if parsed is None:
            return ProviderResult(usage=_zero_usage(), stop_reason="error",
                                  error=problem)
        return ProviderResult(parsed=parsed, usage=_zero_usage())

    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str:
        raise NotImplementedError(
            "The Claude subscription lane has no batch endpoint — run this "
            "work on the API lane (COVER_ANTHROPIC_LANE=api).")

    def poll_batch(self, batch_id: str) -> BatchStatus:
        raise NotImplementedError(
            "The Claude subscription lane has no batch endpoint.")

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        raise NotImplementedError(
            "The Claude subscription lane has no batch endpoint.")


# critique.py and planner.py talk to the `anthropic` SDK directly, because the
# Provider protocol is text-only and both send images. They touch exactly one
# call shape between them — `with client.messages.stream(**params) as s:
# s.get_final_message()` — and read exactly four things off the answer:
# `.usage`, `.stop_reason`, and `.content` blocks with `.type` and `.text`.
# That is the whole surface reproduced below; nothing else about
# anthropic.Anthropic is imitated, because nothing else is used.

class _Usage:
    """Zero tokens, in the shape critique._usage/planner._usage read."""

    input_tokens = 0
    output_tokens = 0
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _TextBlock:
    """One content block, in the shape `b.type == "text"` walks."""

    type = "text"

    def __init__(self, text: str):
        self.text = text


class _Message:
    """The Message both callers read: a stop reason that is neither
    "refusal" nor "max_tokens", one text block holding the JSON object the
    turn produced, and zero usage."""

    stop_reason = "end_turn"

    def __init__(self, text: str):
        self.content = [_TextBlock(text)]
        self.usage = _Usage()


class _Stream:
    """The context manager `client.messages.stream(...)` returns.

    The turn runs inside `get_final_message()` rather than on `__enter__`, so
    a caller that opens the stream and never reads it spends nothing."""

    def __init__(self, client: "SubscriptionAnthropicClient",
                 params: dict[str, Any]):
        self._client = client
        self._params = params

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get_final_message(self) -> _Message:
        return self._client.final_message(self._params)


class _Messages:
    """`client.messages`, with the one method the two callers use."""

    def __init__(self, client: "SubscriptionAnthropicClient"):
        self._client = client

    def stream(self, **params: Any) -> _Stream:
        return _Stream(self._client, params)


class SubscriptionAnthropicClient:
    """An `anthropic.Anthropic` stand-in for critique.py and planner.py,
    answered on the Claude subscription.

    Built by app/routes/cover.py's `_critique_client` in place of the real
    client, and threaded through the pipeline unchanged — those two modules
    never learn which lane they are on. Raises SubscriptionUnavailable from
    `__init__` on a machine that cannot run a subscription turn, the same
    construction-time contract SubscriptionProvider keeps."""

    def __init__(self, *, cwd: str | Path | None = None):
        preflight()
        self.cwd = str(cwd) if cwd else _default_cwd()
        self.messages = _Messages(self)

    def final_message(self, params: dict[str, Any]) -> _Message:
        """One turn from one `messages.stream(**params)` request.

        `model` and the message content (image blocks included) pass straight
        through; `system` gains the schema contract taken from the caller's
        own `output_config`; `max_tokens` and the effort dial are dropped
        (see `_dropped`). A reply with no JSON object in it raises — critique
        and planner both wrap that into their own readable error, and neither
        has a way to be handed "no verdict" as a value."""
        _dropped(max_tokens=params.get("max_tokens"),
                 effort=(params.get("output_config") or {}).get("effort"))
        schema = ((params.get("output_config") or {}).get("format") or {}
                  ).get("schema")
        system = _system_text(params.get("system"))
        if isinstance(schema, dict):
            system += _json_contract(schema, "answer")
        text = _run_turn(model=str(params.get("model") or ""), system=system,
                         content=_flatten_messages(params.get("messages")),
                         cwd=self.cwd)
        parsed, problem = _reply_json(text)
        if parsed is None:
            raise RuntimeError(
                f"the Claude subscription lane's reply was not the JSON this "
                f"call needs — {problem}")
        return _Message(json.dumps(parsed))


def _system_text(system: Any) -> str:
    """The system prompt as one string.

    Both cover callers pass a plain string, but AnthropicProvider passes a
    list of text blocks when prompt caching is on — reading both means this
    shim cannot be broken by a caller that starts caching."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(str(b.get("text", "")) for b in system
                           if isinstance(b, dict))
    return ""


def _flatten_messages(messages: Any) -> Any:
    """The request's messages as one user-message content list.

    Both callers send exactly one user message whose content is already a
    list of `text` and `image` blocks — those pass through UNCHANGED, which
    is what lets the critique call's render and 100px thumbnail reach the
    model on this lane. A multi-message request (nothing sends one today)
    is flattened in order with its roles labeled, so a prior assistant turn
    reads as context rather than as a fresh instruction."""
    if not isinstance(messages, list) or not messages:
        return ""
    blocks: list[Any] = []
    single = len(messages) == 1
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content")
        if not single:
            blocks.append({"type": "text", "text": f"[{role}]"})
        if isinstance(content, str):
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(content)
    return blocks


__all__ = ["MAX_TURNS", "SubscriptionAnthropicClient", "SubscriptionProvider",
           "SubscriptionUnavailable", "preflight"]
