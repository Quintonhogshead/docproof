"""Deterministic house-style sweeps: the rules a script must own.

The Atmosphere proofreading brief draws a hard line — "reading catches
judgment; scripts catch patterns — do not use one for the other's job" — and
then asks for something a model cannot honestly give: a final match count per
rule. A read can only promise it tried. A sweep can prove it finished, because
after applying its own fixes it re-scans and reports what is left.

Every sweep here is one house rule expressed as text patterns. Each produces
ordinary `Finding` objects, so sweeps ride the same validator, reassembler and
report as the model passes — they become tracked changes the same way, and the
validator's first-claim rule is why they run before any model pass.

Sweeps are conservative on purpose. Where a pattern cannot distinguish a house
violation from an author's deliberate prose, the sweep skips it and leaves it
to the error type that can weigh the context. A sweep that fires wrongly is far
more expensive than one that stays quiet: it edits a manuscript no one asked it
to touch.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Callable, Sequence

from .models import Finding, ParagraphRef

log = logging.getLogger("docproof.sweeps")

# Spaces a sweep treats as "the gap between things": ASCII space, tab, and the
# non-breaking space the ellipsis rule itself installs.
_SP = " \t\u00a0"
# Any mark that can end quoted speech, in any variant. Only used to decide
# whether an ellipsis takes a non-breaking space before it, which is true
# after a closing quote whichever kind it is.
_CLOSING_QUOTES = "\"”'’"


@dataclass(frozen=True)
class Hit:
    """One replacement a sweep wants to make, in paragraph-text offsets."""
    start: int
    end: int
    replacement: str
    explanation: str


@dataclass(frozen=True)
class Sweep:
    key: str
    name: str
    scan: Callable[..., list[Hit]]   # (text, variant) -> hits


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did, in the terms the house brief asks for: how many it
    flagged, and how many its own patterns still match after its fixes are
    applied. `remaining` is the honest end of the sentence "…and zero
    remaining" — anything above zero means the sweep is not idempotent and its
    rule is not fully executed."""
    key: str
    name: str
    flagged: int
    remaining: int


# --- shared helpers ----------------------------------------------------------

def apply_hits(text: str, hits: Sequence[Hit]) -> str:
    """The text as it would read with every hit applied. Used for the
    re-scan, and by tests as the readable statement of what a sweep does."""
    out: list[str] = []
    last = 0
    for h in sorted(hits, key=lambda h: h.start):
        out.append(text[last:h.start])
        out.append(h.replacement)
        last = h.end
    out.append(text[last:])
    return "".join(out)


_SENTENCE_END = re.compile(r"[.!?…][\"”’')\]]*\s+")


def occurrence_of(text: str, needle: str, pos: int) -> int:
    """Which occurrence of `needle` starts at `pos` (1-based), counted the way
    the validator's find_nth counts, so the two always agree."""
    n, i = 0, text.find(needle)
    while i != -1 and i < pos:
        n += 1
        i = text.find(needle, i + 1)
    return n + 1


