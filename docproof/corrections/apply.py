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
                    Edit, EditOutcome, FORMAT_ITALIC, FORMAT_ROMAN, FORMATS,
                    NO_CHANGE, NO_CHARACTER_STYLE, NOT_FOUND, OVERLAPS,
                    PARA_ATTRS, PARA_DELETE, PARA_INSERT_BEFORE,
                    PARA_MERGE_NEXT, PARA_SPLIT_AT, ROUTED_TO_DESIGN,
                    UNPLACEABLE, UNSTYLEABLE, WITHHELD, is_italic_style,
                    is_plain_style)
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
         broken over a line end, a spaced em dash);
      4. the same view with case folded, which bridges the difference a reviewer
         cannot see. A title set in full capitals is a *design* — the story holds
         "TO THE PILOT OF THE BICYCLE CARRIAGE", and a reviewer reading the page
         writes it back the way anyone would write a title. So does one quoting a
         line whose first word only starts a sentence because of where the line
         broke. Neither is a mistake worth refusing a correction over.

    The span is returned rather than a start offset because tier 3 can match a
    run of a different length than the needle, and it is the *real* characters
    that get overwritten. `partial_words` allows a match to clip a word in half;
    it is off for the text an edit replaces (`conces-`, a word truncated at a
    line break, must not overwrite the first six letters of "concession") and on
    for a context anchor, which only ever narrows and never overwrites.

    A find that is a *single word* answers to the word-boundary rule on every
    tier, not only the normalized ones. An exact match is exact about characters
    and says nothing about words: a mark reading "delete the" against "the
    Winn-Dixie" found "the" inside "then" and left "And n", and a mark reading
    "was" for "is" found the "is" inside "this" and left "thwas". Both are exact
    substrings and neither is the word the reviewer circled.

    Only for a lone word, because an anchor of several is routinely quoted from a
    proof *line* and so begins and ends wherever the line did — "ball team, h" is
    the tail of "football team, he", and it is not ambiguous for a moment. It is
    the one-word find that has twins inside longer words, and the one-word find
    this refuses to guess at: when every copy left is inside a longer word, it is
    reported as not found, which is a flag rather than a wrong line."""
    if not needle:
        return []
    # Every tier below is a subset of the last one, because each normalization is
    # applied character by character to both sides: text that carries `needle`
    # exactly still carries it once both are folded, and still carries it once
    # both are folded and case-folded. So the widest view answering "no" is a
    # no for all four, and it answers in one cached lookup and one C-level
    # search. Locating one edit asks this of every paragraph in the book and
    # almost every one of them says no, which is the case this exists for — the
    # ladder below is untouched for the handful that say yes.
    if cache is not None:
        widest = cache.get(haystack, fold_case=True)
        if normalize(needle, fold_case=True) not in widest.view:
            return []
    hits = _on_words(haystack, _find_all(haystack, needle), needle,
                     partial_words)
    if hits:
        return hits
    folded = _on_words(haystack, _find_all(fold_punct(haystack),
                                           fold_punct(needle)),
                       fold_punct(needle), partial_words)
    if folded:
        return folded
    idx = cache.get(haystack) if cache is not None else NormIndex(haystack)
    spans = idx.spans(needle)
    if not spans:
        # Last, and only once every case-exact reading has come up empty, so an
        # anchor that already matches somewhere can never be pulled onto a
        # differently-cased twin. A fold that finds two candidates where one exact
        # match would have done still reports them as ambiguous, which is the
        # direction this engine is allowed to fail in.
        cased = (cache.get(haystack, fold_case=True) if cache is not None
                 else NormIndex(haystack, fold_case=True))
        spans = cased.spans(needle)
        if partial_words:
            return spans
        probe = normalize(needle, fold_case=True)
        return [sp for sp in spans if _whole_words(haystack, sp, probe)]
    if partial_words:
        return spans
    probe = normalize(needle)
    return [sp for sp in spans if _whole_words(haystack, sp, probe)]


# Characters that continue a word for the boundary check. The apostrophes are
# here because "wouldn’t" is one word, so a match ending at "wouldn" has cut one
# in half exactly as a match ending mid-"concession" would.
_WORDISH = "'’"


def _one_word(probe: str) -> bool:
    """Whether an anchor is a single word and nothing else — letters, digits and
    the apostrophes that hold a word together, with no space and no punctuation.

    "the" and "wouldn’t" are; "ball team, h" is not, and neither is "LimeW-",
    whose hyphen is a line break the proof put there. It is the bare word that
    needs a boundary check on an exact match, because it is the bare word that
    has copies hiding inside longer words."""
    return bool(probe) and all(c.isalnum() or c in _WORDISH for c in probe)


def _on_words(text: str, starts: list[int], probe: str, partial_words: bool
              ) -> list[tuple[int, int]]:
    """The spans `probe` occupies at each of `starts`, dropped to the ones that
    sit on word boundaries when it is a lone word. `probe` is the needle as this
    tier compared it, so its length is the span's; `partial_words` keeps every
    hit, for an anchor that only narrows and never overwrites."""
    spans = [(s, s + len(probe)) for s in starts]
    if partial_words or not _one_word(probe):
        return spans
    return [sp for sp in spans if _whole_words(text, sp, probe)]


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


class _Flat:
    """One story's paragraphs as a single string, with the map back to the
    paragraph and offset each character came from.

    A correction is quoted from a typeset page, where a sentence is just a
    sentence. In the book it may be several paragraphs — every line of verse is
    one, and a novel's dialogue turns are too — so a reviewer's quoted line
    routinely straddles a break that means nothing to them. Flattening lets such a
    quote be *found*; what is done with it then depends on the edit, and for a
    context anchor the answer is only ever "narrow to here", which is why this is
    safe to use for one.

    Paragraphs are joined with a newline rather than a space so an exact or
    punctuation-folded match can never run across the join by accident: a match
    that crosses a break is always a tier-3 normalized one, where whitespace has
    been dropped on both sides and the crossing is deliberate."""

    __slots__ = ("text", "_bounds")

    def __init__(self, story: Story) -> None:
        bounds, at = [], 0
        for para in story.paragraphs:
            bounds.append((at, at + len(para.text), para))
            at += len(para.text) + 1           # the "\n" join
        self.text = "\n".join(p.text for p in story.paragraphs)
        self._bounds = bounds

    def flat_span(self, para: Paragraph, start: int, end: int):
        """A paragraph-local span in flat coordinates, or None if that paragraph
        is not this story's."""
        for p0, _p1, p in self._bounds:
            if p is para:
                return p0 + start, p0 + end
        return None

    def covered(self, start: int, end: int) -> list[tuple[Paragraph, int, int]]:
        """The paragraphs a flat span touches, each with the local span it covers,
        in order."""
        out = []
        for p0, p1, para in self._bounds:
            if p0 < end and start < p1 or (start == end == p0):
                out.append((para, max(start, p0) - p0, min(end, p1) - p0))
        return out


