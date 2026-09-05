"""Durable control-plane state for a Galley run.

The JSON file stores findings, verdicts, style rulings, hypotheses, budget and
wave history, allowing the orchestrator to resume between waves.

Two invariants:

* **Atomic on disk.** ``save`` writes through a temp file and ``os.replace``
  (the corrections engine's pattern, via :func:`docproof.utils.files.write_atomic`),
  so a reader — or a crash — never sees a torn file.
* **Waves are append-only.** :func:`append_wave` refuses to rewrite or reorder a
  closed wave. History only grows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docproof.utils.files import write_atomic

from galley.contracts import (
    CASEFILE_SCHEMA_VERSION,
    GFinding,
    Hypothesis,
    Verdict,
    WaveRecord,
    _known,
)


@dataclass(frozen=True)
class Ruling:
    """One style-sheet decision: the first ruling on a variant binds the book.

    ``subject`` is the thing decided (``"traveled / travelled"``), ``decision``
    the chosen form, ``rationale`` why, ``source`` the authority cited (``"CMOS
    7.1"``, ``"house: number policy"``). ``wave`` records when it was set.
    """

    subject: str
    decision: str
    rationale: str = ""
    source: str = ""
    wave: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "decision": self.decision,
            "rationale": self.rationale,
            "source": self.source,
            "wave": self.wave,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Ruling":
        d = _known(cls, data)
        return cls(
            subject=str(d.get("subject", "")),
            decision=str(d.get("decision", "")),
            rationale=str(d.get("rationale", "")),
            source=str(d.get("source", "")),
            wave=int(d.get("wave", 0)),
        )


@dataclass(frozen=True)
class Charge:
    """One entry in the budget ledger: what was spent, on what, in which wave."""

    label: str
    cost_usd: float
    wave: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "cost_usd": self.cost_usd, "wave": self.wave}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Charge":
        d = _known(cls, data)
        return cls(
            label=str(d.get("label", "")),
            cost_usd=float(d.get("cost_usd", 0.0)),
            wave=int(d.get("wave", 0)),
        )


@dataclass
class BudgetLedger:
    """The append-only record of spend.

    The governor (Track D) owns the caps and the stop rule; this is the ledger it
    writes into and that serializes into the case file. ``caps`` is a free-form
    dict the governor stashes its :class:`Caps` snapshot in, so the governor can
    round-trip its own config without this contract having to know its shape.
    """

    charges: list[Charge] = field(default_factory=list)
    caps: dict[str, Any] = field(default_factory=dict)

    @property
    def spent_usd(self) -> float:
        return sum(c.cost_usd for c in self.charges)

    def charge(self, label: str, cost_usd: float, *, wave: int = 0) -> Charge:
        """Append a charge and return it. Never rewrites an existing entry."""
        entry = Charge(label=label, cost_usd=cost_usd, wave=wave)
        self.charges.append(entry)
        return entry

    def to_json(self) -> dict[str, Any]:
        return {
            "charges": [c.to_json() for c in self.charges],
            "caps": dict(self.caps),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BudgetLedger":
        d = _known(cls, data)
        return cls(
            charges=[Charge.from_json(c) for c in (d.get("charges", []) or [])],
            caps=dict(d.get("caps", {}) or {}),
        )


@dataclass
class CaseFile:
    """The whole per-book state, loadable and savable as one JSON file."""

    book: str = ""
    schema_version: int = CASEFILE_SCHEMA_VERSION
    style_sheet: list[Ruling] = field(default_factory=list)
    findings: list[GFinding] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    waves: list[WaveRecord] = field(default_factory=list)
    budget: BudgetLedger = field(default_factory=BudgetLedger)


    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "book": self.book,
            "style_sheet": [r.to_json() for r in self.style_sheet],
            "findings": [f.to_json() for f in self.findings],
            "verdicts": [v.to_json() for v in self.verdicts],
            "hypotheses": [h.to_json() for h in self.hypotheses],
            "waves": [w.to_json() for w in self.waves],
            "budget": self.budget.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CaseFile":
        d = _known(cls, data)
        return cls(
            book=str(d.get("book", "")),
            schema_version=int(d.get("schema_version", CASEFILE_SCHEMA_VERSION)),
            style_sheet=[Ruling.from_json(r) for r in (d.get("style_sheet", []) or [])],
            findings=[GFinding.from_json(f) for f in (d.get("findings", []) or [])],
            verdicts=[Verdict.from_json(v) for v in (d.get("verdicts", []) or [])],
            hypotheses=[
                Hypothesis.from_json(h) for h in (d.get("hypotheses", []) or [])
            ],
            waves=[WaveRecord.from_json(w) for w in (d.get("waves", []) or [])],
            budget=BudgetLedger.from_json(d.get("budget", {}) or {}),
        )


    def save(self, path: str | Path) -> None:
        """Write the case file atomically (temp file + ``os.replace``)."""
        text = json.dumps(self.to_json(), indent=2, ensure_ascii=False)
        write_atomic(Path(path), text)

    @classmethod
    def load(cls, path: str | Path) -> "CaseFile":
        """Load a case file. Unknown keys are dropped, not fatal."""
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_json(json.loads(raw))


def append_wave(cf: CaseFile, record: WaveRecord) -> CaseFile:
    """Append ``record`` to ``cf.waves``, refusing to rewrite a closed wave.

    A wave's index must be strictly greater than every index already recorded —
    the same index twice, or an out-of-order index, is a bug in the caller, not a
    silent overwrite. Returns ``cf`` for chaining.
    """

    if cf.waves:
        last = max(w.index for w in cf.waves)
        if record.index <= last:
            raise ValueError(
                f"wave {record.index} would rewrite or reorder closed wave "
                f"history (highest recorded index is {last}); waves are append-only"
            )
    cf.waves.append(record)
    return cf


__all__ = [
    "BudgetLedger",
    "CaseFile",
    "Charge",
    "Ruling",
    "append_wave",
]
