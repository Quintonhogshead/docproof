"""Money-fenced test doubles for Galley.

Per the Standing Orders, unit tests use these only — a real API call from a unit
test is a bug. ``FakeDetector`` satisfies the :class:`DetectorAdapter` protocol
and replays scripted findings at a scripted cost, threading the shared ``Usage``
the way a real adapter does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docproof.models import Usage

from galley.adapters import AdapterResult, DetectorAdapter, Scope
from galley.contracts import (
    Chapter,
    GFinding,
    Manuscript,
    Provenance,
    Span,
)


def gfinding(
    fid: str,
    para_id: str,
    find: str,
    replace: str,
    *,
    error_type: str = "typo",
    start: int = 0,
    end: int | None = None,
    note: str = "",
    confidence: str = "medium",
    detector: str = "fake",
    wave: int = 1,
    model: str = "fake-model",
    cost_usd: float = 0.0,
) -> GFinding:
    """Build a GFinding with a resolved span and provenance in one call."""

    return GFinding(
        id=fid,
        error_type=error_type,
        span=Span(para_id, start, len(find) if end is None else end),
        find=find,
        replace=replace,
        note=note,
        confidence=confidence,
        provenance=Provenance(detector, wave, model, cost_usd),
    )


def make_manuscript(*paragraphs: str, chapter_size: int = 0) -> Manuscript:
    """Build a manuscript from paragraph texts, ids ``body-0001`` onward.

    ``chapter_size > 0`` groups paragraphs into chapters of that many paragraphs.
    """

    order = tuple(f"body-{i:04d}" for i in range(1, len(paragraphs) + 1))
    paras = {pid: text for pid, text in zip(order, paragraphs)}
    chapters: tuple[Chapter, ...] = ()
    if chapter_size > 0:
        chapters = tuple(
            Chapter(idx, f"Chapter {idx + 1}", tuple(order[i : i + chapter_size]))
            for idx, i in enumerate(range(0, len(order), chapter_size))
        )
    return Manuscript(paragraphs=paras, order=order, chapters=chapters)


@dataclass
class FakeDetector:
    """A scripted :class:`DetectorAdapter`.

    Returns ``scripted`` findings that fall within the requested scope, plus a
    scripted ``cost_usd`` and ``coverage_notes``. If its cost would exceed the
    wave's ``budget_usd`` it declines: empty findings, zero cost, one coverage
    note. Every call is recorded on ``.calls`` and threaded through ``usage``.
    """

    name: str = "fake"
    scripted: list[GFinding] = field(default_factory=list)
    cost_usd: float = 0.0
    coverage_notes: list[str] = field(default_factory=list)
    model: str = "fake-model"
    tokens_in: int = 1000
    tokens_out: int = 200
    calls: list[tuple[Scope, float]] = field(default_factory=list)

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        self.calls.append((scope, budget_usd))

        if self.cost_usd > budget_usd:
            note = (
                f"{self.name}: declined scope, ${self.cost_usd:.2f} "
                f"over budget ${budget_usd:.2f}"
            )
            return AdapterResult(findings=[], coverage_notes=[note], cost_usd=0.0)

        target = set(scope.paragraph_ids(ms))
        found = [f for f in self.scripted if f.span.para_id in target]

        # Thread usage the way a real adapter does: flat totals + per-model bucket.
        bucket = usage.by_model.setdefault(
            self.model, {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
        )
        bucket["input_tokens"] += self.tokens_in
        bucket["output_tokens"] += self.tokens_out
        bucket["api_calls"] += 1
        usage.input_tokens += self.tokens_in
        usage.output_tokens += self.tokens_out
        usage.api_calls += 1

        return AdapterResult(
            findings=found,
            coverage_notes=list(self.coverage_notes),
            cost_usd=self.cost_usd,
        )


# Static assertion mirroring the acceptance test: the fake is a DetectorAdapter.
assert isinstance(FakeDetector(), DetectorAdapter)