def _flat(story: Story, flats: dict) -> _Flat:
    """`story`'s flat view, built once per call that needs it. Keyed by story id
    rather than cached globally: paragraph text changes under an edit, and a stale
    flat view would locate the next edit in text that is no longer there."""
    got = flats.get(story.story_id)
    if got is None:
        got = flats[story.story_id] = _Flat(story)
    return got


def _narrow_to_context_across(edit: Edit, candidates: list,
                              cache: IndexCache) -> list:
    """`_narrow_to_context`, allowing the context to span paragraph breaks.

    The context never gets written to — it only chooses which copy of `find` the
    correction meant — so there is nothing unsafe about matching it across a break
    that the reviewer, reading a typeset page, had no way to see. The edit itself
    still lands inside a single paragraph.

    This is what a proof of verse needs to work at all: "And resist the pull of a
    siren's locomotive" is one sentence to the reviewer and two paragraphs to the
    book, and before this the context simply failed to match and the edit was
    handed back as ambiguous."""
    kept, seen, flats = [], set(), {}
    for story, para, start, end in candidates:
        flat = _flat(story, flats)
        here = flat.flat_span(para, start, end)
        if here is None:
            continue
        for c0, c1 in all_spans(flat.text, edit.context, cache=cache,
                                partial_words=True):
            if c0 <= here[0] and here[1] <= c1:
                key = (story.story_id, c0)
                if key not in seen:
                    seen.add(key)
                    kept.append((story, para, start, end))
                break
    return kept


