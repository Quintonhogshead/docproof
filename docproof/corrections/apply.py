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
                    Edit, EditOutcome, FORMATS, NO_CHANGE, NOT_FOUND, OVERLAPS,
                    PARA_ATTRS, PARA_DELETE, PARA_INSERT_BEFORE,
                    ROUTED_TO_DESIGN, UNPLACEABLE, UNSTYLEABLE, WITHHELD)
from .textmatch import IndexCache, NormIndex, normalize

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


def all_spans(haystack: str, needle: str, *, cache: IndexCache | None = None,
              partial_words: bool = False) -> list[tuple[int, int]]:
    """Every real `(start, end)` span of `haystack` that carries `needle`.

    Three tiers, each tried only when the one before it found nothing, so an
    anchor that already matches is located exactly as it always was:

      1. exact;
      2. the length-preserving punctuation fold (curly quotes, dashes, nbsp);
      3. the normalized view — whitespace and hyphens dropped as well — which is
         what bridges the differences between the typeset PDF an anchor was
         quoted from and the IDML it has to land in (`. ’` for `.’`, a word
         broken over a line end, a spaced em dash).

    The span is returned rather than a start offset because tier 3 can match a
    run of a different length than the needle, and it is the *real* characters
    that get overwritten. `partial_words` allows a tier-3 match to clip a word in
    half; it is off for the text an edit replaces (`conces-`, a word truncated at
    a line break, must not overwrite the first six letters of "concession") and
    on for a context anchor, which only ever narrows and never overwrites."""
    if not needle:
        return []
    hits = _find_all(haystack, needle)
    if hits:
        return [(s, s + len(needle)) for s in hits]
    folded = _find_all(fold_punct(haystack), fold_punct(needle))
    if folded:
        return [(s, s + len(needle)) for s in folded]
    idx = cache.get(haystack) if cache is not None else NormIndex(haystack)
    spans = idx.spans(needle)
    if partial_words:
        return spans
    probe = normalize(needle)
    return [sp for sp in spans if _whole_words(haystack, sp, probe)]


# Characters that continue a word for the tier-3 boundary check. The apostrophes
# are here because "wouldn’t" is one word, so a match ending at "wouldn" has cut
# one in half exactly as a match ending mid-"concession" would.
_WORDISH = "'’"


def _whole_words(text: str, span: tuple[int, int], probe: str) -> bool:
    """Whether a normalized match sits on word boundaries — i.e. it did not clip
    a longer word down to a prefix or suffix of itself. Only the ends that are
    themselves word characters are checked; an anchor that starts on punctuation
    is free to begin mid-word."""
    start, end = span

    def wordish(ch: str) -> bool:
        return ch.isalnum() or ch in _WORDISH

    if probe[:1].isalnum() and start > 0 and wordish(text[start - 1]):
        return False
    if probe[-1:].isalnum() and end < len(text) and wordish(text[end]):
        return False
    return True


def all_occurrences(haystack: str, needle: str) -> list[int]:
    """Every start offset of `needle` in `haystack` — `all_spans` read as start
    offsets, for callers that only need to know whether and where it is."""
    return [s for s, _ in all_spans(haystack, needle)]


def _find_all(haystack: str, needle: str) -> list[int]:
    out, start = [], haystack.find(needle)
    while start != -1:
        out.append(start)
        start = haystack.find(needle, start + 1)
    return out


def _direct_candidates(edit: Edit, stories: list[Story],
                       cache: IndexCache) -> list:
    """Every (story, paragraph, start, end) where `find` occurs directly, ignoring
    any context anchor. This is the set a bare find matches against — and the
    fallback a context narrows, or that recovers an edit when the context fails to
    land."""
    out = []
    for s in stories:
        for p in s.paragraphs:
            for start, end in all_spans(p.text, edit.find, cache=cache):
                out.append((s, p, start, end))
    return out


