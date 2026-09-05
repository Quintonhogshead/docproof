"""Export and import the canonical packet for external judgment.

Packets contain the exact span, context, options, provenance, and intent-zone
flag needed for a human or external judge. Import validates each decision and
produces the same findings shape without a model call.

``import_decisions`` refuses stale anchors, duplicate or partial cluster
decisions, unknown actions, and edits inside an ``intent_zone``. Accepted and
replaced decisions produce edits; queries produce margin findings.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

PACKET_SCHEMA_VERSION = 1

# The decision channels a judge may return. Ordered by how they route:
#   accept  -> edit channel, using one of the proposed options
#   replace -> edit channel, using the judge's own text (a synthesis)
#   query   -> query channel (a margin question; nothing auto-applies)
#   reject  -> dropped (the original stands)
ACTIONS = ("accept", "replace", "query", "reject")
_EDIT_ACTIONS = ("accept", "replace")


class PacketError(ValueError):
    """A judgment packet or a returned decision is malformed or non-compliant.

    Raised only for hard violations (bad anchoring, an intent-zone edit, an
    unknown action) — never for a merely empty packet, which imports to zero
    findings, the same as a run that found nothing."""


@dataclass(frozen=True)
class DecisionResult:
    """The outcome of importing one packet: the findings to write plus a
    per-action tally for the report. `findings` are DocProof ``Finding``
    objects, ready for the same envelope + merge path the built-in judge feeds."""
    findings: list[Any]
    counts: dict[str, int]
    source: str | None = None


def _cluster_id(i: int) -> str:
    return f"cl-{i:04d}"


def export_packet(clusters: list, *, source: str | None = None,
                  lane: str = "copyedit",
                  intent_zones: dict[str, list[tuple[int, int]]] | None = None
                  ) -> dict[str, Any]:
    """Build a judgment packet from flight-deck ``Cluster`` objects (or anything
    exposing ``para_id/start/end/original/sentence/para_text/options``).

    ``intent_zones`` maps ``para_id -> [(start, end), ...]`` of protected char
    ranges (from the profile). A cluster whose span intersects a protected range
    is flagged ``intent_zone: true`` so the judge — and :func:`import_decisions`
    — refuse to edit it. Each cluster carries an empty ``decision`` template for
    the judge to fill."""
    zones = intent_zones or {}
    out_clusters = []
    for i, cl in enumerate(clusters, start=1):
        flagged = _intersects_zone(cl.para_id, cl.start, cl.end, zones)
        out_clusters.append({
            "cluster_id": _cluster_id(i),
            "para_id": cl.para_id,
            "span": {"start": cl.start, "end": cl.end},
            "original": cl.original,
            "sentence": cl.sentence,
            "para_text": cl.para_text,
            "intent_zone": flagged,
            "options": [
                {"replacement": p.replacement, "rationale": p.rationale,
                 "model": p.model, "lens": p.lens, "flight": p.flight,
                 "lane": lane}
                for p in cl.options
            ],
            # The judge fills this in. `action: null` marks an unruled cluster.
            "decision": {"action": None, "chosen_index": -1, "replacement": "",
                         "rationale": "", "confidence": "medium"},
        })
    return {
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "lane": lane,
        "source": source,
        "clusters": out_clusters,
    }


def _intersects_zone(para_id: str, start: int, end: int,
                     zones: dict[str, list[tuple[int, int]]]) -> bool:
    for lo, hi in zones.get(para_id, ()):  # half-open [lo, hi)
        if start < hi and lo < end:
            return True
    return False


def import_decisions(packet: dict[str, Any]) -> DecisionResult:
    """Validate a filled packet and turn its decisions into DocProof Findings —
    no model call. Raises :class:`PacketError` on any hard violation (anchoring,
    atomicity, unknown action, an intent-zone edit). Unruled clusters
    (``action`` null) and ``reject`` decisions produce no finding; ``accept`` and
    ``replace`` produce EDIT-channel findings; ``query`` produces a QUERY-channel
    finding."""
    from docproof.flights import LANE, sentence_window
    from docproof.models import Finding

    if not isinstance(packet, dict):
        raise PacketError("packet must be a JSON object")
    clusters = packet.get("clusters")
    if not isinstance(clusters, list):
        raise PacketError("packet.clusters must be a list")
    lane = str(packet.get("lane") or LANE)

    counts = {a: 0 for a in ACTIONS}
    counts["unruled"] = 0
    findings: list[Any] = []
    ids = itertools.count(1)
    seen_ids: set[str] = set()

    for entry in clusters:
        if not isinstance(entry, dict):
            raise PacketError("each cluster must be a JSON object")
        cid = str(entry.get("cluster_id", ""))
        if cid in seen_ids:
            raise PacketError(f"duplicate cluster_id {cid!r} — atomicity "
                              f"requires one decision per cluster")
        seen_ids.add(cid)

        decision = entry.get("decision") or {}
        action = decision.get("action")
        if action is None:
            counts["unruled"] += 1
            continue
        if action not in ACTIONS:
            raise PacketError(f"cluster {cid}: unknown action {action!r}; "
                              f"allowed: {', '.join(ACTIONS)}")

        para_text = str(entry.get("para_text", ""))
        span = entry.get("span") or {}
        start, end = int(span.get("start", 0)), int(span.get("end", 0))
        original = str(entry.get("original", ""))
        # Anchoring: the packet's own original must still match its para_text.
        if para_text[start:end] != original:
            raise PacketError(
                f"cluster {cid}: original {original!r} does not anchor at "
                f"[{start}:{end}] in its paragraph — the decision ruled on "
                f"text that is not there")

        if entry.get("intent_zone") and action in _EDIT_ACTIONS:
            raise PacketError(
                f"cluster {cid}: action {action!r} edits an intent-zone span; "
                f"an intent zone may only be queried or rejected")

        counts[action] += 1
        if action == "reject":
            continue

        new_text = _resolve_replacement(cid, action, decision, entry)
        confidence = decision.get("confidence", "medium")
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        quote, lo, occurrence = sentence_window(para_text, start, end)
        corrected = quote[:start - lo] + new_text + quote[end - lo:]
        rationale = str(decision.get("rationale", "")).strip()
        options = entry.get("options") or []
        lenses = sorted({str(o.get("lens", "")) for o in options
                         if o.get("lens")})
        tag = f" ({'/'.join(lenses)})" if lenses else ""
        verb = "query" if action == "query" else "->"
        explanation = (f'"{original}" {verb} "{new_text}"{tag} — {rationale}'
                       if rationale else f'"{original}" {verb} "{new_text}"{tag}')
        findings.append(Finding(
            finding_id=f"ext-{next(ids):04d}",
            chunk_id="import-judgments",
            para_id=str(entry.get("para_id", "")),
            error_type=lane,
            original_text=quote,
            occurrence=occurrence,
            corrected_text=corrected,
            explanation=explanation,
            confidence=confidence,
            force_query=(action == "query"),
            agreement=len({o.get("flight") for o in options if o.get("flight")}),
        ))

    source = packet.get("source")
    return DecisionResult(findings=findings, counts=counts,
                          source=source if isinstance(source, str) else None)


def _resolve_replacement(cid: str, action: str, decision: dict,
                         entry: dict) -> str:
    """The text an accept/replace/query decision writes into the span.

    * accept -> the chosen option's replacement (by ``chosen_index``).
    * replace -> the judge's own ``replacement`` text.
    * query -> the span is unchanged in the doc (a margin question), so the
      "replacement" is a proposed wording carried for the author to consider:
      the judge's own text if given, else the first option, else the original.
    """
    options = entry.get("options") or []
    if action == "accept":
        idx = int(decision.get("chosen_index", -1))
        if idx < 0 or idx >= len(options):
            raise PacketError(
                f"cluster {cid}: accept needs a valid chosen_index into "
                f"options (0..{len(options) - 1}), got {idx}")
        text = str(options[idx].get("replacement", ""))
        if not text:
            raise PacketError(f"cluster {cid}: chosen option has empty "
                              f"replacement text")
        return text
    if action == "replace":
        text = str(decision.get("replacement", ""))
        if not text:
            raise PacketError(f"cluster {cid}: replace needs non-empty "
                              f"replacement text")
        return text
    # query
    text = str(decision.get("replacement", ""))
    if text:
        return text
    if options and options[0].get("replacement"):
        return str(options[0]["replacement"])
    return str(entry.get("original", ""))


__all__ = [
    "PACKET_SCHEMA_VERSION", "ACTIONS", "PacketError", "DecisionResult",
    "export_packet", "import_decisions",
]
