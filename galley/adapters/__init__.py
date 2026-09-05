"""Detector interfaces, scope selection, and optional registration helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from docproof.models import Usage

from galley.contracts import GFinding, Manuscript


@dataclass(frozen=True)
class Scope:
    """Select the union of chapters and paragraph ids, or the whole manuscript
    when both are empty. Empty error_groups uses adapter defaults;
    char_budget limits character-billed detectors.
    """

    chapters: tuple[int, ...] = ()
    para_ids: tuple[str, ...] = ()
    error_groups: tuple[str, ...] = ()
    model: str = ""
    passes: int = 1
    char_budget: int | None = None

    def paragraph_ids(self, ms: Manuscript) -> list[str]:
        """Resolve selected paragraph ids in manuscript order, dropping unknown
        ids.
        """

        if not self.chapters and not self.para_ids:
            return [p for p in ms.order if p in ms.paragraphs] or list(ms.paragraphs)

        selected: list[str] = []
        seen: set[str] = set()
        by_index = {c.index: c for c in ms.chapters}
        for ch in self.chapters:
            chapter = by_index.get(ch)
            if not chapter:
                continue
            for pid in chapter.para_ids:
                if pid in ms.paragraphs and pid not in seen:
                    seen.add(pid)
                    selected.append(pid)
        for pid in self.para_ids:
            if pid in ms.paragraphs and pid not in seen:
                seen.add(pid)
                selected.append(pid)

        # Return in the manuscript's reading order when we have one.
        order = {pid: i for i, pid in enumerate(ms.order)}
        selected.sort(key=lambda pid: order.get(pid, len(order)))
        return selected


@dataclass
class AdapterResult:
    """Detector findings, coverage gaps, and the actual cost of this call."""

    findings: list[GFinding] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@runtime_checkable
class DetectorAdapter(Protocol):
    """Read within scope without mutating the manuscript. Track usage and
    return findings, coverage notes, and actual spend.
    """

    name: str

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult: ...


# Optional registry retained for callers; the orchestrator uses its supplied
# adapter dictionary.
ADAPTERS: dict[str, DetectorAdapter] = {}


def register(adapter: DetectorAdapter) -> DetectorAdapter:
    """Register ``adapter`` under its ``name``. Returns it, so it can decorate."""

    if not isinstance(adapter, DetectorAdapter):
        raise TypeError(f"{adapter!r} does not satisfy the DetectorAdapter protocol")
    ADAPTERS[adapter.name] = adapter
    return adapter


def get(name: str) -> DetectorAdapter:
    """Look up a registered adapter by name, or raise ``KeyError``."""

    return ADAPTERS[name]


__all__ = [
    "ADAPTERS",
    "AdapterResult",
    "DetectorAdapter",
    "Scope",
    "get",
    "register",
]
