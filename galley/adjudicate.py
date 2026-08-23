"""Adjudication and union merge.

New-wave findings enter through the same earliest-wins arbitration DocProof's
validator uses: the earliest finding to claim a span keeps it, so a later wave can
*add* findings but can never destabilize wave one. A finding that loses a span is
not deleted — it routes to the query channel, where the author still sees it.

Genuine disputes among later-wave findings go to a panel via
:func:`docproof.judges.screen` with a fixed spec; every ruling is recorded as a
:class:`~galley.contracts.Verdict` with its reason. The panel only ever screens
findings from waves after the first, so nothing here can delete a wave-one
finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docproof.judges import SPECS, screen
from docproof.models import Anchor, Finding, Usage

from galley.contracts import GFinding, Manuscript, Span, Verdict


@dataclass
class AdjudicationResult:
    """The outcome of adjudicating a union of findings.

    ``kept`` are the findings that hold a span and survived the panel; ``queries``
    are findings routed to the margin (overlap losers and panel rejects) — present,
    not vanished; ``verdicts`` records every non-trivial ruling with its reason.
    """

    kept: list[GFinding] = field(default_factory=list)
    queries: list[GFinding] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)


def _wave_of(f: GFinding) -> int:
    return f.provenance.wave if f.provenance else 0


def _overlaps(a: Span, b: Span) -> bool:
    """Do two spans in the same paragraph share any character?

    Pure insertions (``start == end``) are points: two insertions collide only at
    the same offset, and an insertion inside a replacement's range collides.
    """

    if a.para_id != b.para_id:
        return False
    if a.start == a.end or b.start == b.end:  # at least one insertion point
        lo, hi = (b.start, b.end) if a.start == a.end else (a.start, a.end)
        point = a.start if a.start == a.end else b.start
        if lo == hi:  # both insertions
            return point == lo
        return lo <= point <= hi
    return a.start < b.end and b.start < a.end


def arbitrate(findings: list[GFinding]) -> AdjudicationResult:
    """Earliest-wins span arbitration across waves.

    Findings are ordered by wave, then by their position in the input, so wave one
    claims its spans before any later wave. A finding overlapping an already-claimed
    span in the same paragraph loses and routes to the query channel with a verdict
    naming the winner.
    """

    order = sorted(range(len(findings)), key=lambda i: (_wave_of(findings[i]), i))
    accepted: dict[str, list[tuple[Span, str]]] = {}
    result = AdjudicationResult()

    for i in order:
        f = findings[i]
        claims = accepted.setdefault(f.span.para_id, [])
        winner = next(
            (wid for span, wid in claims if _overlaps(f.span, span)), None
        )
        if winner is None:
            claims.append((f.span, f.id))
            result.kept.append(f)
        else:
            result.queries.append(f)
            result.verdicts.append(
                Verdict(
                    finding_id=f.id,
                    ruling="query",
                    reason=f"overlaps earlier finding {winner}; routed to query",
                    judge="arbitrator",
                    wave=_wave_of(f),
                )
            )
    return result


def _to_docproof_finding(g: GFinding) -> Finding:
    """Minimal GFinding -> docproof Finding with an anchor, for the panel."""
    return Finding(
        finding_id=g.id,
        chunk_id="",
        para_id=g.span.para_id,
        error_type=g.error_type,
        original_text=g.find,
        occurrence=1,
        corrected_text=g.replace,
        explanation=g.note,
        confidence=g.confidence,
        status="validated",
        anchor=Anchor(
            start=g.span.start,
            end=g.span.end,
            delete_text=g.find,
            insert_text=g.replace,
        ),
    )


def screen_disputes(
    findings: list[GFinding],
    ms: Manuscript,
    provider,
    *,
    model: str,
    usage: Usage,
    spec_key: str = "fix",
) -> tuple[list[Verdict], set[str]]:
    """Screen later-wave findings through the panel; return verdicts + rejects.

    Only findings from waves after the first are screened — the panel can never
    withhold a wave-one finding. Each finding the judge will not vouch for yields a
    ``reject`` verdict (routed to the query channel by the caller); every finding
    it keeps yields a ``keep`` verdict. Returns ``(verdicts, rejected_ids)``.
    """

    screenable = [f for f in findings if _wave_of(f) > 1]
    if not screenable:
        return [], set()

    spec = SPECS.get(spec_key) or SPECS["fix"]
    para_text = {f.span.para_id: ms.text_of(f.span.para_id) for f in screenable}
    docfindings = [_to_docproof_finding(f) for f in screenable]

    report = screen(
        docfindings, para_text, provider, spec=spec, model=model, usage=usage
    )
    withheld_ids = {f.finding_id for f in report.withheld}
    withheld_reason = {f.finding_id: f.explanation for f in report.withheld}

    verdicts: list[Verdict] = []
    for f in screenable:
        if f.id in withheld_ids:
            verdicts.append(
                Verdict(
                    finding_id=f.id,
                    ruling="reject",
                    reason=withheld_reason.get(f.id, spec.withhold_note),
                    judge=f"panel:{spec.key}",
                    wave=_wave_of(f),
                )
            )
        else:
            verdicts.append(
                Verdict(
                    finding_id=f.id,
                    ruling="keep",
                    reason="",
                    judge=f"panel:{spec.key}",
                    wave=_wave_of(f),
                )
            )
    return verdicts, withheld_ids


def adjudicate(
    findings: list[GFinding],
    ms: Manuscript | None = None,
    provider=None,
    *,
    model: str = "",
    usage: Usage | None = None,
    spec_key: str = "fix",
) -> AdjudicationResult:
    """Full adjudication: arbitrate spans, then panel-screen later-wave survivors.

    A panel ``reject`` routes its finding to the query channel — it never vanishes.
    Wave-one findings win every span they claim and are never sent to the panel.
    """

    result = arbitrate(findings)

    if provider is not None and ms is not None:
        usage = usage if usage is not None else Usage()
        verdicts, rejected = screen_disputes(
            result.kept, ms, provider, model=model, usage=usage, spec_key=spec_key
        )
        result.verdicts.extend(verdicts)
        if rejected:
            survivors = [f for f in result.kept if f.id not in rejected]
            moved = [f for f in result.kept if f.id in rejected]
            result.kept = survivors
            result.queries.extend(moved)  # reject -> query, not deletion

    return result


__all__ = [
    "AdjudicationResult",
    "adjudicate",
    "arbitrate",
    "screen_disputes",
]