def _narrow_to_context(edit: Edit, candidates: list, cache: IndexCache) -> list:
    """The candidates that fall inside an occurrence of the context anchor, so a
    common word is pinned to the instance whose surrounding line the correction
    named. At most one per occurrence of the context — the first copy inside the
    marked run — since that run is what the reviewer's mark covered.

    Empty when the context is nowhere to be found (even normalized) or when no
    copy of `find` lies inside it; the caller then keeps the wider set."""
    kept, seen = [], set()
    for story, para, start, end in candidates:
        for c0, c1 in all_spans(para.text, edit.context, cache=cache,
                                partial_words=True):
            if c0 <= start and end <= c1:
                key = (story.story_id, para.index, c0)
                if key not in seen:
                    seen.add(key)
                    kept.append((story, para, start, end))
                break
    return kept


def _narrow_to_page(edit: Edit, candidates: list, scope) -> list:
    """The candidates that lie on the page the correction was marked on.

    This is the narrowing an IDML could not do before — a book file has no pages,
    so a mark on page 49 had the whole book to land in, and a `find` of "," had
    six thousand places to be. `scope` maps a proof's page back to the run of book
    text it set, so the same mark now chooses among the handful of commas on that
    one page."""
    if scope is None or not edit.page or not scope.knows(edit.page):
        return []
    return [c for c in candidates
            if scope.contains(edit.page, c[0].story_id, c[1].index, c[2], c[3])]


def _match(edit: Edit, stories: list[Story], cache: IndexCache, scope=None):
    """Locate the edit across all stories. Returns (story, paragraph, start, end)
    to apply, or an EditOutcome describing why it could not be applied.

    Every copy of `find` in the book is the starting set, and each anchor the
    correction carries — the page it was marked on, the line it was marked in —
    only ever *narrows* that set. Neither is a second thing that must
    independently be present: an anchor that fails to resolve is dropped and the
    wider set is kept, so a correction is never lost to a page map that could not
    place a page or a context line that caught an extraction artifact. Only a
    `find` still repeated after every anchor it carries has been applied is left
    for a human, and the flag then says which anchor failed to choose — a choice
    to surface, not to guess."""
    candidates = _direct_candidates(edit, stories, cache)
    if not candidates:
        return _diagnose_miss(edit, stories, cache)
    failed: list[str] = []

    where = ""
    on_page = _narrow_to_page(edit, candidates, scope)
    if on_page:
        candidates = on_page
        where = f" on page {edit.page}"
    elif edit.page and scope is not None:
        failed.append(f"the page {edit.page} it was marked on"
                      if scope.knows(edit.page)
                      else f"the page {edit.page} it was marked on "
                           f"(which could not be placed in the book)")

    if edit.context:
        in_context = _narrow_to_context(edit, candidates, cache)
        if in_context:
            candidates = in_context
        else:
            failed.append("the marked context")

    n = len(candidates)
    if edit.occurrence == 0:
        if n > 1:
            if failed:
                detail = (f"the text appears {n} times{where} and "
                          + " and ".join(failed)
                          + " could not choose between them")
            else:
                detail = (f"the text appears {n} times{where}; nothing in the "
                          f"correction chooses between them")
            return EditOutcome(edit, AMBIGUOUS, occurrences=n, detail=detail)
        return candidates[0]
    if edit.occurrence > n:
        return EditOutcome(edit, NOT_FOUND, occurrences=n,
                           detail=f"asked for #{edit.occurrence} of {n}")
    return candidates[edit.occurrence - 1]


