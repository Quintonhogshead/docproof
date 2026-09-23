"""Durable, bounded calls for Galley's fixed proofreading recipe.

There is no coordinator model: Python supplies each frozen prompt and schema.
An interrupted submission is never silently repeated. Atomic response receipts
close the response-to-consumer crash window; confirmed failures alone may retry
within the originally saved allowance. Unknown usage retains its reservation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

from docproof import platform_io
from docproof.config import Config
from docproof.providers import (NormalizedUsage, ProviderError, ProviderResult,
                                build_provider, cost_of_usage, estimate_cost)
from docproof.resource_ledger import (CONFIG_ENV, GROUP_ENV, LEDGER_ENV,
    MAX_CALLS_ENV, MAX_OUTPUT_ENV, PARENT_ENV, RESERVATION_ENV, SOURCE_ENV,
    TOKEN_FIELDS, append_usage, normalize_usage, summarize, use_context)

PROTOCOL_VERSION = 1
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_MAX_FILE_BYTES = 32 * 1024 * 1024
# Every OpenAI model the fixed lane reads with goes through the worker's
# ChatGPT subscription login (the Codex transport), Luna included since
# 2026-09-16. The API is not a default for any model; a caller may still
# request it explicitly for Luna (tests and diagnostics), never as a fallback.
_SUBSCRIPTION_MODELS = {"gpt-6-luna", "gpt-6-sol", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra"}
_API_ON_REQUEST = {"gpt-6-luna", "gpt-5.6-luna"}


class FixedCallError(RuntimeError):
    """A required read has no validated, complete answer."""


class FixedReadUnavailable(FixedCallError):
    """A model could not supply a usable answer within its bounded allowance."""


class FixedCallCoverageError(FixedCallError):
    """A terminal model answer did not cover its complete assigned inventory."""


class FixedCallContractError(FixedCallError):
    """The local coverage contract cannot safely authorize another call."""


class FixedCallInterrupted(FixedCallError):
    """Submission outcome is unknown; operator reconciliation is required."""


class FixedCallBudgetExceeded(FixedCallError):
    """The persisted allowance cannot fund the next required read."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        platform_io.sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("oversized receipt")
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("receipt is not an object")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise FixedCallError(f"Cannot read fixed-call receipt {path.name}; reconcile it before resuming.") from exc


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())
    with lock, path.open("a+b") as stream:
        platform_io.flock(stream, platform_io.LOCK_EX)
        try:
            yield
        finally:
            platform_io.flock(stream, platform_io.LOCK_UN)


def _schema(value: Any, schema: dict, root: dict | None = None, where="$", depth=0) -> None:
    """Validate the structured-output schema vocabulary used by our providers."""
    root = schema if root is None else root
    if depth > 100 or not isinstance(schema, dict):
        raise FixedCallError("Invalid or excessively recursive response schema")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise FixedCallError("Only local response schema references are supported")
        child = root
        try:
            for part in ref[2:].split("/"):
                child = child[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise FixedCallError("Unresolved response schema reference") from exc
        _schema(value, child, root, where, depth + 1)
    for kind in ("anyOf", "oneOf", "allOf"):
        if kind in schema:
            matches = 0
            for child in schema[kind]:
                try:
                    _schema(value, child, root, where, depth + 1)
                    matches += 1
                except FixedCallError:
                    pass
            if (kind == "allOf" and matches != len(schema[kind]) or
                    kind == "anyOf" and matches == 0 or kind == "oneOf" and matches != 1):
                raise FixedCallError(f"Malformed response at {where}")
    types = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": type(value) is int,
             "number": type(value) in (int, float) and math.isfinite(value),
             "null": value is None}
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if kind is not None and not any(types.get(k, False) for k in kinds):
        raise FixedCallError(f"Malformed response at {where}")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise FixedCallError(f"Unknown response choice at {where}")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise FixedCallError(f"Unexpected response value at {where}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - set(value):
            raise FixedCallError(f"Incomplete response at {where}")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise FixedCallError(f"Unknown response fields at {where}")
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties"))
            if isinstance(child, dict):
                _schema(item, child, root, where + "." + key, depth + 1)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise FixedCallError(f"Invalid response array length at {where}")
        if schema.get("uniqueItems") and len({_json(item) for item in value}) != len(value):
            raise FixedCallError(f"Duplicate response array item at {where}")
        if "items" in schema:
            for item in value:
                _schema(item, schema["items"], root, where + "[]", depth + 1)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise FixedCallError(f"Invalid response string length at {where}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise FixedCallError(f"Invalid response string at {where}")
    if type(value) in (int, float):
        if (value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf)
                or value <= schema.get("exclusiveMinimum", -math.inf)
                or value >= schema.get("exclusiveMaximum", math.inf)):
            raise FixedCallError(f"Response number outside permitted bounds at {where}")


def _response_payload(parsed, request):
    """Validate raw output, tolerating only Claude's exact schema-name wrapper.

    Earlier subscription prompts called the response a named object. Some
    readers returned {schema_name: payload}. Keep that raw response as evidence;
    never drop siblings, invent fields, or relax validation of the inner object.
    """
    try:
        _schema(parsed, request["schema"])
        return parsed
    except FixedCallError:
        if (request.get("transport") != "claude_subscription" or
                not isinstance(parsed, dict) or set(parsed) != {request["schema_name"]}):
            raise
        payload = parsed[request["schema_name"]]
        _schema(payload, request["schema"])
        return payload


def _coverage_contract(coverage, schema):
    """A frozen inventory supplied by orchestration, never inferred from prose."""
    if not isinstance(coverage, dict):
        raise FixedCallContractError("Coverage contract must be an object")
    for field, rule in coverage.items():
        if (field not in schema.get("properties", {}) or not isinstance(rule, dict) or
                set(rule) != {"ids", "id_key", "context_ids"} or
                rule["id_key"] not in (None, "id")):
            raise FixedCallContractError("Invalid fixed coverage contract")
        for key in ("ids", "context_ids"):
            values = rule[key]
            if (not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values)
                    or len(values) != len(set(values))):
                raise FixedCallContractError("Coverage inventory needs unique string IDs")
        if set(rule["ids"]) & set(rule["context_ids"]) or rule["id_key"] and rule["context_ids"]:
            raise FixedCallContractError("Coverage context must be separate read-only paragraph IDs")
    return coverage


