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
from functools import lru_cache
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
    """Whether `pos` begins a sentence, so a spelled-out word takes a capital."""
    i = pos - 1
    while i >= 0 and text[i] in _SP + "\"“”'‘’":
        i -= 1
    return i < 0 or text[i] in ".!?…"


# --- ellipsis ----------------------------------------------------------------

# Three or more dots, however they are spaced, or an ellipsis character that
# may already be there. Both are correction targets: the house rule is about
# the spacing as much as the glyph.
_ELLIPSIS = re.compile(r"\.(?:[ \t\u00a0]*\.){2,}|…")


def _sweep_ellipsis(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _ELLIPSIS.finditer(text):
        lo, hi = m.start(), m.end()
        while lo > 0 and text[lo - 1] in _SP:
            lo -= 1
        while hi < len(text) and text[hi] in _SP:
            hi += 1
        before = text[lo - 1] if lo > 0 else ""
        after = text[hi] if hi < len(text) else ""
        # A non-breaking space before, so the ellipsis never wraps away from
        # the word it trails; a plain space after, only when a word follows.
        lead = "\u00a0" if before and (before.isalnum() or before in ",;:!?" +
                                       _CLOSING_QUOTES) else ""
        trail = " " if after.isalnum() else ""
        replacement = f"{lead}…{trail}"
        if text[lo:hi] == replacement:
            continue
        hits.append(Hit(lo, hi, replacement,
                        "House style sets an ellipsis as … with a "
                        "non-breaking space before it."))
    return hits


# --- dashes ------------------------------------------------------------------

_DASHES = re.compile(r"(?P<pre>[ \t\u00a0]*)(?P<run>-{2,}|–)(?P<post>[ \t\u00a0]*)")


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
        if run == "–":
            # An en dash tight between numbers is a correct range. Spaced, it
            # is being used as a sentence break, which house style sets as an
            # unspaced em dash.
            if not (m.group("pre") and m.group("post")):
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


# --- a / an ------------------------------------------------------------------

# Which article a word takes is decided by the sound it starts with, not the
# letter, and the two disagree in exactly two directions. Both lists are short,
# closed and famous, which is what makes this a sweep rather than a question
# for a model: there is nothing here to weigh up.

# Vowel letter, consonant sound → takes "a". Almost all of them are the "yoo"
# words plus the "wun" words.
_CONSONANT_SOUND = frozenset("""
one once oneness
unicorn uniform unify unifying unilateral union unionize unique unisex unison
unit unite united unity universal universe university uranium urea urethane
urinal urine usable usage use used useful useless user usual usually usurer
usurious utensil utilitarian utility utilize utopia utopian
eucalyptus eugenics eulogy euphemism euphemistic euphoria euro european
eureka ewe ewer ouija
""".split())

# Consonant letter, vowel sound → takes "an". In American English the h is
# silent in these and only these.
_VOWEL_SOUND = frozenset("""
hour hourly hours honest honestly honesty honor honors honorable honorably
honorary honoured honour honours honourable heir heirs heiress heirloom
herb herbs herbal
""".split())

_ARTICLE = re.compile(r"\b(?P<art>[Aa]n?)(?P<gap>\s+)(?P<word>[A-Za-z][A-Za-z'’-]*)")


def _article_for(word: str) -> str | None:
    """"a" or "an" for this word, or None when the sweep should not guess.

    Silence is the important half. A word this cannot be certain about — an
    acronym, an initial, anything capitalized mid-sentence — is left entirely
    alone, because "an" before a letter read aloud ("an F, an X-ray") and "a"
    before one read as a word ("a UFO, a NASA engineer") depend on knowing how
    the reader says it, and no rule in the text can tell."""
    if word.upper() == word and len(word) > 1:
        return None                          # NASA, UFO, MRI — unknowable here
    # A compound is said the way its first part is said, and only that part
    # decides the article: "a one-time champion", "an hour-long wait". Reading
    # the whole token would make "one-time" look like a vowel.
    word = re.split(r"[-’']", word)[0]
    if len(word) < 2:
        return None                          # "a T-shirt" vs "an X-ray"
    low = word.lower()
    if low in _CONSONANT_SOUND:
        return "a"
    if low in _VOWEL_SOUND:
        return "an"
    if not word[0].isalpha():
        return None
    if word[0].isupper():
        return None                          # a proper noun may be said any way
    # "an historic" is a live and defensible usage, so the h-words that are not
    # on the silent list are left alone rather than argued with.
    if low[0] == "h":
        return None
    return "an" if low[0] in "aeiou" else "a"


def _sweep_article(text: str, variant=None) -> list[Hit]:
    hits: list[Hit] = []
    for m in _ARTICLE.finditer(text):
        want = _article_for(m.group("word"))
        if want is None or want == m.group("art").lower():
            continue
        # The author's capital is the author's: "A hour" at the start of a
        # sentence becomes "An hour", not "an hour".
        if m.group("art")[0].isupper():
            want = want[0].upper() + want[1:]
        hits.append(Hit(m.start("art"), m.end("art"), want,
                        f"“{m.group('word')}” begins with a "
                        f"{'vowel' if want.lower() == 'an' else 'consonant'} "
                        f"sound, so the article is “{want.lower()}”."))
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


# --- registry ----------------------------------------------------------------

SWEEPS: tuple[Sweep, ...] = (
    Sweep("sweep_ellipsis", "Ellipsis character and spacing", _sweep_ellipsis),
    Sweep("sweep_dash", "Dashes", _sweep_dash),
    Sweep("sweep_stacked_punctuation", "Stacked punctuation",
          _sweep_stacked_punctuation),
    Sweep("sweep_doubled_word", "Doubled words", _sweep_doubled_word),
    Sweep("sweep_century", "Centuries spelled out", _sweep_century),
    Sweep("sweep_dialogue_tag", "Dialogue tag punctuation and case",
          _sweep_dialogue_tag),
    Sweep("sweep_article", "a / an before the wrong sound", _sweep_article),
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
               variant=None) -> tuple[list[Finding], list[SweepReport]]:
    """Every enabled sweep over every paragraph, as findings plus the counts
    the change log has to quote.

    Findings are numbered in their own `s-` series so they can never collide
    with the model's `f-` series, whatever order the two arrive in."""
    findings: list[Finding] = []
    reports: list[SweepReport] = []
    n = 0
    for sweep in resolve(keys):
        flagged = remaining = 0
        for para in paragraphs:
            hits = sweep.scan(para.text, variant)
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
            remaining += len(sweep.scan(apply_hits(para.text, hits),
                                              variant))
        reports.append(SweepReport(sweep.key, sweep.name, flagged, remaining))
        if remaining:
            log.error("%s: %d match(es) still present after its own fixes — "
                      "the sweep is not idempotent and its rule is not fully "
                      "executed.", sweep.key, remaining)
        else:
            log.info("%s: %d flagged, 0 remaining", sweep.key, flagged)
    return findings, reports