def _match_across(edit: Edit, stories: list[Story], cache: IndexCache):
    """Locate an anchor that may run across paragraph breaks, as a list of
    `(story, [(paragraph, start, end), ...])` — one entry per place it was found.

    Only the operations that are *about* a break use this (merging two paragraphs,
    splitting one), because for them a `find` that straddles the break is not a
    mistake to refuse but the thing being pointed at."""
    out, flats = [], {}
    for story in stories:
        flat = _flat(story, flats)
        for start, end in all_spans(flat.text, edit.find, cache=cache):
            covered = flat.covered(start, end)
            if covered:
                out.append((story, covered))
    if edit.context:
        narrowed = []
        for story, covered in out:
            flat = _flat(story, flats)
            span = (flat.flat_span(covered[0][0], covered[0][1], covered[0][1]),
                    flat.flat_span(covered[-1][0], covered[-1][2], covered[-1][2]))
            if None in span:
                continue
            for c0, c1 in all_spans(flat.text, edit.context, cache=cache,
                                    partial_words=True):
                if c0 <= span[0][0] and span[1][1] <= c1:
                    narrowed.append((story, covered))
                    break
        if narrowed:
            out = narrowed
    return out


class _Rebase:
    """Each edited paragraph as it was before the run, and where the writes since
    have landed in it — so a line the reviewer quoted can still be found after
    another correction has changed it.

    Every mark on a proof was written against the same page. Corrections are
    applied against the *live* document, though, so the second mark on a line
    arrives to find its own quoted line no longer in the book: "as if there was
    some sort of beacon" is gone the moment the "was" → "were" beside it lands,
    and with it the only thing that said which "the" the next mark meant. The
    context is not text that gets written, only text that chooses, so the answer
    is to look for it where it still exists — in the paragraph as the reviewer
    read it — and carry the span it occupies forward through the writes made
    since.

    Held in original coordinates, which is what makes the two directions
    invertible: writes never overlap (`_Touched` refuses that), so each one moves
    everything after it by a fixed amount and nothing else."""

    __slots__ = ("_before", "_moves")

    def __init__(self) -> None:
        self._before: dict[tuple[str, int], str] = {}
        self._moves: dict[tuple[str, int], list[tuple[int, int, int]]] = {}

    def note(self, key: tuple[str, int], text: str, start: int, end: int,
             new_len: int) -> None:
        """Record a write of `new_len` characters over the live `[start, end)` of
        a paragraph whose text is `text` just before the write."""
        self._before.setdefault(key, text)
        moves = self._moves.setdefault(key, [])
        span = (_to_before(moves, start), _to_before(moves, end), new_len)
        moves.append(span)
        moves.sort()

    def windows(self, key: tuple[str, int], context: str, cache: IndexCache
                ) -> list[tuple[int, int]]:
        """The spans the context occupies in the paragraph *now*, found in the
        paragraph as it was. Empty when this paragraph has not been written to,
        or when the context was not in it to begin with."""
        before = self._before.get(key)
        if before is None:
            return []
        moves = self._moves.get(key, [])
        return [(_to_now(moves, c0), _to_now(moves, c1))
                for c0, c1 in all_spans(before, context, cache=cache,
                                        partial_words=True)]

    def forget(self, story_id: str) -> None:
        """Drop everything remembered about a story whose paragraphs have been
        renumbered. A merge, a split or a deletion rebuilds the paragraph list,
        so an index recorded before it now names a different paragraph — and a
        window computed from one paragraph's old text against another's is worse
        than no window at all. Structural edits run after every text edit, so
        this costs nothing that was going to be used."""
        for store in (self._before, self._moves):
            for key in [k for k in store if k[0] == story_id]:
                del store[key]


def _to_before(moves: list[tuple[int, int, int]], now: int) -> int:
    """A live offset read back into the paragraph's original coordinates."""
    shift = 0
    for start, end, new_len in moves:
        if now <= start + shift:
            break
        shift += new_len - (end - start)
    return now - shift


def _to_now(moves: list[tuple[int, int, int]], before: int) -> int:
    """An original offset carried forward into the paragraph's live
    coordinates."""
    shift = 0
    for start, end, new_len in moves:
        if end > before:
            break
        shift += new_len - (end - start)
    return before + shift


