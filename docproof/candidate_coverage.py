"""The candidate-generation coverage manifest.

A single honest declaration of which error families the candidate detector can
currently *generate*, which are partial, and which are known gaps. A detector
that silently covers a fraction of the taxonomy reads as complete; this manifest
is what lets a run report its own omissions (P2 gap reporting, surfaced in the
report and the results UI, P5-02).

Status values:
  ``covered`` - a generator (local or reused analyzer) produces candidates.
  ``partial`` - some of the family is generated; named sub-cases are not.
  ``gap``     - no generator yet; the family is not screened at all.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FamilyCoverage:
    key: str
    title: str
    status: str          # covered | partial | gap
    candidate_types: tuple[str, ...]
    note: str


# Ordered roughly by the plan's P2 families. ``candidate_types`` links a family
# to the generator output it produces, so a report can count generated
# candidates per family and flag families that produced none.
COVERAGE: tuple[FamilyCoverage, ...] = (
    FamilyCoverage(
        "punctuation", "Punctuation & clause boundaries", "covered",
        ("introductory_comma", "direct_address_comma", "dialogue_tag_punctuation",
         "list_punctuation", "quote_balance", "punctuation_style"),
        "Comma, dialogue-tag, list, quote-balance, semicolon/colon spacing, "
        "bracket balance, and the adapted dash/ellipsis/terminal/quote sweeps."),
    FamilyCoverage(
        "lexical", "Lexical (typos, confusables, consistency)", "partial",
        ("homophone", "repeated_word", "word_echo", "term_consistency"),
        "Confusable homophones, doubled words, nearby echoes, and reused "
        "term-consistency. GAP: dictionary spell-scan and edit-distance typos "
        "are not yet routed as candidates; no phonetic near-miss family."),
    FamilyCoverage(
        "grammar", "Grammar (agreement, fragments, run-ons)", "partial",
        ("grammar",),
        "LanguageTool mechanical floor is wired but opt-in (languagetool_floor). "
        "GAP: parser-backed fragments, run-ons, parallelism, modifier "
        "attachment are not generated; parser absence must read as incomplete."),
    FamilyCoverage(
        "numbers", "Numbers & style", "partial",
        ("number_style", "currency_style"),
        "Every numeral and currency amount, plus adapted century/compound-number "
        "sweeps. GAP: dates, times, units, percentages, ranges, and "
        "cross-document format consistency."),
    FamilyCoverage(
        "dialogue", "Dialogue beyond tag punctuation", "partial",
        ("dialogue_tag_punctuation", "quote_balance"),
        "Tag punctuation and quote balance (multi-paragraph aware). GAP: "
        "speaker-transition, attribution, and per-speaker paragraph breaks."),
    FamilyCoverage(
        "structure", "Document structure & ghost sites", "partial",
        ("heading_sequence", "list_punctuation", "quote_balance"),
        "Heading sequence, list punctuation, and the quote-balance ghost site. "
        "GAP: captions, cross-references, fields, tables, footnotes, comments, "
        "placeholders, and list-numbering gaps."),
    FamilyCoverage(
        "continuity", "Continuity, missingness, layout", "gap",
        (),
        "No candidate generator yet. Entity/timeline/location/ownership/POV, "
        "missing-object, and rendered-layout risks are not screened."),
    FamilyCoverage(
        "open_discovery", "Open discovery (outside the taxonomy)", "covered",
        (),
        "Findings from a co-running production reviewer are re-registered into "
        "the ledger; inert in standalone mode (no production reviewer)."),
)

COVERAGE_BY_TYPE = {
    ctype: family
    for family in COVERAGE
    for ctype in family.candidate_types
}


def coverage_summary(generated_types: "set[str] | None" = None) -> dict:
    """A JSON-able coverage view. When ``generated_types`` (the candidate types a
    run actually produced) is given, each family is annotated with whether it
    contributed anything this run, so an empty family is visible as an omission.
    """
    families = []
    for family in COVERAGE:
        produced = None
        if generated_types is not None and family.candidate_types:
            produced = bool(set(family.candidate_types) & generated_types)
        families.append({
            "key": family.key,
            "title": family.title,
            "status": family.status,
            "candidate_types": list(family.candidate_types),
            "note": family.note,
            **({"produced_this_run": produced} if produced is not None else {}),
        })
    return {
        "families": families,
        "counts": {
            status: sum(1 for f in COVERAGE if f.status == status)
            for status in ("covered", "partial", "gap")
        },
    }