def _diagnose_miss(edit: Edit, stories: list[Story], cache: IndexCache):
    """Why an edit found nowhere to land — told apart so the flag is useful. A
    span that straddles a paragraph break is the specific thing corrections must
    refuse; a story is flattened with a space between paragraphs to catch it."""
    if edit.context:
        # The context was the anchor, so diagnose it. If the context is present
        # but did not contain `find`, that is the mismatch to name; otherwise the
        # context itself is missing or spans a break.
        for s in stories:
            for p in s.paragraphs:
                if all_spans(p.text, edit.context, cache=cache,
                             partial_words=True):
                    return EditOutcome(
                        edit, NOT_FOUND, story_id=s.story_id,
                        detail="the context was found but the text to change was "
                               "not inside it")
        for s in stories:
            flat = " ".join(p.text for p in s.paragraphs)
            if all_spans(flat, edit.context, cache=cache, partial_words=True):
                return EditOutcome(edit, CROSSES_PARAGRAPH, story_id=s.story_id,
                                   detail="the context spans a paragraph break")
        # Reached only once the fallback to `find` alone has also come up empty,
        # so both the target text and its context are absent — name both.
        return EditOutcome(edit, NOT_FOUND, occurrences=0,
                           detail="neither the text to change nor its context "
                                  "was found in the book")
    for s in stories:
        flat = " ".join(p.text for p in s.paragraphs)
        if all_spans(flat, edit.find, cache=cache):
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
                     withheld: dict[str, str] | None = None, scope=None
                     ) -> tuple[list[EditOutcome], set[str]]:
    """Apply edits to already-parsed stories, mutating them in place. Returns
    the per-edit outcomes and the set of changed story ids. The in-memory core
    the verifier reuses to compute what a clean apply would produce.

    `withheld` maps an edit id to the reason a pre-apply sanity gate held it back;
    such an edit is never applied and comes back as a `WITHHELD` outcome for a
    human, so the gate's decision is surfaced, not silent. `scope` is an optional
    page map (see `pagemap`) that narrows each edit to the run of book text the
    page it was marked on set."""
    outcomes: list[EditOutcome] = []
    changed: set[str] = set()
    touched = _Touched()
    cache = IndexCache()
    withheld = withheld or {}
    # Structural edits last: inserting or deleting a paragraph renumbers every one
    # after it, which would leave any edit that has not run yet holding an index
    # that no longer means what it meant. Each re-locates its own anchor by text, so
    # deferring them costs nothing and removes the whole class of drift.
    for edit in sorted(edits, key=lambda e: e.is_structural):
        if edit.id in withheld:
            outcomes.append(EditOutcome(edit, WITHHELD, detail=withheld[edit.id]))
            continue
        # A formatting edit is checked before the design branch and before the
        # no-op one: it changes no text, so both would otherwise swallow it — the
        # first as a layout request to hand to a designer, the second as nothing to
        # do. It is neither; the italics of a film title are as much a correction as
        # its spelling, and an IDML can carry them.
        if edit.is_layout:
            outcomes.append(_apply_paragraph(edit, stories, cache, scope, touched))
            if outcomes[-1].applied:
                changed.add(outcomes[-1].story_id)
            continue
        if edit.is_format:
            outcomes.append(_apply_format(edit, stories, cache, scope, touched))
            if outcomes[-1].applied:
                changed.add(outcomes[-1].story_id)
            continue
        if edit.kind == DESIGN:
            # Nothing is changed, but the note is *located* if it can be: a check a
            # designer cannot find is barely a check, and "a design request" with no
            # page was what these used to amount to.
            placed = _match(edit, stories, cache, scope) if edit.find else None
            if isinstance(placed, tuple):
                story, para, _s, _e = placed
                outcomes.append(EditOutcome(
                    edit, ROUTED_TO_DESIGN, story_id=story.story_id,
                    paragraph=para.index,
                    detail="to check in InDesign — nothing in the file was changed"))
            else:
                outcomes.append(EditOutcome(
                    edit, ROUTED_TO_DESIGN,
                    detail="a design request, not a text edit"))
            continue
        if edit.find == edit.replace:
            outcomes.append(EditOutcome(edit, NO_CHANGE))
            continue
        found = _match(edit, stories, cache, scope)
        if isinstance(found, EditOutcome):
            outcomes.append(found)
            continue
        story, para, start, end = found
        key = (story.story_id, para.index)
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


