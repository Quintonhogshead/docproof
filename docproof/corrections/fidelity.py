"""A deterministic gate between extraction and apply — is each edit faithful to the
mark that produced it?

`apply` proves an edit landed and `verify` proves the file matches the edit. Neither
can see the thing that was actually wrong on Shams Book 7: an edit whose find/replace
pair does something the reviewer's mark did not ask for. The extractor reads a note
and a highlight into a pair, and at low effort it sometimes quotes the wrong sentence,
picks the wrong mark of several, reaches past the highlight, or ignores the literal
the reviewer wrote. Each applies perfectly and verifies clean, because the check
downstream is of faithfulness to the *pair*, never of the pair against the *mark*.

This gate reads each edit beside the comment it came from and holds back the ones it
can prove are unfaithful — never guessing, only refusing, exactly as `apply` refuses
an anchor it cannot place. It costs no model call: every check is a comparison of the
edit's own find/replace against the note text, the highlighted anchor, and the book's
text for the page the mark was on. An edit with no comment behind it (a typed list) is
never touched, and any check that cannot be made with certainty passes.

Two kinds of intervention, both conservative:

  * a *reclassification* — a note that asks a question ("Should this be…?",
    "Recommend…") but was extracted as a concrete mechanical change is turned back
    into the judgment it should have been, so a person decides rather than the
    extractor deciding silently;
  * a *withhold* — an edit whose change provably falls outside the mark, lands on
    the wrong page, or does something other than what the note names is held for a
    human, with the reason, the same way the opt-in sanity gate holds one back.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import replace as _replace

from .apply import _core, all_spans
from .instructions import asks_for_a_change
from .model import DESIGN, Edit, JUDGMENT
from .textmatch import normalize

# A word, apostrophes and all ("aren’t", "y’all"), for the word-level comparisons.
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")

# A note whose action is a styling or enclosing the second look can now carry out
# through a format edit — italics, roman, or enclosing quotation marks. An edit
# that came back a no-op on one of these is one the extractor could not express,
# not one the text already satisfied, so it is turned into a query for the second
# look rather than reported as done or flagged as unactioned.
_STYLING_NOTE = re.compile(r"\b(?:italic|roman|de-?italic|un-?italic|enclos"
                           r"|in quotes|in quotation)\w*", re.IGNORECASE)


def screen_edits(edits, comments, *, book_pages=None, pdf_pages=None,
                 page_labels=None) -> tuple[list[Edit], dict[str, str]]:
    """`(edits, withheld)` after the fidelity checks.

    `edits` comes back with any question-note edit reclassified to a judgment; the
    text and anchors are untouched. `withheld` maps an edit id to the reason it was
    held for a human, to be merged into whatever the sanity gate withheld and passed
    to `apply` — a withheld edit is never written, and surfaces as a flag.

    `comments` is the reviewer comment list (dicts or `PdfComment`s). `book_pages`
    maps a proof page to the book's own text for it (from `pagemap.page_book_text`);
    without it the on-page check cannot run. `pdf_pages` is the proof's own rendered
    text per page (1-based by position), which the on-page check consults as direct
    evidence of what a cited page actually holds before it calls an edit a wrong
    copy — the page map can under-cover a page, and the proof cannot. `page_labels`
    maps a proof page to the finished file's folio, so a withhold reason names the
    page the designer's IDML shows; the checks themselves never read it."""
    by_id = _comments_by_id(comments)
    if not by_id:
        return list(edits), {}
    pages = book_pages or {}
    proof = pdf_pages or ()
    out: list[Edit] = []
    withheld: dict[str, str] = {}
    for e in edits:
        comment = _first_comment(e, by_id)
        # A judgment is already a person's call (apply never writes one), a format or
        # paragraph request changes no words for these checks to read, and a design
        # note edits nothing — none is checked. Only a concrete text edit read from a
        # mark is.
        if (comment is None or e.kind in (DESIGN, JUDGMENT)
                or e.is_format or e.is_layout):
            out.append(e)
            continue
        note = (_cget(comment, "instruction") or e.instruction or "").strip()
        anchor = (_cget(comment, "anchor") or "").strip()
        highlighted = _cget(comment, "kind") == "highlight"

        # A question the extractor answered for the reviewer becomes the judgment it
        # should have been — routed to a person (and to the second look), not applied.
        if e.find != e.replace and _is_question(note):
            out.append(_replace(e, kind=JUDGMENT))
            continue

        # A styling note the extractor could not turn into a change — "the song
        # title should be in quotes, not italics" that came back a no-op because
        # the italics are a format the text swap cannot express. It is not a mark
        # the text already satisfies; it is one the second look can now carry out
        # through a companion format edit, so it becomes a query rather than a flag
        # that reads as an author decision.
        if e.find == e.replace and _STYLING_NOTE.search(note) \
                and asks_for_a_change(note):
            out.append(_replace(e, kind=JUDGMENT))
            continue

        reason = (_off_page(e, pages, proof, page_labels)
                  or _outside_mark(e, anchor, highlighted)
                  or _note_vs_diff(e, note))
        if reason:
            withheld[e.id] = reason
        out.append(e)
    return out, withheld


