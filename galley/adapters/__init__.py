"""The detector seam.

Galley talks to every detector — DocProof's full ladder, a single targeted
error-type pass, LanguageTool, Sapling, spellscan — through one small protocol.
DocProof is the first and best adapter, not the skeleton; nothing above this seam
knows which detector it is driving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from docproof.models import Usage

from galley.contracts import GFinding, Manuscript


@dataclass(frozen=True)
class Scope:
    """What a detector should look at, and how hard.

    Empty ``chapters``/``para_ids`` means "the whole book". When both are set the
    target is their union. ``error_groups`` names the DocProof error-type groups
    to run (empty = the adapter's default set); ``model`` and ``passes`` let a
    wave dial the fleet. ``char_budget`` caps a per-character-billed detector
    (Sapling) so a targeted wave can never spend past its allowance.
    """

    chapters: tuple[int, ...] = ()
    para_ids: tuple[str, ...] = ()
    error_groups: tuple[str, ...] = ()
    model: str = ""
    passes: int = 1
    char_budget: int | None = None

    def paragraph_ids(self, ms: Manuscript) -> list[str]:
        """Resolve this scope to the ordered paragraph ids it selects in ``ms``.

        An empty scope selects the whole book in reading order. Chapter ids and
        explicit para ids union; ids not present in the manuscript are dropped.
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
    """What a detector hands back: findings, coverage notes, and its own cost.

    ``coverage_notes`` surfaces the detector's honest gaps — a degraded pass, an
    unruled window, a scope it declined — so the orchestrator can record them
    rather than mistake silence for a clean sweep. ``cost_usd`` is what this call
    actually billed; the orchestrator's ledger sums these.
    """

    findings: list[GFinding] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@runtime_checkable
class DetectorAdapter(Protocol):
    """The one contract every detector implements.

    ``run`` reads the manuscript within ``scope``, spends up to ``budget_usd``,
    threads the shared ``usage`` object for exact per-model cost accounting, and
    returns an :class:`AdapterResult`. Implementations are read-only with respect
    to the manuscript and to the DocProof package.
    """

    name: str

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult: ...


# The live registry. Adapters register themselves at import; the orchestrator
# looks them up by name. Kept a plain dict per the frozen contract.
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