def _apply_format(edit: Edit, stories: list[Story], cache: IndexCache, scope,
                  touched: "_Touched") -> EditOutcome:
    """Style the span an edit names, leaving its text alone.

    Located exactly as a text edit is — the page it was marked on, the line, the
    same three matching tiers — so "italicize this" is as anchored as "change this",
    and as refusable. A span whose text is not held directly by a character range
    cannot be styled without rewriting more of the story than a correction should,
    so it is flagged rather than forced."""
    found = _match(edit, stories, cache, scope)
    if isinstance(found, EditOutcome):
        return found
    story, para, start, end = found
    key = (story.story_id, para.index)
    if touched.collides(key, start, end):
        return EditOutcome(
            edit, OVERLAPS, story_id=story.story_id, paragraph=para.index,
            detail="its span overlaps a correction already applied here")
    if not para.restyle(start, end, FORMATS[edit.format]):
        return EditOutcome(
            edit, UNSTYLEABLE, story_id=story.story_id, paragraph=para.index,
            detail="the text here is not held by a character range this engine can "
                   "split, so the formatting would mean rewriting more of the story "
                   "than a correction should")
    # Recorded like a text write, at its own length, so a later correction on the
    # same words is flagged instead of landing inside the run just restyled.
    touched.record(key, start, end, end - start)
    return EditOutcome(edit, APPLIED, story_id=story.story_id,
                       paragraph=para.index, occurrences=1)


def _apply_paragraph(edit: Edit, stories: list[Story], cache: IndexCache, scope,
                     touched: "_Touched") -> EditOutcome:
    """Carry out a whole-paragraph request: a forced break, a keep, a paragraph
    style, a paragraph inserted or removed.

    The paragraph acted on is the one the anchor lands in, so these are located and
    refused exactly as a word swap is — a layout note that cannot be placed in the
    book is flagged, not guessed at. Setting a property on one paragraph means first
    giving it a range of its own (`Story.isolate`); a story whose shape will not
    allow that is flagged rather than styled across its neighbours."""
    found = _match(edit, stories, cache, scope)
    if isinstance(found, EditOutcome):
        return found
    story, para, start, end = found
    key = (story.story_id, para.index)
    if touched.collides(key, start, end):
        return EditOutcome(
            edit, OVERLAPS, story_id=story.story_id, paragraph=para.index,
            detail="its span overlaps a correction already applied here")
    index = para.index
    if edit.paragraph == PARA_DELETE:
        ok = story.delete_paragraph(index)
    elif edit.is_structural:
        ok = story.insert_paragraph(
            index, edit.replace, style=edit.paragraph_style,
            after=(edit.paragraph != PARA_INSERT_BEFORE))
    else:
        attrs = dict(PARA_ATTRS.get(edit.paragraph, {}))
        if edit.paragraph_style:
            attrs["AppliedParagraphStyle"] = edit.paragraph_style
        ok = story.set_paragraph_attrs(index, attrs)
    if not ok:
        return EditOutcome(
            edit, UNPLACEABLE, story_id=story.story_id, paragraph=index,
            detail="this paragraph could not be given a style range of its own, so "
                   "the request would have applied to its neighbours too")
    if not edit.is_structural:
        # Structural edits shift every index after them, so their spans are not
        # comparable with anything recorded before; they run last for that reason
        # and re-find their own anchor by text.
        touched.record(key, start, end, end - start)
    return EditOutcome(edit, APPLIED, story_id=story.story_id, paragraph=index,
                       occurrences=1)


def apply_edits(src_idml: str | Path, dest_idml: str | Path,
                edits: list[Edit], *,
                withheld: dict[str, str] | None = None,
                scope=None) -> ApplyReport:
    """Apply every edit to a copy of `src_idml`, writing `dest_idml`.

    The source is never touched. Only stories that actually changed are
    rewritten; the rest of the package is copied byte for byte. `withheld` is the
    optional sanity-gate verdict — edit ids held back for a human, with a reason.
    `scope` is the optional page map that pins each edit to the page it was
    marked on."""
    stories = read_stories(src_idml)
    by_id = {s.story_id: s for s in stories}
    outcomes, changed = apply_to_stories(stories, edits, withheld=withheld,
                                         scope=scope)
    payload = {sid: by_id[sid].serialize() for sid in changed}
    rewrite_stories(src_idml, dest_idml, payload)
    return ApplyReport(outcomes=tuple(outcomes),
                       stories_changed=tuple(sorted(changed)))
