"""Executable intent zones — machine-readable protected spans with permission
classes, resolved from selectors and enforced BEFORE any lane claims a span.

An intent zone is a region of the manuscript the author's intent protects:
quoted Scripture, a historical quotation, a dialect passage, a coined term, a
meta-text that discusses its own wording. Recording that a zone exists is not
enough — the deterministic sweep layer claims spans first, so a quoted "that
that" could be "fixed" before adjudication ever runs. This module makes zones
*executable*: selectors resolve to concrete character ranges, and every zone
carries a PERMISSION class that says what may happen inside it. Detectors and
sweeps consult the resolved zones before proposing a change (see
`docproof.pipeline`'s post-sweep filter).

Selectors (a zone may use any one):

* ``para_ids``   — an explicit list of paragraph ids (whole paragraphs).
* ``para_range`` — ``[first_id, last_id]`` inclusive, in reading order.
* ``terms``      — literal strings; every occurrence in any paragraph is a span.
* ``regex``      — a regex; every match in any paragraph is a span.
* ``quotes``     — every quoted-speech span (variant closing marks), for
  protecting quotation wording wholesale.

Permission classes (quotation-aware proofreading):

* ``locked``      — nothing may be edited; a catch becomes a margin QUERY at
  most (Scripture, liturgy, a coined term, a dialect rendering). The default.
* ``punctuation`` — house-style PUNCTUATION may be applied (curly quotes,
  spacing, an ellipsis), but WORDING is frozen (a historical quotation you may
  normalize the typography of but never modernize; bibliographic material).
* ``open``        — no protection; used to carve an exception INSIDE a broader
  zone (a translator's note within a Scripture block that may be edited).

`permits(zone, kind)` is the one predicate every consumer asks: is an edit of
this ``kind`` ("wording" | "punctuation") allowed here? A finding a zone forbids
is not deleted — it is downgraded to a query, so the author still sees it.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .models import ParagraphRef

PERMISSIONS = ("locked", "punctuation", "open")
_EDIT_KINDS = ("wording", "punctuation")

# The built-in sweeps that only ever touch PUNCTUATION/typography — allowed
# under a `punctuation` permission. Every other sweep and every LLM detector is
# treated as a wording-class edit (the safe default: unknown ⇒ wording ⇒
# blocked by both `locked` and `punctuation`).
PUNCTUATION_SWEEPS = frozenset({
    "sweep_ellipsis", "sweep_dash", "sweep_stacked_punctuation",
    "sweep_terminal_period", "sweep_quote_punctuation", "sweep_nested_quote",
})


class IntentZone(BaseModel):
    """One protected region: a selector, a permission class, and a label.

    Exactly one selector field should be set; if several are, they union."""
    label: str = ""
    permission: str = "locked"
    category: str = ""               # free-text: scripture | quotation | dialect | ...
    para_ids: list[str] = Field(default_factory=list)
    para_range: list[str] = Field(default_factory=list)   # [first_id, last_id]
    terms: list[str] = Field(default_factory=list)
    regex: str | None = None
    quotes: bool = False

    def model_post_init(self, _ctx) -> None:
        if self.permission not in PERMISSIONS:
            raise ValueError(
                f"intent zone {self.label!r}: permission must be one of "
                f"{PERMISSIONS}, got {self.permission!r}")
        if self.para_range and len(self.para_range) != 2:
            raise ValueError(
                f"intent zone {self.label!r}: para_range must be "
                f"[first_id, last_id]")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as e:
                raise ValueError(
                    f"intent zone {self.label!r}: bad regex — {e}")


class IntentZones(BaseModel):
    """A manuscript's intent zones. Loads from ``<book>.intent.json`` or a
    config block; resolves against the paragraph list to concrete spans."""
    zones: list[IntentZone] = Field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.zones)


def permits(zone: IntentZone, kind: str) -> bool:
    """Whether an edit of ``kind`` ("wording" | "punctuation") is allowed inside
    ``zone``. ``locked`` forbids both; ``punctuation`` allows only punctuation;
    ``open`` allows both."""
    if kind not in _EDIT_KINDS:
        raise ValueError(f"kind must be one of {_EDIT_KINDS}, got {kind!r}")
    if zone.permission == "open":
        return True
    if zone.permission == "locked":
        return False
    return kind == "punctuation"                     # permission == "punctuation"


def edit_kind_of(error_type: str, detector: str = "") -> str:
    """Classify a finding as a "punctuation" or a "wording" edit from its
    sweep/detector name. Only the known punctuation sweeps count as punctuation;
    everything else is wording (the conservative default — an unrecognized
    detector is treated as touching wording, so a `locked` OR `punctuation` zone
    both block it)."""
    if error_type in PUNCTUATION_SWEEPS or detector in PUNCTUATION_SWEEPS:
        return "punctuation"
    return "wording"


def _resolve_one(zone: IntentZone, paragraphs: list[ParagraphRef],
                 order: list[str], index: dict[str, ParagraphRef],
                 closing_quotes: str) -> dict[str, list[tuple[int, int]]]:
    """Resolve one zone to ``{para_id: [(start, end), ...]}`` char spans. A
    whole-paragraph selector yields the full ``[0, len(text)]`` span."""
    from .smoothing import quote_spans

    spans: dict[str, list[tuple[int, int]]] = {}

    def whole(pid: str) -> None:
        p = index.get(pid)
        if p is not None:
            spans.setdefault(pid, []).append((0, len(p.text)))

    for pid in zone.para_ids:
        whole(pid)

    if zone.para_range:
        first, last = zone.para_range
        try:
            i0, i1 = order.index(first), order.index(last)
        except ValueError:
            i0 = i1 = None
        if i0 is not None:
            lo, hi = sorted((i0, i1))
            for pid in order[lo:hi + 1]:
                whole(pid)

    if zone.terms:
        for p in paragraphs:
            for term in zone.terms:
                if not term:
                    continue
                start = 0
                while True:
                    j = p.text.find(term, start)
                    if j < 0:
                        break
                    spans.setdefault(p.para_id, []).append((j, j + len(term)))
                    start = j + len(term)

    if zone.regex:
        rx = re.compile(zone.regex)
        for p in paragraphs:
            for m in rx.finditer(p.text):
                spans.setdefault(p.para_id, []).append((m.start(), m.end()))

    if zone.quotes:
        for p in paragraphs:
            for s, e in quote_spans(p.text, closing_quotes):
                spans.setdefault(p.para_id, []).append((s, e))

    return spans


class ResolvedZones:
    """Intent zones resolved to concrete spans, with the fast lookup the
    enforcement filter needs: given (para_id, start, end), which zone (if any)
    contains that span, and what does it permit."""

    def __init__(self, by_para: dict[str, list[tuple[int, int, IntentZone]]]):
        self._by_para = by_para

    @property
    def any(self) -> bool:
        return any(self._by_para.values())

    def zone_at(self, para_id: str, start: int, end: int) -> IntentZone | None:
        """The most restrictive zone whose span intersects [start, end) in this
        paragraph, or None. `locked` beats `punctuation` beats `open` on a tie,
        so the strictest protection wins when zones overlap."""
        rank = {"locked": 0, "punctuation": 1, "open": 2}
        best: IntentZone | None = None
        for lo, hi, zone in self._by_para.get(para_id, ()):  # half-open
            if start < hi and lo < end:
                if best is None or rank[zone.permission] < rank[best.permission]:
                    best = zone
        return best

    def as_dict(self) -> dict[str, list[tuple[int, int]]]:
        """The plain ``{para_id: [(start, end)]}`` map (e.g. for a judgment
        packet's intent-zone flags). Permission is dropped here — this is the
        "is this span protected at all" view."""
        return {pid: [(lo, hi) for lo, hi, _z in rows]
                for pid, rows in self._by_para.items() if rows}


def resolve(zones: IntentZones, paragraphs: list[ParagraphRef], *,
            closing_quotes: str = "”\"") -> ResolvedZones:
    """Resolve every zone against the manuscript into a `ResolvedZones` lookup."""
    order = [p.para_id for p in paragraphs]
    index = {p.para_id: p for p in paragraphs}
    by_para: dict[str, list[tuple[int, int, IntentZone]]] = {}
    for zone in zones.zones:
        for pid, ranges in _resolve_one(zone, paragraphs, order, index,
                                        closing_quotes).items():
            for lo, hi in ranges:
                by_para.setdefault(pid, []).append((lo, hi, zone))
    return ResolvedZones(by_para)


def _edit_span(finding, para_text: str) -> tuple[int, int] | None:
    """The absolute [start, end) char range a finding actually changes in its
    paragraph, or None if its quoted window cannot be located. Locates the
    ``occurrence``-th occurrence of the finding's ``original_text`` window, then
    trims the common prefix/suffix between it and ``corrected_text`` down to the
    minimal differing region — the same shape the validator anchors."""
    window = getattr(finding, "original_text", "") or ""
    corrected = getattr(finding, "corrected_text", "") or ""
    if not window:
        return None
    occ = max(1, getattr(finding, "occurrence", 1) or 1)
    idx = -1
    for _ in range(occ):
        idx = para_text.find(window, idx + 1)
        if idx < 0:
            return None
    # minimal differing region within the window
    p = 0
    while p < len(window) and p < len(corrected) and window[p] == corrected[p]:
        p += 1
    s = 0
    while (s < len(window) - p and s < len(corrected) - p
           and window[-1 - s] == corrected[-1 - s]):
        s += 1
    lo = idx + p
    hi = idx + len(window) - s
    return (lo, max(lo, hi))


def enforce(findings: list, resolved: ResolvedZones,
            paragraphs: list[ParagraphRef]
            ) -> tuple[list, list[dict[str, Any]]]:
    """Consult the resolved zones for every finding and downgrade any that a
    zone forbids from an EDIT to a QUERY (``force_query=True``) — never a silent
    drop, so the author still sees it and the collision is auditable.

    Returns ``(findings, collisions)``: the (possibly rewritten) finding list
    and a record of every downgrade — ``{finding_id, para_id, permission,
    label, edit_kind}`` — for the report and certify's intent-zone check. A
    finding already riding the query channel, or one whose span can't be
    located, is left untouched."""
    import dataclasses

    if not resolved.any:
        return findings, []
    text_of = {p.para_id: p.text for p in paragraphs}
    out: list = []
    collisions: list[dict[str, Any]] = []
    for f in findings:
        if getattr(f, "force_query", False):
            out.append(f)
            continue
        para_text = text_of.get(getattr(f, "para_id", ""))
        if para_text is None:
            out.append(f)
            continue
        span = _edit_span(f, para_text)
        if span is None:
            out.append(f)
            continue
        zone = resolved.zone_at(f.para_id, span[0], span[1])
        if zone is None:
            out.append(f)
            continue
        kind = edit_kind_of(getattr(f, "error_type", ""),
                            getattr(f, "detector", "") or "")
        if permits(zone, kind):
            out.append(f)
            continue
        collisions.append({
            "finding_id": getattr(f, "finding_id", ""),
            "para_id": f.para_id,
            "permission": zone.permission,
            "label": zone.label or zone.category,
            "edit_kind": kind,
        })
        out.append(dataclasses.replace(f, force_query=True))
    return out, collisions


def load_intent_zones(path: str) -> IntentZones:
    """Load an intent-zones document (``{"zones": [...]}`` or a bare list)."""
    import json
    raw = json.loads(open(path, encoding="utf-8").read())
    if isinstance(raw, list):
        raw = {"zones": raw}
    return IntentZones.model_validate(raw)


__all__ = [
    "PERMISSIONS", "PUNCTUATION_SWEEPS", "IntentZone", "IntentZones",
    "ResolvedZones", "permits", "edit_kind_of", "resolve", "enforce",
    "load_intent_zones",
]