# --- reading the comment list -------------------------------------------------

def _comments_by_id(comments) -> dict:
    by_id: dict = {}
    for c in comments or ():
        cid = _cget(c, "id")
        if cid:
            by_id.setdefault(str(cid), c)
    return by_id


def _first_comment(edit: Edit, by_id: dict):
    """The first source comment an edit cites, or None. An edit may cite several
    (one request marked twice); they share a page and a note, so the first settles
    the checks."""
    for cid in (edit.source or "").split():
        if cid in by_id:
            return by_id[cid]
    return None


def _cget(comment, field: str, default=""):
    if isinstance(comment, dict):
        return comment.get(field, default) or default
    return getattr(comment, field, default) or default


# --- the checks ---------------------------------------------------------------

# How far from the cited page an edit may land before it reads as the wrong copy. A
# mark near the foot of a page routinely quotes a line the book set on the next one,
# so a page either side is allowed; anything past that, when the text sits nowhere
# near the page but does sit somewhere else in the book, is the wrong sentence.
_PAGE_WINDOW = 1


def _off_page(edit: Edit, book_pages: dict, pdf_pages=(),
              page_labels=None) -> str | None:
    """A withhold reason when the edit's text is not on the page the mark was made
    on (nor a page either side of it) yet is elsewhere in the book — the "quoted the
    wrong sentence / the identical line on another page" failure.

    Only fires when the cited page was placed (its text is in `book_pages`): an
    unplaced page tells us nothing, so the check declines rather than guesses.

    And only when the proof itself agrees the text is elsewhere. `book_pages` is the
    page map's *alignment* of the cited page, which is a best-effort narrower and can
    under-cover a page — a line near the foot, a text box, a run the artifacts around
    it kept from aligning. The proof's own rendered page (`pdf_pages`) is the direct
    record of what page N holds, so when the marked text is right there on the proof's
    page N the map simply missed it, and calling it a wrong copy would let a bad
    alignment cost a correction — which the page map is expressly never allowed to do.
    So the proof exonerates before this flags."""
    page = edit.page
    if not page or not edit.find:
        return None
    here = "".join(book_pages.get(page + d, "")
                   for d in range(-_PAGE_WINDOW, _PAGE_WINDOW + 1))
    if not here.strip():
        return None                        # the page was not placed; cannot judge
    if all_spans(here, edit.find):
        return None                        # on the page, or a neighbour — fine
    # Not near the cited page. Only a problem if it is a real place elsewhere; if it
    # is nowhere, that is `apply`'s not-found flag, not a fidelity failure.
    elsewhere = any(all_spans(text, edit.find)
                    for p, text in book_pages.items()
                    if abs(p - page) > _PAGE_WINDOW)
    if not elsewhere:
        return None
    # The alignment says elsewhere; ask the proof. If the marked text sits on the
    # proof's own page N (or a neighbour, for a foot-of-page line the book set on the
    # next), the page map under-covered it and this is the right copy after all.
    if _on_proof_page(edit.find, pdf_pages, page):
        return None
    shown = (page_labels or {}).get(page) or page
    return (f"the text this edit changes is not on page {shown}, where the mark "
            f"was made — it was found on another page, so the mark was matched "
            f"to the wrong copy")


