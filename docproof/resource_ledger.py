"""Append-only, source-bound receipts for actual work, including subscriptions.

The parent exports ``DOCPROOF_RESOURCE_LEDGER`` and the source/config hashes to
every phase. Provider subprocesses append here directly. Receipts contain no
prompts, manuscript text or credentials. API list equivalents are not allowance
limits. Thinking is a subset of output; cached input is separate from new input.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import platform_io

LEDGER_ENV = "DOCPROOF_RESOURCE_LEDGER"
SOURCE_ENV = "DOCPROOF_RESOURCE_SOURCE_SHA256"
CONFIG_ENV = "DOCPROOF_RESOURCE_CONFIG_SHA256"
PARENT_ENV = "DOCPROOF_RESOURCE_PARENT_OPERATION"
GROUP_ENV = "DOCPROOF_RESOURCE_GROUP"
MAX_CALLS_ENV = "DOCPROOF_RESOURCE_MAX_CALLS"
MAX_OUTPUT_ENV = "DOCPROOF_RESOURCE_MAX_OUTPUT_TOKENS"
RESERVATION_ENV = "DOCPROOF_RESOURCE_RESERVATION_OUTPUT_TOKENS"
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens", "thinking_tokens")
_THREAD_LOCK = threading.RLock()
_CONTEXT = ContextVar("docproof_resource_context", default={})
_READ_CACHE = {}


def _value(name, default=None):
    return _CONTEXT.get().get(name, os.environ.get(name, default))


def current_context():
    return dict(_CONTEXT.get())


@contextmanager
def use_context(env):
    """Bind in-process calls without mutating another book's environment.

    Subprocess callers pass context_env through their ordinary child env.
    Python thread pools should copy_context().run when crossing this boundary.
    """
    token = _CONTEXT.set({**_CONTEXT.get(), **env})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class ResourceBudgetExceeded(RuntimeError):
    """Engineering budget exhausted; stop without disguising unread work."""


def _positive_env(name):
    value = _value(name)
    if not value:
        return None
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _group_usage(rows, group):
    receipts = {}
    included = set()
    limits = {"max_calls": None, "max_output_tokens": None}
    for row in rows:
        if row.get("group", "book") != group or row.get("reused"):
            continue
        if (row.get("usage") or {}).get("output_tokens") is not None:
            included.update(row.get("included_operation_ids", ()))
        for key, value in (row.get("budget") or {}).items():
            if key in limits and value is not None:
                limits[key] = min(value, limits[key]) if limits[key] is not None else value
        item = receipts.setdefault(row["receipt_id"], {"reserved": 0, "terminal": {},
                                                       "operation_id": row["operation_id"]})
        item["reserved"] = max(item["reserved"], row.get("reservation_output_tokens") or 0)
        if row["status"] not in {"started", "pending", "recovering"}:
            item["terminal"][row["model"]] = row
    receipts = {key: item for key, item in receipts.items() if item["operation_id"] not in included}
    charged = reserved = unknown = 0
    for item in receipts.values():
        terminal = list(item["terminal"].values())
        if any(row["status"] == "completed" for row in terminal):
            # A recovered request can reveal the actual version only after an
            # earlier transport failure under the requested alias. That unknown
            # alias is not an additional paid attempt or native helper model.
            terminal = [row for row in terminal if not (
                row["status"] == "error" and row.get("usage") is None)]
        outputs = [(row.get("usage") or {}).get("output_tokens") for row in terminal]
        known = sum(v for v in outputs if v is not None)
        if not outputs or any(v is None for v in outputs):
            reserved += item["reserved"]
            unknown += 1
            charged += max(known, item["reserved"])
        else:
            charged += known
    return {**limits, "calls": len(receipts), "charged_output_tokens": charged,
            "reserved_output_tokens": reserved, "unknown_output_attempts": unknown}


def context_env(path, source_sha256, config_sha256, *, parent_operation_id=None):
    result = {LEDGER_ENV: str(Path(path).resolve()), SOURCE_ENV: source_sha256,
              CONFIG_ENV: config_sha256}
    if parent_operation_id is not None:
        result[PARENT_ENV] = parent_operation_id
    return result


def normalize_usage(usage):
    """Missing fields stay unknown. Codex input includes its cached subset."""
    if is_dataclass(usage):
        usage = asdict(usage)
    if not isinstance(usage, dict) or not usage:
        return None
    usage = dict(usage)
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict) and "cached_tokens" in input_details:
        usage["cached_input_tokens"] = input_details["cached_tokens"]
    aliases = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
               "cache_creation_input_tokens": "cacheCreationInputTokens",
               "cache_read_input_tokens": "cacheReadInputTokens",
               "thinking_tokens": "thinkingTokens"}
    out = {}
    for key, alias in aliases.items():
        value = usage.get(key, usage.get(alias))
        out[key] = value if type(value) is int and value >= 0 else None
    details = usage.get("output_tokens_details") or {}
    if isinstance(details, dict) and type(details.get("thinking_tokens")) is int:
        out["thinking_tokens"] = details["thinking_tokens"]
    elif isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
        out["thinking_tokens"] = details["reasoning_tokens"]
    if "cached_input_tokens" in usage:
        cached = usage["cached_input_tokens"]
        if type(cached) is int and cached >= 0:
            out["cache_read_input_tokens"] = cached
            if out["input_tokens"] is not None:
                out["input_tokens"] = max(0, out["input_tokens"] - cached)
            out["cache_creation_input_tokens"] = 0
    return out


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_inclusion(rows, operation, children):
    """Inclusive rollups must not make their own cost disappear via a cycle."""
    if not children:
        return
    graph = defaultdict(set)
    for row in rows:
        graph[row["operation_id"]].update(row.get("included_operation_ids", ()))
    graph[operation].update(children)
    pending, seen = list(graph[operation]), set()
    while pending:
        item = pending.pop()
        if item == operation:
            raise ValueError("Resource receipt inclusion must be acyclic")
        if item not in seen:
            seen.add(item)
            pending.extend(graph[item])


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # flock alone does not provide all platforms' in-process thread semantics.
    with _THREAD_LOCK, path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        platform_io.flock(lock, platform_io.LOCK_EX)
        try:
            yield
        finally:
            platform_io.flock(lock, platform_io.LOCK_UN)


def _read(path):
    if not path.exists():
        _READ_CACHE.pop(str(path), None)
        return []
    try:
        stat = path.stat()
        key = str(path.resolve())
        previous = _READ_CACHE.get(key)
        identity = (stat.st_dev, stat.st_ino)
        offset, rows = 0, []
        if previous and previous[0] == identity and stat.st_size >= previous[1]:
            if stat.st_size == previous[1] and stat.st_mtime_ns == previous[2]:
                return previous[3]
            if stat.st_size > previous[1]:
                offset, rows = previous[1], previous[3]
        with path.open("rb") as stream:
            stream.seek(offset)
            added = stream.read()
        if added and not added.endswith(b"\n"):
            raise ValueError("unfinished receipt")
        rows = rows + [json.loads(line) for line in added.splitlines() if line]
        _READ_CACHE[key] = (identity, stat.st_size, stat.st_mtime_ns, rows)
        return rows
    except (ValueError, UnicodeError) as exc:
        # Never silently turn damaged accounting into a zero usage report.
        raise ValueError("Resource ledger is incomplete or corrupt; reconcile it before resuming") from exc


def append_usage(*, receipt_id, operation_id, model, transport, usage=None,
                 status="completed", reused=False, parent_operation_id=None,
                 included_operation_ids=(), path=None, source_sha256=None,
                 config_sha256=None, **metadata):
    """Append an idempotent event; started/finished share one receipt ID.

    Parent relationships alone NEVER imply inclusion: a coordinator's CLI tool
    children are usually separate usage. A caller importing an inclusive rollup
    must name the exact child operation IDs it includes; those rows are excluded
    by ``summarize``. Reusing a saved result records evidence, not new compute.
    """
    target = path or _value(LEDGER_ENV)
    if not target:
        return None
    source = source_sha256 or _value(SOURCE_ENV)
    config = config_sha256 or _value(CONFIG_ENV)
    if not source or not config:
        raise ValueError("Resource ledger requires source and configuration hashes")
    parent = parent_operation_id or _value(PARENT_ENV)
    if parent == operation_id:
        parent = None
    row = {"schema_version": 1, "receipt_id": receipt_id,
           "operation_id": operation_id, "source_sha256": source,
           "config_sha256": config, "phase": _value("GALLEY_BRAIN_PHASE"),
           "parent_operation_id": parent,
           "included_operation_ids": sorted(set(included_operation_ids)),
           "model": model, "transport": transport, "usage": normalize_usage(usage),
           "status": status, "reused": bool(reused), "metadata": metadata,
           "group": _value(GROUP_ENV, "book")}
    target = Path(target)
    with _locked(target):
        old = _read(target)
        if any(r["source_sha256"] != source for r in old):
            raise ValueError("Resource ledger belongs to another source document")
        _check_inclusion(old, operation_id, row["included_operation_ids"])
        if status == "started" and not reused:
            group_usage = _group_usage(old, row["group"])
            limits = {"max_calls": _positive_env(MAX_CALLS_ENV),
                      "max_output_tokens": _positive_env(MAX_OUTPUT_ENV)}
            # A restart cannot silently discard or raise an already reserved
            # group's ceilings. A separately authorized group is a new budget.
            for key, value in group_usage.items():
                if key in limits and value is not None:
                    if limits[key] is not None and limits[key] > value:
                        raise ResourceBudgetExceeded(f"Cannot raise persisted {row['group']} {key}")
                    limits[key] = min(value, limits[key]) if limits[key] is not None else value
            requested = metadata.get("max_output_tokens") or _positive_env(RESERVATION_ENV)
            if limits["max_output_tokens"] is not None and (type(requested) is not int or requested <= 0):
                raise ValueError("An output budget requires a positive per-call output reservation")
            row["reservation_output_tokens"] = requested
            row["budget"] = limits
            prior = [r for r in old if r["receipt_id"] == receipt_id and not r.get("reused")]
            if not prior:
                if limits["max_calls"] is not None and group_usage["calls"] + 1 > limits["max_calls"]:
                    raise ResourceBudgetExceeded(f"{row['group']} call budget exhausted; unread work remains")
                if limits["max_output_tokens"] is not None and group_usage["charged_output_tokens"] + requested > limits["max_output_tokens"]:
                    raise ResourceBudgetExceeded(f"{row['group']} output budget cannot reserve the next read; unread work remains")
        row["event_id"] = hashlib.sha256(_json(row).encode()).hexdigest()
        if any(r.get("event_id") == row["event_id"] for r in old):
            return row["event_id"]
        # A reused result may be the first observed receipt after migration;
        # it stays explicitly reused and never becomes an invented fresh call.
        row["recorded_at"] = datetime.now(timezone.utc).isoformat()
        with target.open("a", encoding="utf-8") as stream:
            stream.write(_json(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return row["event_id"]


def summarize(path):
    target = Path(path)
    with _locked(target):
        rows = _read(target)
    fresh, reused = {}, set()
    for row in rows:
        key = (row["receipt_id"], row["model"])
        if row.get("reused"):
            reused.add(key)
        else:
            fresh[key] = row
    included = {op for row in fresh.values() if row.get("usage") is not None
                for op in row.get("included_operation_ids", [])}
    completed_receipts = {row["receipt_id"] for row in fresh.values()
                          if row["status"] not in {"started", "pending", "recovering"}}
    # A session may resolve a model alias or reveal native helper models only
    # in its terminal result. Do not count the initial requested alias again.
    fresh = {key: row for key, row in fresh.items()
             if row["status"] not in {"started", "pending", "recovering"}
             or row["receipt_id"] not in completed_receipts}
    successful = {row["receipt_id"] for row in fresh.values() if row["status"] == "completed"}
    fresh = {key: row for key, row in fresh.items() if not (
        row["receipt_id"] in successful and row["status"] == "error" and row.get("usage") is None)}
    totals = {key: 0 for key in TOKEN_FIELDS}
    by_model = defaultdict(lambda: {**{key: 0 for key in TOKEN_FIELDS},
                                    "attempts": 0, "unknown_usage": 0})
    unknown = incomplete = excluded = 0
    counted_receipts = set()
    missing_fields = {key: 0 for key in TOKEN_FIELDS}
    for row in fresh.values():
        if row["operation_id"] in included:
            excluded += 1
            continue
        counted_receipts.add(row["receipt_id"])
        bucket = by_model[row["model"]]
        bucket["attempts"] += 1
        if row["status"] in {"started", "pending", "recovering"}:
            incomplete += 1
        usage = row.get("usage")
        if usage is None:
            unknown += 1
            bucket["unknown_usage"] += 1
        for key in TOKEN_FIELDS:
            value = (usage or {}).get(key)
            if value is None:
                missing_fields[key] += 1
            else:
                totals[key] += value
                bucket[key] += value
    groups = {group: _group_usage(rows, group) for group in {r.get("group", "book") for r in rows}}
    for budget in groups.values():
        budget["remaining_calls"] = max(0, budget["max_calls"] - budget["calls"]) if budget["max_calls"] is not None else None
        budget["remaining_output_tokens"] = max(0, budget["max_output_tokens"] - budget["charged_output_tokens"]) if budget["max_output_tokens"] is not None else None
    return {"schema_version": 1, **totals, "new_attempts": len(counted_receipts),
            "reused_receipts": len({key[0] for key in reused}), "unknown_usage": unknown,
            "incomplete_attempts": incomplete, "excluded_inclusive_children": excluded,
            "missing_fields": missing_fields, "by_model": dict(by_model),
            "groups": groups,
            "complete": not unknown and not incomplete and not any(
                missing_fields[key] for key in TOKEN_FIELDS if key != "thinking_tokens"),
            "note": "Known token totals only; thinking is included in output. No subscription quota inferred.",
            "attempt_scope": "Provider invocations; transport-internal retries may not expose individual usage."}


def record_claude_result(result, *, operation_id, receipt_id, model=None,
                         transport="claude_subscription", **metadata):
    """Import terminal SDK/CLI evidence once, preferring per-model buckets.

    modelUsage includes native session subagents; ordinary Bash subprocess
    providers are outside that session and must retain their own receipts.
    """
    if not isinstance(result, dict):
        result = {key: getattr(result, key, None) for key in
                  ("usage", "model_usage", "subtype", "is_error", "num_turns", "duration_ms")}
    models = result.get("modelUsage") or result.get("model_usage")
    status = "error" if result.get("is_error") or result.get("subtype") not in (None, "success") else "completed"
    if isinstance(models, dict) and models:
        for actual_model, usage in models.items():
            append_usage(receipt_id=receipt_id, operation_id=operation_id,
                         model=actual_model, transport=transport, usage=usage,
                         status=status, num_turns=result.get("num_turns"),
                         duration_ms=result.get("duration_ms"), **metadata)
    else:
        append_usage(receipt_id=receipt_id, operation_id=operation_id,
                     model=model or "unknown", transport=transport,
                     usage=result.get("usage"), status=status,
                     num_turns=result.get("num_turns"),
                     duration_ms=result.get("duration_ms"), **metadata)


class MeteredProvider:
    """Observe single API requests without changing Provider behavior."""
    def __init__(self, provider, *, effort=None):
        self._provider = provider
        self.effort = effort
        self._resource_context = current_context()

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete_structured(self, **kwargs):
        with use_context(self._resource_context):
            return self._complete_structured(**kwargs)

    def _complete_structured(self, **kwargs):
        operation = uuid.uuid4().hex
        fields = dict(receipt_id=operation, operation_id=operation,
                      model=kwargs["model"], transport=self._provider.name + "_api",
                      effort=self.effort, max_output_tokens=kwargs["max_tokens"],
                      lane=kwargs.get("schema_name"))
        append_usage(**fields, status="started")
        started = time.monotonic()
        try:
            result = self._provider.complete_structured(**kwargs)
        except BaseException as exc:
            append_usage(**fields, status="error", error_type=type(exc).__name__,
                         duration_ms=round((time.monotonic() - started) * 1000))
            raise
        usage = result.resource_usage or asdict(result.usage)
        # Older providers return empty counters after transport failure.
        if not any(usage.get(key) for key in TOKEN_FIELDS):
            usage = None
        if result.actual_model:
            fields["model"] = result.actual_model
        append_usage(**fields, status="completed" if result.stop_reason == "ok" else "error",
                     usage=usage, stop_reason=result.stop_reason,
                     provider_response_id=result.provider_response_id,
                     duration_ms=round((time.monotonic() - started) * 1000))
        return result


def metered(provider, *, effort=None):
    return MeteredProvider(provider, effort=effort) if _value(LEDGER_ENV) else provider
