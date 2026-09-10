"""Serialized, subscription-only Astra calls through the supported Codex CLI.

This transport does not use the Responses API or copy authentication tokens.
Only a completed, hash-matching result can be reused. An interrupted generation
stops for operational recovery instead of silently spending the allowance twice.
"""
from __future__ import annotations

from docproof import platform_io as fcntl
import hashlib
import json
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
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_TAIL_BYTES = 64 * 1024
_EVENT_TYPES = {"thread.started", "turn.started", "turn.completed", "turn.failed",
                "item.started", "item.updated", "item.completed", "error"}
_ENV_KEYS = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TMP",
             "TEMP", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "SYSTEMROOT",
             "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PATHEXT", "COMSPEC", "WINDIR"}
_AUTH_OPTIONS = ["-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                 "-c", 'cli_auth_credentials_store="file"']


class _StartError(OSError):
    """Popen failed before a child existed, so generation is safe to retry."""


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
    result = _load(directory / "result.json")
    if receipt.get("result_sha256") != _hash(result):
        raise AstraReviewError("Saved Codex review result has changed; it cannot be reused.")
    _schema_check(result, schema, "Codex review")
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
        _atomic(directory / "receipt.json", {
            "protocol_version": PROTOCOL_VERSION, "request_sha256": old["request_sha256"],
            "transport": "codex_subscription", "model": request["model"], "reasoning_effort": request["reasoning_effort"],
            "status": "preflight", "submitted": False, "attempt": attempt + 1,
            "retry_authorization": authorization, "created_at": _now()})
        return {"status": "retry_authorized", **authorization}


def run_structured(prompt: str, schema: dict, work_dir: Path, *, request_id: str,
                   timeout_seconds: int = 1800, codex_bin: str | None = None,
                   model: str = MODEL, reasoning_effort: str = REASONING_EFFORT) -> dict:
    """Return a structured answer using the worker's ChatGPT login.

    All calls sharing GALLEY_CODEX_HOME serialize, including authentication.
    The timeout includes queueing. Preflight failures may be tried again after
    configuration is repaired; a generation that started is never auto-replayed.
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
               'gpt-5.6-luna': {'low', 'medium', 'high', 'xhigh', 'max'}}
    if model not in allowed or reasoning_effort not in allowed[model]:
        raise AstraReviewError("Unsupported subscription model or reasoning effort.")
    _check_schema(schema)
    if schema["type"] != "object":
        raise AstraReviewError("The Codex review output must be a structured object.")
    request = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id,
               "transport": "codex_subscription", "model": model,
               "reasoning_effort": reasoning_effort, "prompt": prompt, "schema": schema}
    evidence_path = Path(work_dir).resolve() / 'astra-evidence.json'
    evidence = _load(evidence_path) if evidence_path.exists() else None
    if evidence is not None:
        request['evidence'] = evidence
    request_sha256 = _hash(request)
    deadline = time.monotonic() + timeout_seconds
    home = codex_home()
    directory = request_directory(work_dir, request_id)
    with _serialized(home, deadline):
        _private_dir(directory.parent)
        _private_dir(directory)
        request_path, receipt_path = directory / "request.json", directory / "receipt.json"
        if request_path.exists():
            if _hash(_load(request_path)) != request_sha256:
                raise AstraReviewError("Codex request ID was already used for different evidence.")
        else:
            _atomic(request_path, request)
        retry_fields: dict[str, Any] = {"attempt": 1}
        if receipt_path.exists():
            receipt = _load(receipt_path)
            if not isinstance(receipt, dict) or receipt.get("request_sha256") != request_sha256:
                raise AstraReviewError("Saved Codex review receipt does not match its request.")
            if receipt.get("status") == "completed" or receipt.get("submitted") is not False:
                return _cached_result(directory, receipt, request_sha256, schema)
            retry_fields = {key: receipt[key] for key in ("attempt", "retry_authorization") if key in receipt}
        schema_path, output_path = directory / "schema.json", directory / "final.json"
        _atomic(schema_path, schema)
        # Unfinished output can only be from a preflight-only attempt here.
        if output_path.exists():
            raise AstraReviewError("Unexpected unfinished Codex output requires operational reconciliation.")
        receipt = {"protocol_version": PROTOCOL_VERSION, "request_sha256": request_sha256,
                   "transport": "codex_subscription", "model": model,
                   "reasoning_effort": reasoning_effort, "status": "preflight",
                   "submitted": False, "created_at": _now(), **retry_fields}
        _atomic(receipt_path, receipt)
        binary = _binary(codex_bin)
        env = child_env(home)
        try:
            _check_login_locked(binary, env=env, cwd=directory, deadline=deadline)
        except AstraReviewError:
            receipt.update(status="preflight_failed", failure_category="authentication")
            _atomic(receipt_path, receipt)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AstraReviewError("Codex review timed out during preflight; no model request was submitted.")
        argv = [binary, "exec", "--model", model, "-c", f'model_reasoning_effort="{reasoning_effort}"',
                *_AUTH_OPTIONS, "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--ephemeral", "-c", 'approval_policy="never"',
                "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                "--json", "-"]
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
        try:
            # Model tools start beside the materialized evidence. The receipt
            # directory is private to the desktop account on Windows, where
            # tool processes can run under a separate sandbox identity.
            executed = _execute(argv, prompt=prompt, env=env, cwd=Path(work_dir).resolve(), timeout=remaining)
        except _StartError as exc:
            receipt.update(status="preflight_failed", submitted=False, failure_category="cli_start")
            _atomic(receipt_path, receipt)
            raise AstraReviewError("The Codex CLI could not start; no model request was submitted.") from exc
        except OSError as exc:
            receipt.update(status="operational_failure", failure_category="cli_io")
            _atomic(receipt_path, receipt)
            raise AstraReviewError("Codex review encountered a local I/O failure after starting. "
                                   "No automatic retry was submitted.") from exc
        receipt.update(finished_at=_now(), process_exited=type(executed["returncode"]) is int,
                       exit_code=executed["returncode"], **executed["events"])
        if executed["timed_out"] or executed["returncode"] != 0:
            category = "timeout" if executed["timed_out"] else _failure_category(
                executed["stdout_tail"] + "\n" + executed["stderr_tail"])
            receipt.update(status="operational_failure", failure_category=category)
            _atomic(receipt_path, receipt)
            raise AstraReviewError(f"Codex subscription review stopped ({category}). "
                                   "No automatic retry or paid API fallback was submitted.")
        try:
            result = _load(output_path)
            _schema_check(result, schema, "Codex review")
        except AstraReviewError:
            receipt.update(status="operational_failure", failure_category="invalid_output")
            _atomic(receipt_path, receipt)
            raise AstraReviewError("Codex returned an incomplete or invalid structured review. "
                                   "No automatic retry was submitted.") from None
        _atomic(directory / "result.json", result)
        fcntl.private_path(output_path, 0o600)
        receipt.update(status="completed", result_sha256=_hash(result))
        _atomic(receipt_path, receipt)
        return result
