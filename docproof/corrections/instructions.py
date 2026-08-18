"""Reviewer notes that are a function of the text they are attached to.

Most of what a copy editor writes on a proof is not a request to be interpreted.
"Lowercase" against a highlighted word means that word in lower case. "Replace
comma with period" against a highlighted clause means that clause's comma is a
full stop. "wouldn't" against "wouldnt" means exactly what it says. Each is one
right answer, computable from the note and the marked span, with nothing to
decide.

Those notes were nonetheless being handed to a model, and the model had to invent
a `find` for them — from a PDF rendering of a book it had never been shown. It
routinely produced a bare `","`, which has seven thousand homes in a novel, or
gave up and marked the note a query for a human. So the failure was not that the
notes were hard; it was that the easy ones were being asked of the wrong thing.

This resolves them directly instead. Every rule is conservative in the same two
ways: the `find` it produces is always a **substring of the marked span**, so it
is grounded in text a reviewer actually pointed at rather than recalled; and any
note a rule cannot read with certainty returns None and goes to the model exactly
as before. A rule that is unsure is worth nothing, so it declines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .model import FORMAT_ITALIC, FORMAT_ROMAN, JUDGMENT, MECHANICAL

# How many words a marked span may hold and still be treated as naming its target.
# A highlight over a word or a short phrase is pointing at it; a sticky note's
# anchor is the whole line it sits on, which points at nothing in particular, and
# a rule that guessed a target inside one would be doing the thing this module
# exists to avoid.
MAX_ANCHOR_WORDS = 12
# The same for the note: a short note is a label, a long one is prose to read.
MAX_NOTE_WORDS = 8

# Punctuation a reviewer names in words, and the character the house sets. The
# curly quotes and the true dashes are deliberate — the book is normalized to
# them, so a rule that inserted a straight quote would introduce the very
# inconsistency the review pass exists to catch.
PUNCTUATION = {
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "question mark": "?",
    "exclamation point": "!",
    "exclamation mark": "!",
    "ellipsis": "…",
    "em dash": "—",
    "en dash": "–",
    "hyphen": "-",
    "colon": ":",
    "semicolon": ";",
    "apostrophe": "’",
    "single quote": "’",
    "single quotes": "’",
    "double quote": "”",
    "double quotes": "”",
}
# Openers, for the marks that come in pairs.
_OPENERS = {"’": "‘", "”": "“"}
# Words that make a note an instruction to carry out rather than a label to
# substitute. "did" against "done" is a replacement; "Delete" is not.
_VERBS = (
    "replace", "remove", "delete", "add", "insert", "capitalize", "capitalise",
    "lowercase", "lower case", "cap ", "italic", "de-italic", "roman",
    "hyphenate", "close up", "spell out", "enclose", "possessive", "should",
    "check", "confirm", "unsure", "confusing", "recommend", "designer",
    "previously", "query", "au:", "comp:", "stet", "move", "delete",
)
# Sentence punctuation trimmed off a marked span to find the word inside it. The
# quotes are NOT here: a mark on ‘Sides is pointing at the quote as much as the
# word, and the rules that care handle it themselves.
_EDGE = " \t\n,.;:!?"


@dataclass(frozen=True)
class Resolved:
    """A note read into an exact edit over the marked span."""
    find: str
    replace: str
    kind: str = MECHANICAL
    rule: str = ""                     # which rule fired, for the report
    format: str = ""                   # character formatting, instead of a rewrite


def resolve(instruction: str, anchor: str, *, context: str = "",
            highlighted: bool = True) -> Resolved | None:
    """The edit a note makes to the span it is attached to, or None.

    `anchor` is the marked text — a highlight's own span, which is what makes a
    rule safe to run. `highlighted` is False for a sticky note, whose anchor is
    only the line it sits on: such a note names no target, so every rule that
    needs one declines.

    `context` is the whole line the mark sat on. It is used only by the one rule
    whose note reaches past the marked words by construction — "replace the comma
    with a period and capitalize *she*", where "she" is the next word and outside
    the highlight. Every other rule stays inside the mark.

    None means "no rule is certain about this", and the caller should fall back to
    the model. That is the common case for anything genuinely discursive, and it is
    the only safe answer for anything else."""
    note = (instruction or "").strip()
    anchor = (anchor or "").strip()
    context = (context or "").strip()
    if not note or not anchor:
        return None
    if len(anchor.split()) > MAX_ANCHOR_WORDS:
        return None
    low = note.lower()

    for rule in (_italic, _literal_after_colon, _replace_punctuation,
                 _remove_punctuation, _add_punctuation, _case_change,
                 _named_capital, _hyphenate, _enclose_in_quotes, _quote_style,
                 _quoted_proposal, _bare_replacement):
        got = rule(low, note, anchor, highlighted)
        if got is None and rule is _replace_punctuation and context:
            got = _replace_punctuation_over_line(low, note, anchor, context)
        if got is not None and got.find and got.find in (anchor + "\n" + context):
            if got.find == got.replace and not got.format:
                return None            # a rule that changes nothing is no rule
            return got
    return None


def _replace_punctuation_over_line(low, note, anchor, context) -> Resolved | None:
    """`Replace comma with period and capitalize "she"`, worked over the whole line
    the mark sat on.

    A reviewer highlights the comma, not the word after it, so this one note
    reaches outside its own mark by construction — and declining it costs the
    commonest instruction on a proof after the bare word swap. Widening is only
    safe under two conditions, both required here: the named mark occurs exactly
    once in the line, so there is no choice to make; and the word to capitalize
    comes *directly* after it, which is what proves the line is the sentence the
    note is about rather than some other line that happens to hold a comma."""
    m = re.search(r'and\s+capitali[sz]e\s+"?([a-z]+)"?', low)
    if not m:
        return None
    want = m.group(1)
    swap = re.match(r"^(?:replace|change)\s+(?:the\s+)?([a-z ]+?)\s+with\s+"
                    r"(?:an?\s+|the\s+)?([a-z ]+?)\s*$", _strip_capitalize(low))
    if not swap:
        return None
    old = PUNCTUATION.get(swap.group(1).strip())
    new = PUNCTUATION.get(swap.group(2).strip())
    if old is None or new is None or context.count(old) != 1:
        return None
    # The mark the note names has to be the one this line's target word follows.
    at = context.index(old)
    after = re.match(rf'\s*["“”\'‘’]*({re.escape(want)})\b', context[at + 1:],
                     re.IGNORECASE)
    if not after:
        return None
    replaced = context[:at] + new + context[at + 1:]
    start = at + 1 + after.start(1)
    replaced = replaced[:start] + replaced[start].upper() + replaced[start + 1:]
    return _focus(context, replaced, "replace-punctuation-over-line")


def edits_from_comments(comments) -> tuple[list[dict], list]:
    """Split a proof's comments into the ones the rules can resolve and the ones
    the model still has to read.

    Returns `(rows, unresolved)`. Each row is a corrections entry in the same shape
    the extractor emits — `find`/`replace`/`context`/`instruction`/`kind`/`source` —
    so the two halves merge into one list a reviewer reads before anything is
    applied, and neither half can tell which produced which. `unresolved` is the
    comments to batch to the model, and it is the only thing that costs anything.

    The page is not carried here: an edit cites its comment's id in `source`, and
    that id already names the page (`parse.page_from_source`), so it arrives on the
    edit without anyone having to retype it."""
    rows: list[dict] = []
    unresolved: list = []
    for c in comments:
        instruction = getattr(c, "instruction", "") or ""
        anchor = getattr(c, "anchor", "") or ""
        context = getattr(c, "context", "") or ""
        highlighted = getattr(c, "kind", "") == "highlight"
        got = resolve(instruction, anchor, context=context,
                      highlighted=highlighted)
        if got is None:
            unresolved.append(c)
            continue
        row = {"find": got.find, "replace": got.replace,
               "instruction": instruction}
        if got.format:
            row["format"] = got.format
        if context and context != got.find:
            row["context"] = context
        if got.kind != MECHANICAL:
            row["kind"] = got.kind
        if getattr(c, "id", ""):
            row["source"] = c.id
        rows.append(row)
    return rows, unresolved


# --- the rules ----------------------------------------------------------------

def _italic(low, note, anchor, highlighted) -> Resolved | None:
    """`Italicize`, `Italicize movie title`, `De-italicize`.

    Not a layout request, whatever it looks like: the italics of a film or album
    title are part of the text as much as its spelling, and an IDML carries them on
    the character range around the words. So the mark names its own target — the
    highlighted span is exactly what gets styled — and the edit changes no text."""
    if not highlighted:
        return None
    if re.match(r"^(?:de-?italici[sz]e|roman|set\s+roman|un-?italici[sz]e)\b", low):
        want = FORMAT_ROMAN
    elif re.match(r"^italici[sz]e\b", low) or low in ("italic", "italics"):
        want = FORMAT_ITALIC
    else:
        return None
    # A note naming what to italicize ("Italicize movie title") is still about the
    # marked words; a note naming something else entirely is not this rule's.
    find = anchor.strip(_EDGE + "“”‘’\"'")
    if not find:
        return None
    return Resolved(find, find, MECHANICAL, f"{want}-format", format=want)

def _literal_after_colon(low, note, anchor, highlighted) -> Resolved | None:
    """`Add abbreviating apostrophe: callin'` — the note states the answer after a
    colon, so the tail is the replacement and the marked word is the find. The
    most reliable shape there is: a reviewer who writes the corrected text is not
    leaving anything to be worked out."""
    if ":" not in note:
        return None
    head, _, tail = note.partition(":")
    literal = tail.strip().rstrip(".").strip()
    if not literal or len(literal.split()) > MAX_NOTE_WORDS:
        return None
    if len(head.split()) > MAX_NOTE_WORDS:
        return None                    # prose that happens to hold a colon
    find = _target_of(anchor, literal)
    if find is None or literal == find:
        return None
    return Resolved(find, literal, MECHANICAL, "literal-after-colon")


def _replace_punctuation(low, note, anchor, highlighted) -> Resolved | None:
    """`Replace comma with period`, and the same with a capitalized next word.

    The named mark has to occur exactly once in the marked span. More than once and
    the note does not say which, so the rule declines rather than picking — the
    whole failure being fixed here came from picking."""
    m = re.match(r"^(?:replace|change)\s+(?:the\s+)?([a-z ]+?)\s+with\s+"
                 r"(?:an?\s+|the\s+)?([a-z ]+?)\s*$", _strip_capitalize(low))
    if not m:
        # "Replace with em dash" — the mark to replace is whatever the span holds.
        m2 = re.match(r"^replace\s+with\s+(?:an?\s+|the\s+)?([a-z ]+?)\s*$",
                      _strip_capitalize(low))
        if not m2:
            return None
        target = PUNCTUATION.get(m2.group(1).strip())
        if target is None:
            return None
        hits = [c for c in anchor if c in ",.;:!?-–—…"]
        if len(set(hits)) != 1:
            return None
        old, new = hits[0], target
    else:
        old = PUNCTUATION.get(m.group(1).strip())
        new = PUNCTUATION.get(m.group(2).strip())
        if old is None or new is None:
            return None
    if anchor.count(old) != 1:
        return None
    find = anchor
    replace = anchor.replace(old, new)
    replace = _apply_capitalize(low, replace)
    if replace is None:
        return None
    return _focus(find, replace, "replace-punctuation")


def _remove_punctuation(low, note, anchor, highlighted) -> Resolved | None:
    """`Remove comma`, `Remove single quotes`."""
    m = re.match(r"^(?:remove|delete|drop|omit)\s+(?:the\s+)?([a-z ]+?)\s*$", low)
    if not m:
        return None
    name = m.group(1).strip()
    mark = PUNCTUATION.get(name)
    if mark is None:
        return None
    pair = name.endswith("s") and mark in _OPENERS
    marks = [mark, _OPENERS[mark]] if pair else [mark]
    if sum(anchor.count(c) for c in marks) < 1:
        return None
    if not pair and anchor.count(mark) != 1:
        return None
    replace = anchor
    for c in marks:
        replace = replace.replace(c, "")
    return _focus(anchor, replace, "remove-punctuation")


def _add_punctuation(low, note, anchor, highlighted) -> Resolved | None:
    """`Add comma after`, `Add period after` — the mark goes at the end of the
    marked word. Needs a marked target, so a sticky note's whole line declines."""
    if not highlighted:
        return None
    m = re.match(r"^(?:add|insert)\s+(?:an?\s+|the\s+)?([a-z ]+?)"
                 r"(?:\s+after)?\s*$", low)
    if not m:
        return None
    mark = PUNCTUATION.get(m.group(1).strip())
    if mark is None:
        return None
    find = anchor.strip(_EDGE)
    if not find or find.endswith(mark):
        return None
    # "after" means after the marked words, so the mark has to end where they do.
    # A span of several words, or one that runs past a full stop into the next
    # sentence, has overshot the target and the end of it is the wrong place — a
    # vocative comma asked for after "Sure" must not land after "Sure.” I was".
    if len(find.split()) > 3 or re.search(r"[.!?][\s”’\"']", find):
        return None
    return Resolved(find, find + mark, MECHANICAL, "add-punctuation")


