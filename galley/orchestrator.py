"""Run a full-book detector wave followed by planned rereads within budget.
Persist findings, charges, and wave history in a case file.

Auditor and planner hooks default to no-ops; the app supplies
implementations from galley.brain.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from docproof.models import Usage

from galley.adapters import DetectorAdapter, Scope
from galley.casefile import CaseFile, append_wave
from galley.contracts import GFinding, Hypothesis, Manuscript, WaveRecord
from galley.governor import Caps, Governor, WaveLimitError

LADDER_ADAPTER = "docproof_ladder"

# Alarm categories emitted with a completed wave.
ALARM_DEGRADED = "degraded_pass"
ALARM_ZERO_COST = "zero_cost_detector"
ALARM_TRUNCATION = "truncation_streak"
ALARM_BUDGET_CAP = "budget_cap_hit"
# Record actual spending even when it exceeds the budget.
ALARM_BUDGET_OVERRUN = "budget_overrun"
TRUNCATION_STREAK_LIMIT = 3
# Adapters allowed to report zero cost.
FREE_ADAPTERS = frozenset({"spellscan", "languagetool"})

# Stop at this marginal cost per added finding; no additions give infinite
# cost.
DEFAULT_STOP_THRESHOLD = 1_000_000.0

# T0/T1 allow only the initial wave.
_TIER_MAX_WAVES = {"T0": 1, "T1": 1, "T2": 2, "T3": 3, "T4": 6}
_TIER_MAX_PANEL = {"T0": 0, "T1": 1, "T2": 4, "T3": 12, "T4": 32}


@dataclass(frozen=True)
class Dispatch:
    """Run one adapter over a manuscript scope."""

    adapter: str
    scope: Scope


# Auditors propose hypotheses; planners produce dispatches. Paid auditors
# may accept governor to record their spend.
Auditor = Callable[[CaseFile, Manuscript], list[Hypothesis]]
Planner = Callable[[list[Hypothesis], Governor, CaseFile], list[Dispatch]]
Clock = Callable[[], str]
Notify = Callable[[str, dict[str, Any]], None]


def _no_audit(cf: CaseFile, ms: Manuscript) -> list[Hypothesis]:
    return []


def _no_plan(
    hyps: list[Hypothesis], gov: Governor, cf: CaseFile
) -> list[Dispatch]:
    return []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def caps_for_tier(tier: str, budget_usd: float) -> Caps:
    """Build tier limits; a single wave may use the entire remaining budget."""

    return Caps(
        total_usd=budget_usd,
        per_wave_usd=budget_usd,
        max_waves=_TIER_MAX_WAVES.get(tier, 2),
        max_panel_calls=_TIER_MAX_PANEL.get(tier, 4),
    )


def _scope_json(scope: Scope) -> dict[str, Any]:
    return {
        "chapters": list(scope.chapters),
        "para_ids": list(scope.para_ids),
        "error_groups": list(scope.error_groups),
        "model": scope.model,
        "passes": scope.passes,
    }


def _call_audit(
    audit: Auditor, cf: CaseFile, ms: Manuscript, gov: Governor
) -> list[Hypothesis]:
    """Call the auditor, passing the governor if its signature accepts one."""

    try:
        params = inspect.signature(audit).parameters
    except (TypeError, ValueError):
        params = {}
    if "governor" in params:
        return audit(cf, ms, governor=gov)
    return audit(cf, ms)


def _estimate(adapter: DetectorAdapter, ms: Manuscript, scope: Scope) -> float | None:
    """Return the adapter's optional cost estimate, or None if unavailable or
    unsuccessful.
    """

    estimate = getattr(adapter, "estimate_usd", None)
    if estimate is None:
        return None
    try:
        value = estimate(ms, scope)
    except Exception:  # noqa: BLE001 - an estimate is advice, never a blocker
        return None
    return float(value) if value is not None else None


def _run_wave(
    *,
    wave_index: int,
    dispatches: list[Dispatch],
    adapters: dict[str, DetectorAdapter],
    ms: Manuscript,
    gov: Governor,
    usage: Usage,
    cf: CaseFile,
    clock: Clock,
    persist: Callable[[], None] | None = None,
    workspace: Path | None = None,
) -> WaveRecord:
    """Run dispatches and append new finding ids and actual charges.

    Skip unaffordable estimates but record actual overruns. Persist after
    dispatches and before reraising adapter errors. Pass workspace to
    adapters that retain checkpoints.
    """

    started = clock()
    known_ids = {f.id for f in cf.findings}
    actions: list[dict[str, Any]] = []
    added = 0
    wave_cost = 0.0

    for d in dispatches:
        budget = min(gov.wave_remaining_usd, gov.remaining_usd)
        if budget <= 0:
            actions.append({"adapter": d.adapter, "skipped": "budget exhausted"})
            break
        adapter = adapters.get(d.adapter)
        if adapter is None:
            actions.append({"adapter": d.adapter, "skipped": "unknown adapter"})
            continue

        # Skip unaffordable work; a later cheaper dispatch may still fit.
        estimate = _estimate(adapter, ms, d.scope)
        if estimate is not None and estimate > budget:
            actions.append(
                {"adapter": d.adapter, "scope": _scope_json(d.scope),
                 "skipped": "over budget (estimated)",
                 "estimate_usd": estimate}
            )
            continue

        # Set provenance and checkpoint location when supported by the
        # adapter.
        if hasattr(adapter, "wave"):
            adapter.wave = wave_index
        if (workspace is not None and hasattr(adapter, "workspace")
                and getattr(adapter, "workspace") is None):
            adapter.workspace = workspace

        try:
            result = adapter.run(ms, d.scope, budget, usage)
        except Exception:
            # Persist earlier dispatches before surfacing the interruption.
            actions.append(
                {"adapter": d.adapter, "scope": _scope_json(d.scope),
                 "error": "adapter raised; wave interrupted"}
            )
            if persist is not None:
                persist()
            raise

        over_cap = False
        if result.cost_usd > 0:
            charge = gov.charge(
                result.cost_usd, f"wave{wave_index}:{d.adapter}",
                allow_over_cap=True)
            over_cap = charge in gov.overruns
            wave_cost += result.cost_usd

        new_here = 0
        for f in result.findings:
            if f.id in known_ids:
                continue
            known_ids.add(f.id)
            cf.findings.append(f)
            added += 1
            new_here += 1

        notes = list(result.coverage_notes)
        if over_cap:
            notes.append(
                f"{d.adapter}: billed ${result.cost_usd:.2f}, overrunning the "
                f"budget cap by ${gov.overrun_usd:.2f} (charged; findings kept)"
            )
        action: dict[str, Any] = {
            "adapter": d.adapter,
            "scope": _scope_json(d.scope),
            "cost_usd": result.cost_usd,
            "findings_added": new_here,
            "coverage_notes": notes,
        }
        if estimate is not None:
            action["estimate_usd"] = estimate
        if over_cap:
            action["over_cap"] = True
        actions.append(action)

        # Persist findings and charges before the next dispatch.
        if persist is not None:
            persist()

    return WaveRecord(
        index=wave_index,
        actions=tuple(actions),
        spend_usd=wave_cost,
        findings_added=added,
        started_at=started,
        ended_at=clock(),
    )


def _marginal(rec: WaveRecord) -> float:
    """Marginal cost per finding a wave added; a wave that adds nothing is inf."""
    if rec.findings_added <= 0:
        return float("inf")
    return rec.spend_usd / rec.findings_added


def run_galley(
    ms: Manuscript,
    tier: str,
    budget_usd: float,
    out_dir: str | Path,
    providers: dict[str, Any] | None = None,
    adapters: dict[str, DetectorAdapter] | None = None,
    notify: Notify | None = None,
    *,
    book: str = "",
    ladder: str = LADDER_ADAPTER,
    audit: Auditor = _no_audit,
    plan_wave: Planner = _no_plan,
    stop_threshold: float = DEFAULT_STOP_THRESHOLD,
    clock: Clock = _utc_now,
    casefile_path: str | Path | None = None,
    free_adapters: frozenset[str] = FREE_ADAPTERS,
) -> CaseFile:
    """Run detector waves until the planner stops or a budget limit is reached.

    Resume completed waves from the case file. Continue an interrupted
    charged wave under its existing index so adapter checkpoints can replay
    paid reads. The providers argument is retained for compatibility; supply
    detectors through adapters.
    """

    adapters = adapters or {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cf_path = Path(casefile_path) if casefile_path is not None else out / "casefile.json"

    # Resume if a case file is already on disk; otherwise start fresh.
    if cf_path.exists():
        cf = CaseFile.load(cf_path)
    else:
        cf = CaseFile(book=book)

    caps = caps_for_tier(tier, budget_usd)
    gov = Governor(cf.budget, caps)
    # Zero-cost waves leave no charge; align the counter with recorded wave
    # history.
    while gov.waves_opened < len(cf.waves):
        try:
            gov.open_wave()
        except WaveLimitError:
            break
    # Reuse the index of a charged but unclosed wave so checkpoints can
    # replay it.
    interrupted = gov.current_wave if gov.waves_opened > len(cf.waves) else None

    usage = Usage()
    streak = {"n": 0}  # consecutive waves that lost windows to truncation

    def _save() -> None:
        cf.budget = gov.ledger
        cf.save(cf_path)

    def _open_wave() -> int:
        nonlocal interrupted
        if interrupted is not None:
            index, interrupted = interrupted, None
            return index
        return gov.open_wave()

    def _after_wave(rec: WaveRecord) -> None:
        """Emit this wave's alarms (immediate) then its routine digest."""
        if not notify:
            return
        for alarm in _wave_alarms(rec, gov, streak=streak, free_adapters=free_adapters):
            notify("alarm", alarm)
        notify("wave", _wave_digest(cf, rec))

    # Wave one: the full ladder. Skipped on resume (cf.waves already populated).
    if not cf.waves:
        wave_index = _open_wave()  # wave 1
        rec = _run_wave(
            wave_index=wave_index,
            dispatches=[Dispatch(ladder, Scope())],
            adapters=adapters,
            ms=ms,
            gov=gov,
            usage=usage,
            cf=cf,
            clock=clock,
            persist=_save,
            workspace=cf_path.parent,
        )
        append_wave(cf, rec)
        _save()
        _after_wave(rec)
        last = rec
    else:
        last = cf.waves[-1]

    # Audit, plan, and dispatch until a stop condition is reached.
    while not gov.should_stop(_marginal(last), stop_threshold):
        hyps = _call_audit(audit, cf, ms, gov)
        # Persist audit charges even when no hypotheses are returned.
        if hyps:
            cf.hypotheses.extend(hyps)
        _save()

        dispatches = plan_wave(hyps, gov, cf)
        if not dispatches:
            break

        try:
            wave_index = _open_wave()
        except WaveLimitError:
            break

        rec = _run_wave(
            wave_index=wave_index,
            dispatches=dispatches,
            adapters=adapters,
            ms=ms,
            gov=gov,
            usage=usage,
            cf=cf,
            clock=clock,
            persist=_save,
            workspace=cf_path.parent,
        )
        append_wave(cf, rec)
        _save()
        _after_wave(rec)
        last = rec

    _save()
    return cf


