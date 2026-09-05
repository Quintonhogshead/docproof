"""Write the scorecard: scorecard.json (machine-diffable) and scorecard.md
(readable). One run in, the three-threshold curve out."""
from __future__ import annotations

import json
from pathlib import Path

from .scorer import Scorecard

THRESHOLDS = ("low", "medium", "high")


def _pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "—"


def _num(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def _card_json(card: Scorecard) -> dict:
    p, r, f = card.micro
    mp, mr, mf = card.macro
    return {
        "threshold": card.threshold,
        "micro": {"precision": _num(p), "recall": _num(r), "f1": _num(f)},
        "macro": {"precision": _num(mp), "recall": _num(mr), "f1": _num(mf)},
        "trap_fp_rate": _num(card.trap_fp_rate),
        "anchor_failure_rate": _num(card.anchor_failure_rate),
        "types": {
            t.error_type: {
                "seeded": t.seeded, "traps": t.traps, "caught": t.caught,
                "correction_exact": t.correction_exact,
                "tp_flags": t.tp_flags, "fp_flags": t.fp_flags,
                "trap_cases_flagged": t.trap_cases_flagged,
                "precision": _num(t.precision), "recall": _num(t.recall),
                "f1": _num(t.f1),
            }
            for t in sorted(card.types.values(), key=lambda t: t.error_type)
        },
    }


def write_scorecard(curve: dict[str, Scorecard], out_dir: str | Path,
                    *, default_gate: str = "medium") -> tuple[Path, Path]:
    """Write both files under `out_dir`. `default_gate` is the threshold the
    per-type table is shown at — the gate the pipeline ships with."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    any_card = curve[default_gate]

    payload = {
        "schema_version": 1,
        "model": any_card.model,
        "cost_usd": _num(any_card.cost),
        "input_tokens": any_card.input_tokens,
        "output_tokens": any_card.output_tokens,
        "total_findings": any_card.total_findings,
        "default_gate": default_gate,
        "by_threshold": {t: _card_json(curve[t]) for t in THRESHOLDS},
    }
    json_path = out_dir / "scorecard.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    md_path = out_dir / "scorecard.md"
    md_path.write_text(_render_md(curve, default_gate), encoding="utf-8")
    return json_path, md_path


def _render_md(curve: dict[str, Scorecard], default_gate: str) -> str:
    head = curve[default_gate]
    cost = f"${head.cost:.2f}" if head.cost is not None else "n/a"
    lines: list[str] = []
    lines.append(f"# Scorecard — `{head.model}`\n")
    lines.append(f"{head.input_tokens:,} in / {head.output_tokens:,} out "
                 f"tokens · {cost} · {head.total_findings} findings\n")

    lines.append("## Headline\n")
    lines.append(f"- **Trap false-positive rate** "
                 f"(deliberate prose wrongly flagged, at `{default_gate}`): "
                 f"**{_pct(head.trap_fp_rate)}**")
    lines.append(f"- **Anchor-failure rate** "
                 f"(`rejected_no_anchor` ÷ findings): "
                 f"{_pct(head.anchor_failure_rate)}\n")

    lines.append("## Precision / recall by confidence gate\n")
    lines.append("| gate | micro P | micro R | micro F1 | macro F1 | trap FP |")
    lines.append("|---|---|---|---|---|---|")
    for t in THRESHOLDS:
        c = curve[t]
        p, r, f = c.micro
        _, _, mf = c.macro
        star = " ◄ ships" if t == default_gate else ""
        lines.append(f"| {t}{star} | {_pct(p)} | {_pct(r)} | {_pct(f)} "
                     f"| {_pct(mf)} | {_pct(c.trap_fp_rate)} |")
    lines.append("")

    lines.append(f"## By error type (at `{default_gate}`)\n")
    lines.append("| type | seeded | caught | R | traps | trap-flagged "
                 "| P | F1 | fix-exact |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for t in sorted(head.types.values(), key=lambda t: (t.f1 or -1)):
        lines.append(
            f"| {t.error_type} | {t.seeded} | {t.caught} | {_pct(t.recall)} "
            f"| {t.traps} | {t.trap_cases_flagged} | {_pct(t.precision)} "
            f"| {_pct(t.f1)} | {t.correction_exact}/{t.caught} |")
    lines.append("")
    return "\n".join(lines)
