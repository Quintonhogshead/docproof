"""Pulling a correction back to the change its note actually describes.

`apply` is faithful by design: it writes the find/replace pair it is given and
nothing else. That makes the pair itself the place the worst failures start,
because a pair that reached past the word that changes takes the neighbours with
it — and every stage after it dutifully reports the edit as applied exactly,
because it was.

Two shapes account for almost all of it, and both are visible in the pair:

  * the find quotes the punctuation around a word and the replace does not, so
    carrying the edit out deletes a mark nobody asked about. "alright.”" →
    "all right" closes no quotation; "“Alright" → "All right" opens none;
    "1000.”" → "1,000" ends the sentence nowhere.
  * a note about one mark produces a pair that changes two. "Replace hyphen with
    en dash", marked on a score, arrives as a find long enough to carry the
    hyphenated compound in front of it — and en-dashes that too, so
    "left-footed" becomes "left–footed" alongside the "2–0" that was wanted.

This family is an over-grab: the find reached past the word that changes and took
a neighbouring mark with it. The members that need no judgement at all are
repaired here — deterministically, on the free path, before the list is ever
reviewed — rather than left for the grammar gate to catch downstream.

Every repair narrows: it shrinks the run an edit writes over and never widens
it, it leaves the replacement's words alone, and it declines wherever the note
licenses the change it is looking at. A repair that would turn the edit into a
no-op is not made, so an edit can never be repaired out of existence.
"""
from __future__ import annotations

import re

from .apply import _core
from .textmatch import normalize

# The marks that carry meaning at the edge of a quoted anchor, by class. The
# class is what tells a *substitution* from a *loss*: a note that turns a comma
# into a full stop writes one terminal mark over another, and that pair is doing
# exactly what it says. A double quote arriving as an apostrophe is not that —
# the two are not interchangeable, and the pair has simply dropped one.
_DOUBLE = "“”\"„«»"
_SINGLE = "‘’'‹›"
# The dashes belong with the sentence marks, not with the compounding hyphen:
# "Replace with em dash" is written against a comma, and a find ending in that
# comma with a replacement ending in the dash is a substitution — the commonest
# one a proof carries. Reading it as a dropped comma took the comma out of the
# find and left "right—, when things got tough" in the book.
_TERMINAL = ".,;:!?…—–"
_EDGE = _DOUBLE + _SINGLE + _TERMINAL
_CLASSES = (("double", _DOUBLE), ("single", _SINGLE), ("terminal", _TERMINAL))

# The words that put a mark in a note's scope. A note naming the mark it is about
# has said what to do with it, so the repair stands back — even at the cost of
# leaving a real over-grab alone, which costs a flag, where repairing a mark the
# reviewer meant to change costs a wrong line in a printed book.
# "quote"/"quotation" are deliberately in both quote classes: a reviewer writing
# "remove the quote" has not said which, and not touching either is the direction
# to be wrong in.
_LICENCES: dict[str, tuple[str, ...]] = {
    "double": ("quote", "quotes", "quotation", "quotation mark", "quote mark",
               "punctuation"),
    "single": ("apostrophe", "quote", "quotes", "quotation", "quotation mark",
               "quote mark", "punctuation"),
    "terminal": ("period", "full stop", "comma", "question mark", "exclamation",
                 "semicolon", "semi-colon", "colon", "ellipsis", "punctuation",
                 "dot"),
}
_WORD = re.compile(r"[a-z]+(?:[ -][a-z]+)?")


def _mark_class(ch: str) -> str:
    """The class of a single character, or "" when it is not an edge mark."""
    for name, chars in _CLASSES:
        if ch and ch in chars:
            return name
    return ""


def _licensed(note: str, ch: str) -> bool:
    """Whether the note names the kind of mark `ch` is, and so has already said
    what should happen to it."""
    words = _LICENCES.get(_mark_class(ch), ())
    if not words:
        return False
    low = (note or "").lower()
    return any(w in low for w in words)