def _case_change(low, note, anchor, highlighted) -> Resolved | None:
    """`Lowercase`, `Capitalize` — the marked word's first letter."""
    if not highlighted:
        return None
    if low in ("lowercase", "lower case", "lc", "lowercase this"):
        want = str.lower
    elif low in ("capitalize", "capitalise", "cap", "uc", "capitalize this",
                 "capitalise this"):
        want = str.upper
    else:
        return None
    find = anchor.strip(_EDGE)
    # "Lowercase" names no word, so the mark has to be *on* the word. Over any
    # longer span the first letter is a guess, and it is the wrong one as often as
    # not — a "Capitalize" on a line that opens mid-sentence means a word further
    # in, and one on a hyphenation fragment ("hibition") means nothing at all.
    if not find or len(find.split()) != 1:
        return None
    for i, ch in enumerate(find):
        if ch.isalpha():
            replace = find[:i] + want(ch) + find[i + 1:]
            return (None if replace == find
                    else Resolved(find, replace, MECHANICAL, "case-change"))
    return None


def _named_capital(low, note, anchor, highlighted) -> Resolved | None:
    """`Capital T` — the note names the letter, so the word is not in doubt even
    when the span holds several.

    It is the *word starting* with that letter, not any occurrence of it: a note
    reading "Capital T" against "the tournament" is asking for one of those two
    words, and counting bare letters would find four t's and mean nothing. Two
    candidate words is still a choice the note does not make, so it declines."""
    m = re.match(r"^capital\s+([a-z])\s*$", low)
    if not m:
        return None
    letter = m.group(1)
    find = anchor.strip(_EDGE)
    starts = [w.start() for w in re.finditer(rf"\b{letter}", find, re.IGNORECASE)]
    if len(starts) != 1:
        return None
    at = starts[0]
    if find[at] == letter.upper():
        return None
    return Resolved(find, find[:at] + letter.upper() + find[at + 1:],
                    MECHANICAL, "named-capital")


