"""Persist observed detector costs and seeded-recall calibration.

The JSON store keeps cumulative weighted rates by ``(adapter, model)`` and a
timestamped history of seeded-recall summaries. Writes are atomic; missing or
unreadable files read as an empty :class:`Calibration`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docproof.utils.files import write_atomic

from galley.casefile import CaseFile
from galley.contracts import GFinding, Manuscript, WaveRecord
from galley.seeding import RecallEstimate

# The default calibration filename, resolved relative to whatever directory the
# caller chooses (the CLI verb defaults it to alongside --config).
DEFAULT_CALIBRATION_FILENAME = "galley_calibration.json"

_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cost_key(adapter: str, model: str) -> str:
    return f"{adapter}:{model or '(default)'}"




@dataclass(frozen=True)
class CostEntry:
    """Cumulative observed cost for one ``(adapter, model)`` pair.

    ``usd_per_kword`` is derived, not stored: cost and kwords each accumulate
    across every :func:`record_run` call, so the rate is always a weighted
    average over every word this pair has ever actually been billed for.
    """

    adapter: str
    model: str = ""
    cost_usd_total: float = 0.0
    kwords_total: float = 0.0
    samples: int = 0
    updated_at: str = ""

    @property
    def usd_per_kword(self) -> float:
        if self.kwords_total <= 0:
            return 0.0
        return self.cost_usd_total / self.kwords_total

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "model": self.model,
            "cost_usd_total": self.cost_usd_total,
            "kwords_total": self.kwords_total,
            "samples": self.samples,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CostEntry":
        d = data if isinstance(data, dict) else {}
        return cls(
            adapter=str(d.get("adapter", "")),
            model=str(d.get("model", "")),
            cost_usd_total=float(d.get("cost_usd_total", 0.0)),
            kwords_total=float(d.get("kwords_total", 0.0)),
            samples=int(d.get("samples", 0)),
            updated_at=str(d.get("updated_at", "")),
        )


@dataclass(frozen=True)
class RecallRecord:
    """One dated seeded-recall summary — a snapshot of a ``RecallEstimate``."""

    planted: int = 0
    caught: int = 0
    rate: float = 0.0
    by_type: dict[str, tuple[int, int]] = field(default_factory=dict)
    caveat: str = ""
    book: str = ""
    recorded_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "planted": self.planted,
            "caught": self.caught,
            "rate": self.rate,
            "by_type": {k: list(v) for k, v in self.by_type.items()},
            "caveat": self.caveat,
            "book": self.book,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RecallRecord":
        d = data if isinstance(data, dict) else {}
        by_type = {
            str(k): (int(v[0]), int(v[1]))
            for k, v in (d.get("by_type") or {}).items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
        return cls(
            planted=int(d.get("planted", 0)),
            caught=int(d.get("caught", 0)),
            rate=float(d.get("rate", 0.0)),
            by_type=by_type,
            caveat=str(d.get("caveat", "")),
            book=str(d.get("book", "")),
            recorded_at=str(d.get("recorded_at", "")),
        )


@dataclass
class Calibration:
    """The whole store: every cost entry, keyed, plus the recall history."""

    schema_version: int = _SCHEMA_VERSION
    cost: dict[str, CostEntry] = field(default_factory=dict)
    recall: list[RecallRecord] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cost": {k: v.to_json() for k, v in self.cost.items()},
            "recall": [r.to_json() for r in self.recall],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Calibration":
        d = data if isinstance(data, dict) else {}
        cost = {
            str(k): CostEntry.from_json(v)
            for k, v in (d.get("cost") or {}).items()
        }
        recall = [RecallRecord.from_json(r) for r in (d.get("recall") or [])]
        return cls(
            schema_version=int(d.get("schema_version", _SCHEMA_VERSION)),
            cost=cost,
            recall=recall,
        )




def read_calibration(path: str | Path) -> Calibration:
    """Read the calibration store at ``path``; a missing/unreadable file reads
    back as an empty :class:`Calibration` rather than raising."""

    p = Path(path)
    if not p.exists():
        return Calibration()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Calibration()
    return Calibration.from_json(data)


def _save(calibration: Calibration, path: str | Path) -> None:
    text = json.dumps(calibration.to_json(), indent=2, ensure_ascii=False)
    write_atomic(Path(path), text)




def _free_adapters() -> frozenset[str]:
    """The adapters whose ``$0`` is a real price: the orchestrator's own list
    (imported lazily — it imports this module's consumers), with a fixed
    fallback so the store works standalone."""

    try:
        from galley.orchestrator import FREE_ADAPTERS

        return FREE_ADAPTERS
    except Exception:  # noqa: BLE001 - keep the calibration store standalone
        return frozenset({"spellscan", "languagetool"})


def record_run(
    cf: CaseFile, ms: Manuscript, path: str | Path, *, now: str | None = None,
    free_adapters: frozenset[str] | None = None,
) -> Calibration:
    """Fold one case file's real spend into the cost table at ``path``.

    Walks case-file actions, resolves each scope to words, and accumulates cost
    and kilo-words by ``(adapter, model)``. Free adapters record zero-cost words;
    skipped actions and unpaid runs from paid adapters are excluded.
    """

    from galley.adapters import Scope

    if free_adapters is None:
        free_adapters = _free_adapters()
    calibration = read_calibration(path)
    ts = now or _utc_now()

    per_key: dict[tuple[str, str], list[float]] = {}
    for wave in cf.waves:
        for action in wave.actions:
            if not isinstance(action, dict):
                continue
            adapter = action.get("adapter")
            scope_json = action.get("scope")
            cost = action.get("cost_usd")
            if not adapter or not isinstance(scope_json, dict) or cost is None:
                continue
            if float(cost) <= 0 and str(adapter) not in free_adapters:
                continue
            scope = Scope(
                chapters=tuple(scope_json.get("chapters") or ()),
                para_ids=tuple(scope_json.get("para_ids") or ()),
                error_groups=tuple(scope_json.get("error_groups") or ()),
                model=str(scope_json.get("model") or ""),
                passes=int(scope_json.get("passes") or 1),
            )
            words = sum(
                len(ms.text_of(pid).split()) for pid in scope.paragraph_ids(ms)
            )
            if words <= 0:
                continue
            key = (str(adapter), scope.model)
            acc = per_key.setdefault(key, [0.0, 0.0])
            acc[0] += float(cost)
            acc[1] += words / 1000.0

    for (adapter, model), (cost_sum, kwords_sum) in per_key.items():
        k = _cost_key(adapter, model)
        prior = calibration.cost.get(k)
        calibration.cost[k] = CostEntry(
            adapter=adapter,
            model=model,
            cost_usd_total=(prior.cost_usd_total if prior else 0.0) + cost_sum,
            kwords_total=(prior.kwords_total if prior else 0.0) + kwords_sum,
            samples=(prior.samples if prior else 0) + 1,
            updated_at=ts,
        )

    _save(calibration, path)
    return calibration


def record_recall(
    estimate: Any,
    path: str | Path,
    *,
    now: str | None = None,
    book: str = "",
) -> Calibration:
    """Append one seeded-recall summary to the history at ``path``.

    ``estimate`` is a :class:`~galley.seeding.RecallEstimate` (or anything
    duck-typed the same way: ``planted``, ``caught``, ``rate``, ``by_type``,
    ``caveat`` attributes) — score once, record forever; the history only grows.
    """

    calibration = read_calibration(path)
    ts = now or _utc_now()
    calibration.recall.append(
        RecallRecord(
            planted=int(getattr(estimate, "planted", 0)),
            caught=int(getattr(estimate, "caught", 0)),
            rate=float(getattr(estimate, "rate", 0.0)),
            by_type=dict(getattr(estimate, "by_type", {}) or {}),
            caveat=str(getattr(estimate, "caveat", "")),
            book=book,
            recorded_at=ts,
        )
    )
    _save(calibration, path)
    return calibration




@dataclass(frozen=True)
class BookRecall(RecallEstimate):
    """A :class:`~galley.seeding.RecallEstimate` that remembers which book it
    was measured on (``""`` for a record written before books were tagged)."""

    book: str = ""


def est_usd_per_kword(
    calibration: Calibration, adapter: str, model: str, default: float
) -> float:
    """The calibrated ``usd_per_kword`` for ``(adapter, model)``, or ``default``.

    Falls back from an exact ``(adapter, model)`` match to an adapter-wide
    weighted average across every model observed for it (a fresh model on a
    calibrated adapter is still better estimated by its sibling models' rate
    than by the frozen constant), and only reaches ``default`` when the adapter
    itself has never been recorded.
    """

    exact = calibration.cost.get(_cost_key(adapter, model))
    if exact is not None and exact.kwords_total > 0:
        return exact.usd_per_kword

    total_cost = 0.0
    total_kwords = 0.0
    for entry in calibration.cost.values():
        if entry.adapter == adapter:
            total_cost += entry.cost_usd_total
            total_kwords += entry.kwords_total
    if total_kwords > 0:
        return total_cost / total_kwords
    return default


def latest_recall(path: str | Path, book: str | None = None) -> Any:
    """The most recently recorded recall estimate, or ``None`` if there is none.

    With ``book`` given, filters to that book; without it, returns the latest
    record across books. Returns ``None`` when no matching record exists.
    """

    calibration = read_calibration(path)
    records = calibration.recall
    if book is not None:
        records = [r for r in records if r.book == book]
    if not records:
        return None
    rec = records[-1]
    return BookRecall(
        planted=rec.planted,
        caught=rec.caught,
        rate=rec.rate,
        by_type=dict(rec.by_type),
        caveat=rec.caveat,
        book=rec.book,
    )




def _scope_json(scope: Any) -> dict[str, Any]:
    """Mirror ``galley.orchestrator._scope_json`` without importing a private
    helper across module boundaries — the shape both write is part of the
    ``WaveRecord.actions`` contract, not orchestrator-internal."""

    return {
        "chapters": list(scope.chapters),
        "para_ids": list(scope.para_ids),
        "error_groups": list(scope.error_groups),
        "model": scope.model,
        "passes": scope.passes,
    }


def run_free_detectors(
    ms: Manuscript,
) -> tuple[list[GFinding], list[dict[str, Any]]]:
    """Run the ``$0`` detector floor (spellscan + LanguageTool) over ``ms``.

    No network, no API key, no paid model — both adapters read only ``ms``
    (never disk), which is exactly why they are the ones usable against an
    in-memory seeded copy. Returns ``(findings, actions)``; ``actions`` is
    shaped like a :class:`~galley.contracts.WaveRecord`'s action list, so the
    caller can fold it straight into a case file.
    """

    from docproof.models import Usage

    from galley.adapters import Scope
    from galley.adapters.local import LanguageToolAdapter, SpellscanAdapter

    scope = Scope()
    findings: list[GFinding] = []
    actions: list[dict[str, Any]] = []
    for adapter in (SpellscanAdapter(), LanguageToolAdapter()):
        result = adapter.run(ms, scope, 0.0, Usage())
        actions.append(
            {
                "adapter": adapter.name,
                "scope": _scope_json(scope),
                "cost_usd": result.cost_usd,
                "findings_added": len(result.findings),
                "coverage_notes": list(result.coverage_notes),
            }
        )
        findings.extend(result.findings)
    return findings, actions


@dataclass(frozen=True)
class FreeLoopResult:
    """What :func:`calibrate_free` hands back: everything both halves of the
    calibration store (:func:`record_run`, :func:`record_recall`) need."""

    estimate: Any  # galley.seeding.RecallEstimate
    casefile: CaseFile
    seeded: Manuscript
    answer_key: Any  # galley.seeding.AnswerKey


def calibrate_free(
    ms: Manuscript,
    n: int,
    *,
    rng_seed: int = 0,
    book: str = "",
    now: str | None = None,
) -> FreeLoopResult:
    """The ``$0`` closed loop: seed a copy, run the free floor, score it.

    Plants ``n`` known errors into a copy of ``ms`` (:func:`galley.seeding.seed_copy`),
    runs :func:`run_free_detectors` over the seeded copy, and scores the catches
    (:func:`galley.seeding.score_catches`). The returned :class:`FreeLoopResult`
    packages a synthetic one-wave :class:`~galley.casefile.CaseFile` — its
    ``waves[0].actions`` are real ``$0`` adapter runs, not fabricated — so
    ``record_run(result.casefile, result.seeded, path)`` and
    ``record_recall(result.estimate, path, book=book)`` are both ready to call.
    """

    from galley.seeding import score_catches, seed_copy

    seeded, key = seed_copy(ms, n, rng_seed=rng_seed)
    findings, actions = run_free_detectors(seeded)
    estimate = score_catches(findings, key)

    ts = now or _utc_now()
    cf = CaseFile(book=book)
    cf.findings = list(findings)
    cf.waves = [
        WaveRecord(
            index=1,
            actions=tuple(actions),
            spend_usd=0.0,
            findings_added=len(findings),
            started_at=ts,
            ended_at=ts,
        )
    ]
    return FreeLoopResult(estimate=estimate, casefile=cf, seeded=seeded, answer_key=key)


__all__ = [
    "BookRecall",
    "Calibration",
    "CostEntry",
    "DEFAULT_CALIBRATION_FILENAME",
    "FreeLoopResult",
    "RecallRecord",
    "calibrate_free",
    "est_usd_per_kword",
    "latest_recall",
    "read_calibration",
    "record_recall",
    "record_run",
    "run_free_detectors",
]