def _on_proof_page(find: str, pdf_pages, page: int) -> bool:
    """Whether `find` appears on the proof's own rendering of `page` (or a page
    either side). `pdf_pages` is 1-based by position; empty or out of range simply
    yields no exoneration, so the caller falls back to the alignment's verdict."""
    if not pdf_pages:
        return False
    n = len(pdf_pages)
    here = "".join(pdf_pages[p - 1]
                   for p in range(page - _PAGE_WINDOW, page + _PAGE_WINDOW + 1)
                   if 1 <= p <= n)
    return bool(here.strip()) and bool(all_spans(here, find))


def _outside_mark(edit: Edit, anchor: str, highlighted: bool) -> str | None:
    """A withhold reason when the run the edit actually changes falls outside the
    highlighted text — the reviewer marked one thing and the edit changed another.

    Read off the anchor, not an offset, so it is robust to the PDF-vs-book coordinate
    gap. Two shapes, each certain:

      * a *word* the edit changes that the highlight does not contain at all
        ("Lowercase both" on "Hundred Percent" that also lowercased the "One" before
        it). Only the words that actually differ between find and replace are looked
        at, so a quote conversion — which rewrites a passage's marks but none of its
        words — is not mistaken for one;
      * a *punctuation* change whose immediate neighbours are both absent from the
        mark ("Remove comma" marked on "But, the playoffs" that removed the comma in
        "expected, another" instead — neither "expected" nor "another" is in the
        highlight)."""
    if not highlighted or not edit.find:
        return None
    marked = normalize(anchor, fold_case=True)
    if len(marked) < 4:
        return None                        # too short an anchor to bound a change
    changed = _changed_words(edit.find, edit.replace)
    absent = [w for w in changed
              if len(w) > 1 and normalize(w, fold_case=True) not in marked]
    if absent:
        return (f"this edit changes text the highlight does not cover "
                f"({', '.join(repr(w) for w in absent[:3])}) — the mark was on "
                f"other words, so the change landed outside it")
    pre, suf = _core(edit.find, edit.replace)
    core = edit.find[pre:len(edit.find) - suf]
    if not changed and core and not _WORD.search(core):
        # A single mark moved, with no word between the two ends of the change — is
        # it even near the highlight? A change that spans a word ("paused, again,"
        # → "paused again") has a word in its core and is a legitimate multi-mark
        # edit, not this. Both neighbours absent from the mark is the wrong-mark
        # signature ("expected, another" changed under a "But, the playoffs" mark).
        before, after = _mark_neighbours(edit)
        if (before and after
                and normalize(before, fold_case=True) not in marked
                and normalize(after, fold_case=True) not in marked):
            return (f"this edit changes a mark between \"{before}\" and \"{after}\", "
                    f"neither of which is in the highlighted text — the mark was "
                    f"made elsewhere on the line")
    return None


def _changed_words(find: str, replace: str) -> list[str]:
    """The words (find side) that actually differ between `find` and `replace`, by a
    word-level diff. Case-sensitive, so a case change counts as a difference; a
    change that only moves punctuation returns nothing."""
    fw, rw = _WORD.findall(find), _WORD.findall(replace)
    changed: list[str] = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
            None, fw, rw, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            changed.extend(fw[i1:i2])
    return changed


def _mark_neighbours(edit: Edit) -> tuple[str, str]:
    """The word just before and just after the run an edit changes — the words on
    either side of a moved punctuation mark."""
    pre, suf = _core(edit.find, edit.replace)
    before = _WORD.findall(edit.find[:pre])
    after = _WORD.findall(edit.find[len(edit.find) - suf:])
    return (before[-1] if before else "", after[0] if after else "")


