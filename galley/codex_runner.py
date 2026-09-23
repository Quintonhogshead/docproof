"""Serialized, subscription-only Astra calls through the supported Codex CLI.

This transport does not use the Responses API or copy authentication tokens.
Only a completed, hash-matching result can be reused. Confirmed exited failures
can resume within their original saved allowance; ambiguous generations remain
protected from duplicate submission.
"""
from __future__ import annotations

from docproof import platform_io as fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from galley.astra_review import AstraReviewError, MODEL, REASONING_EFFORT, _schema_check

PROTOCOL_VERSION = 1
MAX_AUTOMATIC_ATTEMPTS = 3
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_TAIL_BYTES = 64 * 1024
_EVENT_TYPES = {"thread.started", "turn.started", "turn.completed", "turn.failed",
                "item.started", "item.updated", "item.completed", "error"}
# `codex exec --help` documents --disable; these capabilities are listed by
# `codex features list`. Fixed readers need one structured answer, not tools.
_FIXED_READER_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "apps",
    "plugins", "browser_use", "computer_use", "image_generation", "view_image",
    "goals", "sleep_tool", "skill_search", "code_mode", "code_mode_host",
)
# Codex 0.153 emits this startup notice even when code_mode was explicitly
# disabled for a fixed reader. It confirms a tool is unavailable; it is not a
# tool invocation or a failed model turn. All other error items still fail closed.
_DISABLED_CODE_MODE_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
    "enable `features.code_mode_host` and install `codex-code-mode-host`."
)
_ENV_KEYS = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TMP",
             "TEMP", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "SYSTEMROOT",
             "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PATHEXT", "COMSPEC", "WINDIR"}
