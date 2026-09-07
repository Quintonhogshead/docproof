"""The plan ledger: what PLAN.md promised, line by line, and what became of it.

A plan line is a promise. On 2026-09-07 the profile phase found a timeline
problem (the Ch.3/Ch.4 August ordering), deferred it to plan line 4c — the $0
whole-book continuity subagent — and 4c never ran. Nothing noticed: certify
checks hashes, routes, budget and the delivered text, never whether every
approved line executed, so a known issue reached the author unqueried.

The ledger closes that gap without inventing a protocol: the brain records
each numbered plan line as ``ran`` (with the artifact it produced), ``skipped``
(with the reason) or ``deferred`` (with where the work went), and certify
fails on any line it cannot account for. The letter then says, in words, what
the plan promised that did not happen.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

LEDGER_NAME = "plan_ledger.json"
STATUSES = ("ran", "skipped", "deferred")

# A numbered plan line that carries a price — the same definition the plan
# gate uses for a promise (galley.driver._PLAN_ITEM_RE): "2.", "4b.", "0)".
_ITEM_RE = re.compile(
    r"^\s*(?P<label>\d+[a-z]?)[.)]\s+(?P<text>\S.*?)\s*$")
_PRICE_RE = re.compile(r"\$\s*\d")


@dataclass(frozen=True)
class PlanItem:
    label: str
    text: str


def plan_items(plan_text: str) -> list[PlanItem]:
    """Every numbered, priced line of a PLAN.md, in order, first occurrence
    of each label. A caveats list ("3. The book arguably wants…") has no
    price and is not a promise."""
    seen: set[str] = set()
    out: list[PlanItem] = []
    for line in plan_text.splitlines():
        m = _ITEM_RE.match(line)
        if not m or not _PRICE_RE.search(line):
            continue
        label = m.group("label").lower()
        if label in seen:
            continue
        seen.add(label)
        out.append(PlanItem(label, " ".join(m.group("text").split())))
    return out


def load_ledger(path: str | Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("items") if isinstance(raw, dict) else None
    return {str(k).lower(): dict(v) for k, v in (items or {}).items()
            if isinstance(v, dict)}


def record(path: str | Path, label: str, status: str, *,
           evidence: str = "", reason: str = "") -> dict[str, Any]:
    """Record one plan line's fate; returns the entry written."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    p = Path(path)
    items = load_ledger(p)
    entry = {"status": status, "evidence": evidence.strip(),
             "reason": reason.strip()}
    items[label.lower()] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "items": items}, indent=2,
                            ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


def audit(plan_text: str, ledger: Mapping[str, Mapping[str, Any]] | None
          ) -> list[tuple[PlanItem, str, str]]:
    """``(item, status, problem)`` per plan line. ``status`` is the ledger's
    (or ``"unrecorded"``); ``problem`` is empty when the line is accounted
    for, else why it is not."""
    out = []
    for item in plan_items(plan_text):
        entry = (ledger or {}).get(item.label)
        if not entry:
            out.append((item, "unrecorded", "no ledger entry"))
            continue
        status = str(entry.get("status") or "")
        evidence = str(entry.get("evidence") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if status not in STATUSES:
            out.append((item, status, f"unknown status {status!r}"))
        elif status == "ran" and not evidence:
            out.append((item, status, "ran, but names no artifact"))
        elif status == "skipped" and not reason:
            out.append((item, status, "skipped without a reason"))
        elif status == "deferred" and not (evidence or reason):
            out.append((item, status, "deferred to nowhere"))
        else:
            out.append((item, status, ""))
    return out


def check(plan_text: str, ledger: Mapping[str, Mapping[str, Any]] | None,
          *, ledger_exists: bool) -> tuple[str, str]:
    """``(status, detail)`` for the certificate."""
    items = plan_items(plan_text)
    if not items:
        return "skip", "no priced plan lines to account for"
    if not ledger_exists:
        return "fail", (f"{len(items)} plan line(s) and no {LEDGER_NAME} — "
                        f"record each with `docproof galley plan-line`")
    rows = audit(plan_text, ledger)
    bad = [(it, st, why) for it, st, why in rows if why]
    if bad:
        return "fail", "; ".join(
            f"line {it.label} ({it.text[:40]}): {why}" for it, st, why in bad)
    counts: dict[str, int] = {}
    for _it, st, _why in rows:
        counts[st] = counts.get(st, 0) + 1
    return "pass", ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))


def not_done(plan_text: str, ledger: Mapping[str, Mapping[str, Any]] | None
             ) -> list[tuple[PlanItem, str, str]]:
    """The promised lines that did not run: ``(item, status, explanation)``."""
    out = []
    for it, st, _why in audit(plan_text, ledger):
        if st in ("skipped", "deferred", "unrecorded"):
            entry = (ledger or {}).get(it.label) or {}
            why = str(entry.get("reason") or entry.get("evidence") or
                      "not recorded").strip()
            out.append((it, st, why))
    return out


__all__ = ["LEDGER_NAME", "STATUSES", "PlanItem", "audit", "check",
           "load_ledger", "not_done", "plan_items", "record"]