# The marks a note names, by the word it uses for them — so "replace the comma" can
# be checked against the character the edit actually changed.
_NAMED_MARK = {
    "comma": ",", "period": ".", "full stop": ".", "colon": ":",
    "semicolon": ";", "semi-colon": ";", "question mark": "?",
    "exclamation point": "!", "exclamation mark": "!", "ellipsis": "…",
    "em dash": "—", "em-dash": "—", "en dash": "–", "en-dash": "–",
    "hyphen": "-", "apostrophe": "’",
}
# The dashes read as one class: a note that says "em dash" beside a book that sets an
# en dash, or vice versa, is not the mismatch this check exists to catch.
_MARK_CLASS = {"—": "dash", "–": "dash", "-": "dash"}
# A note that is an instruction to carry out, not a literal the reviewer wrote. When
# a note holds one of these it is prose, and the bare-literal check stands down.
_INSTRUCTION_WORD = re.compile(
    r"\b(?:replac|remov|delet|add|insert|capital|lowercas|lower case|italic"
    r"|roman|hyphenat|close up|spell out|enclos|swap|chang|move|stet|swash"
    r"|cap\b|uc\b|lc\b|recommend|should|check|confirm|designer|previously"
    r"|comp|au)\w*",
    re.IGNORECASE)


def _note_vs_diff(edit: Edit, note: str) -> str | None:
    """A withhold reason when the change the edit makes is not the change the note
    names. Three shapes, each certain:

      * a "replace X with Y" note where the mark the edit changed is not the X the
        note names (the note said comma, the edit changed a period);
      * a "replace with <one mark>" note where the edit converted more than one mark
        (one em dash asked for, both commas em-dashed);
      * a bare literal — the corrected words the reviewer wrote out — that the
        replacement does not carry ("would start" written, "started." applied)."""
    low = note.lower()
    return (_wrong_named_mark(edit, low)
            or _too_many_marks(edit, low)
            or _dropped_literal(edit, note, low))


def _changed_marks(edit: Edit) -> tuple[str, str]:
    """The run of `find` the edit removes and the run of `replace` it adds — the
    marks on each side of the change, punctuation and letters alike."""
    pre, suf = _core(edit.find, edit.replace)
    return edit.find[pre:len(edit.find) - suf], edit.replace[pre:len(edit.replace) - suf]


def _wrong_named_mark(edit: Edit, low: str) -> str | None:
    m = re.search(r"\brepl(?:ace|acing)\s+(?:the\s+)?([a-z][a-z \-]*?)\s+with\b", low)
    if not m:
        return None
    named = _NAMED_MARK.get(m.group(1).strip())
    if named is None:
        return None
    old, _new = _changed_marks(edit)
    present = [c for c in old if c in _NAMED_MARK.values()]
    if not present:
        # The note asks to replace a punctuation mark, but the edit changed no mark
        # at all — the punctuation part of a compound note was dropped. "Replace
        # comma with period and capitalize she" that only capitalized: the comma is
        # still a comma. Only when the mark isn't already correct where the change is.
        if named not in edit.replace or edit.find.count(named) > edit.replace.count(named):
            return (f"the note asks to replace a {m.group(1).strip()}, but the edit "
                    f"changed no punctuation — the mark the reviewer named is "
                    f"untouched")
        return None
    want = _MARK_CLASS.get(named, named)
    if any(_MARK_CLASS.get(c, c) == want for c in present):
        return None                        # the named mark is among those changed
    return (f"the note asks to replace a {m.group(1).strip()}, but the edit changed "
            f"{_name_of(present[0])} instead — a different mark than the one marked")