def _narrow_to_context(edit: Edit, candidates: list, cache: IndexCache,
                       rebase: "_Rebase | None" = None) -> list:
    """The candidates that fall inside an occurrence of the context anchor, so a
    common word is pinned to the instance whose surrounding line the correction
    named.

    Every copy inside the marked run is kept, not just the first. A context is
    usually the one line the mark sat on and holds a single copy, which is the
    case this exists for; when it is longer than that — a whole paragraph, which
    is what a model quotes when the mark had no line of its own — it may hold
    several, and taking the first was a guess dressed as an anchor. It read
    "was" → "were", marked on "as if there was some sort of beacon", onto "The
    next time through was" four sentences earlier. Handing all of them back lets
    the caller fall to the evidence that can tell them apart, or flag.

    A context the paragraph no longer carries is looked for in the paragraph as
    it was before this run started, and the span it held is carried forward — see
    `_Rebase`. That is the second mark on a line finding the line it was written
    against, rather than losing it to the first mark.

    Empty when the context is nowhere to be found (even normalized) or when no
    copy of `find` lies inside it; the caller then keeps the wider set."""
    kept = []
    for story, para, start, end in candidates:
        spans = all_spans(para.text, edit.context, cache=cache,
                          partial_words=True)
        if not spans and rebase is not None:
            spans = rebase.windows((story.story_id, para.index), edit.context,
                                   cache)
        for c0, c1 in spans:
            if c0 <= start and end <= c1:
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


def _match(edit: Edit, stories: list[Story], cache: IndexCache, scope=None,
           rebase: "_Rebase | None" = None):
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

    # An ordinal counted on a proof page says nothing about a book the page could
    # not be placed in — "the second copy on page 111" is not "the second copy in
    # the novel", and reading it as one would apply a correction to whichever copy
    # happened to come second. So it is refused rather than reinterpreted.
    if edit.occurrence and edit.page and not on_page and len(candidates) > 1:
        return EditOutcome(
            edit, AMBIGUOUS, occurrences=len(candidates),
            detail=f"the correction names the copy on page {edit.page}, and that "
                   "page could not be placed in the book, so which copy it means "
                   "cannot be told")

    if edit.context:
        # In one paragraph first, then across breaks. The order matters: a context
        # that resolves inside a single paragraph is the tighter answer, and the
        # wider view is only worth reaching for when that finds nothing.
        in_context = (_narrow_to_context(edit, candidates, cache, rebase)
                      or _narrow_to_context_across(edit, candidates, cache))
        if in_context:
            candidates = in_context
        else:
            failed.append("the marked context")

    n = len(candidates)
    # One candidate left is the answer, whatever ordinal the correction carries.
    # The ordinal counts copies on the page; the anchors that ran before it — the
    # page, then the line the mark sat on — have already chosen among those, and
    # having chosen, "the second copy" indexes a set of one and overshoots it. The
    # narrowing is the better evidence, so the ordinal yields to it rather than
    # contradicting it.
    if n == 1:
        return candidates[0]
    # Several copies are still in play, so the line the mark sat on could not
    # choose between them — it holds them all. The ordinal can: it is counted off
    # the mark's own position on the page (`instructions.fill_edit_occurrences`),
    # which is a measurement where "the first copy in the marked run" was a guess.
    # It indexes the page's copies, so it is read against *that* set and not
    # against whatever the context left, and only when the copy it names is one
    # the context kept — two anchors that disagree are not an answer.
    if edit.occurrence and on_page and edit.occurrence <= len(on_page):
        nth = on_page[edit.occurrence - 1]
        if nth in candidates:
            return nth
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


_SENTENCE_END = ".?!"


def _absorb_stale_terminal(para: Paragraph, end: int, found: str,
                           replacement: str) -> int:
    """The span's end, widened over a sentence mark the write would otherwise
    double.

    An edit that *adds* terminal punctuation is anchored on the words, not on a
    mark — "place a ? at the end" says there is none there to quote (see
    `extract._repair_added_terminal_mark`). Usually there is none, and this does
    nothing. When the book turns out to carry one after all, writing "cowardice?"
    in front of it would leave "cowardice?." — so the stale mark is taken into the
    span and replaced along with the words, which is what the reviewer asked for
    either way.

    Deliberately narrow: only a *sentence* mark being written over a sentence
    mark, and only when the text the edit matched did not already end in one — an
    edit that quoted the mark it changes has said what to do with it."""
    if not replacement or replacement[-1] not in _SENTENCE_END:
        return end
    if found and found[-1] in _SENTENCE_END:
        return end                         # the edit rewrote the mark itself
    after = para.text[end:end + 1]
    return end + 1 if after and after in _SENTENCE_END else end


