"""Context-sharing packets for compact candidate judgment."""
from __future__ import annotations

from itertools import islice
from typing import Iterable

from .candidate_models import (
    Candidate, ScreeningContext, ScreeningPacket, ScreeningPacketItem,
    screening_packet_id)
from .context_service import ContextService
from .site_models import ExaminationSite, SiteAnchor


def build_screening_packets(
        candidates: Iterable[Candidate], service: ContextService, *,
        batch_size: int = 100) -> list[ScreeningPacket]:
    if not 1 <= batch_size <= 200:
        raise ValueError("batch_size must be between 1 and 200")
    groups: dict[tuple, list[Candidate]] = {}
    for candidate in candidates:
        key = (candidate.candidate_type, candidate.meaning_change_risk,
               candidate.context_recipe)
        groups.setdefault(key, []).append(candidate)

    packets: list[ScreeningPacket] = []
    for key in sorted(groups, key=repr):
        rows = iter(sorted(groups[key], key=lambda row: row.candidate_id))
        while batch := tuple(islice(rows, batch_size)):
            contexts: dict[str, ScreeningContext] = {}
            items = []
            for candidate in batch:
                site = _context_site(candidate)
                context = service.for_site(site)
                contexts.setdefault(context.context_id, ScreeningContext(
                    context_id=context.context_id, text=context.text,
                    paragraph_ids=context.para_ids, recipes=context.recipes))
                items.append(ScreeningPacketItem(
                    candidate_id=candidate.candidate_id,
                    candidate_type=candidate.candidate_type,
                    context_id=context.context_id,
                    observed_text=candidate.observed_text,
                    candidate_correction=candidate.candidate_correction,
                    evidence=candidate.evidence,
                    anchors=tuple(anchor.model_dump(
                        mode="json", exclude_none=True)
                        for anchor in candidate.anchors),
                    meaning_change_risk=candidate.meaning_change_risk,
                    channel_preference=candidate.channel_preference))
            packets.append(ScreeningPacket(
                packet_id=screening_packet_id(
                    candidate.candidate_id for candidate in batch),
                contexts=tuple(contexts.values()), candidates=tuple(items)))
    return packets


def _context_site(candidate: Candidate) -> ExaminationSite:
    anchors = tuple(SiteAnchor(
        part=anchor.document_part, paragraph_id=anchor.paragraph_id,
        run_index=anchor.run_index, start_offset=anchor.start_offset,
        end_offset=anchor.end_offset, object_id=anchor.object_id,
        virtual_location=({"description": anchor.virtual_location}
                          if anchor.virtual_location else None))
        for anchor in candidate.anchors)
    return ExaminationSite(
        site_id=candidate.candidate_id,
        site_type=candidate.candidate_type, anchors=anchors,
        generator=candidate.generator_id, evidence=candidate.evidence,
        context_recipe=candidate.context_recipe,
        risk_prior=candidate.risk_prior,
        meaning_change_risk=candidate.meaning_change_risk)
