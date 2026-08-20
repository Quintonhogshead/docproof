"""Phase-one site generators backed by the current DocProof pipeline.

The mature detectors keep doing the detecting.  These adapters expose their
work as explicit examination obligations without changing validation or edit
application.  That is the shadow boundary: the new layer observes; the existing
pipeline remains the only component allowed to write manuscript changes.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Iterable, Sequence

from ..models import DocumentModel, Finding, ParagraphRef, index_paragraphs
from ..site_models import ExaminationSite, LedgerState, SiteAnchor
from ..validator import anchor_offset, shrink


_TYPE_FAMILIES = {
    "sweep_ellipsis": "punctuation",
    "sweep_dash": "punctuation",
    "sweep_stacked_punctuation": "punctuation",
    "sweep_terminal_period": "sentence_boundaries",
    "sweep_quote_punctuation": "dialogue_punctuation",
    "sweep_nested_quote": "dialogue_punctuation",
    "sweep_dialogue_tag": "dialogue_punctuation",
    "sweep_doubled_word": "repeated_words",
    "sweep_century": "numbers",
    "sweep_compound_number": "numbers",
    "number_style": "numbers",
    "currency_style": "currency",
    "heading_case": "headings",
    "list_intro_colon": "lists",
    "unclosed_quote": "missingness",
    "term_consistency": "proper_names",
    "name_consistency": "proper_names",
    "continuity": "continuity",
    "speaker_change": "dialogue_attribution",
    "dialogue_tag": "dialogue_punctuation",
    "terminal_mark": "sentence_boundaries",
}

_HIGH_RISK = frozenset({
    "continuity", "speaker_change", "factcheck", "pronoun_antecedent",
    "missing_word", "preposition_error",
})

_MEDIUM_RISK = frozenset({
    "tense_shift", "pronoun_agreement", "subject_verb_agreement",
    "dialogue_attribution", "proper_names", "lists",
})

_WORD_BOUNDARY = r"(?<![A-Za-z'’]){}(?![A-Za-z'’])"


def site_type_for(error_type: str) -> str:
    return _TYPE_FAMILIES.get(error_type, error_type)


def finding_generator(finding: Finding) -> str:
    if finding.chunk_id == "sweep":
        return "deterministic.sweeps"
    if finding.chunk_id == "consistency":
        return "deterministic.consistency"
    if finding.chunk_id == "residual":
        return "deterministic.residuals"
    if finding.chunk_id == "adjudicate":
        return "judgment.adjudicate"
    if finding.chunk_id in {"calendar", "languagetool"}:
        return f"deterministic.{finding.chunk_id}"
    if finding.chunk_id == "sapling":
        return "external.sapling"
    return "open_discovery.legacy_model"


def _risk(site_type: str) -> tuple[float, str]:
    if site_type in _HIGH_RISK:
        return 0.8, "high"
    if site_type in _MEDIUM_RISK:
        return 0.55, "medium"
    return 0.25, "low"


def _site_id(site_type: str, anchor: SiteAnchor) -> str:
    material = "\x1f".join([
        site_type, anchor.part, anchor.paragraph_id or "",
        str(anchor.start_offset), str(anchor.end_offset),
        anchor.object_id or "",
    ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"X-{digest}"


def _site(site_type: str, anchor: SiteAnchor, *, generator: str,
          evidence: dict, context_recipe: tuple[str, ...],
          status: LedgerState = LedgerState.GENERATED) -> ExaminationSite:
    prior, meaning = _risk(site_type)
    return ExaminationSite(
        site_id=_site_id(site_type, anchor), site_type=site_type,
        anchors=(anchor,), generator=generator, evidence=evidence,
        context_recipe=context_recipe, risk_prior=prior,
        meaning_change_risk=meaning, status=status)


def _anchor_for_finding(finding: Finding, para: ParagraphRef) -> SiteAnchor | None:
    # A downgraded edit is delivered as a query anchored to the whole sentence,
    # but the examination site is still the exact word/punctuation span under
    # judgment. Recompute that minimal diff so it joins the deterministic
    # candidate registered in prepare instead of becoming a second, broad site.
    downgraded_edit = (finding.force_query
                       and finding.corrected_text != finding.original_text)
    if finding.anchor is not None and not downgraded_edit:
        return SiteAnchor(
            part=para.part, paragraph_id=para.para_id,
            start_offset=finding.anchor.start, end_offset=finding.anchor.end)
    start = anchor_offset(para.text, finding.original_text, finding.occurrence)
    if start < 0:
        return None
    if finding.force_query or finding.corrected_text == finding.original_text:
        end = start + len(finding.original_text)
    else:
        prefix, deleted, inserted = shrink(
            finding.original_text, finding.corrected_text)
        if not deleted and not inserted:
            end = start + len(finding.original_text)
        else:
            start += prefix
            end = start + len(deleted)
    return SiteAnchor(part=para.part, paragraph_id=para.para_id,
                      start_offset=start, end_offset=end)


def site_from_finding(finding: Finding, doc: DocumentModel,
                      *, generator: str | None = None) -> ExaminationSite | None:
    para = index_paragraphs(doc).get(finding.para_id)
    if para is None:
        return None
    anchor = _anchor_for_finding(finding, para)
    if anchor is None:
        # Preserve the obligation even when the legacy finding cannot anchor.
        anchor = SiteAnchor(
            part=para.part, paragraph_id=para.para_id,
            object_id=f"unanchored:{finding.finding_id}")
    site_type = site_type_for(finding.error_type)
    return _site(
        site_type, anchor, generator=generator or finding_generator(finding),
        evidence={
            "finding_id": finding.finding_id,
            "chunk_id": finding.chunk_id,
            "error_type": finding.error_type,
            "original_text": finding.original_text,
            "corrected_text": finding.corrected_text,
            "explanation": finding.explanation,
            "confidence": finding.confidence,
        },
        context_recipe=("current sentence", "current paragraph"))


def site_from_candidate(candidate, doc: DocumentModel) -> ExaminationSite | None:
    para = index_paragraphs(doc).get(candidate.para_id)
    if para is None:
        return None
    anchor = SiteAnchor(
        part=para.part, paragraph_id=para.para_id,
        start_offset=candidate.start, end_offset=candidate.end)
    return _site(
        "spelling", anchor,
        generator=f"deterministic.adjudicate.{candidate.kind}",
        evidence={"word": candidate.word, "suggestion": candidate.suggestion,
                  "candidate_kind": candidate.kind},
        context_recipe=("current sentence", "current paragraph"),
        status=LedgerState.NEEDS_JUDGMENT)


def sites_from_spell_scan(spell, doc: DocumentModel) -> list[ExaminationSite]:
    """Site every unknown-word occurrence the dictionary asks to examine."""
    by_id = index_paragraphs(doc)
    sites: list[ExaminationSite] = []
    for candidate in tuple(spell.candidates) + tuple(spell.recurring):
        pattern = re.compile(_WORD_BOUNDARY.format(re.escape(candidate.word)),
                             re.IGNORECASE)
        for para_id in candidate.para_ids:
            para = by_id.get(para_id)
            if para is None:
                continue
            for match in pattern.finditer(para.text):
                anchor = SiteAnchor(
                    part=para.part, paragraph_id=para.para_id,
                    start_offset=match.start(), end_offset=match.end())
                sites.append(_site(
                    "spelling", anchor, generator="deterministic.spellscan",
                    evidence={"word": match.group(0),
                              "suggestions": list(candidate.suggestions),
                              "stem": candidate.stem,
                              "recurring": candidate in spell.recurring},
                    context_recipe=("current sentence", "current paragraph"),
                    status=LedgerState.NEEDS_JUDGMENT))
    return sites


def sites_from_sweep_obligations(
        paragraphs: Sequence[ParagraphRef], sweep_keys: Sequence[str],
        sweep_findings: Sequence[Finding]) -> list[ExaminationSite]:
    """One accounted scan obligation per (sweep, paragraph).

    Precise hit sites are registered separately.  This row records the negative
    evidence too: a sweep that found nothing in a paragraph locally passed it,
    rather than disappearing behind an empty findings list.
    """
    hits: dict[tuple[str, str], int] = defaultdict(int)
    for finding in sweep_findings:
        hits[(finding.error_type, finding.para_id)] += 1
    sites = []
    for para in paragraphs:
        for key in sweep_keys:
            anchor = SiteAnchor(
                part=para.part, paragraph_id=para.para_id,
                start_offset=0, end_offset=len(para.text),
                object_id=f"scan:{key}")
            n = hits[(key, para.para_id)]
            state = (LedgerState.LOCALLY_CONFIRMED if n
                     else LedgerState.LOCALLY_PASSED)
            sites.append(_site(
                site_type_for(key), anchor, generator=f"deterministic.{key}",
                evidence={"sweep": key, "hits": n},
                context_recipe=("current paragraph",), status=state))
    return sites


def sites_from_model_obligations(
        paragraphs: Sequence[ParagraphRef], error_groups: Sequence[Sequence[str]]
        ) -> list[ExaminationSite]:
    """Expose the old finding-only model contract as explicit pending work.

    Legacy detector replies return findings, not an id-by-id pass/error verdict.
    Shadow mode therefore must not infer that an absent finding means a pass.
    These obligations remain ``needs_judgment`` unless a matching finding is
    observed.  Their unresolved count is the measurement that tells us how much
    work remains before site judgment can replace open-ended discovery.
    """
    sites = []
    for para in paragraphs:
        if not para.reviewable:
            continue
        for group in error_groups:
            keys = tuple(group)
            if not keys:
                continue
            families = {site_type_for(key) for key in keys}
            site_type = next(iter(families)) if len(families) == 1 \
                else "model_category"
            label = "+".join(keys)
            anchor = SiteAnchor(
                part=para.part, paragraph_id=para.para_id,
                start_offset=0, end_offset=len(para.text),
                object_id=f"obligation:{label}")
            sites.append(_site(
                site_type, anchor,
                generator="legacy.model_obligation",
                evidence={"error_types": list(keys),
                          "legacy_contract": "findings_only"},
                context_recipe=("current paragraph", "previous paragraph",
                                "next paragraph"),
                status=LedgerState.NEEDS_JUDGMENT))
    return sites


def precise_sites_from_findings(findings: Iterable[Finding],
                                doc: DocumentModel) -> list[ExaminationSite]:
    return [site for finding in findings
            if (site := site_from_finding(finding, doc)) is not None]