def _absorb_duplicate(para: Paragraph, end: int, found: str,
                      replacement: str) -> int:
    """The span's end, widened over text the write would otherwise say twice.

    An edit that *adds* words keeps the ones it matched and puts more after them
    — find "player of the", replace "Player of the Tournament". When the book
    already carries what is being added, writing the replacement in front of it
    doubles it: "the Player of the Tournament tournament", and "the Shanklins’,,"
    where the replacement ended in the comma the line already had. Both are
    edits the reviewer asked for once and the file then said twice.

    So the added tail is compared against the text that follows the match, and
    whatever the two share is taken into the span and written over. Case is
    folded because the addition is often a capitalization ("tournament" →
    "Tournament") and punctuation is folded because the book's mark and the
    reviewer's may be spelled differently.

    Narrow by construction: it fires only when the replacement is the matched
    text plus something, and only over the characters the book already has, so an
    edit that adds words the book does not carry is untouched."""
    tail = _added_tail(found, replacement)
    if not tail:
        return end
    after = para.text[end:]
    for n in range(min(len(tail), len(after)), 0, -1):
        if fold_punct(after[:n]).lower() == fold_punct(tail[-n:]).lower():
            return end + n
    return end


def _close_deletion_gap(para: Paragraph, start: int, end: int, replacement: str
                        ) -> tuple[int, int]:
    """The span widened by one space, when deleting a word would leave two.

    "Remove the 'the'" is find "the", replace "" — and lifting a word out from
    between its spaces closes over a gap that then sets as a visible double space
    in the book, or as a paragraph that begins with one. The space after the word
    goes with it, which is what a person deleting the word would do. Only for a
    whole deletion, and only where a space would really be left over."""
    if replacement:
        return start, end
    text = para.text
    before = text[start - 1:start] if start else ""
    if text[end:end + 1] == " " and (before == " " or not start):
        return start, end + 1
    if before == " " and end >= len(text):
        return start - 1, end
    return start, end


def _added_tail(found: str, replacement: str) -> str:
    """What `replacement` adds after the text it matched, or "" when it is not
    that shape. Compared case- and punctuation-folded, since a replacement that
    recapitalizes or re-spells a mark inside the matched run is still the same
    run plus a tail."""
    if len(replacement) <= len(found) or not found:
        return ""
    head = replacement[:len(found)]
    if fold_punct(head).lower() != fold_punct(found).lower():
        return ""
    return replacement[len(found):]


def _keep_book_case(found: str, find: str, replace: str) -> str:
    """The replacement to write, in the book's capitals rather than the
    reviewer's, when those are the only thing the two disagree about.

    Case folding is what lets a correction to a title set in full capitals land at
    all — the reviewer writes the title, the story holds the design. But having
    located it that way, writing the reviewer's capitals back would *apply* their
    rendering to the book: shortening a running head would have quietly retyped
    "TO THE PILOT OF THE BICYCLE CARRIAGE" as "To the Pilot of the Bicycle
    Carriage" and taken the design out with the correction.

    So an all-capital run keeps its capitals. Only that one pattern is honoured:
    it is the one a design imposes, and it is unambiguous to restore. Anything
    else — a reviewer correcting case on purpose, mixed case, a difference that is
    not only case — is left exactly as the edit asked."""
    if not replace or found == find:
        return replace
    if normalize(found, fold_case=True) != normalize(find, fold_case=True):
        return replace                     # they differ by more than case
    letters = [c for c in found if c.isalpha()]
    if len(letters) < 2 or not all(c.isupper() for c in letters):
        return replace
    if all(c.isupper() for c in find if c.isalpha()):
        return replace                     # the reviewer wrote capitals too
    return replace.upper()


