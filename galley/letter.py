"""Editorial letter and style-sheet renderers — pure templating over the case file.

Two human-facing Markdown documents fall out of a finished Galley run, and both
are *reports*, not decisions: they read the durable :class:`~galley.casefile.CaseFile`
and lay it out. Nothing here calls a model, touches the network, or mutates the
case file — the run already happened; this narrates it.

* :func:`render_letter` writes ``letter.md``, the cover letter a proofreader
  sends: what ran wave by wave, the spend curve, coverage holes, every open
  query, and an honest confidence statement.
* :func:`render_style_sheet` writes ``style-sheet.md``, the per-book decision log
  drawn from ``cf.style_sheet``.
* :func:`render_all` writes both.

The letter's cardinal rule: **no unresolved query is ever hidden.** Every
``query`` verdict, and any finding routed to the query channel, appears by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from galley.casefile import CaseFile
from galley.contracts import Manuscript, WaveRecord


# --------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------


def _money(value: float) -> str:
    """Format a dollar amount to cents, e.g. ``$1.23``."""

    return f"${value:,.2f}"


def _recall_rate(recall: Any) -> float | None:
    """Pull an overall recall rate out of a RecallEstimate-like object or dict.

    Tolerant of shape: accepts a mapping or an object, and looks for any of a
    few conventional names for the headline rate. Returns ``None`` when nothing
    usable is present so the caller can simply omit the confidence figure.
    """

    if recall is None:
        return None
    for key in ("overall", "overall_rate", "rate", "recall"):
        value: Any = None
        if isinstance(recall, dict):
            if key in recall:
                value = recall[key]
        elif hasattr(recall, key):
            value = getattr(recall, key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _pct(rate: float) -> str:
    """Render a rate as a percentage; accepts either 0..1 or 0..100 input."""

    pct = rate * 100.0 if rate <= 1.0 else rate
    return f"{pct:.1f}%"


def _wave_action_lines(wave: WaveRecord) -> list[str]:
    """Summarize each action dict a wave dispatched into a bullet line."""

    lines: list[str] = []
    for action in wave.actions:
        adapter = action.get("adapter") or action.get("action") or "action"
        scope = action.get("scope")
        cost = action.get("cost_usd")
        added = action.get("findings_added")
        parts = [f"**{adapter}**"]
        if scope:
            parts.append(f"scope `{scope}`")
        if added is not None:
            parts.append(f"{added} finding(s)")
        if cost is not None:
            try:
                parts.append(_money(float(cost)))
            except (TypeError, ValueError):
                pass
        lines.append("  - " + ", ".join(parts))
    return lines


def _coverage_notes(cf: CaseFile) -> list[str]:
    """Every coverage note across every action of every wave, in wave order."""

    notes: list[str] = []
    for wave in cf.waves:
        for action in wave.actions:
            raw = action.get("coverage_notes") or []
            if isinstance(raw, str):
                raw = [raw]
            for note in raw:
                notes.append(f"wave {wave.index}: {note}")
    return notes


# --------------------------------------------------------------------------
# section renderers
# --------------------------------------------------------------------------


def _section_what_ran(cf: CaseFile) -> list[str]:
    out = ["## What ran", ""]
    if not cf.waves:
        out.append("_No waves were recorded._")
        out.append("")
        return out
    for wave in sorted(cf.waves, key=lambda w: w.index):
        out.append(f"### Wave {wave.index}")
        span = " → ".join(x for x in (wave.started_at, wave.ended_at) if x)
        if span:
            out.append(f"- Timing: {span}")
        out.append(f"- Spend: {_money(wave.spend_usd)}")
        out.append(f"- Findings added: {wave.findings_added}")
        action_lines = _wave_action_lines(wave)
        if action_lines:
            out.append("- Dispatched:")
            out.extend(action_lines)
        else:
            out.append("- Dispatched: (no actions recorded)")
        out.append("")
    return out


def _section_spend(cf: CaseFile) -> list[str]:
    out = ["## Spend curve", ""]
    out.append("| Wave | Spend | Cumulative |")
    out.append("| --- | --- | --- |")
    cumulative = 0.0
    for wave in sorted(cf.waves, key=lambda w: w.index):
        cumulative += wave.spend_usd
        out.append(f"| {wave.index} | {_money(wave.spend_usd)} | {_money(cumulative)} |")
    out.append("")
    out.append(f"**Total spend: {_money(cf.budget.spent_usd)}**")
    if cf.budget.charges:
        out.append("")
        out.append("Ledger charges:")
        for charge in cf.budget.charges:
            out.append(
                f"- wave {charge.wave}: {charge.label} — {_money(charge.cost_usd)}"
            )
    out.append("")
    return out


def _section_coverage(cf: CaseFile) -> list[str]:
    out = ["## Coverage holes", ""]
    notes = _coverage_notes(cf)
    if not notes:
        out.append("Coverage was clean — no gaps were flagged by any adapter.")
    else:
        out.append(
            "The following gaps were flagged during the run; a human should "
            "weigh whether they need a targeted pass:"
        )
        out.append("")
        for note in notes:
            out.append(f"- {note}")
    out.append("")
    return out


def _section_queries(cf: CaseFile) -> list[str]:
    """Every unresolved query — from a query verdict or a query-routed finding.

    Nothing is deduplicated away that would hide a distinct query: verdicts are
    keyed by finding id, and a finding whose id never received a query verdict
    but whose own confidence marks it a query is surfaced too.
    """

    out = ["## Open queries", ""]
    finding_by_id = {f.id: f for f in cf.findings}

    query_verdicts = [v for v in cf.verdicts if v.ruling == "query"]
    verdict_ids = {v.finding_id for v in query_verdicts}

    if not query_verdicts:
        out.append("No open queries — every finding was adjudicated to a decision.")
        out.append("")
        return out

    out.append(
        "These need an editorial decision before delivery. Each is listed so "
        "none is silently dropped:"
    )
    out.append("")
    for verdict in query_verdicts:
        finding = finding_by_id.get(verdict.finding_id)
        head = f"- **{verdict.finding_id}**"
        if finding is not None:
            head += f" ({finding.error_type})"
            if finding.find:
                head += f": `{finding.find}` → `{finding.replace}`"
        reason = verdict.reason or (finding.note if finding else "") or "(no reason recorded)"
        out.append(f"{head} — {reason}")
    out.append("")

    # Findings whose id carries no query verdict at all but that name themselves
    # a query in their note/confidence still deserve a line, not silence.
    orphans = [
        f
        for f in cf.findings
        if f.id not in verdict_ids
        and (f.confidence == "query" or f.error_type == "query")
    ]
    if orphans:
        out.append("Additionally routed to query with no separate verdict:")
        for finding in orphans:
            note = finding.note or "(no note)"
            out.append(f"- **{finding.id}** ({finding.error_type}) — {note}")
        out.append("")
    return out


def _section_confidence(
    cf: CaseFile, ms: Manuscript | None, recall: Any
) -> list[str]:
    out = ["## Confidence", ""]
    rate = _recall_rate(recall)
    if rate is not None:
        out.append(
            f"Against our seeded gauge, estimated recall is **{_pct(rate)}**. "
            "Read this honestly: a seeded gauge only measures the error classes "
            "we know to seed, so it cannot see blind spots outside our own "
            "taxonomy. The true miss rate is at least this high."
        )
    else:
        out.append(
            "No recall estimate was supplied for this run. Absence of a figure "
            "is not a clean bill of health — a seeded gauge can only see error "
            "classes inside our own taxonomy."
        )
    out.append("")

    if ms is not None and ms.chapters:
        out.append("Per-chapter finding counts:")
        out.append("")
        counts: dict[int, int] = {}
        para_to_chapter: dict[str, int] = {}
        for chapter in ms.chapters:
            for pid in chapter.para_ids:
                para_to_chapter[pid] = chapter.index
        for finding in cf.findings:
            idx = para_to_chapter.get(finding.span.para_id)
            if idx is not None:
                counts[idx] = counts.get(idx, 0) + 1
        for chapter in ms.chapters:
            out.append(
                f"- {chapter.title or f'Chapter {chapter.index}'}: "
                f"{counts.get(chapter.index, 0)} finding(s)"
            )
        out.append("")
    return out


# --------------------------------------------------------------------------
# public renderers
# --------------------------------------------------------------------------


def render_letter(
    cf: CaseFile,
    out_dir: str | Path,
    *,
    ms: Manuscript | None = None,
    recall: Any = None,
) -> Path:
    """Render the editorial cover letter to ``out_dir/letter.md``; return the path."""

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    title = cf.book or "the manuscript"
    lines.append(f"# Editorial letter — {title}")
    lines.append("")
    lines.append(
        "This letter reports what our proofreading pass did: which waves ran, "
        "what they cost, where coverage may be thin, and every question left "
        "open for you. It is a record of the run, not a set of new decisions."
    )
    lines.append("")

    lines.extend(_section_what_ran(cf))
    lines.extend(_section_spend(cf))
    lines.extend(_section_coverage(cf))
    lines.extend(_section_queries(cf))
    lines.extend(_section_confidence(cf, ms, recall))

    out_file = out_path / "letter.md"
    out_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_file


def render_style_sheet(cf: CaseFile, out_dir: str | Path) -> Path:
    """Render the per-book decision log to ``out_dir/style-sheet.md``; return the path."""

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    title = cf.book or "the manuscript"
    lines.append(f"# Style sheet — {title}")
    lines.append("")

    if not cf.style_sheet:
        lines.append(
            "_No rulings were recorded for this book yet._ The first ruling on "
            "each variant binds the book; once decisions are made they will be "
            "logged here with their rationale and cited authority."
        )
        lines.append("")
    else:
        lines.append(
            "Each entry is a binding decision for this book, with the authority "
            "cited. The first ruling on a variant binds it."
        )
        lines.append("")
        lines.append("| Subject | Decision | Rationale | Source | Wave |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ruling in cf.style_sheet:
            lines.append(
                f"| {ruling.subject} | {ruling.decision} | "
                f"{ruling.rationale or '—'} | {ruling.source or '—'} | "
                f"{ruling.wave} |"
            )
        lines.append("")

    out_file = out_path / "style-sheet.md"
    out_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_file


def render_all(cf: CaseFile, out_dir: str | Path, **kw: Any) -> tuple[Path, Path]:
    """Write both documents; return ``(letter_path, style_sheet_path)``.

    Keyword arguments (``ms``, ``recall``) are forwarded to :func:`render_letter`.
    """

    letter = render_letter(cf, out_dir, **kw)
    style = render_style_sheet(cf, out_dir)
    return letter, style


__all__ = [
    "render_all",
    "render_letter",
    "render_style_sheet",
]