def _wave_digest(cf: CaseFile, rec: WaveRecord) -> dict[str, Any]:
    """A small per-wave digest for the notify hook."""
    notes: list[str] = []
    for action in rec.actions:
        notes.extend(action.get("coverage_notes", []) if isinstance(action, dict) else [])
    return {
        "wave": rec.index,
        "spend_usd": rec.spend_usd,
        "total_spent_usd": cf.budget.spent_usd,
        "findings_added": rec.findings_added,
        "findings_total": len(cf.findings),
        "coverage_notes": notes,
    }


def _wave_alarms(
    rec: WaveRecord,
    gov: Governor,
    *,
    streak: dict[str, int],
    free_adapters: frozenset[str],
) -> list[dict[str, Any]]:
    """Report degraded reads, unexpected zero cost, truncation streaks, budget
    skips, and overruns. The caller preserves streak across waves.
    """

    notes: list[str] = []
    zero_cost: list[str] = []
    overran: list[str] = []
    over_budget = False
    for action in rec.actions:
        if not isinstance(action, dict):
            continue
        notes.extend(action.get("coverage_notes", []))
        if action.get("skipped") in (
                "over budget", "over budget (estimated)", "budget exhausted"):
            over_budget = True
        if action.get("over_cap"):
            overran.append(action.get("adapter", ""))
        ran = "skipped" not in action and "error" not in action
        adapter = action.get("adapter", "")
        if (ran and action.get("cost_usd") == 0.0
                and adapter not in free_adapters):
            zero_cost.append(adapter)

    low = " ".join(notes).lower()
    alarms: list[dict[str, Any]] = []
    if "degrad" in low:
        alarms.append({"alarm": ALARM_DEGRADED, "wave": rec.index, "detail": notes})
    for adapter in zero_cost:
        alarms.append(
            {"alarm": ALARM_ZERO_COST, "wave": rec.index, "detail": adapter}
        )

    truncated = "truncat" in low or "unruled" in low
    streak["n"] = streak["n"] + 1 if truncated else 0
    if streak["n"] >= TRUNCATION_STREAK_LIMIT:
        alarms.append(
            {"alarm": ALARM_TRUNCATION, "wave": rec.index, "detail": streak["n"]}
        )

    if over_budget or gov.remaining_usd <= 0:
        alarms.append(
            {"alarm": ALARM_BUDGET_CAP, "wave": rec.index,
             "detail": round(gov.remaining_usd, 4)}
        )
    if overran:
        alarms.append(
            {"alarm": ALARM_BUDGET_OVERRUN, "wave": rec.index,
             "detail": f"{', '.join(overran)} overran the budget cap by "
                       f"${gov.overrun_usd:.2f}; charged, findings kept"}
        )
    return alarms