class _Touched:
    """The character spans already edited in each paragraph, in current (live)
    coordinates, kept correct as later edits shift the text after them. Two
    corrections that land on overlapping spans of one paragraph is the collision
    that garbles a line ("said just said"); the second is flagged, not applied.

    Each span remembers which edit made it, so a collision can *name* the
    correction it hit — which is what lets the flag say something useful, and
    what lets the opt-in second look put the two colliding corrections side by
    side and merge them into one."""

    def __init__(self) -> None:
        self._by_para: dict[tuple[str, int], list[list]] = {}

    def collides(self, key: tuple[str, int], start: int, end: int
                 ) -> tuple[str, ...]:
        """The ids of the edits whose spans this one overlaps — empty when it
        lands clear of everything applied here so far."""
        return tuple(eid for s, e, eid in self._by_para.get(key, ())
                     if start < e and s < end)

    def record(self, key: tuple[str, int], start: int, end: int,
               new_len: int, edit_id: str = "") -> None:
        delta = new_len - (end - start)
        spans = self._by_para.setdefault(key, [])
        for span in spans:                     # shift what sits after this write
            if span[0] >= end:
                span[0] += delta
                span[1] += delta
        spans.append([start, start + new_len, edit_id])


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
    rebase = _Rebase()
    cache = IndexCache()
    withheld = withheld or {}
    # Paragraphs the text edits emptied, and who emptied them — swept up after the
    # loop; see `_remove_emptied`.
    emptied: dict[tuple[str, int], list[str]] = {}
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
            outcomes.append(_apply_paragraph(edit, stories, cache, scope,
                                             touched, rebase))
            if outcomes[-1].applied:
                changed.add(outcomes[-1].story_id)
            continue
        if edit.is_format:
            outcomes.append(_apply_format(edit, stories, cache, scope, touched,
                                          rebase))
            if outcomes[-1].applied:
                changed.add(outcomes[-1].story_id)
            continue
        if edit.kind == DESIGN:
            # Nothing is changed, but the note is *located* if it can be: a check a
            # designer cannot find is barely a check, and "a design request" with no
            # page was what these used to amount to.
            placed = (_match(edit, stories, cache, scope, rebase)
                  if edit.find else None)
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
            # `advice` rides onto the outcome so a query a model studied but would
            # not answer arrives at the report carrying what it found.
            outcomes.append(EditOutcome(edit, NO_CHANGE, detail=edit.advice))
            continue
        found = _match(edit, stories, cache, scope, rebase)
        if isinstance(found, EditOutcome):
            outcomes.append(found)
            continue
        story, para, start, end = found
        key = (story.story_id, para.index)
        hit = touched.collides(key, start, end)
        if hit:
            outcomes.append(EditOutcome(
                edit, OVERLAPS, story_id=story.story_id, paragraph=para.index,
                detail="its span overlaps a correction already applied here",
                collides_with=hit))
            continue
        found_text = para.text[start:end]
        replacement = _keep_book_case(found_text, edit.find, edit.replace)
        r_start, new_text = _with_article_fix(para, start, end, replacement)
        r_end = _absorb_duplicate(para, end, found_text, new_text)
        r_end = _absorb_stale_terminal(para, r_end, found_text, new_text)
        r_start, r_end = _close_deletion_gap(para, r_start, r_end, new_text)
        rebase.note(key, para.text, r_start, r_end, len(new_text))
        para.replace(r_start, r_end, new_text)
        touched.record(key, r_start, r_end, len(new_text), edit.id)
        changed.add(story.story_id)
        if not para.text.strip():
            emptied.setdefault(key, []).append(edit.id)
        outcomes.append(EditOutcome(edit, APPLIED, story_id=story.story_id,
                                    paragraph=para.index, occurrences=1))
    outcomes.extend(_remove_emptied(stories, emptied, changed))
    return outcomes, changed


def _remove_emptied(stories: list[Story], emptied: dict[tuple[str, int], list[str]],
                    changed: set[str]) -> list[EditOutcome]:
    """Take out the paragraphs the corrections emptied, and say so.

    "Remove this" against a whole line is carried out as a deletion of its text,
    and a paragraph with no text in it is still a paragraph: it sets as a blank
    line where the line used to be, which is not what removing it meant. The
    reviewer asked for the line to go, so the line goes.

    It runs after every text edit, with the structural edits, because deleting a
    paragraph renumbers the ones after it — and in descending order within each
    story, so no deletion moves a paragraph another one is still pointing at.
    Each is reported as its own applied change, naming the corrections that
    emptied it, so a designer reading the change log sees the line was removed
    rather than wondering where it went."""
    made: list[EditOutcome] = []
    by_id = {s.story_id: s for s in stories}
    for (story_id, index), ids in sorted(emptied.items(), reverse=True):
        story = by_id.get(story_id)
        if story is None or not 0 <= index < len(story.paragraphs):
            continue
        if story.paragraphs[index].text.strip():
            continue                       # something put text back into it
        # Its own id, carrying the ones that emptied the paragraph. Distinct from
        # theirs because the report reads outcomes back by id, and a duplicate
        # would have this shadow the correction it came from.
        edit = Edit(id="+".join(ids) + "-para", find="", replace="",
                    paragraph=PARA_DELETE,
                    instruction="remove the now-empty paragraph the correction "
                                "above emptied, so it does not set as a blank line")
        if not story.delete_paragraph(index):
            made.append(EditOutcome(
                edit, UNPLACEABLE, story_id=story_id, paragraph=index,
                detail="the paragraph is now empty but could not be given a range "
                       "of its own to remove, so it will set as a blank line "
                       "until someone deletes it in InDesign"))
            continue
        changed.add(story_id)
        made.append(EditOutcome(edit, APPLIED, story_id=story_id,
                                paragraph=index, occurrences=1))
    return made


