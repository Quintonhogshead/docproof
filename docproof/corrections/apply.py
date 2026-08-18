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
from .idml import Paragraph, Story, read_stories, rewrite_stories
from .model import (AMBIGUOUS, APPLIED, ApplyReport, CROSSES_PARAGRAPH, DESIGN,
                    Edit, EditOutcome, NO_CHANGE, NOT_FOUND, OVERLAPS,
                    ROUTED_TO_DESIGN, WITHHELD)

# Consonant-lettered words that take "an" because they open on a vowel sound (a
# silent h). Kept short and high-confidence; anything not here follows its first
# letter. A "u"/"eu" word is left undecided — "a unicorn" and "an umbrella" split
# on sound, not spelling, and guessing wrong is worse than leaving the article the
# reviewer had — so the article fixer never touches those.
_AN_H_WORDS = ("hour", "honest", "honor", "honour", "heir", "heirloom", "homage")
# Vowel-lettered words that take "a" because they open on a consonant sound (a
# "w" or "y" glide). A closed set of the common ones — "one" and "once" — so the
# fixer never writes "an one"; anything else on a vowel letter keeps "an".
_A_VOWEL_WORDS = ("one", "once", "oneself", "onetime")


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


def _candidates(edit: Edit, stories: list[Story]) -> list:
    """Every (story, paragraph, offset) where this edit's `find` lands.

    With a context anchor, `find` is located *inside* each occurrence of the
    context — so a common word is pinned to the instance whose surrounding text
    the correction named, not to all of them. Without one, `find` is located
    directly, as before. A context that does not itself contain `find` yields no
    candidate there; the caller diagnoses that."""
    out = []
    if edit.context:
        clen = len(edit.context)
        for s in stories:
            for p in s.paragraphs:
                for c_off in all_occurrences(p.text, edit.context):
                    within = all_occurrences(p.text[c_off:c_off + clen], edit.find)
                    if within:
                        out.append((s, p, c_off + within[0]))
    else:
        for s in stories:
            for p in s.paragraphs:
                for off in all_occurrences(p.text, edit.find):
                    out.append((s, p, off))
    return out


def _match(edit: Edit, stories: list[Story]):
    """Locate the edit across all stories. Returns (story, paragraph, offset) to
    apply, or an EditOutcome describing why it could not be applied."""
    matches = _candidates(edit, stories)
    n = len(matches)
    if n == 0:
        return _diagnose_miss(edit, stories)
    if edit.occurrence == 0:
        if n > 1:
            anchor = "context" if edit.context else "text"
            return EditOutcome(edit, AMBIGUOUS, occurrences=n,
                               detail=f"the {anchor} appears {n} times; no "
                                      f"occurrence given")
        return matches[0]
    if edit.occurrence > n:
        return EditOutcome(edit, NOT_FOUND, occurrences=n,
                           detail=f"asked for #{edit.occurrence} of {n}")
    return matches[edit.occurrence - 1]


def _diagnose_miss(edit: Edit, stories: list[Story]):
    """Why an edit found nowhere to land — told apart so the flag is useful. A
    span that straddles a paragraph break is the specific thing corrections must
    refuse; a story is flattened with a space between paragraphs to catch it."""
    if edit.context:
        # The context was the anchor, so diagnose it. If the context is present
        # but did not contain `find`, that is the mismatch to name; otherwise the
        # context itself is missing or spans a break.
        for s in stories:
            for p in s.paragraphs:
                if all_occurrences(p.text, edit.context):
                    return EditOutcome(
                        edit, NOT_FOUND, story_id=s.story_id,
                        detail="the context was found but the text to change was "
                               "not inside it")
        for s in stories:
            flat = " ".join(p.text for p in s.paragraphs)
            if all_occurrences(flat, edit.context):
                return EditOutcome(edit, CROSSES_PARAGRAPH, story_id=s.story_id,
                                   detail="the context spans a paragraph break")
        return EditOutcome(edit, NOT_FOUND, occurrences=0,
                           detail="the context was not found")
    for s in stories:
        flat = " ".join(p.text for p in s.paragraphs)
        if all_occurrences(flat, edit.find):
            return EditOutcome(edit, CROSSES_PARAGRAPH, story_id=s.story_id,
                               detail="the text spans a paragraph break")
    return EditOutcome(edit, NOT_FOUND, occurrences=0)


def indefinite_article(word: str) -> str | None:
    """"a" or "an" for the word that follows — or None when it cannot be decided
    from spelling alone and the article is better left as the reviewer had it.

    Handles the case a blind word swap breaks: the article agrees with the *sound*
    of the next word, so changing "huge" to "immense" leaves a stale "a" that
    should be "an". Vowel-letter openings take "an"; a silent-h word (`hour`,
    `honest`) does too; a plain consonant takes "a". Only "u"/"eu" is refused —
    "a unicorn" vs "an umbrella" splits on pronunciation, not letters, so a guess
    there could introduce a fresh error, which is not a trade worth making."""
    w = word.strip().lower()
    if not w:
        return None
    if w in _A_VOWEL_WORDS:
        return "a"
    if w[0] == "u" or w.startswith("eu"):
        return None                            # undecidable from spelling ("yoo")
    if w[0] in "aeio":
        return "an"
    if w[0] == "h":
        return "an" if any(w.startswith(p) for p in _AN_H_WORDS) else "a"
    return "a"