class Transport(Protocol):
    """Deliver a wave digest or alarm through send(kind, subject, body)."""

    def send(self, kind: str, subject: str, body: str) -> None: ...


def make_notifier(transport: Transport, *, book: str = "") -> Notify:
    """Format wave digests and alarms for the supplied transport."""

    tag = f"[{book}] " if book else ""

    def notify(kind: str, payload: dict[str, Any]) -> None:
        if kind == "alarm":
            name = payload.get("alarm", "alarm")
            subject = f"{tag}Galley alarm: {name} (wave {payload.get('wave', 0)})"
            body = f"{name}: {payload.get('detail', '')}"
            transport.send("alarm", subject, body)
            return
        subject = (
            f"{tag}Galley wave {payload.get('wave', 0)} — "
            f"${payload.get('total_spent_usd', 0.0):.2f} spent, "
            f"{payload.get('findings_total', 0)} findings"
        )
        lines = [
            f"Wave {payload.get('wave', 0)}",
            f"Spent this wave: ${payload.get('spend_usd', 0.0):.2f}",
            f"Total spent: ${payload.get('total_spent_usd', 0.0):.2f}",
            f"Findings added: {payload.get('findings_added', 0)}",
            f"Findings total: {payload.get('findings_total', 0)}",
        ]
        notes = payload.get("coverage_notes") or []
        if notes:
            lines.append("Coverage notes:")
            lines.extend(f"  - {n}" for n in notes)
        transport.send("wave", subject, "\n".join(lines))

    return notify


__all__ = [
    "ALARM_BUDGET_CAP",
    "ALARM_BUDGET_OVERRUN",
    "ALARM_DEGRADED",
    "ALARM_TRUNCATION",
    "ALARM_ZERO_COST",
    "Dispatch",
    "LADDER_ADAPTER",
    "Transport",
    "caps_for_tier",
    "make_notifier",
    "run_galley",
]
