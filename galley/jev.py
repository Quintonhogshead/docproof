"""Jev, TypeSafe's System One judgment model, for the fixed recipe.

Jev never writes text. It answers typed questions about a state with
calibrated probabilities: a Noul (probability a statement is true) or a
Choice (a probability per option, plus a confidence). The fixed recipe uses
it in two places, both upstream of the Sonnet + Luna screen:

* as a candidate SOURCE, ranking brute-force single-character sites (every
  comma boundary, every existing comma, every confusion-set word), and
* as a PRE-SCREEN over the local rule candidates, dropping the obvious
  misfires before they cost a paid screening window.

Every answer is a proposal for the ordinary gates; nothing here edits. Every
request is receipted under ``<run>/jev/<stage>/<sha>.json`` so a resumed run
replays the same answers for free and the timeline can price the stage.
Pricing: $42 per billion input tokens, output free (typesafe.ai, 2026-09).

The lane is optional. Without ``TYPESAFE_API_KEY`` (or with ``GALLEY_JEV=off``)
``enabled()`` is False and callers skip the lane with a history note; a run
never fails for want of Jev. A billing or auth failure mid-run raises
``JevUnavailable`` so the caller can degrade the same way.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from galley.fixed_calls import _atomic, _load

PRICE_INPUT_PER_MTOK = 0.042
DEFAULT_MODEL = "jev-latest"
# One request holds about 32,000 tokens of state plus questions; a Choice
# takes up to 255 options. Callers batch below these.
REQUEST_TOKEN_BUDGET = 32_000
MAX_CHOICE_OPTIONS = 255


class JevUnavailable(RuntimeError):
    """The lane cannot answer: no key, no credits, or the service refused."""


def model_name() -> str:
    return os.environ.get("GALLEY_JEV_MODEL", DEFAULT_MODEL)


def enabled() -> bool:
    if os.environ.get("GALLEY_JEV", "").strip().lower() in {"off", "0", "false", "no"}:
        return False
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        return False
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _build_questions(questions: dict[str, dict]) -> dict:
    """Plain dicts -> SDK question objects. A question is
    {"type": "noul", "instructions": str, "criteria": {"true": str, "false": str}?}
    or {"type": "choice", "instructions": str, "criteria": {option: description|None}}."""
    from typesafe_sdk import Choice, Noul, NoulCriteria
    built = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            crit = q.get("criteria")
            built[qid] = Noul(instructions=q["instructions"],
                              **({"criteria": NoulCriteria(**crit)} if crit else {}))
        elif q["type"] == "choice":
            if len(q["criteria"]) > MAX_CHOICE_OPTIONS:
                raise ValueError(f"Choice {qid} has {len(q['criteria'])} options; the limit is {MAX_CHOICE_OPTIONS}")
            built[qid] = Choice(instructions=q["instructions"], criteria=q["criteria"])
        else:
            raise ValueError(f"Unknown Jev question type {q['type']!r}")
    return built


def _plain_answers(response) -> dict[str, dict]:
    out = {}
    for qid, ans in response.answers.items():
        if hasattr(ans, "noul"):
            out[qid] = {"type": "noul", "noul": float(ans.noul)}
        else:
            out[qid] = {"type": "choice", "choice": ans.choice, "confidence": float(ans.confidence),
                        "probabilities": {k: float(v) for k, v in ans.probabilities.items()}}
    return out


class JevLedger:
    """Receipted, replayable Jev requests for one fixed run.

    ``ask`` returns ``{"answers": {qid: answer}, "usage": {...}, "reused": bool}``.
    Answers are plain dicts (see ``_plain_answers``). Identical requests
    (same model, state and questions) are answered from the receipt on disk.
    """

    def __init__(self, directory: Path, *, model: str | None = None, timeout: float = 120.0,
                 workers: int = 8, client_factory: Callable | None = None):
        self.directory = Path(directory)
        self.model = model or model_name()
        self.timeout = timeout
        self.workers = workers
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.Lock()
        self.calls = 0
        self.reused = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.seconds = 0.0

    # ---------------------------------------------------------------- client
    def _get_client(self):
        with self._lock:
            if self._client is None:
                if self._client_factory is not None:
                    self._client = self._client_factory()
                else:
                    if not enabled():
                        raise JevUnavailable("Jev is not enabled: TYPESAFE_API_KEY missing, GALLEY_JEV=off, or SDK absent")
                    from typesafe_sdk import TypeSafeClient
                    self._client = TypeSafeClient(timeout=self.timeout)
            return self._client

    # ---------------------------------------------------------------- one request
    def ask(self, stage: str, state: Any, questions: dict[str, dict]) -> dict:
        if not stage or not questions:
            raise ValueError("A Jev request needs a stage name and at least one question")
        request = {"version": 1, "model": self.model, "state": state, "questions": questions}
        sha = _sha(request)
        path = self.directory / stage / f"{sha}.json"
        if path.is_file():
            saved = _load(path)
            if saved.get("request_sha256") == sha and isinstance(saved.get("answers"), dict):
                with self._lock:
                    self.reused += 1
                return {"answers": saved["answers"], "usage": saved.get("usage", {}), "reused": True}
        client = self._get_client()
        started = time.perf_counter()
        try:
            response = client.system_one(state=state, questions=_build_questions(questions), model=self.model)
        except Exception as exc:  # the SDK's typed errors all subclass its base error
            status = getattr(exc, "status_code", None)
            if status in (401, 402, 403):
                raise JevUnavailable(f"Jev refused the request ({status}): {exc}") from exc
            raise
        elapsed = time.perf_counter() - started
        answers = _plain_answers(response)
        usage = {"input_tokens": int(response.usage.input_tokens),
                 "output_tokens": int(response.usage.output_tokens),
                 "usd": response.usage.input_tokens / 1e6 * PRICE_INPUT_PER_MTOK,
                 "seconds": elapsed}
        receipt = {"version": 1, "request_sha256": sha, "stage": stage, "model": getattr(response, "model", self.model),
                   "request": request, "answers": answers, "usage": usage, "recorded_at": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, receipt)
        with self._lock:
            self.calls += 1
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.seconds += elapsed
        return {"answers": answers, "usage": usage, "reused": False}

    # ---------------------------------------------------------------- many requests
    def ask_many(self, stage: str, jobs: list[tuple[Any, dict[str, dict]]],
                 *, should_cancel: Callable[[], bool] | None = None,
                 progress: Callable[[int, int], None] | None = None) -> list[dict]:
        """Answer ``jobs`` (state, questions) concurrently, in order. A
        JevUnavailable from any job stops the rest and propagates."""
        results: list[dict | None] = [None] * len(jobs)
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            futures = {pool.submit(self.ask, stage, state, questions): i
                       for i, (state, questions) in enumerate(jobs)}
            for future in as_completed(futures):
                if should_cancel is not None and should_cancel():
                    for f in futures:
                        f.cancel()
                    raise JevUnavailable("Jev requests cancelled")
                results[futures[future]] = future.result()
                done += 1
                if progress is not None:
                    progress(done, len(jobs))
        return results  # type: ignore[return-value]

    # ---------------------------------------------------------------- accounting
    def usage_summary(self) -> dict:
        return {"model": self.model, "calls": self.calls, "reused": self.reused,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "usd": round(self.input_tokens / 1e6 * PRICE_INPUT_PER_MTOK, 4),
                "seconds": round(self.seconds, 1)}


def usage_from_receipts(directory: Path) -> dict:
    """Total Jev usage recorded under ``<run>/jev``, by stage, for the timeline."""
    root = Path(directory)
    stages: dict[str, dict] = {}
    for path in sorted(root.glob("*/*.json")):
        try:
            receipt = _load(path)
        except Exception:
            continue
        usage = receipt.get("usage") or {}
        row = stages.setdefault(receipt.get("stage", path.parent.name),
                                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "seconds": 0.0})
        row["calls"] += 1
        row["input_tokens"] += int(usage.get("input_tokens") or 0)
        row["output_tokens"] += int(usage.get("output_tokens") or 0)
        row["usd"] += float(usage.get("usd") or 0.0)
        row["seconds"] += float(usage.get("seconds") or 0.0)
    total = {k: sum(s[k] for s in stages.values()) for k in ("calls", "input_tokens", "output_tokens", "usd", "seconds")}
    return {"stages": stages, "total": total}


__all__ = ["JevLedger", "JevUnavailable", "enabled", "model_name", "usage_from_receipts",
           "PRICE_INPUT_PER_MTOK", "REQUEST_TOKEN_BUDGET", "MAX_CHOICE_OPTIONS"]