def _with_article_fix(para: Paragraph, start: int, end: int,
                      replace: str) -> tuple[int, str]:
    """The span and replacement to write for an edit, widened to correct a
    preceding indefinite article when the swap changed the following word's
    initial sound.

    Returns `(start, replace)` unchanged in the common case. When the word just
    before `start` is a standalone "a"/"an" whose form no longer agrees with the
    word that will follow, the returned span reaches back to include the article
    and the replacement carries the corrected one — folded into the one write so
    offsets shift once, not twice. Case is preserved (sentence-initial "A"/"An")."""
    text = para.text
    j = start
    while j > 0 and text[j - 1] == " ":
        j -= 1
    k = j
    while k > 0 and text[k - 1].isalpha():
        k -= 1
    prev = text[k:j]
    if prev.lower() not in ("a", "an"):
        return start, replace
    tail = replace + text[end:]                # what will sit after the article
    m = 0
    while m < len(tail) and not tail[m].isalnum():
        m += 1
    n = m
    while n < len(tail) and (tail[n].isalpha() or tail[n] in "'’"):
        n += 1
    following = tail[m:n]
    want = indefinite_article(following)
    if want is None:
        return start, replace
    if prev[0].isupper():
        want = want[:1].upper() + want[1:]
    if want == prev:
        return start, replace
    return k, want + text[j:start] + replace


class _Touched:
    """The character spans already edited in each paragraph, in current (live)
    coordinates, kept correct as later edits shift the text after them. Two
    corrections that land on overlapping spans of one paragraph is the collision
    that garbles a line ("said just said"); the second is flagged, not applied."""

    def __init__(self) -> None:
        self._by_para: dict[tuple[str, int], list[list[int]]] = {}

    def collides(self, key: tuple[str, int], start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in self._by_para.get(key, ()))

    def record(self, key: tuple[str, int], start: int, end: int,
               new_len: int) -> None:
        delta = new_len - (end - start)
        spans = self._by_para.setdefault(key, [])
        for span in spans:                     # shift what sits after this write
            if span[0] >= end:
                span[0] += delta
                span[1] += delta
        spans.append([start, start + new_len])


def apply_to_stories(stories: list[Story], edits: list[Edit], *,
                     withheld: dict[str, str] | None = None
                     ) -> tuple[list[EditOutcome], set[str]]:
    """Apply edits to already-parsed stories, mutating them in place. Returns
    the per-edit outcomes and the set of changed story ids. The in-memory core
    the verifier reuses to compute what a clean apply would produce.

    `withheld` maps an edit id to the reason a pre-apply sanity gate held it back;
    such an edit is never applied and comes back as a `WITHHELD` outcome for a
    human, so the gate's decision is surfaced, not silent."""
    outcomes: list[EditOutcome] = []
    changed: set[str] = set()
    touched = _Touched()
    withheld = withheld or {}
    for edit in edits:
        if edit.id in withheld:
            outcomes.append(EditOutcome(edit, WITHHELD, detail=withheld[edit.id]))
            continue
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
        key = (story.story_id, para.index)
        start, end = offset, offset + len(edit.find)
        if touched.collides(key, start, end):
            outcomes.append(EditOutcome(
                edit, OVERLAPS, story_id=story.story_id, paragraph=para.index,
                detail="its span overlaps a correction already applied here"))
            continue
        r_start, new_text = _with_article_fix(para, start, end, edit.replace)
        para.replace(r_start, end, new_text)
        touched.record(key, r_start, end, len(new_text))
        changed.add(story.story_id)
        outcomes.append(EditOutcome(edit, APPLIED, story_id=story.story_id,
                                    paragraph=para.index, occurrences=1))
    return outcomes, changed


def apply_edits(src_idml: str | Path, dest_idml: str | Path,
                edits: list[Edit], *,
                withheld: dict[str, str] | None = None) -> ApplyReport:
    """Apply every edit to a copy of `src_idml`, writing `dest_idml`.

    The source is never touched. Only stories that actually changed are
    rewritten; the rest of the package is copied byte for byte. `withheld` is the
    optional sanity-gate verdict — edit ids held back for a human, with a reason."""
    stories = read_stories(src_idml)
    by_id = {s.story_id: s for s in stories}
    outcomes, changed = apply_to_stories(stories, edits, withheld=withheld)
    payload = {sid: by_id[sid].serialize() for sid in changed}
    rewrite_stories(src_idml, dest_idml, payload)
    return ApplyReport(outcomes=tuple(outcomes),
                       stories_changed=tuple(sorted(changed)))
