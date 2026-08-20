"""Compact, context-sharing judgment packets."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import islice
from typing import Iterable

from .context_service import ContextService, SiteContext
from .site_models import ExaminationSite


@dataclass(frozen=True)
class PacketSite:
    site_id: str
    site_type: str
    context_id: str
    evidence: dict
    anchors: tuple[dict, ...]


@dataclass(frozen=True)
class JudgmentPacket:
    packet_id: str
    contexts: tuple[SiteContext, ...]
    sites: tuple[PacketSite, ...]

    @property
    def site_ids(self) -> tuple[str, ...]:
        return tuple(site.site_id for site in self.sites)

    def subset(self, site_ids: Iterable[str]) -> "JudgmentPacket":
        wanted = set(site_ids)
        sites = tuple(site for site in self.sites if site.site_id in wanted)
        context_ids = {site.context_id for site in sites}
        contexts = tuple(c for c in self.contexts if c.context_id in context_ids)
        return JudgmentPacket(_packet_id(site.site_id for site in sites),
                              contexts, sites)

    def prompt_payload(self) -> str:
        return json.dumps({
            "contexts": [{"context_id": c.context_id, "text": c.text}
                         for c in self.contexts],
            "sites": [{"site_id": s.site_id, "site_type": s.site_type,
                       "context_id": s.context_id, "anchors": list(s.anchors),
                       "evidence": s.evidence}
                      for s in self.sites],
        }, ensure_ascii=False, separators=(",", ":"))


def build_packets(sites: Iterable[ExaminationSite], service: ContextService,
                  *, batch_size: int = 100) -> list[JudgmentPacket]:
    """Batch compatible sites while de-duplicating shared context text.

    Compatibility is deliberately conservative in phase one: site type and
    meaning-change risk must match.  Within a packet, the same paragraph slice
    is emitted once and every site references it by ``context_id``.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    groups: dict[tuple, list[ExaminationSite]] = {}
    for site in sites:
        key = (site.site_type, site.meaning_change_risk, site.context_recipe)
        groups.setdefault(key, []).append(site)
    packets: list[JudgmentPacket] = []
    for key in sorted(groups, key=repr):
        rows = iter(groups[key])
        while batch := tuple(islice(rows, batch_size)):
            contexts_by_id: dict[str, SiteContext] = {}
            packet_sites = []
            for site in batch:
                context = service.for_site(site)
                contexts_by_id.setdefault(context.context_id, context)
                packet_sites.append(PacketSite(
                    site_id=site.site_id, site_type=site.site_type,
                    context_id=context.context_id, evidence=site.evidence,
                    anchors=tuple(a.model_dump(mode="json")
                                  for a in site.anchors)))
            packets.append(JudgmentPacket(
                _packet_id(site.site_id for site in batch),
                tuple(contexts_by_id.values()), tuple(packet_sites)))
    return packets


def _packet_id(site_ids: Iterable[str]) -> str:
    material = "\x1f".join(site_ids)
    return "PKT-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
