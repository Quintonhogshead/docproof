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

The model sanity gate calls this family "over_grab" and holds it back for a
person to look at. These are the members of it that need no judgement at all, so
they are repaired here instead — deterministically, on the free path, before the
list is ever reviewed.

Every repair narrows: it shrinks the run an edit writes over and never widens
it, it leaves the replacement's words alone, and it declines wherever the note
licenses the change it is looking at. A repair that would turn the edit into a
no-op is not made, so an edit can never be repaired out of existence.
"""
from __future__ import annotations

import re

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
#
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