def _bind_coverage(directory, request, receipt, coverage):
    path = directory / "coverage.json"
    if receipt.get("coverage_sha256"):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != receipt["coverage_sha256"]:
            raise FixedCallContractError("Saved coverage contract changed or is missing")
    saved = _load(path) if path.exists() else None
    if coverage is not None:
        coverage = json.loads(_json(_coverage_contract(coverage, request["schema"])))
        expected = {"version": 1, "request_sha256": _hash(request), "coverage": coverage}
        if saved is not None and saved != expected:
            raise FixedCallContractError("Coverage inventory changed for an existing request")
        if saved is None:
            _atomic(path, expected)
            saved = expected
    if saved is not None:
        if saved.get("version") != 1 or saved.get("request_sha256") != _hash(request):
            raise FixedCallContractError("Coverage contract belongs to another request")
        _coverage_contract(saved.get("coverage"), request["schema"])
        receipt["coverage_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        _atomic(directory / "receipt.json", receipt)


def _checked_response(parsed, request, directory, receipt):
    parsed = _response_payload(parsed, request)
    if not receipt.get("coverage_sha256"):
        if (directory / "coverage.json").exists():
            raise FixedCallContractError("Coverage contract is not bound to its response receipt")
        return parsed  # Previously certified calls retain their original contract.
    path = directory / "coverage.json"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != receipt["coverage_sha256"]:
        raise FixedCallContractError("Saved coverage contract changed or is missing")
    saved = _load(path)
    if saved.get("version") != 1 or saved.get("request_sha256") != _hash(request):
        raise FixedCallContractError("Coverage contract belongs to another request")
    coverage = _coverage_contract(saved.get("coverage"), request["schema"])
    normalized = dict(parsed)
    rule = coverage.get("decisions")
    if rule and rule["id_key"] == "id":
        from galley.fixed_decisions import grouped_decisions
        recovered = grouped_decisions(parsed, request, rule)
        if recovered is not None:
            normalized, audit = recovered
            audit.update(request_sha256=_hash(request), normalized_sha256=_hash(normalized))
            if receipt.get("decision_normalization") not in (None, audit):
                raise FixedCallContractError("Saved dispute normalization changed")
            receipt["decision_normalization"] = audit
            _schema(normalized, request["schema"])
    from galley.fixed_decisions import nested_proposal_ids
    nested_ids = nested_proposal_ids(request)
    discarded = {}
    for field, rule in coverage.items():
        rows = normalized.get(field)
        actual = ([row.get(rule["id_key"]) if isinstance(row, dict) else None for row in rows]
                  if isinstance(rows, list) and rule["id_key"] else rows)
        if not isinstance(actual, list) or any(not isinstance(x, str) for x in actual):
            raise FixedCallCoverageError(f"{field}: invalid coverage IDs")
        owned, context = set(rule["ids"]), set(rule["context_ids"])
        if field in {"decisions", "comment_decisions"} and rule["id_key"] == "id":
            assigned = [x for x in actual if x in owned]
            # An unassigned decision has no authority to edit or comment. It
            # may be discarded only when every real assignment is present once.
            if len(assigned) == len(owned) and set(assigned) == owned:
                extra = [row for row in rows if row["id"] not in owned]
                if extra and not (field == "decisions" and any(row["id"] in nested_ids for row in extra)):
                    discarded[field] = extra
                    normalized[field] = rows = [row for row in rows if row["id"] in owned]
                    actual = assigned
        missing, unknown = owned - set(actual), set(actual) - owned - context
        duplicates = len(actual) - len(set(actual))
        if missing or unknown or duplicates:
            raise FixedCallCoverageError(f"{field}: {len(missing)} missing, {len(unknown)} unknown, {duplicates} duplicate IDs")
        if context:
            normalized[field] = [row for row in rows if row in owned]
    if discarded:
        audit = {"request_sha256": _hash(request), "normalized_sha256": _hash(normalized),
                 "rejected_unassigned_decisions": discarded}
        if receipt.get("discarded_decisions") not in (None, audit):
            raise FixedCallContractError("Saved unassigned-decision audit changed")
        receipt["discarded_decisions"] = audit
        _schema(normalized, request["schema"])
    return normalized


def _check_schema_definition(schema):
    allowed = {"type", "$ref", "$defs", "$schema", "properties", "required", "additionalProperties",
               "items", "enum", "const", "anyOf", "oneOf", "allOf", "title", "description",
               "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
               "maxLength", "pattern", "minItems", "maxItems", "uniqueItems"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise FixedCallError("Unsupported fixed-call response schema constraint")
    for key in ("properties", "$defs"):
        for child in schema.get(key, {}).values():
            _check_schema_definition(child)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            _check_schema_definition(schema[key])
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []):
            _check_schema_definition(child)


def _transport(model: str, requested: str | None) -> str:
    expected = ("codex_subscription" if model in _SUBSCRIPTION_MODELS else
                "claude_subscription" if model.startswith("claude-") else None)
    aliases = {"codex": "codex_subscription", "subscription": expected,
               "subagent": "claude_subscription", "openai": "api"}
    requested = aliases.get(requested, requested)
    if requested == "api" and model in _API_ON_REQUEST:
        return "api"
    if expected is None or requested is not None and requested != expected:
        raise FixedCallError(f"Unsupported fixed-call transport for {model}; no fallback was submitted.")
    return expected


CLAUDE_READ_TIMEOUT_SECONDS = 900
# A whole-book read at high effort needs longer than a windowed one.
CLAUDE_STAGE_TIMEOUT_SECONDS = {"continuity": 1800, "story_sheet": 1800}


def _make_provider(factory, cfg, model, stage):
    """Pass the stage only to a factory that accepts it: the stage selects a
    read timeout, and older factories (tests, custom transports) take
    `(cfg, *, model)` alone."""
    import inspect
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts = "stage" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    return factory(cfg, model=model, stage=stage) if accepts else factory(cfg, model=model)


def _default_provider(cfg: Config, *, model: str, stage: str | None = None):
    if not model.startswith("claude-"):
        return build_provider(cfg, model=model)
    from docproof.agent_lane import require_cli_for
    from docproof.providers.subagent import SubagentProvider, availability
    ok, why = availability()
    if not ok:
        raise ProviderError("Claude subscription unavailable; no API fallback: " + str(why))
    require_cli_for(model)

    class CheckedSubagentProvider(SubagentProvider):
        async def _turn(self, *args, evidence, **kwargs):
            import asyncio
            result = await asyncio.wait_for(super()._turn(*args, evidence=evidence, **kwargs),
                                            timeout=CLAUDE_STAGE_TIMEOUT_SECONDS.get(stage, CLAUDE_READ_TIMEOUT_SECONDS))
            message = evidence.get("result")
            if message is None:
                return replace(result, stop_reason="incomplete", error="Claude supplied no terminal result")
            stop_reason = getattr(message, "stop_reason", None)
            if stop_reason not in (None, "end_turn", "stop_sequence"):
                return replace(result, stop_reason="max_tokens" if stop_reason == "max_tokens" else "refusal"
                    if stop_reason == "refusal" else "error", error="Claude returned a nonterminal stop reason",
                    resource_usage=getattr(message, "usage", None))
            if getattr(message, "is_error", False) or getattr(message, "subtype", None) != "success":
                return replace(result, stop_reason="error", error="Claude turn did not complete successfully",
                               resource_usage=getattr(message, "usage", None))
            return replace(result, resource_usage=getattr(message, "usage", None), actual_model=model)

    return CheckedSubagentProvider(model=model, effort=cfg.api.effort)


def _queue_pause(exc, category=None):
    """Preserve the worker queue's quota/authentication control exceptions.

    The whole cause chain is read, not just the outermost exception: a quota
    error raised inside an SDK turn can reach here wrapped in asyncio's own
    shutdown error ("aclose(): asynchronous generator is already running"),
    and on 2026-09-16 that wrapper read as a generic failure, so the fixed
    lane skipped every remaining model review and delivered an untouched
    manuscript as done instead of pausing until the limit reset."""
    from docproof.agent_lane import AgentLaneUnavailable, CredentialsError as LaneCredentialsError
    from docproof.subscription_limits import UsageLimitError, is_usage_limited
    from galley.driver import CredentialsError, detect_credential_failure
    if category == "subscription_limit":
        return UsageLimitError(str(exc))
    seen, cause = set(), exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (UsageLimitError, CredentialsError)):
            return cause
        if is_usage_limited(str(cause)):
            return UsageLimitError(str(cause))
        if (isinstance(cause, LaneCredentialsError) or
                isinstance(cause, (AgentLaneUnavailable, ProviderError)) and detect_credential_failure(str(cause))):
            return CredentialsError(str(cause))
        cause = cause.__cause__ or cause.__context__
    if category == "authentication":
        return CredentialsError(str(exc))
    return None