def keep_edge_marks(find: str, replace: str, instruction: str = ""
                    ) -> tuple[str, str]:
    """`find` shortened at either end, wherever the pair drops a mark silently.

    Only the find moves: shortening it leaves the mark outside the run the edit
    writes over, so the book keeps it and the words still change exactly as the
    replacement says. That is the same result as adding the mark back onto the
    replacement, arrived at without inventing a character.

    A trim is made only when all of these hold, so the repair cannot fire on a
    pair that is doing what it says:

      * the mark is at the very edge of the find, and the replacement's own edge
        is not the same mark (it would be kept anyway) nor even one of the same
        class (that is a substitution — a comma written as a full stop);
      * the note nowhere names that kind of mark;
      * the replacement really does carry fewer of them than the find;
      * and the shorter find is still a different string from the replacement,
        so an edit is never trimmed down into a no-op.

    A pure deletion (an empty replacement) is left alone: it names the run it
    removes, and the marks in it are part of what was asked for."""
    if not find or not replace:
        return find, replace
    find = _trim(find, replace, instruction, at_start=True)
    find = _trim(find, replace, instruction, at_start=False)
    return find, replace


def _trim(find: str, replace: str, note: str, *, at_start: bool) -> str:
    """`find` with the dropped marks taken off one end, outermost first."""
    while len(find) > 1:
        ch = find[0] if at_start else find[-1]
        if ch not in _EDGE:
            return find
        edge = replace[:1] if at_start else replace[-1:]
        if edge == ch or _mark_class(edge) == _mark_class(ch):
            return find                     # kept, or written over by its own kind
        if _licensed(note, ch) or find.count(ch) <= replace.count(ch):
            return find
        shorter = find[1:] if at_start else find[:-1]
        if shorter == replace:
            return find                     # trimming it away leaves nothing to do
        find = shorter
    return find


# The dashes a hyphen is marked to become. An em dash is here as well as an en:
# the note says "en dash" and the reviewer means the mark they drew, and either
# way one hyphen is being pointed at, not two.
_DASHES = "–—"


def keep_marked_dash(find: str, replace: str, instruction: str = ""
                     ) -> tuple[str, str]:
    """A hyphen-to-dash pair held to the hyphen the mark was on.

    A mark on a score — "2-0", "1-0" — comes back with a find long enough to be
    unique, and the replacement re-typed with every hyphen in that run changed.
    The one between the digits is the correction; a hyphen inside a closed
    compound ("left-footed", "tap-in") caught in the same run is not, and an en
    dash there is wrong.

    So when a pair does nothing but turn hyphens into dashes, and does it in more
    than one place, and one of those places sits between two digits, the others
    are put back. Deliberately narrow: a pair with a single swap is untouched
    whatever it flanks (an en dash between letters is right for an open compound
    — "high school–aged", "Enzo Scifo–style"), and so is one whose swaps are all
    numeric or all not."""
    if len(find) != len(replace) or find == replace:
        return find, replace
    swaps, other = [], False
    for i, (a, b) in enumerate(zip(find, replace)):
        if a == b:
            continue
        if a == "-" and b in _DASHES:
            swaps.append(i)
        else:
            other = True
    if other or len(swaps) < 2:
        return find, replace                # not a pure dash pair, or one mark only
    numeric = {i for i in swaps
               if find[i - 1:i].isdigit() and find[i + 1:i + 2].isdigit()}
    if not numeric or len(numeric) == len(swaps):
        return find, replace
    kept = list(replace)
    for i in swaps:
        if i not in numeric:
            kept[i] = "-"
    return find, "".join(kept)


def repair_pair(find: str, replace: str, instruction: str = ""
                ) -> tuple[str, str]:
    """Both repairs, in the order they compose: the edges first, so the dash pass
    reads the trimmed run. Returns the pair unchanged when neither applies."""
    find, replace = keep_edge_marks(find, replace, instruction)
    return keep_marked_dash(find, replace, instruction)


