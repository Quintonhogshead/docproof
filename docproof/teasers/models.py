from __future__ import annotations

import hashlib
import json
import re
from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(Record):
    claim: str
    paragraph_ids: list[int]


class Reading(Record):
    chunk_id: int
    first_paragraph: int
    last_paragraph: int
    narrative: str
    facts: list[Fact]
    revelations: list[str]
    source_limitations: list[str]


class Storysheet(Record):
    title: str
    author: str
    source_complete: bool
    source_limitations: list[str]
    reader_promise: str
    narrative_center: str
    premise: str
    central_pressure: str
    stakes: str
    genre_and_audience: str
    voice: str
    public_facts: list[Fact]
    conditional_disclosures: list[str]
    protected_revelations: list[str]
    five_angles: list[str]
    qwen_instructions: str


class Teaser(Record):
    number: int
    angle: str
    paragraphs: list[str]


class Element(Record):
    name: str
    purpose: str
    book_specific_guidance: str


class Draft(Record):
    teasers: list[Teaser]
    opening_hooks: list[str]
    editorial_note: str
    elements: list[Element]
    best_practices: list[str]
    modification_checklist: list[str]


class SourceReview(Record):
    chunk_id: int
    draft_sha256: str
    findings: list[str]
    supported_details: list[str]


class OptionCheck(Record):
    number: int
    accurate: bool
    spoiler_safe: bool
    clear: bool
    faithful_voice: bool
    distinct_angle: bool
    feedback: str


class Review(Record):
    draft_sha256: str
    covered_chunk_ids: list[int]
    approved: bool
    recommended_option: int
    options: list[OptionCheck]
    guidance_approved: bool
    feedback: list[str]


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w]+(?:[’'−-][\w]+)*\b", text))


def draft_issues(draft: Draft) -> list[str]:
    issues = []
    if sorted(t.number for t in draft.teasers) != [1, 2, 3, 4, 5]:
        issues.append("Supply exactly five teasers numbered 1 through 5.")
    normalized = set()
    for t in draft.teasers:
        text = "\n\n".join(t.paragraphs)
        key = " ".join(text.casefold().split())
        if key in normalized:
            issues.append(f"Option {t.number} duplicates another option.")
        normalized.add(key)
        if not 2 <= len(t.paragraphs) <= 4 or any(not p.strip() for p in t.paragraphs):
            issues.append(f"Option {t.number} needs two to four nonempty paragraphs.")
        if not 140 <= word_count(text) <= 190:
            issues.append(f"Option {t.number} has {word_count(text)} words; use 140–190.")
        if not t.angle.strip():
            issues.append(f"Option {t.number} needs an accurate angle label.")
    if len(draft.opening_hooks) != 3 or any(not 5 <= word_count(h) <= 18 for h in draft.opening_hooks):
        issues.append("Supply three opening hooks, each 5–18 words.")
    if not draft.editorial_note.strip() or word_count(draft.editorial_note) > 180:
        issues.append("Supply a spoiler-safe editorial note of at most 180 words.")
    if len(draft.elements) < 5 or any(not all((e.name.strip(), e.purpose.strip(),
                                            e.book_specific_guidance.strip())) for e in draft.elements):
        issues.append("Explain at least five teaser elements with book-specific editing advice.")
    if len(draft.best_practices) < 5 or any(not s.strip() for s in draft.best_practices):
        issues.append("Supply at least five usable best practices.")
    if len(draft.modification_checklist) < 5 or any(not s.strip() for s in draft.modification_checklist):
        issues.append("Supply at least five final editing checks.")
    return issues


def approval_issues(draft: Draft, review: Review, chunk_ids: list[int]) -> list[str]:
    issues = draft_issues(draft)
    if review.draft_sha256 != digest(draft):
        issues.append("The review does not match the saved Qwen draft.")
    if sorted(review.covered_chunk_ids) != sorted(chunk_ids):
        issues.append("The review did not account for the complete manuscript.")
    if not review.approved or not review.guidance_approved:
        issues.append("Sol has not approved all author-facing content.")
    if review.recommended_option not in range(1, 6):
        issues.append("Choose a recommended option from 1 through 5.")
    if sorted(o.number for o in review.options) != [1, 2, 3, 4, 5]:
        issues.append("Sol must review all five options.")
    if any(not all((o.accurate, o.spoiler_safe, o.clear, o.faithful_voice,
                    o.distinct_angle)) for o in review.options):
        issues.append("One or more options failed editorial review.")
    return issues