# The quotation marks that come in pairs, opener -> closer. A straight quote is
# its own partner, which is why the test below is on the pair and not on "is this
# a quotation mark": one closing quote next to a span is not a pair hugging it.
_QUOTE_PAIRS = {"“": "”", "‘": "’", "«": "»", '"': '"', "'": "'"}


def _with_hugging_quotes(para: Paragraph, start: int, end: int, fmt: str
                         ) -> tuple[int, int]:
    """A span widened over a matched pair of quotation marks sitting either side
    of it.

    A reviewer marking a song title for roman writes the title, not the quotes
    around it — but the quotes are set in the same italic as the title, and
    leaving them behind is the difference between the correction being made and
    looking made. Only for italic and roman, only for a *matched* pair, so it can
    take nothing but the marks that belong to the run."""
    if fmt not in (FORMAT_ITALIC, FORMAT_ROMAN):
        return start, end
    text = para.text
    opener = text[start - 1:start] if start else ""
    if opener and _QUOTE_PAIRS.get(opener) == text[end:end + 1]:
        return start - 1, end + 1
    return start, end


def _format_attrs(stories: list[Story], para: Paragraph, start: int, end: int,
                  fmt: str) -> dict:
    """The character attributes that carry out a format request *on this book*.

    An IDML says "italic" two ways — a local `FontStyle` override on the range,
    or a character style the designer applied — and a correction has to answer in
    whichever one the book is already speaking. Clearing a `FontStyle` that was
    never set is what left every "de-italicize" reported as applied and visibly
    unchanged in the file, and writing a local override into a book that keeps a
    style for it leaves italics that will not follow that style if it is edited
    later.

    So: taking italics off clears the local override *and* an applied style that
    is one, and putting them on reaches for the book's own style when the span
    carries no character style to overwrite. Everything else — a swash, a span
    already carrying some other character style — is written exactly as before."""
    attrs = dict(FORMATS[fmt])
    applied = para.applied_character_styles(start, end)
    if fmt == FORMAT_ROMAN:
        if any(is_italic_style(s) for s in applied):
            attrs["AppliedCharacterStyle"] = NO_CHARACTER_STYLE
        return attrs
    if fmt == FORMAT_ITALIC and all(is_plain_style(s) for s in applied):
        named = _book_italic_style(stories)
        if named:
            # The style carries the italic, so no local override is written
            # beside it — one that said "Regular" would fight it.
            return {"AppliedCharacterStyle": named}
    return attrs


def _book_italic_style(stories: list[Story]) -> str:
    """The character style this book sets its italics in, or "" when it uses
    none. Read off the stories themselves, so it is a style the document really
    defines rather than one invented here."""
    for story in stories:
        for applied in sorted(story.character_styles()):
            if is_italic_style(applied):
                return applied
    return ""


def _apply_format(edit: Edit, stories: list[Story], cache: IndexCache, scope,
                  touched: "_Touched", rebase: "_Rebase") -> EditOutcome:
    """Style the span an edit names, leaving its text alone.

    Located exactly as a text edit is — the page it was marked on, the line, the
    same three matching tiers — so "italicize this" is as anchored as "change this",
    and as refusable. A span whose text is not held directly by a character range
    cannot be styled without rewriting more of the story than a correction should,
    so it is flagged rather than forced.

    The overlap guard that every text edit answers to does not apply here, and
    that is the point of the exception: the collision it exists to prevent is one
    write garbling another's words, and this writes no words. A reviewer who
    marks a song title for de-italicizing and separately corrects a quotation
    mark inside it has asked for both, and a designer doing it by hand would do
    both — refusing the second because the first had been made was losing real
    work over a danger that is not present. The span is re-located against the
    live text, so it is the corrected words that get styled; two formats on one
    span settle last-wins, exactly as they would in InDesign.

    How the change is written is read off the book rather than assumed — see
    `_format_attrs`. A book that sets its italics in a character style has them
    taken off the same way; a run marked with the quotation marks around it takes
    them with it, since a song title's quotes are set in whatever the title is."""
    found = _match(edit, stories, cache, scope, rebase)
    if isinstance(found, EditOutcome):
        return found
    story, para, start, end = found
    start, end = _with_hugging_quotes(para, start, end, edit.format)
    key = (story.story_id, para.index)
    if not para.restyle(start, end, _format_attrs(stories, para, start, end,
                                                  edit.format)):
        return EditOutcome(
            edit, UNSTYLEABLE, story_id=story.story_id, paragraph=para.index,
            detail="the text here is not held by a character range this engine can "
                   "split, so the formatting would mean rewriting more of the story "
                   "than a correction should")
    # Recorded like a text write, at its own length, so a later edit that does
    # rewrite these words is flagged instead of landing inside the run just
    # restyled and taking the styling out with it.
    touched.record(key, start, end, end - start, edit.id)
    return EditOutcome(edit, APPLIED, story_id=story.story_id,
                       paragraph=para.index, occurrences=1)


