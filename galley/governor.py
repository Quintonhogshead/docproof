"""Budget allocator and hard spending gates for Galley waves.

The governor owns per-wave and total caps, records charges in an append-only
ledger, and decides whether another wave or panel call may proceed. Gated
charges cannot exceed caps; explicitly allowed overruns remain recorded and are
flagged. :meth:`Governor.should_stop` also checks marginal cost and wave limits.

It performs no I/O or model calls and can be reconstructed from the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from galley.casefile import BudgetLedger, Charge
from galley.contracts import _known

# Charges recorded by :meth:`Governor.charge_panel_call` carry this label prefix
# so the panel-call counter can be recovered from a loaded ledger.
PANEL_LABEL_PREFIX = "panel:"


class GovernorError(RuntimeError):
    """Base class for governor guard violations."""


class BudgetError(GovernorError):
    """A charge would exceed the per-wave or total cap."""


class WaveLimitError(GovernorError):
    """Opening another wave would exceed ``max_waves``."""


class PanelLimitError(GovernorError):
    """Another panel call would exceed ``max_panel_calls``."""


@dataclass(frozen=True)
class Caps:
    """The hard limits the governor enforces.

    ``total_usd`` bounds spend over the whole job; ``per_wave_usd`` bounds spend
    within one wave; ``max_waves`` bounds how many waves may be opened;
    ``max_panel_calls`` bounds escalations to the panel. Serializes into
    ``BudgetLedger.caps`` so it round-trips inside the case file.
    """

    total_usd: float
    per_wave_usd: float
    max_waves: int
    max_panel_calls: int

    def to_json(self) -> dict[str, Any]:
        return {
            "total_usd": self.total_usd,
            "per_wave_usd": self.per_wave_usd,
            "max_waves": self.max_waves,
            "max_panel_calls": self.max_panel_calls,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Caps":
        d = _known(cls, data)
        return cls(
            total_usd=float(d.get("total_usd", 0.0)),
            per_wave_usd=float(d.get("per_wave_usd", 0.0)),
            max_waves=int(d.get("max_waves", 0)),
            max_panel_calls=int(d.get("max_panel_calls", 0)),
        )


class Governor:
    """Budget allocator over a :class:`BudgetLedger` and a :class:`Caps`.

    Construction stamps the caps snapshot into ``ledger.caps`` and derives the
    live counters (current wave, per-wave spend, waves opened, panel calls) from
    whatever charges the ledger already carries, so a fresh ledger starts at zero
    and a loaded one resumes where it left off.
    """

    def __init__(self, ledger: BudgetLedger, caps: Caps) -> None:
        self._ledger = ledger
        self._caps = caps
        # The ledger round-trips the caps snapshot into the case file.
        ledger.caps = caps.to_json()
        self._recompute_from_ledger()

    # ---- reconstruction ------------------------------------------------

    @classmethod
    def from_ledger(cls, ledger: BudgetLedger) -> "Governor":
        """Rebuild a governor from a loaded ledger, reading caps from it."""
        return cls(ledger, Caps.from_json(ledger.caps))

    def _recompute_from_ledger(self) -> None:
        charges = self._ledger.charges
        if charges:
            self._current_wave = max(c.wave for c in charges)
        else:
            self._current_wave = 0
        # Waves are numbered consecutively from 1 by ``open_wave``, so the
        # highest wave index seen is the number of waves opened.
        self._waves_opened = self._current_wave
        self._wave_spent = sum(
            c.cost_usd for c in charges if c.wave == self._current_wave
        )
        self._panel_calls = sum(
            1 for c in charges if c.label.startswith(PANEL_LABEL_PREFIX)
        )
        # Replay the charges in order to recover which ones breached a cap, so
        # a loaded ledger remembers its overruns without a second record.
        self._overruns: list[Charge] = []
        total = 0.0
        per_wave: dict[int, float] = {}
        for c in charges:
            total += c.cost_usd
            per_wave[c.wave] = per_wave.get(c.wave, 0.0) + c.cost_usd
            if (total > self._caps.total_usd
                    or per_wave[c.wave] > self._caps.per_wave_usd):
                self._overruns.append(c)

    # ---- spend predicate & choke point ---------------------------------

    def can_spend(self, action_cost: float) -> bool:
        """Would charging ``action_cost`` stay within BOTH the per-wave and
        total caps? A negative cost is never spendable."""
        if action_cost < 0:
            return False
        within_total = self._ledger.spent_usd + action_cost <= self._caps.total_usd
        within_wave = self._wave_spent + action_cost <= self._caps.per_wave_usd
        return within_total and within_wave

    def charge(
        self, cost: float, label: str, *, allow_over_cap: bool = False
    ) -> Charge:
        """Record ``cost`` against the ledger and the current wave.

        The single choke point for spend: it re-checks :meth:`can_spend` and
        raises :class:`BudgetError` rather than record an over-cap charge.
        Callers should gate on :meth:`can_spend` first; a charge that trips this
        guard is a bug.

        ``allow_over_cap=True`` is for money that has ALREADY been spent — a
        detector that billed more than its pre-flight estimate. The charge is
        recorded regardless (an uncharged dollar is a lie in the ledger) and,
        if it breached a cap, also appended to :attr:`overruns` so the caller
        can raise the alarm. A negative cost is refused either way.
        """
        if not self.can_spend(cost):
            if not allow_over_cap or cost < 0:
                raise BudgetError(
                    f"charge of ${cost:.4f} ({label!r}) would exceed a cap: "
                    f"wave {self._wave_spent:.4f}+{cost:.4f} vs {self._caps.per_wave_usd}, "
                    f"total {self._ledger.spent_usd:.4f}+{cost:.4f} vs {self._caps.total_usd}"
                )
            self._wave_spent += cost
            entry = self._ledger.charge(label, cost, wave=self._current_wave)
            self._overruns.append(entry)
            return entry
        self._wave_spent += cost
        return self._ledger.charge(label, cost, wave=self._current_wave)

    # ---- wave accounting -----------------------------------------------

    def open_wave(self) -> int:
        """Open the next wave: reset the per-wave counter, bump the wave number.

        Raises :class:`WaveLimitError` when ``max_waves`` waves have already been
        opened. Returns the new wave number (1-based).
        """
        if self._waves_opened >= self._caps.max_waves:
            raise WaveLimitError(
                f"cannot open wave {self._waves_opened + 1}: "
                f"max_waves is {self._caps.max_waves}"
            )
        self._waves_opened += 1
        self._current_wave = self._waves_opened
        self._wave_spent = 0.0
        return self._current_wave

    def close_wave(self) -> float:
        """Close the current wave; return what it spent. The per-wave counter is
        reset by the next :meth:`open_wave`."""
        return self._wave_spent

    def can_open_wave(self) -> bool:
        return self._waves_opened < self._caps.max_waves

    # ---- panel-call accounting -----------------------------------------

    def can_escalate(self) -> bool:
        """Is there a panel call left under ``max_panel_calls``?"""
        return self._panel_calls < self._caps.max_panel_calls

    def charge_panel_call(self, cost: float, label: str = "panel call") -> Charge:
        """Escalate to the panel: bump the panel counter and record its cost.

        Raises :class:`PanelLimitError` past ``max_panel_calls`` and
        :class:`BudgetError` if the cost would breach a budget cap; neither
        side-effect lands when it raises.
        """
        if not self.can_escalate():
            raise PanelLimitError(
                f"cannot make panel call {self._panel_calls + 1}: "
                f"max_panel_calls is {self._caps.max_panel_calls}"
            )
        if not self.can_spend(cost):
            raise BudgetError(
                f"panel call of ${cost:.4f} would exceed a budget cap"
            )
        self._panel_calls += 1
        return self.charge(cost, PANEL_LABEL_PREFIX + label)

    # ---- stop rule -----------------------------------------------------

    def should_stop(
        self, marginal_cost_per_validated_finding: float, threshold: float
    ) -> bool:
        """Stop when the marginal $/validated-finding reaches ``threshold``, or a
        hard cap is reached: no wave left to open, or the total budget floor hit.
        """
        if marginal_cost_per_validated_finding >= threshold:
            return True
        if not self.can_open_wave():
            return True
        if self.remaining_usd <= 0:
            return True
        return False

    # ---- exposed totals ------------------------------------------------

    @property
    def ledger(self) -> BudgetLedger:
        """The underlying ledger, for saving into the case file."""
        return self._ledger

    @property
    def caps(self) -> Caps:
        return self._caps

    @property
    def spent_usd(self) -> float:
        return self._ledger.spent_usd

    @property
    def remaining_usd(self) -> float:
        return self._caps.total_usd - self._ledger.spent_usd

    @property
    def wave_spent_usd(self) -> float:
        return self._wave_spent

    @property
    def overruns(self) -> tuple[Charge, ...]:
        """Charges that landed past a cap (``allow_over_cap`` charges only)."""
        return tuple(self._overruns)

    @property
    def overrun_usd(self) -> float:
        """How far the ledger sits past the total cap; zero while within it."""
        return max(0.0, self._ledger.spent_usd - self._caps.total_usd)

    @property
    def wave_remaining_usd(self) -> float:
        return self._caps.per_wave_usd - self._wave_spent

    @property
    def current_wave(self) -> int:
        return self._current_wave

    @property
    def waves_opened(self) -> int:
        return self._waves_opened

    @property
    def waves_remaining(self) -> int:
        return max(0, self._caps.max_waves - self._waves_opened)

    @property
    def panel_calls(self) -> int:
        return self._panel_calls

    @property
    def panel_calls_remaining(self) -> int:
        return max(0, self._caps.max_panel_calls - self._panel_calls)


__all__ = [
    "BudgetError",
    "Caps",
    "Governor",
    "GovernorError",
    "PANEL_LABEL_PREFIX",
    "PanelLimitError",
    "WaveLimitError",
]