# Three more repairs, each reading the reviewer's note beside the find/replace and
# each narrow enough to be wrong about nothing else. Unlike the two above — which
# read only the pair — these need the note, because the failure is a mismatch
# between what the reviewer wrote and what the edit carries: a "remove" that
# substitutes, a written-out correction whose case was dropped, a spell-out that
# lost the qualifier fused to it. They run late, over the whole edit list, so they
# catch a slip a model pass introduces (a re-anchored "remove" that came back as a
# swap) as well as one the extractor made.

# The annotation a model pass appends to a note ("Remove comma — second look:
# re-anchored: …"). The reviewer's own words are what these repairs read, so the
# tail is cut off first.
_ANNOTATION = re.compile(r"\s+—\s+(?:second look|resolved on the book"
                         r"|re-anchored)\b", re.IGNORECASE)
# A note that is an instruction to carry out rather than a literal the reviewer
# wrote — so the two literal-repairs below stand down, exactly as the extraction
# rules do.
_INSTRUCTION_VERB = re.compile(
    r"\b(?:replac|remov|delet|add|insert|capital|lowercas|lower case|italic"
    r"|roman|hyphenat|close up|spell out|enclos|swap|chang|correct|move|stet"
    r"|swash)\w*", re.IGNORECASE)
# The marks a "remove" note can name, by the word it uses.
_NAMED_MARK = {
    "comma": ",", "period": ".", "full stop": ".", "colon": ":",
    "semicolon": ";", "semi-colon": ";", "question mark": "?",
    "exclamation point": "!", "exclamation mark": "!", "hyphen": "-",
}
_PUNCT = set(".,;:!?—–-…")
# A qualifier a spell-out note may leave fused to the front of a hyphenated find.
# Deliberately a closed set of the prefixes a date or degree carries, so a closed
# compound ("left-footed", "tap-in") is never rebuilt around a swapped tail.
_QUALIFIER = re.compile(
    r"^(?:mid|late|early|pre|post|non|self|ex|anti|semi|sub|super|co|re|un|well"
    r"|multi|inter|over|under|high|low)$", re.IGNORECASE)


def _note_head(instruction: str) -> str:
    """The reviewer's own note, before any pass appended its annotation."""
    return _ANNOTATION.split(instruction or "", 1)[0].strip()


def enforce_removal(find: str, replace: str, instruction: str = ""
                    ) -> tuple[str, str]:
    """A "remove <mark>" note whose edit *substitutes* the mark, repaired to
    delete it.

    "Remove comma" is a deletion: the comma goes and nothing takes its place. An
    edit that came back turning the comma into a full stop — which a re-anchor
    pass did on the last proof, shipping "your buddy. here know'd" — did the
    opposite of what the note said, and left a sentence broken mid-line. So when
    the note is purely a removal of a named mark, and the run the edit changes is
    exactly that mark swapped for a single other mark, the swap is turned back
    into the deletion the reviewer asked for. A pair that already deletes the
    mark, or changes more than the one mark, is left alone."""
    head = _note_head(instruction).lower()
    m = re.fullmatch(r"(?:remove|delete|drop|omit|take out)\s+(?:the\s+)?"
                     r"([a-z][a-z ]*?)", head)
    if not m:
        return find, replace
    name = m.group(1).strip()
    mark = _NAMED_MARK.get(name) or _NAMED_MARK.get(name.rstrip("s"))
    if mark is None:
        return find, replace
    pre, suf = _core(find, replace)
    old = find[pre:len(find) - suf]
    new = replace[pre:len(replace) - suf]
    if old != mark or not new:
        return find, replace               # already a deletion, or a different run
    if len(new) != 1 or new not in _PUNCT or new == mark:
        return find, replace               # not a plain mark-for-mark substitution
    return find, find[:pre] + find[len(find) - suf:]


