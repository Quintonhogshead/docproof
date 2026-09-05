"""Frozen, forward-tolerant data contracts for Galley.

All shapes are immutable dataclasses with JSON round-tripping. Deserializers
drop unknown keys so older readers tolerate newer case files.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

CASEFILE_SCHEMA_VERSION = 1

# ruling values a Verdict may carry
RULINGS = ("keep", "reject", "downgrade", "query")


def _known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Return only the keys of ``data`` that name a field on ``cls``.

    Unknown keys are dropped, not fatal — an older reader tolerates a newer
    writer. ``data`` that is not a mapping yields an empty dict so a malformed
    record degrades to defaults rather than crashing the load.
    """

    if not isinstance(data, dict):
        return {}
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass(frozen=True)
class Span:
    """A located region of the manuscript: a paragraph and a char range in it.

    Offsets index into the paragraph's *canonical* (normalized) text, the same
    text DocProof's validator anchors against. ``start == end`` marks a pure
    insertion point.
    """

    para_id: str
    start: int
    end: int

    def to_json(self) -> dict[str, Any]:
        return {"para_id": self.para_id, "start": self.start, "end": self.end}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Span":
        d = _known(cls, data)
        return cls(
            para_id=str(d.get("para_id", "")),
            start=int(d.get("start", 0)),
            end=int(d.get("end", 0)),
        )


@dataclass(frozen=True)
class Chapter:
    """One chapter unit: its ordinal, title, and the paragraph ids it spans."""

    index: int
    title: str
    para_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "para_ids": list(self.para_ids),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Chapter":
        d = _known(cls, data)
        return cls(
            index=int(d.get("index", 0)),
            title=str(d.get("title", "")),
            para_ids=tuple(d.get("para_ids", ()) or ()),
        )


@dataclass(frozen=True)
class Manuscript:
    """Canonical text with stable paragraph ids, reading order, and chapters.

    ``paragraphs`` maps ``para_id -> canonical text``; ``order`` is the reading
    order of those ids (a book rarely needs it, but consistency tools do). This
    is the shape DocProof's ingest/normalize stage already produces.
    """

    paragraphs: dict[str, str] = field(default_factory=dict)
    order: tuple[str, ...] = ()
    chapters: tuple[Chapter, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "paragraphs": dict(self.paragraphs),
            "order": list(self.order),
            "chapters": [c.to_json() for c in self.chapters],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Manuscript":
        d = _known(cls, data)
        return cls(
            paragraphs=dict(d.get("paragraphs", {}) or {}),
            order=tuple(d.get("order", ()) or ()),
            chapters=tuple(
                Chapter.from_json(c) for c in (d.get("chapters", ()) or ())
            ),
        )

    def text_of(self, para_id: str) -> str:
        return self.paragraphs.get(para_id, "")


@dataclass(frozen=True)
class Provenance:
    """Where a finding came from: which detector, which wave, which model, cost."""

    detector: str
    wave: int
    model: str = ""
    cost_usd: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "wave": self.wave,
            "model": self.model,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Provenance":
        d = _known(cls, data)
        return cls(
            detector=str(d.get("detector", "")),
            wave=int(d.get("wave", 0)),
            model=str(d.get("model", "")),
            cost_usd=float(d.get("cost_usd", 0.0)),
        )


@dataclass(frozen=True)
class GFinding:
    """A located candidate error — Galley's superset of DocProof's ``Finding``.

    ``find`` is the verbatim text to replace, ``replace`` its correction, ``note``
    the human-facing rationale. ``span`` locates it; ``provenance`` records who
    raised it, in which wave, at what cost.
    """

    id: str
    error_type: str
    span: Span
    find: str
    replace: str
    note: str = ""
    confidence: str = "medium"
    provenance: Provenance | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "error_type": self.error_type,
            "span": self.span.to_json(),
            "find": self.find,
            "replace": self.replace,
            "note": self.note,
            "confidence": self.confidence,
            "provenance": self.provenance.to_json() if self.provenance else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GFinding":
        d = _known(cls, data)
        prov = d.get("provenance")
        return cls(
            id=str(d.get("id", "")),
            error_type=str(d.get("error_type", "")),
            span=Span.from_json(d.get("span", {}) or {}),
            find=str(d.get("find", "")),
            replace=str(d.get("replace", "")),
            note=str(d.get("note", "")),
            confidence=str(d.get("confidence", "medium")),
            provenance=Provenance.from_json(prov) if isinstance(prov, dict) else None,
        )


@dataclass(frozen=True)
class Verdict:
    """An adjudication of one finding, recorded with its reason.

    ``ruling`` is one of :data:`RULINGS`. A ``reject`` never deletes the finding
    silently — downstream routes it to the query channel — but the record of the
    ruling is what makes the case file auditable after delivery.
    """

    finding_id: str
    ruling: str
    reason: str = ""
    judge: str = ""
    wave: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "ruling": self.ruling,
            "reason": self.reason,
            "judge": self.judge,
            "wave": self.wave,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Verdict":
        d = _known(cls, data)
        return cls(
            finding_id=str(d.get("finding_id", "")),
            ruling=str(d.get("ruling", "")),
            reason=str(d.get("reason", "")),
            judge=str(d.get("judge", "")),
            wave=int(d.get("wave", 0)),
        )


@dataclass(frozen=True)
class Hypothesis:
    """An auditor's guess at a missed error: where to look and for what.

    ``span_hint`` is a free-text locator (a quoted phrase or a paragraph id),
    not a resolved :class:`Span` — the auditor reads pages, it does not anchor.
    """

    chapter: int
    error_class: str
    why: str = ""
    span_hint: str = ""
    confidence: str = "medium"

    def to_json(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "error_class": self.error_class,
            "why": self.why,
            "span_hint": self.span_hint,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Hypothesis":
        d = _known(cls, data)
        return cls(
            chapter=int(d.get("chapter", 0)),
            error_class=str(d.get("error_class", "")),
            why=str(d.get("why", "")),
            span_hint=str(d.get("span_hint", "")),
            confidence=str(d.get("confidence", "medium")),
        )


@dataclass(frozen=True)
class WaveRecord:
    """An append-only record of one wave of the loop.

    ``actions`` is stored as opaque JSON (a list of dicts) so this contract stays
    frozen while the planner's action union (Track D) evolves independently.
    """

    index: int
    actions: tuple[dict[str, Any], ...] = ()
    spend_usd: float = 0.0
    findings_added: int = 0
    started_at: str = ""
    ended_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "actions": [dict(a) for a in self.actions],
            "spend_usd": self.spend_usd,
            "findings_added": self.findings_added,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "WaveRecord":
        d = _known(cls, data)
        actions = d.get("actions", ()) or ()
        return cls(
            index=int(d.get("index", 0)),
            actions=tuple(dict(a) for a in actions if isinstance(a, dict)),
            spend_usd=float(d.get("spend_usd", 0.0)),
            findings_added=int(d.get("findings_added", 0)),
            started_at=str(d.get("started_at", "")),
            ended_at=str(d.get("ended_at", "")),
        )


# Sanity: every public shape here is a frozen dataclass exposing to_json/from_json.
_CONTRACTS = (
    Span,
    Chapter,
    Manuscript,
    Provenance,
    GFinding,
    Verdict,
    Hypothesis,
    WaveRecord,
)
assert all(is_dataclass(c) for c in _CONTRACTS)