def _hyphenate(low, note, anchor, highlighted) -> Resolved | None:
    """`Hyphenate` — the space between the two marked words becomes a hyphen."""
    if low not in ("hyphenate", "hyphen", "hyphenate this", "add hyphen"):
        return None
    find = anchor.strip(_EDGE)
    if find.count(" ") != 1:
        return None
    return Resolved(find, find.replace(" ", "-"), MECHANICAL, "hyphenate")


def _enclose_in_quotes(low, note, anchor, highlighted) -> Resolved | None:
    """`Enclose in quotation marks` — the marked span, quoted."""
    if not highlighted:
        return None
    if not re.match(r"^enclose\s+in\s+(quotes|quotation\s+marks)\s*$", low):
        return None
    find = anchor.strip(_EDGE)
    if not find or find[0] in "“‘\"'":
        return None
    return Resolved(find, f"“{find}”", MECHANICAL, "enclose-in-quotes")


def _quote_style(low, note, anchor, highlighted) -> Resolved | None:
    """`Replace single quotes with doubles` — the pair the reviewer marked."""
    if not re.match(r"^(?:replace|change)\s+single\s+quotes?\s+with\s+"
                    r"double(?:s|\s+quotes?)?\s*$", low):
        return None
    opens, closes = anchor.count("‘"), anchor.count("’")
    if opens + closes == 0:
        return None
    # An apostrophe is the same character as a closing single quote, so a span with
    # closers but no opener cannot be told from one holding contractions. Only a
    # matched pair is unambiguous.
    if opens != 1 or closes != 1:
        return None
    replace = anchor.replace("‘", "“").replace("’", "”")
    return _focus(anchor, replace, "quote-style")


