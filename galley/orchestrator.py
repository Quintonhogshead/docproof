"""The wave loop — Galley's stateless orchestrator over a durable case file.

Each cycle is a *wave*. Wave one is the full DocProof ladder; every later wave is
a targeted re-read the auditor asked for and the planner turned into dispatches.
The case file is saved after every wave, so the orchestrator holds no state
between waves: a crash, a kill, or a redeploy resumes by reloading the case file
and continuing at the last closed wave.

The auditor and planner are injected. Until Track D3 lands (gated on the P0
experiment), the defaults are a stub auditor that finds no hypotheses and a stub
planner that dispatches nothing — so ``run_galley`` runs the ladder and stops,
which is exactly the T0/T1 product. Tests inject scripted auditors/planners to
exercise multi-wave budget honoring and resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from docproof.models import Usage

from galley.adapters import DetectorAdapter, Scope
from galley.casefile import CaseFile, append_wave
from galley.contracts import GFinding, Hypothesis, Manuscript, WaveRecord
from galley.governor import Caps, Governor, WaveLimitError

LADDER_ADAPTER = "docproof_ladder"

# When marginal $/validated-finding reaches this, the governor stops. A wave that
# adds nothing has infinite marginal cost, so it always trips the stop.
DEFAULT_STOP_THRESHOLD = 1_000_000.0

# Coarse per-tier caps. max_waves is what makes T0/T1 ladder-only and T2+ agentic.
# The real dollar calibration is E1's job; these are structural, not priced.
_TIER_MAX_WAVES = {"T0": 1, "T1": 1, "T2": 2, "T3": 3, "T4": 6}
_TIER_MAX_PANEL = {"T0": 0, "T1": 1, "T2": 4, "T3": 12, "T4": 32}


@dataclass(frozen=True)
class Dispatch:
    """One unit of wave work: run adapter ``adapter`` over ``scope``.

    The planner (Track D3) produces these from its action union; the orchestrator
    only needs to know which adapter to call over which scope, so this stays the
    stable seam between the two tickets.
    """

    adapter: str
    scope: Scope


# Injected-hook types. The auditor reads the case file + manuscript and proposes
# hypotheses; the planner turns hypotheses (and the budget) into dispatches.
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
    """Build the governor caps for a tier and a total budget.

    ``per_wave_usd`` is the whole budget: a single wave may use all of it, and the
    total cap is what actually bounds the run. ``max_waves`` is the structural
    difference between tiers.
    """

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
) -> WaveRecord:
    """Run one wave's dispatches, folding findings and spend into the case file.

    New findings union into ``cf.findings`` by id (wave one is never destabilized:
    a later wave can add an id but never overwrite one). Spend is charged through
    the governor, the single choke point; a dispatch that cannot be afforded ends
    the wave rather than overspending.
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

        result = adapter.run(ms, d.scope, budget, usage)

        if result.cost_usd > 0:
            if not gov.can_spend(result.cost_usd):
                # Can't afford what it actually cost — record and stop the wave.
                actions.append(
                    {"adapter": d.adapter, "skipped": "over budget",
                     "cost_usd": result.cost_usd}
                )
                break
            gov.charge(result.cost_usd, f"wave{wave_index}:{d.adapter}")
            wave_cost += result.cost_usd

        new_here = 0
        for f in result.findings:
            if f.id in known_ids:
                continue
            known_ids.add(f.id)
            cf.findings.append(f)
            added += 1
            new_here += 1

        actions.append(
            {
                "adapter": d.adapter,
                "scope": _scope_json(d.scope),
                "cost_usd": result.cost_usd,
                "findings_added": new_here,
                "coverage_notes": list(result.coverage_notes),
            }
        )

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
) -> CaseFile:
    """Run the wave loop to convergence or budget, returning the case file.

    Wave one is the ``ladder`` adapter over the whole book. Then, while the
    governor permits and the planner still asks for work: audit -> record
    hypotheses -> plan -> dispatch -> record the wave -> stop check. The case file
    is saved after wave one and after every subsequent wave; a rerun over an
    existing case file resumes at the last closed wave (the ladder is never
    re-run).
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
    # A completed wave may have cost nothing, leaving no ledger charge to
    # reconstruct its number from — so align the governor's wave counter to the
    # authoritative wave history before opening any new wave.
    while gov.waves_opened < len(cf.waves):
        try:
            gov.open_wave()
        except WaveLimitError:
            break

    usage = Usage()

    def _save() -> None:
        cf.budget = gov.ledger
        cf.save(cf_path)

    # Wave one: the full ladder. Skipped on resume (cf.waves already populated).
    if not cf.waves:
        gov.open_wave()  # wave 1
        rec = _run_wave(
            wave_index=gov.current_wave,
            dispatches=[Dispatch(ladder, Scope())],
            adapters=adapters,
            ms=ms,
            gov=gov,
            usage=usage,
            cf=cf,
            clock=clock,
        )
        append_wave(cf, rec)
        _save()
        if notify:
            notify("wave", _wave_digest(cf, rec))
        last = rec
    else:
        last = cf.waves[-1]

    # Subsequent waves: audit -> plan -> dispatch, until the governor stops or the
    # planner runs dry.
    while not gov.should_stop(_marginal(last), stop_threshold):
        hyps = audit(cf, ms)
        if hyps:
            cf.hypotheses.extend(hyps)
            _save()

        dispatches = plan_wave(hyps, gov, cf)
        if not dispatches:
            break

        try:
            wave_index = gov.open_wave()
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
        )
        append_wave(cf, rec)
        _save()
        if notify:
            notify("wave", _wave_digest(cf, rec))
        last = rec

    _save()
    return cf


def _wave_digest(cf: CaseFile, rec: WaveRecord) -> dict[str, Any]:
    """A small per-wave digest for the notify hook (E4 formalizes the channel)."""
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


__all__ = [
    "Dispatch",
    "LADDER_ADAPTER",
    "caps_for_tier",
    "run_galley",
]
