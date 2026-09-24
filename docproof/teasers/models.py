"""The teaser package, the adjudicator's ruling, and the checks both must pass."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict

# Every teaser: exactly three paragraphs, 150–200 words.
PARAGRAPHS = 3
MIN_WORDS, MAX_WORDS = 150, 200
# "As minimally as possible", made checkable: each correction replaces a short
# span, and an option needing more than this is a rewrite, not a correction.
MAX_SPAN_WORDS = 35
MAX_CORRECTED_WORDS_PER_OPTION = 60
MAX_CORRECTIONS = 25
# An evidence quote long enough to locate one passage, short enough to be a quote.
MIN_EVIDENCE_WORDS, MAX_EVIDENCE_WORDS = 3, 80


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Teaser(Record):
    number: int
    angle: str
    paragraphs: list[str]


class Draft(Record):
    """What the writer returns and what the author document prints."""
    title: str
    author: str
    teasers: list[Teaser]


class Correction(Record):
    option: int
    paragraph: int  # one-based, within that option
    before: str     # exact text in that paragraph, matched once
    after: str
    kind: Literal["factual_error", "hallucination", "spoiler"]
    evidence: str   # verbatim manuscript passage that shows the problem
    explanation: str


class OptionRuling(Record):
    number: int
    ruling: Literal["accurate", "corrected", "rewrite"]
    # Private. For a rewrite, what is wrong and what the book actually says.
    note: str


class Adjudication(Record):
    draft_sha256: str
    options: list[OptionRuling]
    corrections: list[Correction]


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w]+(?:[’'−-][\w]+)*\b", text))


def teaser_words(teaser: Teaser) -> int:
    return word_count("\n\n".join(teaser.paragraphs))


def teaser_issues(teaser: Teaser) -> list[str]:
    issues = []
    if len(teaser.paragraphs) != PARAGRAPHS or any(not p.strip() for p in teaser.paragraphs):
        issues.append(f"Option {teaser.number} has {len(teaser.paragraphs)} paragraphs; "
                      f"it needs exactly {PARAGRAPHS} nonempty paragraphs.")
    words = teaser_words(teaser)
    if not MIN_WORDS <= words <= MAX_WORDS:
        issues.append(f"Option {teaser.number} has {words} words; it needs {MIN_WORDS}–{MAX_WORDS}.")
    if not teaser.angle.strip():
        issues.append(f"Option {teaser.number} needs an angle label.")
    return issues


def draft_issues(draft: Draft) -> list[str]:
    issues = []
    if sorted(t.number for t in draft.teasers) != [1, 2, 3, 4, 5]:
        return ["Supply exactly five teasers numbered 1 through 5."]
    seen, angles = set(), set()
    for t in sorted(draft.teasers, key=lambda t: t.number):
        key = " ".join("\n\n".join(t.paragraphs).casefold().split())
        if key in seen:
            issues.append(f"Option {t.number} duplicates another option.")
        seen.add(key)
        angle = " ".join(t.angle.casefold().split())
        if angle and angle in angles:
            issues.append(f"Option {t.number} repeats another option's angle.")
        angles.add(angle)
        issues.extend(teaser_issues(t))
    return issues


def _words(text: str) -> str:
    """Punctuation-, case- and quote-style-blind form, for locating a quotation."""
    return " " + " ".join(re.findall(r"\w+", text.casefold())) + " "


class Manuscript:
    """The accepted manuscript text, searchable for verbatim evidence."""

    def __init__(self, text: str):
        self.text = text
        self._normalized = _words(text)

    def quotes(self, passage: str) -> bool:
        return _words(passage).strip() != "" and _words(passage) in self._normalized

    def states(self, value: str) -> bool:
        """A title or author name the manuscript itself prints."""
        return bool(value.strip()) and self.quotes(value)


def bibliographic(draft: Draft, manuscript: Manuscript) -> Draft:
    """Keep a title or author only when the manuscript prints it; never guess."""
    return draft.model_copy(update={
        "title": draft.title.strip() if manuscript.states(draft.title) else "",
        "author": draft.author.strip() if manuscript.states(draft.author) else ""})


def apply_adjudication(draft: Draft, ruling: Adjudication, manuscript: Manuscript) -> Draft:
    """Apply the adjudicator's corrections atomically, or refuse the whole ruling.

    Returns the corrected package. Options ruled `rewrite` are left as they
    were for the writer; everything else must pass the content checks after
    the edits."""
    if ruling.draft_sha256 != digest(draft):
        raise ValueError("The adjudication is for a different draft.")
    if sorted(o.number for o in ruling.options) != [1, 2, 3, 4, 5]:
        raise ValueError("Rule on each of the five options exactly once.")
    rulings = {o.number: o for o in ruling.options}
    if len(ruling.corrections) > MAX_CORRECTIONS:
        raise ValueError(f"At most {MAX_CORRECTIONS} corrections; an option that needs more is a rewrite.")
    for number, option in rulings.items():
        count = sum(c.option == number for c in ruling.corrections)
        if option.ruling == "corrected" and not count:
            raise ValueError(f"Option {number} is ruled corrected but has no corrections.")
        if option.ruling != "corrected" and count:
            raise ValueError(f"Option {number} is ruled {option.ruling}; "
                             "only an option ruled corrected may carry corrections.")
        if option.ruling == "rewrite" and not option.note.strip():
            raise ValueError(f"Option {number} is ruled rewrite; the note must say what is wrong.")
    result = draft.model_copy(deep=True)
    teasers = {t.number: t for t in result.teasers}
    changed = {}
    for c in ruling.corrections:
        where = f"Option {c.option} paragraph {c.paragraph}"
        if c.option not in teasers or not 1 <= c.paragraph <= len(teasers[c.option].paragraphs):
            raise ValueError(f"{where} does not exist.")
        if (not c.before.strip() or not c.after.strip() or c.before == c.after or
                "\n" in c.before or "\n" in c.after):
            raise ValueError(f"{where}: a correction replaces a nonempty span with a different one; "
                             "to delete words, include a neighbouring word in both before and after.")
        if word_count(c.before) > MAX_SPAN_WORDS or word_count(c.after) > MAX_SPAN_WORDS:
            raise ValueError(f"{where}: each side of a correction is at most {MAX_SPAN_WORDS} words.")
        if not c.explanation.strip():
            raise ValueError(f"{where}: explain the error.")
        if not MIN_EVIDENCE_WORDS <= word_count(c.evidence) <= MAX_EVIDENCE_WORDS:
            raise ValueError(f"{where}: evidence is one quoted passage of "
                             f"{MIN_EVIDENCE_WORDS}–{MAX_EVIDENCE_WORDS} words.")
        if not manuscript.quotes(c.evidence):
            raise ValueError(f"{where}: the evidence is not a verbatim manuscript passage: {c.evidence[:120]!r}")
        paragraphs = teasers[c.option].paragraphs
        text = paragraphs[c.paragraph - 1]
        if text.count(c.before) != 1:
            raise ValueError(f"{where}: {c.before[:80]!r} must appear exactly once in that paragraph "
                             f"(found {text.count(c.before)}).")
        paragraphs[c.paragraph - 1] = text.replace(c.before, c.after, 1)
        changed[c.option] = changed.get(c.option, 0) + word_count(c.before)
    for number, words in changed.items():
        if words > MAX_CORRECTED_WORDS_PER_OPTION:
            raise ValueError(f"Option {number}: corrections replace {words} words; more than "
                             f"{MAX_CORRECTED_WORDS_PER_OPTION} is a rewrite, not a correction.")
    issues = [i for i in draft_issues(result)
              if not any(i.startswith(f"Option {n} ") for n, o in rulings.items() if o.ruling == "rewrite")]
    if issues:
        raise ValueError("After the corrections: " + "; ".join(issues))
    return result


def rewrite_options(ruling: Adjudication) -> dict[int, str]:
    return {o.number: o.note for o in ruling.options if o.ruling == "rewrite"}