class FixedCalls:
    def __init__(self, directory: Path, identity: dict, cfg: Config, *,
                 provider_factory=None, codex_runner=None, max_calls=10_000,
                 max_output_tokens=20_000_000, max_api_usd=10.0, max_attempts=3,
                 progress=None, continue_on_model_failure=False, parallel_subscription=False):
        for name, value in (("max_calls", max_calls), ("max_output_tokens", max_output_tokens),
                            ("max_attempts", max_attempts)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_attempts > 3:
            raise ValueError("At most three fixed-call attempts are supported")
        if type(max_api_usd) not in (int, float) or not math.isfinite(max_api_usd) or max_api_usd < 0:
            raise ValueError("max_api_usd must be a finite nonnegative number")
        self.directory = Path(directory).resolve()
        self.identity = json.loads(_json(identity))
        self.cfg = cfg.model_copy(deep=True)
        self.provider_factory = provider_factory or _default_provider
        self.codex_runner = codex_runner
        self.progress = progress
        self.continue_on_model_failure = continue_on_model_failure
        self.subscription_session = None
        if parallel_subscription:
            from galley.codex_session import SubscriptionSession
            self.subscription_session = SubscriptionSession()
        self.max_attempts = max_attempts
        self.directory.mkdir(parents=True, exist_ok=True)
        platform_io.private_path(self.directory, 0o700)
        limits = dict(max_calls=max_calls, max_output_tokens=max_output_tokens,
                      max_api_usd=float(max_api_usd))
        with _locked(self.directory / "budget.lock"):
            path = self.directory / "budget.json"
            budget = _load(path) if path.exists() else {"limits": limits, "entries": {}}
            self._check_budget(budget)
            # Tightening is allowed; restarting never silently raises ceilings.
            budget["limits"] = {key: min(value, budget["limits"][key]) for key, value in limits.items()}
            _atomic(path, budget)

    @staticmethod
    def _check_budget(budget):
        if (not isinstance(budget.get("entries"), dict) or not isinstance(budget.get("limits"), dict)
                or set(budget["limits"]) != {"max_calls", "max_output_tokens", "max_api_usd"}):
            raise FixedCallError("Fixed-call budget receipt is malformed")
        for key, value in budget["limits"].items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise FixedCallError("Fixed-call budget limits are malformed")

    @staticmethod
    def _summary(budget):
        FixedCalls._check_budget(budget)
        entries = list(budget["entries"].values())
        known = {key: sum((row.get("usage") or {}).get(key) or 0 for row in entries) for key in TOKEN_FIELDS}
        return {**known, "calls": len(entries), "limits": dict(budget["limits"]),
            "charged_output_tokens": sum(max(row["reserved_output_tokens"], (row.get("usage") or {}).get("output_tokens") or 0)
                if (row.get("usage") or {}).get("output_tokens") is None else row["usage"]["output_tokens"] for row in entries),
            "charged_api_usd": sum(row["reserved_api_usd"] if row.get("api_usd") is None else row["api_usd"] for row in entries),
            "known_api_usd": sum(row.get("api_usd") or 0 for row in entries),
            "unknown_output_attempts": sum((row.get("usage") or {}).get("output_tokens") is None for row in entries),
            "unknown_api_attempts": sum(row.get("api_usd") is None and row["transport"] == "api" for row in entries),
            "missing_fields": {key: sum((row.get("usage") or {}).get(key) is None for row in entries) for key in TOKEN_FIELDS},
            "incomplete_attempts": sum(row["status"] in {"started", "unknown"} for row in entries),
            "note": "Known token totals only; unknown attempts retain their maximum reservations."}

    def close(self):
        if self.subscription_session is not None:
            self.subscription_session.close()

    def usage_summary(self) -> dict:
        with _locked(self.directory / "budget.lock"):
            return self._summary(_load(self.directory / "budget.json"))

    def assert_complete(self) -> None:
        validate_fixed_call_evidence(self.directory, identity=self.identity)

    def _reserve(self, request, sha, attempt):
        reservation = 0.0
        if request["transport"] == "api":
            # UTF-8 bytes are a deliberately conservative token upper estimate,
            # including output schema and a framing allowance. No cache discount.
            input_maximum = len((request["system"] + request["user"] + _json(request["schema"])).encode("utf-8")) + 1024
            reservation = estimate_cost(request["model"], input_tokens=input_maximum,
                                        output_tokens=request["max_tokens"])
            if reservation is None:
                raise FixedCallBudgetExceeded("The API model has no local budget price")
        key = f"{sha}:{attempt}"
        with _locked(self.directory / "budget.lock"):
            path = self.directory / "budget.json"
            budget = _load(path)
            summary = self._summary(budget)
            if key in budget["entries"]:
                raise FixedCallInterrupted("A saved reservation has no reconciled response; no duplicate call was submitted.")
            limits = budget["limits"]
            if (summary["calls"] + 1 > limits["max_calls"] or
                    summary["charged_output_tokens"] + request["max_tokens"] > limits["max_output_tokens"] or
                    summary["charged_api_usd"] + reservation > limits["max_api_usd"] + 1e-12):
                raise FixedCallBudgetExceeded("The fixed workflow cannot reserve its next required read within the saved budget.")
            budget["entries"][key] = {"request_sha256": sha, "attempt": attempt, "status": "started",
                "model": request["model"], "transport": request["transport"],
                "reserved_output_tokens": request["max_tokens"], "reserved_api_usd": reservation,
                "usage": None, "api_usd": 0.0 if request["transport"] != "api" else None}
            _atomic(path, budget)
        return key

    def _account(self, key, status, usage=None):
        with _locked(self.directory / "budget.lock"):
            path = self.directory / "budget.json"
            budget = _load(path)
            row = budget["entries"][key]
            row.update(status=status, usage=normalize_usage(usage))
            required = TOKEN_FIELDS[:4]
            if row["transport"] == "api" and usage and all(usage.get(k) is not None for k in required):
                row["api_usd"] = cost_of_usage(usage, fallback_model=row["model"])
            _atomic(path, budget)

    def _record(self, receipt, *, reused=False):
        append_usage(receipt_id="fixed:" + receipt["request_sha256"] + ":" + str(receipt["attempt"]),
            operation_id="fixed:" + receipt["request_sha256"], model=receipt["model"],
            transport=receipt["transport"], usage=receipt.get("usage"),
            status="completed" if receipt["status"] == "completed" else "error",
            reused=reused, effort=receipt["effort"], stage=receipt["stage"])

    def _provider_config(self, model, effort):
        cfg = self.cfg.model_copy(deep=True)
        cfg.api.model = model
        cfg.api.provider = "anthropic" if model.startswith("claude-") else "openai"
        cfg.api.claude_lane = "subagent"
        cfg.api.effort = effort
        # Every retry must have its own persisted reservation and receipt.
        cfg.api.max_retries = 0
        return cfg

    @staticmethod
    def _usage(result, transport_ledger):
        if result.resource_usage:
            return normalize_usage(result.resource_usage)
        if transport_ledger.exists():
            summary = summarize(transport_ledger)
            return {key: None if summary["missing_fields"][key] else summary[key] for key in TOKEN_FIELDS}
        # Legacy provider defaults cannot prove a measured zero.
        counts = asdict(result.usage)
        observed = {key: counts[key] for key in TOKEN_FIELDS if counts.get(key)}
        return normalize_usage(observed)

    def _transport_context(self, request, directory, attempt):
        ledger = directory / "attempts" / str(attempt) / "transport-usage.jsonl"
        context = {LEDGER_ENV: str(ledger), SOURCE_ENV: _hash(self.identity), CONFIG_ENV: _hash(request),
                   GROUP_ENV: "fixed-transport", PARENT_ENV: None, MAX_CALLS_ENV: None,
                   MAX_OUTPUT_ENV: None, RESERVATION_ENV: None}
        return ledger, context

    def _invoke(self, request, directory, attempt, provider=None):
        from galley import codex_runner as codex
        ledger, context = self._transport_context(request, directory, attempt)
        with use_context(context):
            if request["transport"] == "codex_subscription":
                runner = self.codex_runner or codex.run_structured
                work = directory / "reader"
                work.mkdir(parents=True, exist_ok=True)
                options = {"session": self.subscription_session} if self.subscription_session is not None and self.codex_runner is None else {}
                parsed = runner(request["system"] + "\n\n" + request["user"], request["schema"], work,
                    request_id=self._codex_request_id(request, attempt), model=request["model"], reasoning_effort=request["effort"], no_tools=True, **options)
                result = ProviderResult(parsed=parsed, usage=NormalizedUsage(billed=False), actual_model=request["model"])
            else:
                result = provider.complete_structured(**{key: request[key] for key in
                    ("model", "system", "user", "schema", "schema_name", "max_tokens")})
        if not isinstance(result, ProviderResult):
            raise FixedCallError("Fixed-call provider returned no ProviderResult")
        usage = self._usage(result, ledger)
        normalized = asdict(result.usage)
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            if (usage or {}).get(key) is not None:
                normalized[key] = usage[key]
        normalized["billed"] = request["transport"] == "api"
        return replace(result, usage=NormalizedUsage(**normalized), resource_usage=usage), usage

    @staticmethod
    def _codex_request_id(request, attempt):
        return _hash(request) if attempt == 1 else f"{_hash(request)}:attempt:{attempt}"

    def _saved_codex(self, request, directory, attempt):
        """Adopt a completed transport receipt locally, never submit to recover."""
        from galley import codex_runner as codex
        request_id = self._codex_request_id(request, attempt)
        path = codex.request_directory(directory / "reader", request_id)
        if not (path / "receipt.json").exists():
            return None
        receipt = _load(path / "receipt.json")
        saved = _load(path / "request.json")
        if (saved.get("request_id") != request_id or saved.get("model") != request["model"] or
                saved.get("prompt") != request["system"] + "\n\n" + request["user"] or
                saved.get("schema") != request["schema"] or saved.get("no_tools") is not True):
            raise FixedCallError("Subscription receipt does not match the fixed reader request")
        if not codex._fixed_response_complete(receipt):
            return None
        if receipt.get("status") != "completed":
            adopted = codex._adopt_completed_output(path, receipt, request["schema"])
            if adopted is None:
                return None
        parsed = codex._cached_result(path, receipt, codex._hash(saved), request["schema"])
        return ProviderResult(parsed=parsed, usage=NormalizedUsage(billed=False),
            resource_usage=normalize_usage(receipt.get("usage")), actual_model=request["model"])

    def _finish(self, request, directory, receipt, envelope):
        if envelope.get("request_sha256") != receipt["request_sha256"] or envelope.get("attempt") != receipt["attempt"]:
            raise FixedCallError("Saved response belongs to a different fixed request")
        result = ProviderResult(**{**envelope["result"], "usage": NormalizedUsage(**envelope["result"]["usage"])})
        usage = result.resource_usage
        valid = result.stop_reason == "ok" and not result.error and isinstance(result.parsed, dict)
        reason = result.stop_reason
        if valid:
            try:
                result = replace(result, parsed=_checked_response(result.parsed, request, directory, receipt))
            except FixedCallCoverageError as exc:
                valid, reason = False, "invalid_coverage"
                receipt["coverage_error"] = str(exc)
            except FixedCallContractError:
                raise
            except FixedCallError:
                valid, reason = False, "invalid_schema"
        elif result.stop_reason == "ok":
            reason = "invalid_output"
        confirmed = (result.stop_reason in {"ok", "refusal", "max_tokens"} or
                     bool(result.provider_response_id) or bool(usage and any(v is not None for v in usage.values())) or
                     bool(re.match(r"^[45]\d\d:", result.error or "")))
        status = "completed" if valid else "failed" if confirmed else "unknown"
        if valid:
            receipt.pop("coverage_error", None)
        receipt.update(status=status, response_sha256=_hash(envelope), usage=usage,
                       failure_category=None if valid else reason,
                       retryable=confirmed and reason not in {"refusal", "max_tokens"},
                       actual_model=result.actual_model)
        _atomic(directory / "receipt.json", receipt)
        self._account(f"{receipt['request_sha256']}:{receipt['attempt']}", status, usage)
        self._record(receipt)
        return result if valid else None

    def result(self, stage: str, *, model: str, system: str, user: str, schema: dict,
               schema_name: str, max_tokens=8192, effort="low", transport=None, coverage=None) -> ProviderResult:
        if not all(isinstance(value, str) and value.strip() for value in (stage, model, system, user, schema_name)):
            raise ValueError("Fixed calls require nonempty stage, model, prompts and schema name")
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise FixedCallError("Unsupported fixed-call reasoning effort")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise FixedCallError("Fixed calls require an object response schema")
        _check_schema_definition(schema)
        request = json.loads(_json({"protocol_version": PROTOCOL_VERSION, "identity": self.identity,
            "stage": stage, "model": model, "system": system, "user": user, "schema": schema,
            "schema_name": schema_name, "max_tokens": max_tokens, "effort": effort,
            "transport": _transport(model, transport)}))
        try:
            return self._request_result(request, coverage)
        except Exception as exc:
            # A quota or token pause is never a skippable model failure: the
            # queue checkpoints and resumes this same read once the limit
            # resets or the token is replaced. Freezing it as a skip (as the
            # unattended lane did until 2026-09-16) discards the review and
            # delivers a manuscript nobody read.
            pause = _queue_pause(exc)
            if pause is not None:
                raise pause if pause is exc else pause from exc
            if (not self.continue_on_model_failure or
                    not isinstance(exc, (FixedReadUnavailable, FixedCallBudgetExceeded, FixedCallInterrupted))):
                raise
            from galley.fixed_skips import freeze_skip, skipped_result
            freeze_skip(self, request, str(exc))
            return skipped_result(self.directory / "calls" / _hash(request), request)

    def _request_result(self, request, coverage):
        stage, model, effort = (request[k] for k in ("stage", "model", "effort"))
        sha = _hash(request)
        directory = self.directory / "calls" / sha
        with _locked(directory / "request.lock"):
            request_path, receipt_path = directory / "request.json", directory / "receipt.json"
            if request_path.exists() and _hash(_load(request_path)) != sha:
                raise FixedCallError("Saved fixed request has changed")
            if (directory / "skipped.json").exists():
                from galley.fixed_skips import skipped_result
                if coverage is not None:
                    supplied = {"version": 1, "request_sha256": sha, "coverage":
                        json.loads(_json(_coverage_contract(coverage, request["schema"])))}
                    if not (directory / "coverage.json").exists() or _load(directory / "coverage.json") != supplied:
                        raise FixedCallContractError("Coverage inventory changed for a skipped request")
                return skipped_result(directory, request)
            _atomic(request_path, request)
            receipt = _load(receipt_path) if receipt_path.exists() else {
                "request_sha256": sha, "stage": stage, "model": model, "effort": effort,
                "transport": request["transport"], "status": "preflight", "attempt": 0,
                "max_attempts": self.max_attempts}
            if receipt.get("request_sha256") != sha:
                raise FixedCallError("Saved fixed receipt has changed")
            if (type(receipt.get("attempt")) is not int or receipt["attempt"] < 0 or
                    type(receipt.get("max_attempts")) is not int or not 1 <= receipt["max_attempts"] <= 3 or
                    receipt["attempt"] > receipt["max_attempts"]):
                raise FixedCallError("Saved fixed-call retry allowance is malformed")
            receipt["max_attempts"] = min(receipt["max_attempts"], self.max_attempts)
            _bind_coverage(directory, request, receipt, coverage)
            while True:
                response_path = directory / "attempts" / str(receipt["attempt"]) / "response.json"
                recover_validation = (receipt["status"] == "failed" and
                                      receipt.get("failure_category") in {"invalid_schema", "invalid_coverage"})
                if (receipt["status"] in {"started", "completed", "unknown"} or recover_validation) and response_path.exists():
                    envelope = _load(response_path)
                    if receipt.get("response_sha256") and receipt["response_sha256"] != _hash(envelope):
                        raise FixedCallError("Saved fixed response has changed")
                    completed = receipt["status"] == "completed"
                    if completed:
                        if envelope.get("request_sha256") != sha or envelope.get("attempt") != receipt["attempt"]:
                            raise FixedCallError("Saved fixed response belongs to another request")
                        saved_result = envelope.get("result") or {}
                        if saved_result.get("stop_reason") != "ok" or saved_result.get("error"):
                            raise FixedCallError("Saved fixed response did not complete successfully")
                        try:
                            parsed = _checked_response(saved_result.get("parsed"), request, directory, receipt)
                        except FixedCallCoverageError:
                            # Reclassify a terminal but incomplete old answer;
                            # preserve its bytes/usage and retry only this read.
                            self._finish(request, directory, receipt, envelope)
                            continue
                        with _locked(self.directory / "budget.lock"):
                            accounted = _load(self.directory / "budget.json")["entries"].get(f"{sha}:{receipt['attempt']}", {})
                        if accounted.get("status") == "completed":
                            if accounted.get("usage") != saved_result.get("resource_usage"):
                                raise FixedCallError("Saved fixed response differs from its usage evidence")
                            self._record(receipt, reused=True)
                            return ProviderResult(**{**saved_result, "parsed": parsed, "usage": NormalizedUsage(billed=False),
                                                    "resource_usage": None})
                    result = self._finish(request, directory, receipt, envelope)
                    if result is not None:
                        if completed:
                            self._record(receipt, reused=True)
                            return replace(result, usage=NormalizedUsage(billed=False), resource_usage=None)
                        return result
                if receipt["status"] in {"started", "unknown"} and request["transport"] == "codex_subscription":
                    saved = self._saved_codex(request, directory, receipt["attempt"])
                    if saved is not None:
                        envelope = {"request_sha256": sha, "attempt": receipt["attempt"], "result": asdict(saved)}
                        _atomic(response_path, envelope)
                        result = self._finish(request, directory, receipt, envelope)
                        if result is not None:
                            return result
                if receipt["status"] in {"started", "unknown", "completed"}:
                    raise FixedCallInterrupted(f"{stage}: prior submission has no safely reusable response; reconcile its receipt before retrying.")
                if receipt["status"] == "failed" and (not receipt.get("retryable") or receipt["attempt"] >= receipt["max_attempts"]):
                    raise FixedReadUnavailable(f"{stage}: required read failed ({receipt.get('failure_category')}); saved retry allowance is unavailable or exhausted.")
                attempt = receipt["attempt"] + 1
                provider = None
                if request["transport"] != "codex_subscription":
                    _, context = self._transport_context(request, directory, attempt)
                    try:
                        with use_context(context):
                            provider = _make_provider(self.provider_factory, self._provider_config(model, effort), model, stage)
                    except Exception as exc:
                        receipt.update(status="preflight_failed", failure_category=type(exc).__name__)
                        _atomic(receipt_path, receipt)
                        pause = _queue_pause(exc)
                        if pause is not None:
                            raise pause
                        from docproof.agent_lane import ClaudeCliOutdated
                        if isinstance(exc, ClaudeCliOutdated):
                            # Every read on this model would be refused the
                            # same way; skipping them would deliver a book
                            # nobody read. The run stops until the CLI is fixed.
                            raise FixedCallContractError(f"{stage}: {exc}") from exc
                        raise FixedReadUnavailable(f"{stage}: provider is unavailable before submission; no API fallback was submitted.") from exc
                try:
                    self._reserve(request, sha, attempt)
                except FixedCallError:
                    if not receipt_path.exists():
                        _atomic(receipt_path, receipt)
                    raise
                receipt.update(status="started", attempt=attempt)
                _atomic(receipt_path, receipt)
                if self.progress:
                    self.progress(f"{stage}: reading with {model}")
                try:
                    result, usage = self._invoke(request, directory, attempt, provider)
                except BaseException as exc:
                    # Without a terminal result, a timeout/network/process error
                    # cannot prove that generation did not already consume work.
                    receipt.update(status="unknown", failure_category=type(exc).__name__)
                    transport_ledger, _ = self._transport_context(request, directory, attempt)
                    usage = self._usage(ProviderResult(), transport_ledger)
                    if request["transport"] == "codex_subscription":
                        from galley import codex_runner as codex
                        transport_receipt = codex.request_directory(directory / "reader", self._codex_request_id(request, attempt)) / "receipt.json"
                        if transport_receipt.exists():
                            transport_state = _load(transport_receipt)
                            usage = normalize_usage(transport_state.get("usage"))
                            if (transport_state.get("submitted") is False or
                                    transport_state.get("status") == "operational_failure" and
                                    (transport_state.get("process_exited") is True or
                                     transport_state.get("execution_kind") == "app_server" and
                                     transport_state.get("turn_terminal") is True)):
                                receipt.update(status="failed", retryable=bool(
                                    transport_state.get("submitted") is False or codex._retry_allowed(transport_state)),
                                    failure_category=transport_state.get("failure_category", type(exc).__name__))
                            if transport_state.get("submitted") is False:
                                usage = {key: 0 for key in TOKEN_FIELDS}
                    receipt["usage"] = usage
                    pause = _queue_pause(exc, receipt.get("failure_category"))
                    if pause is not None:
                        receipt.update(status="failed", retryable=True,
                            failure_category="subscription_limit" if type(pause).__name__ == "UsageLimitError" else "authentication")
                    _atomic(receipt_path, receipt)
                    self._account(f"{sha}:{attempt}", receipt["status"], usage)
                    self._record(receipt)
                    if not isinstance(exc, Exception):
                        raise
                    if pause is not None:
                        raise pause
                    if receipt["status"] == "failed":
                        continue
                    raise FixedCallInterrupted(f"{stage}: submission did not return a terminal response; no automatic retry or API fallback was submitted.") from exc
                envelope = {"request_sha256": sha, "attempt": attempt, "result": asdict(result)}
                response_path = directory / "attempts" / str(attempt) / "response.json"
                _atomic(response_path, envelope)
                completed = self._finish(request, directory, receipt, envelope)
                if completed is not None:
                    return completed

    def ask(self, stage: str, **kwargs) -> dict:
        return self.result(stage, **kwargs).parsed

    def provider(self, stage: str, cfg: Config | None = None):
        calls = self
        config = (cfg or self.cfg).model_copy(deep=True)

        class FixedProvider:
            name = "anthropic" if config.api.model.startswith("claude-") else "openai"

            def complete_structured(self, **kwargs):
                return calls.result(stage, effort=config.api.effort or "low", **kwargs)

            def fetch_owned(self, analyzer, chunk):
                from docproof.analyzer import render_chunk
                owned = [p.para_id for p in chunk.paragraphs]
                return calls.result(stage, effort=config.api.effort or "low",
                    model=analyzer.cfg.api.model, system=analyzer.system_prompt,
                    user=render_chunk(chunk), schema=analyzer.schema,
                    schema_name=analyzer.schema_name, max_tokens=analyzer.cfg.api.max_output_tokens,
                    coverage={"reviewed_paragraph_ids": {"ids": owned, "id_key": None,
                        "context_ids": [p.para_id for p in chunk.context_paragraphs if p.para_id not in owned]}})

            def submit_batch(self, **kwargs):
                raise FixedCallError("The fixed recipe requires individually receipted reads; batch mode is disabled")

        return FixedProvider()


def validate_fixed_call_evidence(directory: Path, *, identity: dict | None = None) -> list[dict[str, str]]:
    """Read-only delivery gate and immutable certificate inputs; never calls a model.

    The persisted budget is the call inventory, so deleting a receipt directory
    cannot make unread work disappear from this check. The certificate freezes
    every JSON evidence file, including failed attempts and transport receipts.
    """
    directory = Path(directory).resolve()
    budget_path = directory / "budget.json"
    budget = _load(budget_path)
    FixedCalls._check_budget(budget)
    entries = budget["entries"]
    by_request: dict[str, dict[int, dict]] = {}
    for key, entry in entries.items():
        sha, _, attempt_text = key.rpartition(":")
        if (not re.fullmatch(r"[0-9a-f]{64}", sha) or not attempt_text.isdecimal() or
                not isinstance(entry, dict) or entry.get("request_sha256") != sha or
                entry.get("attempt") != int(attempt_text) or not 1 <= int(attempt_text) <= 3):
            raise FixedCallError("Fixed-call budget inventory is malformed")
        skipped = (directory / "calls" / sha / "skipped.json").exists()
        if entry.get("status") in {"started", "unknown"} and not skipped:
            raise FixedCallInterrupted("A fixed-call submission is unresolved; reconcile it before delivery.")
        if entry.get("status") not in ({"completed", "failed", "started", "unknown"} if skipped else {"completed", "failed"}):
            raise FixedCallError("Fixed-call budget contains a nonterminal attempt")
        by_request.setdefault(sha, {})[int(attempt_text)] = entry
    folders = {path.name: path for path in (directory / "calls").iterdir() if path.is_dir()} if (directory / "calls").exists() else {}
    # A directory holding a request and nothing else, with no line anywhere in
    # the budget, is a call that was never submitted: the request file is
    # written first and the reservation second, so a run that stopped between
    # the two leaves exactly this. The budget is the call inventory, and a
    # reservation that exists but never reconciled is caught above as an
    # unresolved submission — so nothing was asked, nothing was answered and
    # nothing was spent here. Gunn - Book 1 (2026-09-17) stopped this way when
    # the final comment review's coverage inventory was refused, and the shell
    # it left behind would have blocked the delivery of the resumed run.
    unsubmitted = {sha for sha, folder in folders.items()
                   if sha not in by_request
                   and {p.name for p in folder.iterdir()} <= {"request.json", "request.lock"}}
    folders = {sha: folder for sha, folder in folders.items() if sha not in unsubmitted}
    # Check preflight failures explicitly before comparing the charged inventory.
    for sha, folder in folders.items():
        receipt = _load(folder / "receipt.json")
        if receipt.get("status") != "completed" and not (folder / "skipped.json").exists():
            raise FixedCallError(f"Required fixed read {receipt.get('stage', sha)} is {receipt.get('status', 'incomplete')}; delivery is blocked.")
    zero_attempt_skips = {sha for sha, folder in folders.items() if (folder / "skipped.json").exists()
                          and _load(folder / "receipt.json").get("attempt") == 0}
    if set(folders) != set(by_request) | zero_attempt_skips:
        raise FixedCallError("Fixed-call receipt inventory differs from its saved budget; delivery is blocked.")
    evidence = [budget_path]
    for sha, folder in sorted(folders.items()):
        request, receipt = _load(folder / "request.json"), _load(folder / "receipt.json")
        if _hash(request) != sha or receipt.get("request_sha256") != sha:
            raise FixedCallError("Fixed-call request identity or receipt has changed")
        if identity is not None and request.get("identity") != identity:
            raise FixedCallError("Fixed-call evidence belongs to a different source or recipe")
        if (folder / "skipped.json").exists():
            from galley.fixed_skips import validate_skip
            skipped_read = validate_skip(folder, request=request)
            evidence.extend(folder / name for name in skipped_read["files"])
            evidence.append(folder / "skipped.json")
            continue
        attempt = receipt.get("attempt")
        if type(attempt) is not int or not 1 <= attempt <= 3 or set(by_request[sha]) != set(range(1, attempt + 1)):
            raise FixedCallError("Fixed-call attempt history is incomplete")
        final = by_request[sha][attempt]
        if final["status"] != "completed" or any(row["status"] != "failed" for n, row in by_request[sha].items() if n < attempt):
            raise FixedCallError("Fixed-call attempt history does not end in exactly one completed response")
        envelope = _load(folder / "attempts" / str(attempt) / "response.json")
        if (receipt.get("response_sha256") != _hash(envelope) or envelope.get("request_sha256") != sha or
                envelope.get("attempt") != attempt):
            raise FixedCallError("Saved fixed response has changed or is incomplete")
        result = envelope.get("result") or {}
        if result.get("stop_reason") != "ok" or result.get("error") or not isinstance(result.get("parsed"), dict):
            raise FixedCallError("Saved fixed response is not a complete successful read")
        _check_schema_definition(request["schema"])
        _checked_response(result["parsed"], request, folder, receipt)
        if normalize_usage(result.get("resource_usage")) != final.get("usage") or final.get("usage") != receipt.get("usage"):
            raise FixedCallError("Fixed-call usage evidence does not match its completed response")
        for entry in by_request[sha].values():
            if entry.get("model") != request.get("model") or entry.get("transport") != request.get("transport"):
                raise FixedCallError("Fixed-call resource inventory has changed model or transport")
        evidence.extend(sorted(path for path in folder.rglob("*") if path.suffix in {".json", ".jsonl"}))
    return [{"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in evidence]


def fixed_usage_summary(directory: Path) -> dict:
    """Read persisted consumption after a pause/failure without creating calls."""
    return FixedCalls._summary(_load(Path(directory).resolve() / "budget.json"))
