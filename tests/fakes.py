"""Stand-in providers. No test in this suite may touch a real API — every
vendor call goes through the Provider protocol, so a fake here covers both the
synchronous and the batch path."""
from __future__ import annotations

import itertools
from typing import Any, Sequence

from docproof.providers import (BatchRequest, BatchStatus, NormalizedUsage,
                                ProviderResult)

USAGE = NormalizedUsage(input_tokens=100, output_tokens=20,
                        cache_read_input_tokens=50)


class FakeProvider:
    """Replays scripted results, one per call, and records what it was asked."""

    name = "fake"

    def __init__(self, results: Sequence[ProviderResult] | None = None):
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> ProviderResult:
        if self.results:
            return self.results.pop(0)
        return ProviderResult(parsed={"findings": []}, usage=USAGE)

    def complete_structured(self, **kwargs) -> ProviderResult:
        self.calls.append(kwargs)
        return self._next()

    # -- batch ----------------------------------------------------------------

    def submit_batch(self, *, requests: Sequence[BatchRequest],
                     **kwargs) -> str:
        self.calls.append({"batch": True, "requests": list(requests), **kwargs})
        self._ids = [r.custom_id for r in requests]
        return "batch-fake-0001"

    def poll_batch(self, batch_id: str) -> BatchStatus:
        n = len(getattr(self, "_ids", []))
        return BatchStatus(state="completed", total=n, succeeded=n)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        return {cid: self._next() for cid in getattr(self, "_ids", [])}


class ScriptedBatchProvider(FakeProvider):
    """Reports `pending_polls` in-progress polls before completing, so restart
    and resume behaviour can be exercised without sleeping."""

    def __init__(self, results=None, pending_polls: int = 1):
        super().__init__(results)
        self.pending_polls = pending_polls
        self.polls = 0

    def poll_batch(self, batch_id: str) -> BatchStatus:
        self.polls += 1
        n = len(getattr(self, "_ids", []))
        if self.polls <= self.pending_polls:
            return BatchStatus(state="in_progress", total=n)
        return BatchStatus(state="completed", total=n, succeeded=n)


def finding_result(*, para_id: str, error_type: str, original: str,
                   corrected: str, confidence: str = "high") -> ProviderResult:
    return ProviderResult(parsed={"findings": [{
        "para_id": para_id, "error_type": error_type,
        "original_text": original, "occurrence": 1,
        "corrected_text": corrected, "explanation": "Test finding.",
        "confidence": confidence}]}, usage=USAGE)


def ids() -> itertools.count:
    return itertools.count(1)