def _quoted_proposal(low, note, anchor, highlighted) -> Resolved | None:
    """`Should this be "not at all"?` — a question, so a person still decides, but
    the text it proposes is right there in quotes. Carried as a judgment edit with
    the proposal filled in, which is strictly more use to an editor than the
    "the model proposed no concrete change" it used to become."""
    if not highlighted or "?" not in note:
        return None
    quoted = re.findall(r'"([^"]{1,60})"|“([^”]{1,60})”', note)
    if len(quoted) != 1:
        return None
    literal = (quoted[0][0] or quoted[0][1]).strip()
    find = anchor.strip(_EDGE)
    if not literal or not find or literal == find:
        return None
    return Resolved(find, literal, JUDGMENT, "quoted-proposal")


def _bare_replacement(low, note, anchor, highlighted) -> Resolved | None:
    """A note that is simply the corrected text — `wouldn't`, `all right`,
    `twenty-five`, `’til`. What a copy editor writes when the fix is obvious, and
    a third of the notes on a real proof.

    Gated hard, because "the note is the answer" is only true when the note is not
    an instruction and not prose: a marked target, both sides short, no
    instruction verb, no question."""
    if not highlighted or note.endswith("?") or len(note.split()) > 4:
        return None
    if any(v in low for v in _VERBS):
        return None
    if not re.fullmatch(r"[\w’'\-–—.,!\" ]+", note):
        return None
    if not any(ch.isalnum() for ch in note):
        return None
    find = _target_of(anchor, note)
    if find is None or find.lower() == note.lower():
        return None                    # nothing close enough, or a mere quotation
    return Resolved(find, note, MECHANICAL, "bare-replacement")