def _too_many_marks(edit: Edit, low: str) -> str | None:
    m = re.search(r"\brepl(?:ace|acing)\s+with\s+(?:an?\s+|the\s+)?"
                  r"([a-z][a-z \-]*?)\s*$", _strip_paren(low).rstrip("."))
    if not m:
        return None
    named = _NAMED_MARK.get(m.group(1).strip())
    if named is None:
        return None
    _old, new = _changed_marks(edit)
    inserted = new.count(named)
    if named in _MARK_CLASS:               # count the whole dash class as one target
        inserted = sum(new.count(c) for c, k in _MARK_CLASS.items()
                       if k == _MARK_CLASS[named])
    if inserted > 1:
        return (f"the note asks for one {m.group(1).strip()}, but the edit made "
                f"{inserted} — it converted more marks than were marked")
    return None


def _dropped_literal(edit: Edit, note: str, low: str) -> str | None:
    """A note that is the corrected text itself — "would start", "she was not" — the
    reviewer writing out the fix. The replacement must carry those words; when it
    does not, the edit ignored what the reviewer wrote.

    Held to the unambiguous case: a short note, no instruction verb in it, that reads
    as a run of words (a substitution), and whose words are simply not in the
    replacement. A note that IS an instruction, or that the replacement already
    honours, passes."""
    words = note.split()
    if not 1 <= len(words) <= 6 or _INSTRUCTION_WORD.search(note):
        return None
    if not re.fullmatch(r"[\w’'\- ]+", note) or not any(c.isalpha() for c in note):
        return None
    if normalize(note, fold_case=True) in normalize(edit.replace, fold_case=True):
        return None                        # the replacement carries the literal
    # Every content word of the literal is already in the replacement — the edit
    # honoured it, whatever the order or added marks. Nothing to flag.
    if _overlap(note, edit.replace) >= 1.0:
        return None
    # The literal has to be a genuine replacement for the marked text, not an
    # unrelated short note: it shares something with the find (it is a rewrite of it),
    # and it is not simply a re-quote of the find unchanged.
    if _overlap(note, edit.find) < 0.25 or _overlap(note, edit.find) >= 0.9:
        return None
    return (f"the reviewer wrote the correction out — \"{note}\" — but the edit "
            f"applied \"{edit.replace}\" instead")


def _overlap(a: str, b: str) -> float:
    """The fraction of `a`'s words that also appear in `b`, case-insensitively — a
    rough measure of how much of one run the other carries."""
    aw = {w.lower() for w in _WORD.findall(a)}
    if not aw:
        return 0.0
    bw = {w.lower() for w in _WORD.findall(b)}
    return len(aw & bw) / len(aw)


def _name_of(mark: str) -> str:
    for name, ch in _NAMED_MARK.items():
        if ch == mark:
            return f"a {name}"
    return f"\"{mark}\""


_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_paren(low: str) -> str:
    """A note without a trailing parenthetical — "replace with en dash (–)" reads the
    same as "replace with en dash"."""
    prev = None
    core = low.strip()
    while core != prev:
        prev = core
        core = _PAREN.sub("", core).strip()
    return core


_QUESTION = re.compile(r"\?\s*$")
# Openers that propose rather than order. Deliberately NOT "would"/"could"/"is" and
# the like: those begin a real question ("Should this be...?", caught by the "?"),
# but they are also the commonest bare-literal corrections a reviewer writes
# ("would", "could", "would get"), and reading one of those as a question would flag
# a third of the proof. A recommendation verb is never a bare literal, so it is safe.
_ASKS = re.compile(r"^\s*(?:recommend|recommending|suggest|suggesting|consider"
                   r"|advise|advising)\b", re.IGNORECASE)


def _is_question(note: str) -> bool:
    """Whether a note is asking rather than instructing — a question, or a
    recommendation that proposes rather than orders. "Should this be 'could'?",
    "Recommend removing the quotes". Such a note names a possibility, and a
    possibility is a person's to accept, not the extractor's to apply."""
    note = (note or "").strip()
    if not note:
        return False
    return bool(_QUESTION.search(note) or _ASKS.match(note))
