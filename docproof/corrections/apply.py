"""Applying an edit list to an IDML, deterministically.

Each edit is anchored to the exact text it names and that span is replaced —
nothing else. The anchoring is the review pipeline's: exact match first, then a
punctuation-tolerant retry (curly quotes, dashes, nbsp), so an edit typed with
straight quotes still lands on the book's curly ones.

Edits are applied in order against the live document, so an edit sees the text
as the edits before it left it. A `find` that is not present exactly once (or at
the occurrence asked for) is never guessed at — it is flagged for a human,
before the file is written, which is the whole safety argument.
"""
from __future__ import annotations

from pathlib import Path

from ..validator import fold_punct
from .idml import Story, read_stories, rewrite_stories
from .model import (AMBIGUOUS, APPLIED, ApplyReport, CROSSES_PARAGRAPH, DESIGN,
                    Edit, EditOutcome, NO_CHANGE, NOT_FOUND, ROUTED_TO_DESIGN)


def all_occurrences(haystack: str, needle: str) -> list[int]:
    """Every start offset of `needle` in `haystack`. Exact first; if that finds
    nothing, a punctuation-folded retry whose offsets still index `haystack`
    (the fold never changes a string's length)."""
    if not needle:
        return []
    hits = _find_all(haystack, needle)
    if hits:
        return hits
    folded = _find_all(fold_punct(haystack), fold_punct(needle))
    return folded


def _find_all(haystack: str, needle: str) -> list[int]:
    out, start = [], haystack.find(needle)
    while start != -1:
        out.append(start)
        start = haystack.find(needle, start + 1)
    return out


def _match(edit: Edit, stories: list[Story]):
    """Locate the edit across all stories. Returns (story, paragraph, offset) to
    apply, or an EditOutcome describing why it could not be applied."""
    matches = [(s, p, off)
               for s in stories
               for p in s.paragraphs
               for off in all_occurrences(p.text, edit.find)]
    n = len(matches)
    if n == 0:
        # Distinguish "not there at all" from "there, but across a paragraph
        # break" — the latter is the specific thing corrections must refuse. A
        # human writes such a find with a space where the break is, so the story
        # is flattened with a space between paragraphs to catch it.
        for s in stories:
            flat = " ".join(p.text for p in s.paragraphs)
            if all_occurrences(flat, edit.find):
                return EditOutcome(edit, CROSSES_PARAGRAPH, story_id=s.story_id,
                                   detail="the text spans a paragraph break")
        return EditOutcome(edit, NOT_FOUND, occurrences=0)
    if edit.occurrence == 0:
        if n > 1:
            return EditOutcome(edit, AMBIGUOUS, occurrences=n,
                               detail=f"appears {n} times; no occurrence given")
        return matches[0]
    if edit.occurrence > n:
        return EditOutcome(edit, NOT_FOUND, occurrences=n,
                           detail=f"asked for #{edit.occurrence} of {n}")
    return matches[edit.occurrence - 1]


def apply_to_stories(stories: list[Story],
                     edits: list[Edit]) -> tuple[list[EditOutcome], set[str]]:
    """Apply edits to already-parsed stories, mutating them in place. Returns
    the per-edit outcomes and the set of changed story ids. The in-memory core
    the verifier reuses to compute what a clean apply would produce."""
    outcomes: list[EditOutcome] = []
    changed: set[str] = set()
    for edit in edits:
        if edit.kind == DESIGN:
            outcomes.append(EditOutcome(edit, ROUTED_TO_DESIGN,
                                        detail="a design request, not a text edit"))
            continue
        if edit.find == edit.replace:
            outcomes.append(EditOutcome(edit, NO_CHANGE))
            continue
        found = _match(edit, stories)
        if isinstance(found, EditOutcome):
            outcomes.append(found)
            continue
        story, para, offset = found
        para.replace(offset, offset + len(edit.find), edit.replace)
        changed.add(story.story_id)
        outcomes.append(EditOutcome(edit, APPLIED, story_id=story.story_id,
                                    paragraph=para.index, occurrences=1))
    return outcomes, changed


def apply_edits(src_idml: str | Path, dest_idml: str | Path,
                edits: list[Edit]) -> ApplyReport:
    """Apply every edit to a copy of `src_idml`, writing `dest_idml`.

    The source is never touched. Only stories that actually changed are
    rewritten; the rest of the package is copied byte for byte."""
    stories = read_stories(src_idml)
    by_id = {s.story_id: s for s in stories}
    outcomes, changed = apply_to_stories(stories, edits)
    payload = {sid: by_id[sid].serialize() for sid in changed}
    rewrite_stories(src_idml, dest_idml, payload)
    return ApplyReport(outcomes=tuple(outcomes),
                       stories_changed=tuple(sorted(changed)))
