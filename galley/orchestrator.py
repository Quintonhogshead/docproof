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
from typing import Any, Callable, Protocol

from docproof.models import Usage

from galley.adapters import DetectorAdapter, Scope
from galley.casefile import CaseFile, append_wave
from galley.contracts import GFinding, Hypothesis, Manuscript, WaveRecord
from galley.governor import Caps, Governor, WaveLimitError

LADDER_ADAPTER = "docproof_ladder"

# Alarm classes the loop raises the moment they happen — the failure modes a
# person needs to hear about mid-run, not at the end (see E4). A digest is the
# routine per-wave summary; an alarm is an interruption.
ALARM_DEGRADED = "degraded_pass"
ALARM_ZERO_COST = "zero_cost_detector"
ALARM_TRUNCATION = "truncation_streak"
ALARM_BUDGET_CAP = "budget_cap_hit"
TRUNCATION_STREAK_LIMIT = 3
# Detectors that legitimately bill nothing, so a $0 charge from them is not the
# silent-no-run failure the zero-cost alarm exists to catch.
FREE_ADAPTERS = frozenset({"spellscan", "languagetool"})

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
    free_adapters: frozenset[str] = FREE_ADAPTERS,
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
    streak = {"n": 0}  # consecutive waves that lost windows to truncation

    def _save() -> None:
        cf.budget = gov.ledger
        cf.save(cf_path)

    def _after_wave(rec: WaveRecord) -> None:
        """Emit this wave's alarms (immediate) then its routine digest."""
        if not notify:
            return
        for alarm in _wave_alarms(rec, gov, streak=streak, free_adapters=free_adapters):
            notify("alarm", alarm)
        notify("wave", _wave_digest(cf, rec))

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
        _after_wave(rec)
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
    """The alarms one wave's outcome raises, if any.

    * degraded pass — a coverage note reports a whole pass fell open;
    * zero-cost detector — a paid adapter ran but billed nothing (the silent
      no-run, Sapling's classic failure);
    * truncation streak — ``TRUNCATION_STREAK_LIMIT`` consecutive waves lost
      windows to a token ceiling;
    * budget cap hit — a dispatch was skipped for budget, or the total floor is
      reached.

    ``streak`` is carried across waves by the caller so the truncation run counts.
    """

    notes: list[str] = []
    zero_cost: list[str] = []
    over_budget = False
    for action in rec.actions:
        if not isinstance(action, dict):
            continue
        notes.extend(action.get("coverage_notes", []))
        if action.get("skipped") in ("over budget", "budget exhausted"):
            over_budget = True
        ran = "skipped" not in action
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
    return alarms


class Transport(Protocol):
    """A pluggable delivery channel for digests and alarms.

    One method, so email today and Twilio/Pushover later add a transport without
    touching the orchestrator or the notifier. ``kind`` is ``"wave"`` or
    ``"alarm"``.
    """

    def send(self, kind: str, subject: str, body: str) -> None: ...


def make_notifier(transport: Transport, *, book: str = "") -> Notify:
    """Turn a :class:`Transport` into the orchestrator's ``notify`` callable.

    Formats a per-wave digest and each alarm into a subject/body and hands them to
    the transport. Call sites stay transport-agnostic: swapping the transport
    changes where the message goes, not how the loop emits it.
    """

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