def sentence_window(text: str, start: int, end: int) -> tuple[str, int, int]:
    """The sentence containing [start, end), where it begins, and which
    occurrence of itself it is.

    Findings quote a sentence rather than the whole paragraph for the same
    reason the model is told to: it is what a person reads in the report, and
    it is short enough to anchor unambiguously."""
    # An out-of-range span (an end-of-text insertion computed as pos+1) must
    # degrade to the trailing sentence, not crash the run at write time.
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    bounds = [0] + [m.end() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    lo = max(b for b in bounds if b <= start)
    hi = min(b for b in bounds if b >= end)
    window = text[lo:hi]
    trimmed = window.rstrip()
    # Only trim when the trailing space is not part of what we are changing.
    if lo + len(trimmed) >= end:
        window = trimmed
    return window, lo, occurrence_of(text, window, lo)


def _sentence_starts_at(text: str, pos: int) -> bool:
    """Whether `pos` begins a sentence, so a spelled-out word takes a capital.
    A line break inside the paragraph (w:br in the canonical text) counts:
    what follows it opens its own line."""
    i = pos - 1
    while i >= 0 and text[i] in _SP + "\"“”'‘’":
        i -= 1
    return i < 0 or text[i] in ".!?…\n"


# --- ellipsis ----------------------------------------------------------------

# Three or more dots, however they are spaced, or an ellipsis character that
# may already be there. The glyph is always normalized to …; whether an
# already-correct … counts as a target depends on the spacing the house wants
# (see `style`), so a bare … is matched and then filtered out when rebuilding
# it changes nothing.
_ELLIPSIS = re.compile(r"\.(?:[ \t\u00a0]*\.){2,}|…")

# The marks a leading space attaches to: a word character or any of these.
# Nothing before the ellipsis (a paragraph start) takes no lead in any mode.
_ELLIPSIS_LEADS = ",;:!?" + _CLOSING_QUOTES


def _sweep_ellipsis(text: str, variant=None, style: str = "nbsp") -> list[Hit]:
    hits: list[Hit] = []
    for m in _ELLIPSIS.finditer(text):
        lo, hi = m.start(), m.end()
        while lo > 0 and text[lo - 1] in _SP:
            lo -= 1
        while hi < len(text) and text[hi] in _SP:
            hi += 1
        before = text[lo - 1] if lo > 0 else ""
        after = text[hi] if hi < len(text) else ""
        attaches = bool(before) and (before.isalnum() or before in _ELLIPSIS_LEADS)
        # Only the lead varies with house style. The trailing space is a plain
        # space whenever a word follows, the same in every mode.
        if style == "nbsp":
            # A non-breaking space, so the ellipsis never wraps away from the
            # word it trails.
            lead = "\u00a0" if attaches else ""
            why = ("House style sets an ellipsis as … with a non-breaking "
                   "space before it.")
        elif style == "space":
            lead = " " if attaches else ""
            why = "House style sets an ellipsis as … with a space on each side."
        else:  # "closed"
            lead = ""
            why = "House style closes up an ellipsis: … with no space before it."
        trail = " " if after.isalnum() else ""
        replacement = f"{lead}…{trail}"
        if text[lo:hi] == replacement:
            continue
        hits.append(Hit(lo, hi, replacement, why))
    return hits


# --- dashes ------------------------------------------------------------------

_DASHES = re.compile(r"(?P<pre>[ \t\u00a0]*)(?P<run>-{2,}|–|—|-)(?P<post>[ \t\u00a0]*)")


def _sweep_dash(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    stripped = text.strip()
    for m in _DASHES.finditer(text):
        # A line that is nothing but dashes is a scene divider, not prose.
        if m.group(0).strip() == stripped:
            continue
        before = text[:m.start("pre")][-1:]
        after = text[m.end("post"):][:1]
        run = m.group("run")
        if run == "—":
            # Already an em dash. House style sets it unspaced, so the only
            # thing to fix is a space around it; the idempotency check below
            # drops the ones that are already tight.
            replacement, why = "—", "House style sets an em dash unspaced."
        elif run == "–":
            # An en dash tight between numbers is a correct range. Spaced, it
            # is being used as a sentence break, which house style sets as an
            # unspaced em dash.
            if not (m.group("pre") and m.group("post")):
                continue
            replacement, why = "—", ("House style sets a sentence-break dash "
                                     "as an unspaced em dash.")
        elif run == "-":
            # A single hyphen reads as a dash ONLY standing alone between
            # words: "it was late - too late". Unspaced it is a compound
            # (well-known) and none of this sweep's business; between digits
            # it is arithmetic or a loose range, both judgment calls; at a
            # line edge it is a bullet or a dangling mark.
            if not (m.group("pre") and m.group("post")):
                continue
            if not before or not after:
                continue
            if before.isdigit() and after.isdigit():
                continue
            if not ((before.isalnum() or before in _CLOSING_QUOTES)
                    and (after.isalnum() or after in "\"“‘'")):
                continue
            replacement, why = "—", ("House style sets a sentence-break dash "
                                     "as an unspaced em dash.")
        elif before.isdigit() and after.isdigit():
            replacement, why = "–", ("A range between numbers takes an "
                                     "unspaced en dash.")
        else:
            replacement, why = "—", ("A typed hyphen run used as a sentence "
                                     "break becomes an unspaced em dash.")
        if m.group(0) == replacement:
            continue
        hits.append(Hit(m.start(), m.end(), replacement, why))
    return hits


# --- stacked punctuation -----------------------------------------------------

_STACKED = re.compile(r"[!?]{2,}|‽")


def _sweep_stacked_punctuation(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _STACKED.finditer(text):
        run = m.group(0)
        # A question survives its own emphasis: "?!" is still a question.
        keep = "?" if ("?" in run or run == "‽") else "!"
        hits.append(Hit(m.start(), m.end(), keep,
                        "House style allows no stacked punctuation."))
    return hits


# --- doubled words -----------------------------------------------------------

_DOUBLED = re.compile(r"\b(\w+)[ \t\u00a0]+\1\b", re.IGNORECASE)

# Doublings that are ordinary English or ordinary speech. "had had" is a past
# perfect, "that that" is a real if awkward construction, and the rest are how
# people talk.
_LEGITIMATE_DOUBLES = frozenset({
    "had", "that", "no", "ha", "yes", "very", "so", "really", "well",
    "blah", "bye", "hey", "now", "come", "there", "long",
})


def _sweep_doubled_word(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _DOUBLED.finditer(text):
        word = m.group(1)
        if word.lower() in _LEGITIMATE_DOUBLES:
            continue
        if not any(c.isalpha() for c in word):
            continue
        hits.append(Hit(m.start(), m.end(), word,
                        f"Doubled word: “{word}” appears twice."))
    return hits


# --- centuries ---------------------------------------------------------------

_CENTURY = re.compile(
    r"\b(?P<num>\d{1,2})(?P<ord>st|nd|rd|th)(?P<sep>[ \t\u00a0-])"
    r"(?P<noun>[Cc]entur(?:y|ies))\b")

_ONES = ("", "first", "second", "third", "fourth", "fifth", "sixth",
         "seventh", "eighth", "ninth")
_TEENS = ("tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
          "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth")
_TENS_ORDINAL = {2: "twentieth", 3: "thirtieth", 4: "fortieth", 5: "fiftieth",
                 6: "sixtieth", 7: "seventieth", 8: "eightieth", 9: "ninetieth"}
_TENS_CARDINAL = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
                  6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def ordinal_word(n: int) -> str | None:
    """"20" -> "twentieth". None for anything outside 1–99, where a century
    reference is almost certainly not what the digits mean."""
    if not 1 <= n <= 99:
        return None
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return _TENS_ORDINAL[tens]
    return f"{_TENS_CARDINAL[tens]}-{_ONES[ones]}"


_ONES_CARDINAL = ("zero", "one", "two", "three", "four", "five", "six",
                  "seven", "eight", "nine")
_TEENS_CARDINAL = ("ten", "eleven", "twelve", "thirteen", "fourteen",
                   "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")


def cardinal_word(n: int) -> str | None:
    """"98" -> "ninety-eight". None outside 0–100, the range the house
    spells out."""
    if not 0 <= n <= 100:
        return None
    if n == 100:
        return "one hundred"
    if n < 10:
        return _ONES_CARDINAL[n]
    if n < 20:
        return _TEENS_CARDINAL[n - 10]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return _TENS_CARDINAL[tens]
    return f"{_TENS_CARDINAL[tens]}-{_ONES_CARDINAL[ones]}"


def _sweep_century(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _CENTURY.finditer(text):
        word = ordinal_word(int(m.group("num")))
        if word is None:
            continue
        if _sentence_starts_at(text, m.start()):
            word = word[0].upper() + word[1:]
        hits.append(Hit(m.start(), m.end(),
                        f"{word}{m.group('sep')}{m.group('noun')}",
                        "House style spells out centuries."))
    return hits


# --- compound numbers --------------------------------------------------------

# A spelled-out compound number from twenty-one to ninety-nine written as two
# words. CMOS and the house guide hyphenate these, so "Chapter Twenty Four"
# becomes "Chapter Twenty-Four". Only cardinals: an ordinal ("twenty first")
# reads far more often as two separate words ("twenty first drafts") and is
# left to a judgment read.
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_ONES_WORD = "one|two|three|four|five|six|seven|eight|nine"
_COMPOUND_NUMBER = re.compile(
    rf"\b(?P<tens>{_TENS})[ \t\u00a0]+(?P<ones>{_ONES_WORD})\b", re.IGNORECASE)


def _sweep_compound_number(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _COMPOUND_NUMBER.finditer(text):
        # A following hyphen means the ones word heads a compound modifier —
        # "twenty four-year-olds" is twenty of them, not twenty-four — so the
        # pairing is ambiguous and the sweep leaves it alone.
        if text[m.end():m.end() + 1] == "-":
            continue
        # Case is taken from the text, so "Twenty Four" -> "Twenty-Four" and
        # "twenty four" -> "twenty-four".
        hits.append(Hit(m.start(), m.end(),
                        f"{m.group('tens')}-{m.group('ones')}",
                        "House style hyphenates a compound number from "
                        "twenty-one to ninety-nine."))
    return hits


# --- dialogue tags -----------------------------------------------------------

# The house brief builds this as a table and warns against assembling it from
# ad hoc regexes, because the row that gets missed that way is period +
# capitalized pronoun. So the pattern below finds every candidate and the
# decision is made by building the correct form and comparing — there is no
# per-row regex to forget.
_DIALOGUE_TAG_TEMPLATE = (
    r"(?P<inner>[.,!?…])?"
    r"(?P<quote>[{quotes}])"
    r"(?P<outer>[.,!?])?"
    r"(?P<gap>[ \t\u00a0]+)"
    r"(?P<subject>[A-Za-z][\w'’]*)"
    r"[ \t\u00a0]+"
    r"(?P<verb>[A-Za-z][\w'’]*)"
    r"(?:[ \t\u00a0]+(?P<nxt>[A-Za-z][\w'’]*))?")


@lru_cache(maxsize=4)
def _dialogue_tag_re(quotes: str):
    """The tag pattern for one variant's closing quotation marks.

    U.K. and Australian manuscripts open dialogue with a single quote, so a
    pattern hard-coded to double quotes would run over such a book and report
    zero matches — which reads exactly like a clean manuscript."""
    return re.compile(_DIALOGUE_TAG_TEMPLATE.format(quotes=re.escape(quotes)))

# Deliberately the house brief's list, borderline verbs included.
REPORTING_VERBS = frozenset("""
said asked replied whispered yelled muttered shouted murmured called answered
added continued explained insisted demanded admitted agreed snapped hissed
stammered mumbled repeated warned begged offered observed remarked noted urged
pleaded cried breathed growled laughed sighed drawled countered protested
announced declared responded retorted teased grumbled barked
""".split())

# The pronouns the brief names. "I" is excluded on purpose: it is always
# capitalized, and lowercasing it is never a correction.
_TAG_PRONOUNS = frozenset({"he", "she", "they", "we", "it", "you"})

# A reporting verb taking one of these as its object is not a tag at all — it
# is a new narrative sentence about the speech ("She said it again."). The
# period before it is correct, and changing it would be an error.
_TAKES_OBJECT = frozenset({
    "it", "that", "this", "so", "them", "those", "nothing", "something",
    "anything", "everything", "little", "much", "more", "less", "none",
    "no", "yes", "as", "otherwise", "goodbye", "hello", "grace", "prayers",
})


def _sweep_dialogue_tag(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    quotes = variant.closing_quotes if variant else "\"”"
    for m in _dialogue_tag_re(quotes).finditer(text):
        if m.group("verb").lower() not in REPORTING_VERBS:
            continue
        nxt = m.group("nxt")
        if nxt and nxt.lower() in _TAKES_OBJECT:
            continue
        # A reporting verb immediately followed by a present participle is an
        # ACTION beat, not a tag: "She continued typing", "he kept reading". The
        # period before it is a sentence break and correct — turning it into a
        # comma (and lowercasing the subject) is the exact wrong edit. A real tag
        # never runs verb-straight-into-gerund; the participle would carry a
        # comma ("she said, smiling"), which this pattern does not match.
        if nxt and len(nxt) > 4 and nxt.lower().endswith("ing"):
            continue
        subject = m.group("subject")
        # Pronoun subjects only. A name after the quote needs a judgment the
        # dialogue_tag error type makes; here it would risk lowercasing a name.
        if subject.lower() not in _TAG_PRONOUNS:
            continue

        raw = m.group("inner") or m.group("outer") or ""
        # A period before a tag is a comma; no punctuation at all is a missing
        # comma; a question or exclamation mark is already right and stays.
        punct = "," if raw in (".", "") else raw
        fixed = subject[0].lower() + subject[1:]
        replacement = f"{punct}{m.group('quote')}{m.group('gap')}{fixed}"

        start, end = m.start(), m.end("subject")
        if text[start:end] == replacement:
            continue
        why = []
        if raw == ".":
            why.append("a period before a dialogue tag becomes a comma")
        elif raw == "":
            why.append("a dialogue tag is preceded by a comma")
        if m.group("outer"):
            why.append("punctuation belongs inside the closing quote")
        if fixed != subject:
            why.append("a tag pronoun is lowercase")
        hits.append(Hit(start, end, replacement,
                        "House style: " + ", and ".join(why) + "."))
    return hits


# --- terminal punctuation ----------------------------------------------------

# Sentence-ending marks (and their close-quote/bracket tails) that mean the
# paragraph already ends cleanly, plus the marks whose absence is deliberate:
# an em dash is an interruption, an ellipsis a trailing-off, a colon a label.
_ENDS_CLEAN = tuple(".!?…:—–-*")
_CLOSERS = "”’\"')]"
# A mark is an internal sentence END only when what follows opens like a new
# sentence. An ellipsis (or period) followed by a lowercase word is a pause or
# an abbreviation, not a break — "F*cking forties… what a dumpster fire" is a
# sentence-case TITLE, and its ellipsis granting "prose shape" is exactly how
# the Purpura beta got a period appended to the book's title page.
_INTERNAL_END = re.compile(r"[.!?…][\"”’')\]]?\s+[\"“‘'(\[]?[A-Z0-9]")


def _sweep_terminal_period(text: str, variant=None) -> list[Hit]:
    """A body paragraph of prose that runs off the end without terminal
    punctuation — "…Alex just crossed his arms" — wants a period.

    Every guard here is there to keep the sweep off the things that legitimately
    end without one: chapter and poem titles, glossary labels ("Mortal: a…"),
    dialogue fragments, and interrupted lines (— or …). The sweep only fires on
    something that unmistakably reads as a finished narrative sentence, because a
    period is a silent edit and a period on a title is a wrong one."""
    s = text.rstrip()
    if not s:
        return []
    end = len(s)
    # Step back over any closing quote/bracket so the period lands inside them.
    while end > 0 and s[end - 1] in _CLOSERS:
        end -= 1
    if end == 0:
        return []
    last = s[end - 1]
    if last in _ENDS_CLEAN or not (last.isalpha() or last.isdigit()):
        return []
    words = s.split()
    if len(words) < 5:                              # too short to be sure it is prose
        return []
    if not (s[0].isupper() or s[0] in "\"“‘'"):     # sentences open like sentences
        return []
    if ":" in s:                                    # a label or definition, not prose
        return []
    tail = words[-1]
    if tail.isupper() and len(tail) <= 3:           # an acronym or initial, not a word
        return []
    # Prose, not a title: it either carries an internal sentence break (so it is
    # clearly more than a label) or a closing quote (dialogue), or it is simply
    # long. And a title capitalises most of its words; prose does not.
    has_prose_shape = (bool(_INTERNAL_END.search(s)) or any(q in s for q in "”’\"")
                       or len(words) >= 12)
    if not has_prose_shape:
        return []
    if sum(1 for w in words if w[:1].isupper()) / len(words) > 0.6:
        return []
    return [Hit(end, end, ".",
                "A sentence ends without terminal punctuation.")]


# --- punctuation against a closing quote --------------------------------------

# A period or comma AFTER a curly closing double quote. U.S. convention (and
# the house style) sets both inside; the human pass fixed `thanks".` four
# times where the model glided. Curly marks only: a straight " the normalizer
# left straight is one it could not tell from an opener, and this sweep must
# not out-guess it.
#
# A trailing duplicate closing quote is consumed too (`”.”` and `Go!”.”`): a
# malformed doubled close, common in author manuscripts. Without the optional
# `”?`, the match took only the first `”.` and the replacement re-emitted a
# single `”`, stranding the second one — turning `rosy”.”` into `rosy.””`
# instead of the intended `rosy.”`. A `”` after sentence-terminal punctuation is
# never a legitimate opener (openers are `“`), so absorbing it is safe.
_QUOTE_THEN_PUNCT = re.compile(r"”([.,])”?")


def _sweep_quote_punctuation(text: str, variant=None) -> list[Hit]:
    """Move a period or comma inside the closing double quote — or drop it
    when the quotation already ends with terminal punctuation ("Go!”." keeps
    only the exclamation). Double-primary variants only: U.K. and Australian
    logical placement legitimately sets marks outside."""
    if variant is not None and variant.opens_dialogue_with_single:
        return []
    hits: list[Hit] = []
    for m in _QUOTE_THEN_PUNCT.finditer(text):
        before = text[m.start() - 1] if m.start() > 0 else ""
        if not before or before.isdigit() or before.isspace():
            continue      # an inch mark after a digit, or a mark adrift
        if before == ",":
            continue      # ,". is malformed both ways round: a judgment call
        punct = m.group(1)
        if before in ".!?…":
            replacement = "”"
            why = ("House style sets periods and commas inside a closing "
                   "quotation mark; this quotation already ends with its own "
                   "punctuation, so the mark after the quote is dropped.")
        else:
            replacement = punct + "”"
            why = ("House style sets periods and commas inside a closing "
                   "quotation mark.")
        hits.append(Hit(m.start(), m.end(), replacement, why))
    hits += _single_close_punct_hits(text)
    return hits


def _single_close_punct_hits(text: str) -> list[Hit]:
    """The same rule at a closing SINGLE quote — ‘…alongside me’. — with the
    extra burden that ’ is also the apostrophe. Only a ’ that CLOSES an open ‘
    is a quotation mark, so the scan walks the paragraph tracking unmatched ‘
    openers: a terminal possessive ("the boys’.") has no ‘ open and never
    matches. An intra-word ’ (don’t) neither closes nor opens anything, and an
    elision opener (’tis) can at worst swallow one closer — a missed fix, never
    a wrong one."""
    hits: list[Hit] = []
    open_count = 0
    for i, ch in enumerate(text):
        if ch == "‘":
            open_count += 1
            continue
        if ch != "’":
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if prev.isalpha() and nxt.isalpha():
            continue                     # apostrophe inside a word
        if not open_count:
            continue                     # possessive or stray: not a closer
        open_count -= 1
        if nxt not in ".,":
            continue
        if not prev or prev.isdigit() or prev.isspace() or prev == ",":
            continue                     # a minutes mark, or malformed anyway
        if prev in ".!?…":
            replacement, why = "’", (
                "House style sets periods and commas inside a closing "
                "quotation mark; this quotation already ends with its own "
                "punctuation, so the mark after the quote is dropped.")
        else:
            replacement, why = nxt + "’", (
                "House style sets periods and commas inside a closing "
                "quotation mark.")
        hits.append(Hit(i, i + 2, replacement, why))
    return hits


# --- nested quotations ---------------------------------------------------------

_NESTED_WHY = ("A quotation inside dialogue takes single quotation marks "
               "(U.S. convention).")


def _sweep_nested_quote(text: str, variant=None) -> list[Hit]:
    """Demote a double-quoted phrase INSIDE double-quoted dialogue to single
    quotes — “the ‘Wave of Autonomy’” — the systematic conversion the human
    pass made ~17 times and the model pass never did.

    Only where the ground is completely firm: double-primary variants, no
    straight quotes left in the paragraph (the normalizer could not place
    those), doubles perfectly balanced (the unclosed-quote check owns
    imbalance), and only one level down — a quote nested inside a nested
    quote alternates back to double and is rare enough to leave to a reader.
    Anything else returns no hits at all: a wrongly-demoted quote pair is two
    edits an author must notice and reject together."""
    if variant is not None and variant.opens_dialogue_with_single:
        return []
    if '"' in text or text.count("“") != text.count("”"):
        return []
    hits: list[Hit] = []
    stack: list[int] = []                    # positions of unmatched “
    for i, ch in enumerate(text):
        if ch == "“":
            stack.append(i)
        elif ch == "”":
            if not stack:                    # malformed: counts lied
                return []
            start = stack.pop()
            if len(stack) == 1:              # this pair sat inside exactly one
                hits.append(Hit(start, start + 1, "‘", _NESTED_WHY))
                hits.append(Hit(i, i + 1, "’", _NESTED_WHY))
    return hits


# --- headings in title case ----------------------------------------------------

# The words Chicago sets lowercase mid-title: articles, coordinating
# conjunctions, and prepositions. Prepositions of every length, per Chicago —
# with the known cost that a particle used adverbially ("Waking Up Slow")
# cannot be told from a preposition by a script and will be lowercased; the
# edit is a tracked change the author can reject, and the first/last-word rule
# protects the commonest cases ("Coming Up").
_TITLE_MINOR = frozenset("""
a an the and but or nor for so yet as at by down from in into like near of
off on onto out over past per to up upon via with within without about above
across after against along among around before behind below beneath beside
between beyond during inside outside through toward towards under until
""".split())

# Small roman numerals as a closed list rather than a pattern: a pattern
# admits real words ("mix" is a valid numeral), and "Part ii" deserves
# "Part II" while "did" must never become "DID".
_ROMAN = frozenset("""
ii iii iv vi vii viii ix xi xii xiii xiv xv xvi xvii xviii xix xx
""".split())

_TITLE_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
# A word after one of these opens a subtitle or a new line, and takes a
# capital the way a first word does.
_TITLE_OPENERS = ":—–\n"

_TITLE_WHY = "House style sets headings in title case."


def title_case_hits(text: str) -> list[Hit]:
    """The word-level edits that set one heading in Chicago title case.

    Word-level on purpose: one finding per word keeps every diff a character
    or two, each rejectable on its own, instead of one wholesale retype of the
    line. Conservative where styling could be deliberate: a heading with no
    lowercase letters at all (CHAPTER ONE) is left whole, and any word already
    carrying a capital anywhere (McCoy, EVTOL, iPhone, I) is left alone — the
    only words touched are entirely-lowercase ones and a stray capitalized
    minor word ("The Shape Of Things" loses only the Of)."""
    if not any(c.islower() for c in text):
        return []                     # all-caps or letterless: a styling choice
    words = list(_TITLE_WORD.finditer(text))
    if not words:
        return []
    last_of_line: set[int] = set()
    for i, m in enumerate(words):
        gap = text[m.end():words[i + 1].start()] if i + 1 < len(words) else ""
        if i + 1 == len(words) or "\n" in gap:
            last_of_line.add(i)
    hits: list[Hit] = []
    for i, m in enumerate(words):
        w = m.group(0)
        lead = text[words[i - 1].end():m.start()] if i else text[:m.start()]
        first = i == 0 or any(ch in lead for ch in _TITLE_OPENERS)
        edge = first or i in last_of_line
        lower = w.lower()
        if lower in _ROMAN:
            want = lower.upper()      # Part ii -> Part II, wherever it stands
        elif not edge and lower in _TITLE_MINOR:
            if w != lower.capitalize():
                continue              # "OF" is styling; only "Of" is drift
            want = lower
        elif w.islower():
            want = w[0].upper() + w[1:]
        else:
            continue                  # carries its own casing: leave it
        if want != w:
            hits.append(Hit(m.start(), m.end(), want, _TITLE_WHY))
    return hits


def heading_case_findings(paragraphs: Sequence[ParagraphRef],
                          skip) -> tuple[list[Finding], SweepReport]:
    """Title-case every heading-styled paragraph, as ordinary sweep findings
    plus the flagged/remaining counts the change log quotes.

    "Heading-styled" is the press's own definition — the styles the skip
    config lists as sweep-only — so the paragraphs this touches are exactly
    the ones no model pass reviews. That gap is what left "the shape of
    things to come" untouched through an entire run (DP-006); folding the
    headings' names into the scripted pass closes it without ever showing a
    heading to a model."""
    from .headings import is_structural_heading
    findings: list[Finding] = []
    flagged = remaining = 0
    n = 0
    for para in paragraphs:
        # The SHARED structural predicate, not the style name alone: a long body
        # paragraph mis-styled as a heading must not be title-cased (a wrong
        # edit to real prose). Same definition the chapter segmentation uses.
        if not is_structural_heading(para, skip.is_sweep_only):
            continue
        hits = title_case_hits(para.text)
        if not hits:
            continue
        flagged += len(hits)
        for hit in hits:
            window, lo, occurrence = sentence_window(para.text, hit.start,
                                                     hit.end)
            corrected = (window[:hit.start - lo] + hit.replacement
                         + window[hit.end - lo:])
            n += 1
            findings.append(Finding(
                finding_id=f"hc-{n:04d}",
                chunk_id="sweep",
                para_id=para.para_id,
                error_type="heading_case",
                original_text=window,
                occurrence=occurrence,
                corrected_text=corrected,
                explanation=hit.explanation,
                confidence="high"))
        remaining += len(title_case_hits(apply_hits(para.text, hits)))
    report = SweepReport("heading_case", "Headings set in title case",
                         flagged, remaining)
    if flagged:
        log.info("heading_case: %d word(s) recased across headings, "
                 "%d remaining", flagged, remaining)
    return findings, report


# --- unclosed quotations (a question, never an edit) ---------------------------

def unclosed_quote_findings(paragraphs: Sequence[ParagraphRef],
                            variant=None) -> list[Finding]:
    """A paragraph whose double quotes do not balance, raised as a margin
    query. Not a Sweep: the one legitimate imbalance — speech running on
    across paragraphs — can only be recognised by looking at the NEXT
    paragraph, which a per-paragraph pattern cannot do.

    The convention: a multi-paragraph speech omits the closing quote on every
    paragraph but its last, and each continuation paragraph RE-OPENS with a
    quotation mark. So an unclosed paragraph whose successor opens with one is
    the convention at work; an unclosed paragraph whose successor does not is
    where a closing quote has gone missing ("…without blinking. Steve says,
    agitated." — the human's catch, fully absent from the model pass).

    Curly marks only, double-primary variants only, and any paragraph still
    carrying a straight " is left alone: the normalizer could not settle those
    marks, so no count of them is evidence. Queries change nothing, so a
    false positive here costs a margin note, never an edit."""
    if variant is not None and variant.opens_dialogue_with_single:
        return []
    findings: list[Finding] = []
    n = 0
    for i, para in enumerate(paragraphs):
        text = para.text
        opens, closes = text.count("“"), text.count("”")
        if opens == closes or '"' in text:
            continue
        if opens > closes:
            nxt = next((p for p in paragraphs[i + 1:]
                        if p.part == para.part and p.location == para.location),
                       None)
            if nxt is not None and nxt.text.lstrip()[:1] == "“":
                continue          # speech carried into the next paragraph
            at = text.rfind("“")
            why = ("A quotation opens here and never closes, and the next "
                   "paragraph does not reopen with a quotation mark — a "
                   "closing quote may be missing.")
        else:
            at = text.find("”")
            why = ("A closing quotation mark here has no opening partner in "
                   "this paragraph — a quote may be missing or stray.")
        window, _lo, occurrence = sentence_window(text, at, at + 1)
        n += 1
        findings.append(Finding(
            finding_id=f"uq-{n:04d}",
            chunk_id="sweep",
            para_id=para.para_id,
            error_type="unclosed_quote",
            original_text=window,
            occurrence=occurrence,
            corrected_text=window,
            explanation=why,
            confidence="medium",
            force_query=True))
    if findings:
        log.info("Unbalanced quotation marks: %d paragraph(s) queried.",
                 len(findings))
    return findings


# --- time of day --------------------------------------------------------------

# A clock time with an AM/PM meridiem attached to a digit: "3:40AM", "2:00 AM",
# "4:15PM", "at 2 PM". The digit requirement is what keeps the sweep off the
# stray capital pair — "I AM here", an "AM" radio band — that is not a time at
# all. Already-correct "3:40 a.m." matches too, but the replacement equals the
# text, so no hit is emitted and the sweep stays idempotent.
_TIME_MERIDIEM = re.compile(
    r"(?<![.\d])(\d{1,2}(?::\d{2})?)[  ]*([AaPp])[  ]*\.?[  ]*([Mm])\.?")


def _sweep_time_of_day(text: str, variant=None) -> list[Hit]:
    """House/Chicago style sets a meridiem lowercase with periods and a space:
    "3:40 a.m.", not "3:40AM". Only the meridiem is touched — whether the hour
    itself should be spelled out or take ":00" is a number-style judgment the
    number rules own, so a bare "2 p.m." keeps its digit here."""
    hits: list[Hit] = []
    for m in _TIME_MERIDIEM.finditer(text):
        canonical = f"{m.group(1)} {m.group(2).lower()}.{m.group(3).lower()}."
        if m.group(0) == canonical:
            continue
        hits.append(Hit(m.start(), m.end(), canonical,
                        "House style sets a time's meridiem lowercase with "
                        "periods: “a.m.”/“p.m.”"))
    return hits


# --- the deity capital ---------------------------------------------------------

# "god" (lowercase g) as the monotheistic deity in a fixed interjection —
# "oh my god", "thank god". The leading word is what makes the reference the
# proper name rather than a common noun, so the sweep never has to decide
# "a god"/"the gods"/"godforsaken" on its own: those never carry one of these
# openers, and the \b after the word keeps "goddess"/"godsend" out. Only a
# lowercase "god" is a hit, so re-scanning the capitalised result finds nothing.
_DEITY_FRAME = re.compile(
    r"\b(oh my|my|oh|thank|dear|good|god)\s+god\b", re.IGNORECASE)


def _sweep_deity_capital(text: str, variant=None) -> list[Hit]:
    """Capitalise “God” in the set interjections where it is the deity's name.
    Kept to fixed frames on purpose — the general "is this god the deity"
    question is a judgment, and a wrongly-capitalised god in a polytheistic
    passage is a silent edit no one asked for."""
    hits: list[Hit] = []
    for m in _DEITY_FRAME.finditer(text):
        # The "god god" arm of the alternation ("...god God...") is not a frame
        # we fix; the real openers are the others. Locate the final "god".
        gpos = m.end() - 3
        if text[gpos:gpos + 3] != "god":            # already "God"
            continue
        # "god of war/thunder" is a common noun, not the deity's name.
        if text[m.end():m.end() + 4].lower() == " of ":
            continue
        # An article before the frame marks a common-noun "god" ("a good god",
        # "the god"), never the interjection.
        before = text[max(0, m.start() - 5):m.start()].lower()
        if re.search(r"\b(a|an|the|one|another|every|no|that|this)\s*$", before):
            continue
        # "god damn"/"goddamn" is a house-consistency question, not a cap fix.
        if text[m.end():m.end() + 5].lower().startswith(" damn"):
            continue
        hits.append(Hit(gpos, gpos + 1, "G",
                        "“God” takes a capital as the name of the deity in "
                        "this expression."))
    return hits


# --- dialogue splice: tag after a complete quoted sentence --------------------

# A reporting verb (optionally with one adverb) that sits between a
# sentence-final quote and the NEXT opening quote, joined to it by a comma:
#   "Of course!" Raymond said, "Anything for you."
# The first quote ended the sentence (! or ?), so the tag closes it with a
# period, and the second quote opens a new one — the comma is a splice.
_SPLICE_TAG = re.compile(
    r"(?P<end>[!?][”\"’'])[  ]+"
    r"(?P<subject>[A-Z][\w'’]*|he|she|they|we|it|you)"
    r"(?:[  ]+(?P<verb>[A-Za-z][\w'’]*))"
    r"(?:[  ]+(?P<adverb>[A-Za-z]+ly))?"
    r"(?P<comma>,)[  ]+(?=[“\"‘])")

# Physical-action verbs that CANNOT take speech as their object, so a comma
# before them inside a quote is a run-on, not a tag: the quote ends with a
# period and the beat is its own sentence. Deliberately disjoint from
# REPORTING_VERBS — a borderline verb the house counts as reporting (laughed,
# sighed, breathed, cried) is left to the dialogue_tag error type to weigh.
_ACTION_BEATS = frozenset("""
smiled nodded grinned shrugged frowned chuckled winced blinked shuddered
swallowed beamed gestured scowled smirked snorted flinched
""".split())

# A closing quote whose own comma sits just inside it, then a subject and a
# physical-action verb that ENDS the beat (terminal punctuation or paragraph
# end follows). "…at this point," Raymond smiled.  ->  …point." Raymond smiled.
_ACTION_BEAT = re.compile(
    r"(?P<comma>,)(?P<quote>[”\"’'])[  ]+"
    r"(?P<subject>(?:[A-Z][\w'’]*|he|she|they|we|it|you))"
    r"[  ]+(?P<verb>[A-Za-z]+)(?P<after>[  ]*[.!?]|\s*$)")


def _sweep_dialogue_splice(text: str, variant=None) -> list[Hit]:
    """Two comma-for-period splices around a dialogue tag that the per-sentence
    passes glide over because each half reads locally fine:

      * a tag after a ! or ?-ended quote, joined to the next quote by a comma
        ("Of course!" Raymond said, "Anything.") — the tag takes a period;
      * a physical-action beat mistaken for a tag ("…," Raymond smiled.) — the
        quote takes a period and the beat is its own sentence.

    Both are idempotent: the fixed comma is gone, so a re-scan matches nothing.
    """
    hits: list[Hit] = []
    for m in _SPLICE_TAG.finditer(text):
        if m.group("verb").lower() not in REPORTING_VERBS:
            continue
        c = m.start("comma")
        hits.append(Hit(c, c + 1, ".",
                        "House style: a complete quoted sentence ends the tag "
                        "with a period before the next quotation."))
    for m in _ACTION_BEAT.finditer(text):
        if m.group("verb").lower() not in _ACTION_BEATS:
            continue
        subject = m.group("subject")
        fixed = subject[0].upper() + subject[1:]
        # comma -> period (inside the quote), and the beat's subject capitalised.
        replacement = ("." + m.group("quote") + text[m.end("quote"):m.start("subject")]
                       + fixed)
        start, end = m.start("comma"), m.end("subject")
        if text[start:end] == replacement:
            continue
        hits.append(Hit(start, end, replacement,
                        "House style: an action beat is its own sentence, so "
                        "the quotation closes with a period."))
    return hits


# --- trailing whitespace --------------------------------------------------------

# Whitespace at the very end of a paragraph. Normalization strips these
# silently at ingest, so post-normalize the only producer is the speaker-split
# pass (the boundary space it leaves on each earlier fragment, on purpose — a
# pre-snapshot pass must not make tracked changes of its own); this sweep
# deletes them through the ordinary audited channel. It also stands alone as a
# backstop on runs with normalize.spaces off.
_TRAILING_WS = re.compile("[ \u00a0]+$")


def _sweep_trailing_space(text: str, variant=None) -> list[Hit]:
    """Delete paragraph-trailing whitespace. Renders as nothing, and if the
    author later merges two paragraphs it becomes a mid-paragraph double."""
    if not any(c.isalnum() for c in text):
        return []                     # scene dividers space as they please
    m = _TRAILING_WS.search(text)
    if not m:
        return []
    return [Hit(m.start(), m.end(), "",
                "Trailing spaces at the end of a paragraph are removed.")]


# --- initialisms set in capitals ----------------------------------------------

# A short whitelist, not a heuristic: each entry is an initialism with no
# common-word homograph, safe to capitalize wherever it appears as its own
# word ("live tv", "the tv show", "tv’s"). Candidates with a homograph (OK,
# id, us, am, it) can never join this list — they need context a regex does
# not have. Keys lowercase; matching is case-insensitive so "Tv" corrects too.
_INITIALISMS = {
    "tv": "TV",
}
_INITIALISM_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _INITIALISMS) + r")\b",
    re.IGNORECASE)


def _sweep_initialism(text: str, variant=None) -> list[Hit]:
    """A whitelisted initialism set in lowercase — "live tv" — capitalized.
    The human pass fixed tv→TV where the model pass glided; domains and
    filenames ("channel4.tv", "tv.com") are left alone."""
    hits: list[Hit] = []
    for m in _INITIALISM_RE.finditer(text):
        fixed = _INITIALISMS[m.group(0).lower()]
        if m.group(0) == fixed:
            continue
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        # A dot joined straight to either side reads as a domain or file part.
        if before == "." or (after == "." and m.end() + 1 < len(text)
                             and text[m.end() + 1].isalnum()):
            continue
        hits.append(Hit(m.start(), m.end(), fixed,
                        f"House style sets the initialism {fixed} in "
                        f"capitals."))
    return hits


# --- registry ----------------------------------------------------------------

SWEEPS: tuple[Sweep, ...] = (
    Sweep("sweep_ellipsis", "Ellipsis character and spacing", _sweep_ellipsis),
    Sweep("sweep_dash", "Dashes", _sweep_dash),
    Sweep("sweep_stacked_punctuation", "Stacked punctuation",
          _sweep_stacked_punctuation),
    Sweep("sweep_doubled_word", "Doubled words", _sweep_doubled_word),
    Sweep("sweep_century", "Centuries spelled out", _sweep_century),
    Sweep("sweep_compound_number", "Compound numbers hyphenated",
          _sweep_compound_number),
    Sweep("sweep_dialogue_tag", "Dialogue tag punctuation and case",
          _sweep_dialogue_tag),
    Sweep("sweep_terminal_period", "Missing sentence-final period",
          _sweep_terminal_period),
    Sweep("sweep_quote_punctuation", "Punctuation inside closing quotes",
          _sweep_quote_punctuation),
    Sweep("sweep_nested_quote", "Nested quotations set in singles",
          _sweep_nested_quote),
    Sweep("sweep_time_of_day", "Times of day (a.m./p.m.)", _sweep_time_of_day),
    Sweep("sweep_deity_capital", "Deity capitalized in set expressions",
          _sweep_deity_capital),
    Sweep("sweep_dialogue_splice", "Comma splices around a dialogue tag",
          _sweep_dialogue_splice),
    Sweep("sweep_initialism", "Initialisms set in capitals (TV)",
          _sweep_initialism),
    Sweep("sweep_trailing_space", "Paragraph-trailing whitespace",
          _sweep_trailing_space),
)

SWEEPS_BY_KEY = {s.key: s for s in SWEEPS}


def resolve(keys: Sequence[str]) -> list[Sweep]:
    unknown = [k for k in keys if k not in SWEEPS_BY_KEY]
    if unknown:
        raise ValueError(
            f"Unknown sweep(s): {', '.join(unknown)}. "
            f"Available: {', '.join(SWEEPS_BY_KEY)}")
    return [SWEEPS_BY_KEY[k] for k in keys]


def run_sweeps(paragraphs: Sequence[ParagraphRef], keys: Sequence[str],
               variant=None, *, ellipsis_style: str = "nbsp"
               ) -> tuple[list[Finding], list[SweepReport]]:
    """Every enabled sweep over every paragraph, as findings plus the counts
    the change log has to quote.

    Findings are numbered in their own `s-` series so they can never collide
    with the model's `f-` series, whatever order the two arrive in."""
    return run_sweep_objects(paragraphs, resolve(keys), variant,
                             ellipsis_style=ellipsis_style)


def run_sweep_objects(paragraphs: Sequence[ParagraphRef],
                      sweeps: Sequence[Sweep], variant=None, *,
                      ellipsis_style: str = "nbsp", start: int = 0
                      ) -> tuple[list[Finding], list[SweepReport]]:
    """Run a set of `Sweep` objects, not registry keys — the seam a bespoke,
    agent-authored sweep enters through (`docproof sweep --rule`). Identical to
    what `run_sweeps` does for the built-ins: same `s-` finding series, same
    Hit -> sentence-window Finding, same idempotency re-scan. `run_sweeps` is
    now this over `resolve(keys)`, so the built-in path is byte-for-byte
    unchanged."""
    findings: list[Finding] = []
    reports: list[SweepReport] = []
    n = start
    for sweep in sweeps:
        # The ellipsis sweep is the one whose right answer is a house choice
        # (config/default.yaml → style.ellipsis); every other sweep reads only
        # text and variant, so it is bound here and the rest are called as-is.
        scan = (partial(sweep.scan, style=ellipsis_style)
                if sweep.key == "sweep_ellipsis" else sweep.scan)
        flagged = remaining = 0
        for para in paragraphs:
            hits = scan(para.text, variant)
            if not hits:
                continue
            flagged += len(hits)
            for hit in hits:
                window, lo, occurrence = sentence_window(
                    para.text, hit.start, hit.end)
                corrected = (window[:hit.start - lo] + hit.replacement
                             + window[hit.end - lo:])
                n += 1
                findings.append(Finding(
                    finding_id=f"s-{n:04d}",
                    chunk_id="sweep",
                    para_id=para.para_id,
                    error_type=sweep.key,
                    original_text=window,
                    occurrence=occurrence,
                    corrected_text=corrected,
                    explanation=hit.explanation,
                    confidence="high",
                ))
            remaining += len(scan(apply_hits(para.text, hits), variant))
        reports.append(SweepReport(sweep.key, sweep.name, flagged, remaining))
        if remaining:
            log.error("%s: %d match(es) still present after its own fixes — "
                      "the sweep is not idempotent and its rule is not fully "
                      "executed.", sweep.key, remaining)
        else:
            log.info("%s: %d flagged, 0 remaining", sweep.key, flagged)
    return findings, reports