def restore_literal_case(find: str, replace: str, instruction: str = ""
                         ) -> tuple[str, str]:
    """A bare-literal note whose reviewer-written case the edit dropped, restored.

    A copy editor who writes "Fourth" beside "4th" has written the case they want
    — it is the holiday, not the ordinal — and applying it as "fourth" corrects
    the numeral and loses the capital in the same stroke. When the note is a plain
    literal (no instruction verb), and the replacement is that literal in a
    different case and nothing more, the reviewer's case is restored."""
    note = _note_head(instruction)
    if not note or _INSTRUCTION_VERB.search(note):
        return find, replace
    if not 1 <= len(note.split()) <= 3 or not re.fullmatch(r"[\w’'\- ]+", note):
        return find, replace
    if note == replace or note.casefold() != replace.casefold():
        return find, replace               # already the reviewer's case, or not it
    return find, note


def keep_hyphen_qualifier(find: str, replace: str, instruction: str = ""
                          ) -> tuple[str, str]:
    """A bare spell-out that dropped a hyphen-attached qualifier the note never
    named, restored.

    "nineties" marked on "mid-90s" spells out the "90s"; the "mid-" is a separate
    morpheme the reviewer did not touch, and dropping it broadens the date from
    the middle of the decade to the whole of it. The same reviewer's same note on
    "late-90s" was applied as "late-nineties", so the convention is not in doubt.
    When the note is a plain literal equal to the whole replacement, the find is a
    known qualifier hyphenated to a tail, and neither the note nor the replacement
    carries that qualifier, it is put back."""
    note = _note_head(instruction)
    if not note or _INSTRUCTION_VERB.search(note):
        return find, replace
    if "-" not in find or "-" in replace or note.casefold() != replace.casefold():
        return find, replace
    prefix = find.partition("-")[0]
    if not _QUALIFIER.fullmatch(prefix):
        return find, replace
    if prefix.casefold() in note.casefold() \
            or replace.casefold().startswith(prefix.casefold()):
        return find, replace               # the note named it, or it is already kept
    return find, f"{prefix}-{replace}"


# A note that names an apostrophe and nothing lexical, and the openers a dialect
# elision gets set with. A reviewer marking ‘on't, ‘sides, ‘round writes "drop
# apostrophe" or just "apostrophe": the wrong-facing open single quote the PDF
# carries is to be set as the elision apostrophe ’ it should be, and NOTHING else
# is licensed. The failure this repairs shipped ’on't as "don't" — a model that
# read the elision as a truncated word and completed the letter, under a note that
# named only the mark. The straight ' and the backtick ` are the other shapes the
# same wrong-facing opener arrives as.
_APOSTROPHE_NOTE = re.compile(r"\bapostrophe\b", re.IGNORECASE)
_WRONG_OPENERS = "‘'`"


def flip_wrong_apostrophe(find: str, replace: str, instruction: str = ""
                          ) -> tuple[str, str]:
    """A "drop apostrophe" note whose edit changed the marked quote into a letter,
    repaired to the pure ’ flip the note asked for.

    Fires only on the exact, unambiguous shape: the note names an apostrophe, the
    one mark the edit changes is a wrong-facing or straight open single quote, and
    the replacement set it to something other than ’. The repair rewrites that one
    character to ’ and leaves every other character of the run alone — so ‘on't
    becomes ’on't, never don't. A pair already making the flip (‘sides → ’sides),
    or one whose changed run is not a lone opener, is left untouched."""
    if not find or not _APOSTROPHE_NOTE.search(_note_head(instruction)):
        return find, replace
    pre, suf = _core(find, replace)
    old = find[pre:len(find) - suf]
    new = replace[pre:len(replace) - suf]
    if len(old) != 1 or old not in _WRONG_OPENERS or new == "’":
        return find, replace               # not a lone opener, or already the flip
    fixed = find[:pre] + "’" + find[len(find) - suf:]
    if fixed == find or fixed == replace:
        return find, replace               # no change to make, or already there
    return find, fixed


def repair_from_note(find: str, replace: str, instruction: str = ""
                     ) -> tuple[str, str]:
    """The note-reading repairs, composed. Returns the pair unchanged when none
    applies."""
    find, replace = enforce_removal(find, replace, instruction)
    find, replace = flip_wrong_apostrophe(find, replace, instruction)
    find, replace = restore_literal_case(find, replace, instruction)
    return keep_hyphen_qualifier(find, replace, instruction)