_AUTH_OPTIONS = ["-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                 "-c", 'cli_auth_credentials_store="file"']


class _StartError(OSError):
    """Popen failed before a child existed, so generation is safe to retry."""


class CodexRetryableError(AstraReviewError):
    """A confirmed exited attempt can resume within its saved original budget."""
    retryable = True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fcntl.private_path(path, 0o700)
    return path


def _atomic(path: Path, value: Any) -> None:
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fcntl.sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _load(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise ValueError("too large")
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise AstraReviewError(f"Cannot read the saved Codex review file: {path.name}") from exc


def codex_home() -> Path:
    """Keep the worker's refreshable login separate from interactive Codex."""
    explicit = os.environ.get("GALLEY_CODEX_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("FLY_APP_NAME") or os.environ.get("FLY_MACHINE_ID"):
        return Path("/data/galley-codex")
    return Path.home() / ".galley" / "codex"


def child_env(home: Path) -> dict[str, str]:
    """An allowlist excludes every inherited API key, OAuth token and endpoint.

    Auth is read by Codex from its own dedicated cache. In particular, a parent
    process's OPENAI_API_KEY, CODEX_API_KEY, provider overrides, and Claude login
    can never turn this subscription request into a metered provider request.
    """
    env = {key: value for key, value in os.environ.items()
           if key.upper() in _ENV_KEYS or key.startswith("LC_")}
    env.update(CODEX_HOME=str(home), NO_COLOR="1", TERM="dumb")
    return env


def request_directory(work_dir: Path, request_id: str) -> Path:
    return Path(work_dir).resolve() / "codex-requests" / hashlib.sha256(
        request_id.encode("utf-8")).hexdigest()[:32]


def _binary(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("GALLEY_CODEX_BIN") or shutil.which("codex")
    if not candidate:
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        candidate = str(bundled) if bundled.is_file() else None
    located = shutil.which(candidate) if candidate else None
    if not located:
        raise AstraReviewError("Codex CLI is not installed in the Galley worker environment.")
    return located


def normalize_schema(schema: dict) -> dict:
    """The same schema in the runner's small vocabulary, or the input unchanged.

    A schema generated from a pydantic model (`strict_json_schema`) writes
    nested models as `$defs` with `$ref` pointers and a fixed literal as
    `const`; the hand-written fixed-lane schemas never do. Both mean exactly
    what an inlined definition and a one-value `enum` mean, so they are
    rewritten to that rather than refused. On 2026-09-16, the first day Luna
    read through this transport, every typed-detector and Story Sheet read
    (694 of Kyler 2's 2,198) was refused for these keys and skipped, while
    every hand-written schema went through. Anything else unsupported still
    fails `_check_schema`."""
    if not isinstance(schema, dict):
        return schema
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        definitions = {}

    def rewrite(node, depth=0):
        if depth > 32:
            raise AstraReviewError("Codex review output schema nests too deeply.")
        if isinstance(node, list):
            return [rewrite(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = definitions.get(ref[len("#/$defs/"):])
            if not isinstance(target, dict):
                raise AstraReviewError("Codex review output schema references an unknown definition.")
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return rewrite(merged, depth + 1)
        out = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            if key == "const":
                out["enum"] = [value]
                continue
            out[key] = rewrite(value, depth + 1)
        return out

    return rewrite(schema)


def _check_schema(schema: dict) -> None:
    """The same strict, deliberately small schema vocabulary as Astra review."""
    if not isinstance(schema, dict) or schema.get("type") not in {
            "object", "array", "string", "integer", "boolean"}:
        raise AstraReviewError("Unsupported Codex review output schema.")
    allowed = {"type", "enum", "description", "title"}
    if schema["type"] == "object":
        allowed |= {"properties", "required", "additionalProperties"}
        props = schema.get("properties")
        required = schema.get("required")
        if (not isinstance(props, dict) or not isinstance(required, list)
                or len(required) != len(set(required)) or set(required) != set(props)
                or schema.get("additionalProperties") is not False):
            raise AstraReviewError("Codex review objects require every field and forbid extra fields.")
        for value in props.values():
            _check_schema(value)
    elif schema["type"] == "array":
        allowed.add("items")
        _check_schema(schema.get("items"))
    if set(schema) - allowed:
        raise AstraReviewError("Unsupported Codex review output schema constraint.")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise AstraReviewError("Codex review output schema has an invalid enum.")


@contextmanager
def _serialized(home: Path, deadline: float):
    _private_dir(home)
    lock = home / ".galley-review.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AstraReviewError("The subscription reviewer is busy; no new request was submitted.")
                time.sleep(min(0.2, remaining))
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _tail(stream) -> str:
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - _TAIL_BYTES))
    return stream.read(_TAIL_BYTES).decode("utf-8", errors="replace")


def _safe_events(stream) -> dict:
    """Keep accounting only. Agent text and command/tool output are discarded."""
    stream.seek(0)
    counts: dict[str, int] = {}
    metadata: dict[str, Any] = {"event_counts": counts}
    oversized = False
    while line := stream.readline(_TAIL_BYTES + 1):
        if len(line) > _TAIL_BYTES or oversized:
            oversized = not line.endswith(b"\n")
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") not in _EVENT_TYPES:
            continue
        kind = event["type"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind.startswith("item.") and isinstance(event.get("item"), dict):
            item_type = event["item"].get("type")
            if item_type == "error" and event["item"].get("message") == _DISABLED_CODE_MODE_NOTICE:
                metadata["disabled_code_mode_notice"] = True
            elif isinstance(item_type, str) and item_type not in {"agent_message", "reasoning"}:
                items = metadata.setdefault("non_response_item_types", {})
                items[item_type] = items.get(item_type, 0) + 1
        if kind == "thread.started" and isinstance(event.get("thread_id"), str) and re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", event["thread_id"]):
            metadata["thread_id"] = event["thread_id"]
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {key: value for key, value in event["usage"].items()
                     if key in {"input_tokens", "cached_input_tokens", "output_tokens"}
                     and type(value) is int and value >= 0}
            accumulated = metadata.setdefault("usage", {})
            for key, value in usage.items():
                accumulated[key] = accumulated.get(key, 0) + value
    return metadata


def _failure_category(text: str) -> str:
    lowered = text.lower()
    if any(s in lowered for s in ("cancelled by user", "canceled by user",
                                   "user cancelled", "user canceled")):
        return "cancelled"
    if any(s in lowered for s in ("usage limit", "rate limit", "quota", "usage_limit")):
        return "subscription_limit"
    if any(s in lowered for s in ("not logged in", "unauthorized", "authentication", "refresh token", "login")):
        return "authentication"
    if "context" in lowered and any(s in lowered for s in ("limit", "exceed", "length", "large")):
        return "context_limit"
    if "model" in lowered and any(s in lowered for s in ("not found", "not supported", "unavailable", "access")):
        return "model_unavailable"
    return "cli_failure"


def _execute(argv: list[str], *, prompt: str | None, env: dict[str, str], cwd: Path,
             timeout: float) -> dict:
    """Unlinked temporary transcripts prevent log leaks and unbounded RAM use."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == "nt" else {"start_new_session": True})
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                    env=env, cwd=cwd, **options)
        except OSError as exc:
            raise _StartError("Codex process did not start") from exc
        with fcntl.process_job(proc):
            timed_out = False
            try:
                proc.communicate(None if prompt is None else prompt.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                fcntl.terminate_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    fcntl.terminate_process_tree(proc, force=True)
                    proc.communicate(timeout=5)
        return {"returncode": proc.returncode, "timed_out": timed_out,
                "stdout_tail": _tail(stdout), "stderr_tail": _tail(stderr),
                "events": _safe_events(stdout)}


def _cached_result(directory: Path, receipt: dict, request_sha256: str, schema: dict) -> dict:
    if receipt.get("request_sha256") != request_sha256:
        raise AstraReviewError("Saved Codex request differs from the requested manuscript evidence.")
    if receipt.get("status") != "completed":
        raise AstraReviewError("A previous Codex review request did not complete. No automatic retry was submitted.")
    if receipt.get("no_tools") and not _fixed_response_complete(receipt):
        raise AstraReviewError("Saved fixed-reader receipt includes tools or an incomplete turn.")
    result = _load(directory / "result.json")
    if receipt.get("result_sha256") != _hash(result):
        raise AstraReviewError("Saved Codex review result has changed; it cannot be reused.")
    _schema_check(result, schema, "Codex review")
    return result


def _fixed_response_complete(receipt: dict) -> bool:
    counts = receipt.get("event_counts") or {}
    return (not receipt.get("non_response_item_types") and
            counts.get("turn.completed") == 1 and not counts.get("turn.failed"))


def _retry_allowed(receipt: dict) -> bool:
    budget = receipt.get("execution_budget") or {}
    seconds, used = budget.get("timeout_seconds"), budget.get("elapsed_seconds")
    attempt, maximum = receipt.get("attempt"), budget.get("max_attempts")
    return (receipt.get("status") == "operational_failure"
            and (receipt.get("process_exited") is True or
                 receipt.get("execution_kind") == "app_server" and receipt.get("turn_terminal") is True)
            and type(receipt.get("exit_code")) is int
            and receipt.get("failure_category") in {
                "cli_failure", "invalid_output", "authentication", "subscription_limit"}
            and type(seconds) in (int, float) and math.isfinite(seconds)
            and type(used) in (int, float) and math.isfinite(used)
            and 0 <= used < seconds
            and type(attempt) is int and type(maximum) is int
            and 1 <= attempt < maximum <= MAX_AUTOMATIC_ATTEMPTS)


def _archive_automatic_retry(directory: Path, receipt: dict) -> dict:
    """Preserve failed output before making the same frozen request runnable."""
    archives = _private_dir(directory / "attempts")
    archive = Path(tempfile.mkdtemp(prefix=f"{receipt['attempt']:04d}-", dir=archives))
    for filename in ("request.json", "receipt.json", "schema.json", "final.json", "result.json"):
        source = directory / filename
        if source.exists():
            destination = archive / filename
            shutil.copyfile(source, destination)
            fcntl.private_path(destination, 0o600)
            with destination.open("rb+") as copied:
                os.fsync(copied.fileno())
    authorization = {"kind": "automatic_within_original_budget",
                     "previous_receipt_sha256": _hash(receipt),
                     "previous_attempt": receipt["attempt"],
                     "next_attempt": receipt["attempt"] + 1,
                     "failure_category": receipt["failure_category"],
                     "recorded_at": _now(), "archive": str(archive)}
    _atomic(archive / "retry-authorization.json", authorization)
    for filename in ("final.json", "result.json"):
        (directory / filename).unlink(missing_ok=True)
    replacement = {"protocol_version": PROTOCOL_VERSION,
                   "request_sha256": receipt["request_sha256"],
                   "transport": "codex_subscription", "model": receipt["model"],
                   "reasoning_effort": receipt["reasoning_effort"],
                   "status": "preflight", "submitted": False,
                   "attempt": receipt["attempt"] + 1,
                   "execution_budget": dict(receipt["execution_budget"]),
                   "retry_authorization": authorization, "created_at": _now()}
    if receipt.get("no_tools"):
        replacement["no_tools"] = True
    _atomic(directory / "receipt.json", replacement)
    return replacement


def _adopt_completed_output(directory: Path, receipt: dict, schema: dict) -> dict | None:
    """Close the exit-to-receipt crash window without submitting another call."""
    if (receipt.get("status") != "running" or not (receipt.get("process_exited") is True or
            receipt.get("execution_kind") == "app_server" and receipt.get("turn_terminal") is True)
            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
            or not (receipt.get("event_counts") or {}).get("turn.completed")):
        return None
    if receipt.get("no_tools") and not _fixed_response_complete(receipt):
        return None
    result_path, output_path = directory / "result.json", directory / "final.json"
    try:
        if result_path.is_file() and receipt.get("result_sha256"):
            result = _load(result_path)
            if _hash(result) != receipt["result_sha256"]:
                return None
        elif output_path.is_file() and receipt.get("output_sha256"):
            if hashlib.sha256(output_path.read_bytes()).hexdigest() != receipt["output_sha256"]:
                return None
            result = _load(output_path)
        else:
            return None
        _schema_check(result, schema, "Codex review")
    except (OSError, AstraReviewError):
        return None
    _atomic(result_path, result)
    receipt.update(status="completed", result_sha256=_hash(result),
                   recovered_completion_at=_now())
    _atomic(directory / "receipt.json", receipt)
    return result


def _check_login_locked(binary: str, *, env: dict[str, str], cwd: Path, deadline: float) -> None:
    """Caller already holds the shared authentication-cache lock."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AstraReviewError("Codex login check timed out; no model request was submitted.")
    try:
        auth = _execute([binary, *_AUTH_OPTIONS, "login", "status"], prompt=None, env=env,
                        cwd=cwd, timeout=min(30, remaining))
    except OSError as exc:
        raise AstraReviewError("Cannot start the Codex CLI authentication check.") from exc
    auth_text = auth["stdout_tail"] + "\n" + auth["stderr_tail"]
    if (auth["returncode"] != 0 or auth["timed_out"] or not re.search(
            r"\blogged in using chatgpt\b", auth_text, re.IGNORECASE)
            or re.search(r"\busing (?:an? )?api[ -]?key\b", auth_text, re.IGNORECASE)):
        raise AstraReviewError("The Galley Codex worker needs a ChatGPT subscription login in "
                               "its dedicated GALLEY_CODEX_HOME. No model request was submitted.")


def check_login(*, codex_bin: str | None = None, timeout_seconds: int = 30) -> None:
    """Check subscription authentication before starting expensive book work.

    This performs only ``codex login status`` using the same isolated credentials
    and serialized cache as the reviewer. It neither submits a model request nor
    exposes authentication output, and it does not establish remaining allowance.
    """
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise AstraReviewError("The Codex login timeout must be a positive number of seconds.")
    deadline = time.monotonic() + timeout_seconds
    home = codex_home()
    with _serialized(home, deadline):
        _check_login_locked(_binary(codex_bin), env=child_env(home), cwd=home, deadline=deadline)


def reset_failed_request(work_dir: Path, request_id: str, *, reason: str) -> dict:
    """Explicitly authorize one fresh attempt after a confirmed exited failure.

    This is an operator action, never called automatically. A running or ambiguous
    request cannot be reset. Prior evidence and output stay in an immutable
    attempt archive before the current receipt becomes eligible to run again.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise AstraReviewError("Resetting a failed Codex request requires a short operator reason.")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        raise AstraReviewError("The Codex review request ID is invalid.")
    directory = request_directory(work_dir, request_id)
    with _serialized(codex_home(), time.monotonic() + 30):
        old = _load(directory / "receipt.json")
        request = _load(directory / "request.json")
        if (not isinstance(old, dict) or not isinstance(request, dict)
                or request.get("request_id") != request_id
                or old.get("request_sha256") != _hash(request)):
            raise AstraReviewError("The failed Codex request does not match its saved evidence.")
        if (old.get("status") != "operational_failure" or old.get("process_exited") is not True
                or type(old.get("exit_code")) is not int):
            raise AstraReviewError("Only a confirmed exited Codex failure can be reset; "
                                   "running, ambiguous, and completed requests remain protected.")
        attempt = old.get("attempt", 1)
        if type(attempt) is not int or attempt < 1:
            raise AstraReviewError("The failed Codex attempt number is invalid.")
        archives = _private_dir(directory / "attempts")
        archive = Path(tempfile.mkdtemp(prefix=f"{attempt:04d}-", dir=archives))
        for filename in ("request.json", "receipt.json", "schema.json", "final.json", "result.json"):
            source = directory / filename
            if source.exists():
                destination = archive / filename
                shutil.copyfile(source, destination)
                fcntl.private_path(destination, 0o600)
                with destination.open("rb+") as copied:
                    os.fsync(copied.fileno())
        authorization = {"request_id": request_id, "request_sha256": old["request_sha256"],
                         "previous_receipt_sha256": _hash(old), "previous_attempt": attempt,
                         "next_attempt": attempt + 1, "reason": reason.strip(),
                         "authorized_at": _now(), "archive": str(archive)}
        _atomic(archive / "retry-authorization.json", authorization)
        # Do not make the request runnable until every prior output is archived.
        for filename in ("final.json", "result.json"):
            (directory / filename).unlink(missing_ok=True)
        replacement = {
            "protocol_version": PROTOCOL_VERSION, "request_sha256": old["request_sha256"],
            "transport": "codex_subscription", "model": request["model"], "reasoning_effort": request["reasoning_effort"],
            "status": "preflight", "submitted": False, "attempt": attempt + 1,
            "retry_authorization": authorization, "created_at": _now()}
        if request.get("no_tools"):
            replacement["no_tools"] = True
        _atomic(directory / "receipt.json", replacement)
        return {"status": "retry_authorized", **authorization}


def _resource_receipt(receipt, *, reused=False):
    """Mirror frozen transport receipts into the parent book's usage ledger."""
    from docproof.resource_ledger import append_usage
    operation = "codex:" + receipt["request_sha256"]
    status = receipt.get("status")
    append_usage(receipt_id=operation + ":" + str(receipt.get("attempt", 1)),
                 operation_id=operation, model=receipt.get("model", "unknown"),
                 transport="codex_subscription", usage=receipt.get("usage"),
                 status=("started" if status in ("preflight", "running") else
                         "completed" if status == "completed" else "error"),
                 reused=reused, effort=receipt.get("reasoning_effort"),
                 submitted=receipt.get("submitted"),
                 failure_category=receipt.get("failure_category"),
                 started_at=receipt.get("started_at"),
                 finished_at=receipt.get("finished_at"))


def run_structured(prompt: str, schema: dict, work_dir: Path, *, request_id: str,
                   timeout_seconds: int = 1800, codex_bin: str | None = None,
                   model: str = MODEL, reasoning_effort: str = REASONING_EFFORT,
                   no_tools: bool = False, session=None) -> dict:
    """Return a structured answer using the worker's ChatGPT login.

    Legacy CLI calls serialize the shared authentication cache. Fixed readers
    can share an app-server session: authentication remains single-owner while
    independent turns run concurrently under per-request locks.
    The timeout includes queueing. Confirmed failed generations can resume the
    same request within a persisted time/attempt envelope; completed requests
    are reused and ambiguous legacy generations are never auto-replayed.
    A result is an editorial input, never itself a human-proofreading verdict.
    """
    if (Path(work_dir) / 'cancel-review.txt').exists():
        raise AstraReviewError('This local review was cancelled before model submission. Inspect cancel-review.txt.')
    if not isinstance(prompt, str) or not prompt.strip():
        raise AstraReviewError("The Codex review prompt is empty.")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        raise AstraReviewError("The Codex review request ID is invalid.")
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise AstraReviewError("The Codex review timeout must be a positive number of seconds.")
    allowed = {MODEL: {'low', 'medium', 'high', 'xhigh', 'max'},
               'gpt-5.6-luna': {'low', 'medium', 'high', 'xhigh', 'max'},
               'gpt-5.6-sol': {'low', 'medium', 'high', 'xhigh', 'max'},
               'gpt-6-luna': {'low', 'medium', 'high', 'xhigh', 'max'},
               'gpt-6-sol': {'low', 'medium', 'high', 'xhigh', 'max'}}
    if model not in allowed or reasoning_effort not in allowed[model]:
        raise AstraReviewError("Unsupported subscription model or reasoning effort.")
    schema = normalize_schema(schema)
    _check_schema(schema)
    if schema["type"] != "object":
        raise AstraReviewError("The Codex review output must be a structured object.")
    request = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
               "transport": "codex_subscription", "model": model,
               "reasoning_effort": reasoning_effort, "prompt": prompt, "schema": schema}
    if type(no_tools) is not bool:
        raise AstraReviewError("The fixed-reader no_tools option must be boolean.")
    if no_tools:
        request["no_tools"] = True
    evidence_path = Path(work_dir).resolve() / 'astra-evidence.json'
    evidence = _load(evidence_path) if evidence_path.exists() else None
    if no_tools and evidence is not None:
        raise AstraReviewError("A fixed response reader cannot attach model-accessible evidence tools.")
    if evidence is not None:
        request['evidence'] = evidence
    request_sha256 = _hash(request)
    started = time.monotonic()
    deadline = started + timeout_seconds
    home = codex_home()
    directory = request_directory(work_dir, request_id)
    if session is not None and not no_tools:
        raise AstraReviewError("Shared subscription sessions are only for fixed readers without tools")
    with _serialized(directory if session is not None else home, deadline):
        _private_dir(directory.parent)
        _private_dir(directory)
        request_path, receipt_path = directory / "request.json", directory / "receipt.json"
        if request_path.exists():
            if _hash(_load(request_path)) != request_sha256:
                raise AstraReviewError("Codex request ID was already used for different evidence.")
        else:
            _atomic(request_path, request)
        retry_fields: dict[str, Any] = {"attempt": 1}
        unsubmitted_receipt = False
        budget = {"timeout_seconds": timeout_seconds, "elapsed_seconds": 0.0,
                  "max_attempts": MAX_AUTOMATIC_ATTEMPTS}
        if receipt_path.exists():
            receipt = _load(receipt_path)
            if not isinstance(receipt, dict) or receipt.get("request_sha256") != request_sha256:
                raise AstraReviewError("Saved Codex review receipt does not match its request.")
            if no_tools and receipt.get("no_tools") is not True:
                raise AstraReviewError("Saved fixed-reader receipt does not preserve its no-tools contract.")
            if receipt.get("status") == "completed":
                result = _cached_result(directory, receipt, request_sha256, schema)
                # A crash may have written the transport completion before its
                # matching book-ledger completion. This append is idempotent.
                _resource_receipt(receipt)
                _resource_receipt(receipt, reused=True)
                return result
            recovered = _adopt_completed_output(directory, receipt, schema)
            if recovered is not None:
                _resource_receipt(receipt)
                return recovered
            if receipt.get("submitted") is not False:
                if not _retry_allowed(receipt):
                    return _cached_result(directory, receipt, request_sha256, schema)
                receipt = _archive_automatic_retry(directory, receipt)
            unsubmitted_receipt = receipt.get("submitted") is False
            retry_fields = {key: receipt[key] for key in ("attempt", "retry_authorization") if key in receipt}
            if receipt.get("execution_budget"):
                saved = receipt["execution_budget"]
                seconds, used = saved.get("timeout_seconds"), saved.get("elapsed_seconds")
                maximum = saved.get("max_attempts")
                if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                        or type(used) not in (int, float) or not math.isfinite(used)
                        or not 0 <= used <= seconds
                        or type(maximum) is not int or not 1 <= maximum <= MAX_AUTOMATIC_ATTEMPTS):
                    raise AstraReviewError("Saved Codex execution allowance is invalid.")
                budget = {"timeout_seconds": min(seconds, timeout_seconds),
                          "elapsed_seconds": used, "max_attempts": maximum}
        used_before = budget["elapsed_seconds"]
        deadline = min(deadline, started + budget["timeout_seconds"] - used_before)
        if deadline <= time.monotonic():
            raise AstraReviewError("Codex request exhausted its original execution allowance.")
        schema_path, output_path = directory / "schema.json", directory / "final.json"
        _atomic(schema_path, schema)
        # The receipt explicitly proves no generation was submitted. Retain
        # unexpected preflight output for diagnosis, but do not let it block
        # the unchanged, authorized request or treat it as a reviewed result.
        if output_path.exists():
            if not unsubmitted_receipt:
                raise AstraReviewError("Codex output without a submission receipt cannot be safely reconciled.")
            orphan_dir = _private_dir(directory / "preflight-output")
            orphan = orphan_dir / (str(time.time_ns()) + ".json")
            os.replace(output_path, orphan)
            fcntl.private_path(orphan, 0o600)
        receipt = {"protocol_version": PROTOCOL_VERSION, "request_sha256": request_sha256,
                   "transport": "codex_subscription", "model": model,
                   "reasoning_effort": reasoning_effort, "status": "preflight",
                   "submitted": False, "created_at": _now(),
                   "execution_budget": budget, **retry_fields}
        if no_tools:
            receipt["no_tools"] = True

        def record_elapsed(*, exhausted=False):
            budget["elapsed_seconds"] = (budget["timeout_seconds"] if exhausted else
                used_before + max(0.0, time.monotonic() - started))

        _atomic(receipt_path, receipt)
        _resource_receipt(receipt)
        binary = _binary(codex_bin)
        env = child_env(home)
        try:
            if session is None:
                _check_login_locked(binary, env=env, cwd=directory, deadline=deadline)
            else:
                session.check_login(binary, home, deadline)
        except AstraReviewError:
            record_elapsed()
            receipt.update(status="preflight_failed", failure_category="authentication")
            _atomic(receipt_path, receipt)
            _resource_receipt(receipt)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AstraReviewError("Codex review timed out during preflight; no model request was submitted.")
        argv = [binary, "exec", "--model", model, "-c", f'model_reasoning_effort="{reasoning_effort}"',
                *_AUTH_OPTIONS, "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--ephemeral", "-c", 'approval_policy="never"',
                "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                "--json", "-"]
        if no_tools:
            # Official config reference: web_search="disabled" removes the
            # search tool; an empty MCP table prevents inherited server tools.
            # https://learn.chatgpt.com/docs/config-file/config-reference
            overrides = ["-c", 'web_search="disabled"', "-c", "mcp_servers={}",
                         "-c", "apps._default.enabled=false"]
            for feature in _FIXED_READER_DISABLED_FEATURES:
                overrides.extend(["--disable", feature])
            argv[2:2] = overrides
        if evidence is not None:
            # Only this request's frozen JSON/image registry is exposed. The
            # server has no command execution, writes, credentials or network tools.
            registry = directory / 'evidence.json'
            _atomic(registry, evidence)
            configuration = {
                'command': sys.executable,
                'args': ['-m', 'docproof.interior.evidence_server', '--manifest', str(registry)],
                'cwd': str(Path(__file__).resolve().parent.parent),
                'required': True, 'startup_timeout_sec': 60, 'tool_timeout_sec': 60,
                'enabled_tools': ['list_evidence', 'read_evidence', 'read_json_field',
                                  'find_in_stories', 'find_story_anchors',
                                  'view_evidence_image', 'view_evidence_images'],
            }
            overrides = []
            for key, value in configuration.items():
                overrides.extend(['-c', 'mcp_servers.docproof_evidence.'+key+'='+_json(value)])
            argv[2:2] = overrides
        receipt.update(status="running", submitted=True, started_at=_now())
        _atomic(receipt_path, receipt)
        _resource_receipt(receipt)
        try:
            # Model tools start beside the materialized evidence. The receipt
            # directory is private to the desktop account on Windows, where
            # tool processes can run under a separate sandbox identity.
            execute = _execute if session is None else session.execute
            executed = execute(argv, prompt=prompt, env=env, cwd=Path(work_dir).resolve(), timeout=remaining)
        except _StartError as exc:
            record_elapsed()
            receipt.update(status="preflight_failed", submitted=False, failure_category="cli_start")
            _atomic(receipt_path, receipt)
            _resource_receipt(receipt)
            raise AstraReviewError("The Codex CLI could not start; no model request was submitted.") from exc
        except OSError as exc:
            record_elapsed()
            receipt.update(status="operational_failure", failure_category="cli_io")
            _atomic(receipt_path, receipt)
            _resource_receipt(receipt)
            raise AstraReviewError("Codex review encountered a local I/O failure after starting. "
                                   "No automatic retry was submitted.") from exc
        record_elapsed(exhausted=executed["timed_out"])
        receipt.update(finished_at=_now(), process_exited=session is None and type(executed["returncode"]) is int,
                       exit_code=executed["returncode"], **executed["events"])
        if output_path.is_file() and output_path.stat().st_size <= MAX_OUTPUT_BYTES:
            receipt["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        category = ("timeout" if executed["timed_out"] else
                    "cancelled" if executed["returncode"] in (-2, 130) else
                    executed["events"].get("failure_category") or _failure_category(executed["stdout_tail"] + "\n" + executed["stderr_tail"])
                    if executed["returncode"] != 0 else "")
        if category:
            receipt.update(status="operational_failure", failure_category=category)
        # Persist process completion and output identity before parsing. A
        # coordinator interruption here can adopt this exact completed output.
        _atomic(receipt_path, receipt)
        if executed["timed_out"] or executed["returncode"] != 0:
            _resource_receipt(receipt)
            error = CodexRetryableError if category == "cli_failure" and _retry_allowed(receipt) else AstraReviewError
            raise error(f"Codex subscription review stopped ({category}). "
                        + ("The same request can resume within its original saved allowance."
                           if error is CodexRetryableError else
                           "No automatic retry or paid API fallback was submitted."))
        if no_tools and not _fixed_response_complete(receipt):
            receipt.update(status="operational_failure", failure_category="fixed_reader_contract")
            _atomic(receipt_path, receipt)
            _resource_receipt(receipt)
            raise AstraReviewError("The fixed reader used tools or did not complete exactly one turn.")
        try:
            result = _load(output_path)
            _schema_check(result, schema, "Codex review")
        except AstraReviewError:
            receipt.update(status="operational_failure", failure_category="invalid_output")
            _atomic(receipt_path, receipt)
            _resource_receipt(receipt)
            error = CodexRetryableError if _retry_allowed(receipt) else AstraReviewError
            raise error("Codex returned an incomplete or invalid structured review. "
                        + ("The same request can resume within its original saved allowance."
                           if error is CodexRetryableError else
                           "The saved retry allowance is exhausted.")) from None
        _atomic(directory / "result.json", result)
        fcntl.private_path(output_path, 0o600)
        receipt.update(status="completed", result_sha256=_hash(result))
        _atomic(receipt_path, receipt)
        _resource_receipt(receipt)
        return result