def _apply_paragraph(edit: Edit, stories: list[Story], cache: IndexCache, scope,
                     touched: "_Touched", rebase: "_Rebase") -> EditOutcome:
    """Carry out a whole-paragraph request: a forced break, a keep, a paragraph
    style, a paragraph inserted or removed.

    The paragraph acted on is the one the anchor lands in, so these are located and
    refused exactly as a word swap is — a layout note that cannot be placed in the
    book is flagged, not guessed at. Setting a property on one paragraph means first
    giving it a range of its own (`Story.isolate`); a story whose shape will not
    allow that is flagged rather than styled across its neighbours."""
    breaks = edit.paragraph in (PARA_MERGE_NEXT, PARA_SPLIT_AT)
    found = _match(edit, stories, cache, scope, rebase)
    if isinstance(found, EditOutcome):
        # An anchor that straddles a break is a failure for every other edit and
        # the normal case for these two: the reviewer quoted a sentence, and the
        # break they are asking about is inside it. So retry across paragraphs,
        # but only for the operations that are about the break itself.
        if not (breaks and found.status == CROSSES_PARAGRAPH):
            return found
        spanned = _match_across(edit, stories, cache)
        if len(spanned) != 1:
            return EditOutcome(
                edit, AMBIGUOUS if spanned else NOT_FOUND, occurrences=len(spanned),
                detail=(f"the text appears {len(spanned)} times across the book "
                        "and nothing in the correction chooses between them")
                if spanned else "")
        story, covered = spanned[0]
        para, start, end = covered[0]
        if edit.paragraph == PARA_SPLIT_AT:
            return EditOutcome(
                edit, UNPLACEABLE, story_id=story.story_id, paragraph=para.index,
                detail="the text a new paragraph should start at spans a break "
                       "already, so there is no one point to split")
        if len(covered) != 2:
            return EditOutcome(
                edit, UNPLACEABLE, story_id=story.story_id, paragraph=para.index,
                detail=f"the text spans {len(covered) - 1} paragraph breaks, and "
                       "this joins two paragraphs at one")
    else:
        story, para, start, end = found
        key = (story.story_id, para.index)
        hit = touched.collides(key, start, end)
        if hit:
            return EditOutcome(
                edit, OVERLAPS, story_id=story.story_id, paragraph=para.index,
                detail="its span overlaps a correction already applied here",
                collides_with=hit)
    index = para.index
    if edit.paragraph == PARA_MERGE_NEXT:
        ok = story.merge_paragraph(index)
        if not ok:
            return EditOutcome(
                edit, UNPLACEABLE, story_id=story.story_id, paragraph=index,
                detail="this paragraph and the one after it could not be joined — "
                       "they are set in different paragraph styles, or it is the "
                       "last paragraph of its story")
    elif edit.paragraph == PARA_SPLIT_AT:
        if start == 0:
            # Nothing in front of the anchor means the break the reviewer asked
            # for is already there. That is the request met, not a failure to
            # place it — reporting it as one puts a correction that needs no work
            # on the list of the ones that do, which is where a real problem then
            # hides. It happens whenever a proof is marked against an earlier
            # revision than the file the corrections are applied to.
            return EditOutcome(
                edit, NO_CHANGE, story_id=story.story_id, paragraph=index,
                detail="the paragraph already begins here, so the break the "
                       "correction asks for is present")
        ok = story.split_paragraph(index, start)
        if not ok:
            return EditOutcome(
                edit, UNPLACEABLE, story_id=story.story_id, paragraph=index,
                detail="the paragraph could not be broken here")
    elif edit.paragraph == PARA_DELETE:
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
    if edit.is_structural:
        # Structural edits shift every index after them, so their spans are not
        # comparable with anything recorded before; they run last for that reason
        # and re-find their own anchor by text. What was remembered about this
        # story's paragraphs is now filed under the wrong numbers, so it goes.
        rebase.forget(story.story_id)
    else:
        touched.record(key, start, end, end - start, edit.id)
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
