from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


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


class SmallEdit(Record):
    field: Literal["teaser", "angle", "hook", "editorial_note", "element_name",
                   "element_purpose", "element_guidance", "best_practice", "checklist"]
    index: int  # One-based option/item number; editorial_note uses 1.
    paragraph: int  # One-based paragraph for teaser; all other fields use 1.
    before: str
    after: str
    reason: str
    paragraph_ids: list[int]


class Review(Record):
    draft_sha256: str
    covered_chunk_ids: list[int]
    approved: bool
    recommended_option: int
    options: list[OptionCheck]
    guidance_approved: bool
    feedback: list[str]
    edits: list[SmallEdit] = Field(default_factory=list)


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
        issues.append("The review does not match the saved draft.")
    if review.edits:
        issues.append("Proposed corrections require a fresh review before approval.")
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


def apply_small_edits(draft: Draft, edits: list[SmallEdit], paragraph_ids: set[int]) -> Draft:
    """Apply a bounded, exact edit batch atomically; never accept replacement packages."""
    if not 1 <= len(edits) <= 5:
        raise ValueError("Sol may make at most five small corrections per review.")
    if any(sum(word_count(getattr(e, side)) for e in edits) > 80 for side in ("before", "after")):
        raise ValueError("The correction batch exceeds 80 words; ask Qwen to revise it.")
    result = draft.model_copy(deep=True)
    for edit in edits:
        if (not edit.before.strip() or not edit.after.strip() or edit.before == edit.after or
                any(word_count(s) > 40 or len(s) > 320 or "\n" in s for s in (edit.before, edit.after))):
            raise ValueError("Each correction must replace a name, phrase or short sentence (at most 40 words).")
        if not edit.reason.strip() or not edit.paragraph_ids or not set(edit.paragraph_ids) <= paragraph_ids:
            raise ValueError("Each correction needs a reason and valid manuscript evidence.")
        if edit.index < 1 or edit.paragraph < 1 or (edit.field != "teaser" and edit.paragraph != 1):
            raise ValueError("Correction locations use one-based item and paragraph numbers.")
        try:
            index = edit.index - 1
            if edit.field in ("teaser", "angle"):
                option = next(t for t in result.teasers if t.number == edit.index)
                target, key = ((option.paragraphs, edit.paragraph - 1) if edit.field == "teaser"
                               else (option, "angle"))
            elif edit.field == "editorial_note":
                if edit.index != 1:
                    raise IndexError
                target, key = result, "editorial_note"
            elif edit.field.startswith("element_"):
                target = result.elements[index]
                key = {"element_name": "name", "element_purpose": "purpose",
                       "element_guidance": "book_specific_guidance"}[edit.field]
            else:
                target, key = getattr(result, {"hook": "opening_hooks", "best_practice": "best_practices",
                                              "checklist": "modification_checklist"}[edit.field]), index
            text = target[key] if isinstance(target, list) else getattr(target, key)
        except (IndexError, StopIteration):
            raise ValueError("The correction points to a missing field or paragraph.") from None
        if text.count(edit.before) != 1:
            raise ValueError("A correction must match exactly once at its stated location.")
        replacement = text.replace(edit.before, edit.after, 1)
        if isinstance(target, list):
            target[key] = replacement
        else:
            setattr(target, key, replacement)
    issues = draft_issues(result)
    if issues:
        raise ValueError("Small corrections did not resolve the package requirements: " + "; ".join(issues))
    return result