# --- helpers ------------------------------------------------------------------

# How like the note a run of the marked span has to be for the note to be read as
# a correction *of that run*. A copy editor writing "all right" against "alright"
# is rewriting one word, not the line it sits on, and replacing the whole span
# with the note would delete the rest of the line. So the run is identified by
# similarity, and a note that resembles nothing in the span is declined.
TARGET_FLOOR = 0.5
# The longest run, in words, a short note is allowed to be correcting. "all right"
# replaces one or two words; nothing a four-word note rewrites is safe to guess.
TARGET_WORDS = 3


def _compare(s: str) -> str:
    """A run reduced to what a comparison should care about: letters and digits,
    folded. `alright.` and `Alright` compare equal, and the quotes and spacing the
    PDF reader may have invented drop out."""
    return "".join(c.lower() for c in s if c.isalnum())


def _target_of(anchor: str, literal: str) -> str | None:
    """The run of `anchor` that `literal` is a correction of, or None.

    Every run of up to `TARGET_WORDS` words is scored against the note, and the
    closest is taken if it is close enough. This is what keeps "all right" from
    replacing the whole line it was marked on — and what lets a note land on the
    word it means when the marked span reaches past it."""
    want = _compare(literal)
    if not want:
        return None
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", anchor)]
    best_key, best = (TARGET_FLOOR, 0), None
    for i in range(len(tokens)):
        for j in range(i, min(i + TARGET_WORDS, len(tokens))):
            start, end = tokens[i][0], tokens[j][1]
            run = _compare(anchor[start:end])
            score = SequenceMatcher(None, run, want).ratio()
            # A correction of a word almost always keeps the word's opening letter.
            # Where it does not, the match is more likely a fragment the marked span
            # happens to share — "backup" against a span truncated to "up the" would
            # otherwise rewrite "up" and produce "back backup the". Declining costs
            # nothing; the model still sees the note.
            if not run or run[0] != want[0]:
                continue
            # On a tie the shorter run wins: a note rewrites a word, not the phrase
            # around it.
            key = (score, -(end - start))
            if key > best_key:
                best_key, best = key, (start, end)
    if best is None:
        return None
    return anchor[best[0]:best[1]].strip(_EDGE) or None

def _strip_capitalize(low: str) -> str:
    """The note without a trailing `and capitalize "she"` clause, so the
    punctuation rules can read the part that names the marks."""
    return re.sub(r"\s+and\s+capitali[sz]e\s+.*$", "", low).strip()


def _apply_capitalize(low: str, text: str) -> str | None:
    """`… and capitalize "she"` applied to a replacement, or None if the word the
    note names is not in the span (so the mark did not reach it and the rule
    cannot carry the note out in full)."""
    m = re.search(r'and\s+capitali[sz]e\s+"?([a-z]+)"?', low)
    if not m:
        return text
    word = m.group(1)
    hit = re.search(rf"\b{re.escape(word)}\b", text)
    if not hit:
        return None
    at = hit.start()
    return text[:at] + text[at].upper() + text[at + 1:]


# The shortest `find` a rule will emit. A minimal span is the right thing to
# *write* — it cannot disturb a neighbour, and two marks on one line do not
# collide — but it is the wrong thing to *locate*: the minimal span of "replace
# this comma with a period" is "," and a novel holds seven thousand of those. So a
# rule narrows to the change and then widens back out until the span is long
# enough to be an address.
MIN_FIND = 12


def _focus(find: str, replace: str, rule: str) -> Resolved:
    """The edit narrowed to the run that actually changes, then widened until the
    `find` is long enough to locate. Both sides keep the same window, so the
    replacement stays a character-for-character rewrite of the text it replaces and
    cannot drop a word."""
    pre = 0
    while pre < len(find) and pre < len(replace) and find[pre] == replace[pre]:
        pre += 1
    suf = 0
    while (suf < len(find) - pre and suf < len(replace) - pre
           and find[-1 - suf] == replace[-1 - suf]):
        suf += 1
    # Widen alternately, left first, so the span reaches back over the word the
    # mark sat on rather than only forward into the next one.
    while pre + suf > 0 and (len(find) - pre - suf) < MIN_FIND:
        if pre:
            pre -= 1
        elif suf:
            suf -= 1
    return Resolved(find[pre:len(find) - suf], replace[pre:len(replace) - suf],
                    MECHANICAL, rule)
