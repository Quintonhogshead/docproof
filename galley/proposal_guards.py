"""Refusals for proposals that are legal text but the wrong edit.

Every guard here answers one question about one proposal and reads nothing it
is not handed — no run state, no disk — so each is testable on its own and cheap enough to
run at the single chokepoint every applied edit passes
(:meth:`galley.fixed_workflow.FixedWorkflow._apply`).

The refusals are the Cooper QA's bad edits (2026-09-17): a time already written
in 24-hour form converted to a meridiem, a plural pronoun narrowed to a
singular one with only a plural antecedent in view, a comma pushed against an
ellipsis, and a deliberately flat line of speech turned into a question. The
Immanuel QA (2026-09-22) added one that reads a book-level input, passed in
explicitly: a name's possessive converted against the author's own form. A
guard refuses only the clear case. A proposal it cannot judge belongs to the
readers, not to Python: when the evidence points both ways these functions
return None and the edit stands.
"""
from __future__ import annotations

import re

# --- 24-hour clock times -----------------------------------------------------

# A 24-hour clock token: an hour of 13–23, or any hour carrying the leading
# zero a 12-hour clock never writes (00:05, 08:21). “12:05” and “3:00” are
# ambiguous by shape and are none of this guard's business.
_H24 = re.compile(r"(?<![\d:.])(?:1[3-9]|2[0-3]|0\d):[0-5]\d(?![\d:])")
# 0830, 1430 — military time only when a time word stands next to it, because
# the same four digits are also a year.
_MILITARY = re.compile(r"(?<![\d:.])(?:[01]\d|2[0-3])[0-5]\d(?![\d:])")
_TIME_WORD = re.compile(r"\b(?:hours?|hrs?|local|UTC|GMT|Zulu|zone|o.clock)\b", re.I)
_MERIDIEM = re.compile(r"\b[ap]\.?\s?m\b\.?", re.I)
_ADJACENT_MERIDIEM = re.compile(r"[\s ]*[ap]\.?\s?m\b\.?", re.I)
_CLOCK = re.compile(r"(?<!\d)\d{1,2}[:.]\d{2}(?!\d)")
_NUMBER_WORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
                r"twenty|thirty|forty|fifty|hundred)")
# Spoken 24-hour time: “oh-eight-thirty”, “oh eight hundred”, “thirteen hundred”.
_SPELLED_24H = re.compile(
    rf"\boh[-\s]{_NUMBER_WORD}(?:[-\s]{_NUMBER_WORD})?\b|"
    rf"\b(?:thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    rf"twenty[-\s](?:one|two|three))[-\s]hundred\b", re.I)

_TIME_REASON = "a time already written in 24-hour form stays 24-hour"


def time_24h_conversion(before: str, replacement: str) -> str | None:
    """Refuse converting a 24-hour time to a 12-hour one (policy, 2026-09-17).

    This is about the conversion, never the formatting: “3PM” → “3:00 p.m.”
    fixes a 12-hour time and passes, while “18:03” → “6:03 p.m.” and “00:05
    UTC” → “12:05 a.m. UTC” change what the clock says. A leading-zero token
    that already carries a meridiem (“08:21 PM”) is a 12-hour time written
    long, so it is left to the ordinary clock rule.
    """
    converted = _CLOCK.search(replacement) or _MERIDIEM.search(replacement)
    for match in _H24.finditer(before):
        token = match.group()
        if _ADJACENT_MERIDIEM.match(before[match.end():]):
            continue
        at = replacement.find(token)
        if at < 0:
            if converted:
                return _TIME_REASON
        elif _ADJACENT_MERIDIEM.match(replacement[at + len(token):]):
            return _TIME_REASON
    if _TIME_WORD.search(before):
        for match in _MILITARY.finditer(before):
            token = match.group()
            if token not in replacement and converted:
                return _TIME_REASON
    if (_SPELLED_24H.search(before) and converted
            and not _SPELLED_24H.search(replacement)):
        return _TIME_REASON
    return None


# --- commas against an ellipsis ----------------------------------------------

# House style sets the ellipsis with a space (a non-breaking one) on either
# side; the typed three-dot form still occurs in unfixed manuscripts.
_COMMA_ELLIPSIS = re.compile(r",[   ]*(?:…|\.\.\.)|(?:…|\.\.\.)[   ]*,")


def comma_adjacent_ellipsis(before: str, replacement: str) -> str | None:
    """Refuse a comma newly set against an ellipsis (“Sofia, … someone else”).

    Counted rather than matched, so respacing an existing “,…” into “, …” is
    still allowed: only a comma the proposal ADDS beside the mark is refused.
    """
    if len(_COMMA_ELLIPSIS.findall(replacement)) > len(_COMMA_ELLIPSIS.findall(before)):
        return "a comma is never set against an ellipsis"
    return None


# --- pronoun number ----------------------------------------------------------

_PLURAL_TO_SINGULAR: dict[str, frozenset[str]] = {
    "they": frozenset({"he", "she", "it"}),
    "them": frozenset({"him", "her", "it"}),
    "their": frozenset({"his", "her", "its"}),
    "theirs": frozenset({"his", "hers", "its"}),
    "themselves": frozenset({"himself", "herself", "itself"}),
    "we": frozenset({"i"}), "us": frozenset({"me"}),
    "our": frozenset({"my"}), "ours": frozenset({"mine"}),
    "ourselves": frozenset({"myself"}),
}
_SINGULAR_TO_PLURAL: dict[str, set[str]] = {}
for _plural, _singulars in _PLURAL_TO_SINGULAR.items():
    for _singular in _singulars:
        _SINGULAR_TO_PLURAL.setdefault(_singular, set()).add(_plural)

# A plural antecedent the paragraph itself supplies: two names joined by “and”,
# a named pair, or a plural pronoun already standing for them.
_PLURAL_JOIN = re.compile(r"\b[A-Z][\w’'\-]+ and (?:the )?[A-Z][\w’'\-]+\b")
_PAIR_PHRASE = re.compile(r"\b(?:both|the two|the pair|the couple|"
                          r"(?:an?|the) \w+ and (?:an?|the) \w+)\b", re.I)
_PLURAL_PRONOUN = re.compile(r"\b(?:they|them|their|theirs|themselves|we|us|our|ours)\b", re.I)
_SINGULAR_PRONOUN = re.compile(r"\b(?:he|him|his|she|her|hers|himself|herself)\b", re.I)
_NAME = re.compile(r"\b[A-Z][a-z’'\-]+\b")
_TRIM = " \t“”\"‘’'(),.;:!?—–-"


def _names(text: str) -> list[str]:
    """Capitalized words that are not a sentence's opening capital.

    “Then they stood” opens a sentence; its capital is the sentence's doing and
    says nothing about who is in the scene.
    """
    found = []
    for match in _NAME.finditer(text):
        head = text[:match.start()].rstrip(" \t“”\"‘’'(—–-")
        if not head or head[-1] in ".!?:;":
            continue
        found.append(match.group())
    return found


def _antecedents(text: str) -> tuple[bool, bool]:
    """(plural candidate, singular candidate) in the text before a pronoun.

    Deliberately shallow — no parser, no coreference. A name inside a joined
    pair is not counted as a singular candidate, since the pair is what the
    plural pronoun refers to.
    """
    rest = _PLURAL_JOIN.sub(" ", text)
    plural = bool(_PLURAL_JOIN.search(text) or _PAIR_PHRASE.search(text)
                  or _PLURAL_PRONOUN.search(text))
    singular = bool(_SINGULAR_PRONOUN.search(rest) or _names(rest))
    return plural, singular


def _pronoun_swap(before: str, replacement: str) -> str | None:
    """"plural"/"singular" — the number of the pronoun this edit replaces."""
    old_words, new_words = before.split(), replacement.split()
    if len(old_words) != len(new_words):
        return None
    differing = [(a, b) for a, b in zip(old_words, new_words) if a != b]
    if len(differing) != 1:
        return None
    old, new = (word.strip(_TRIM).casefold() for word in differing[0])
    if new in _PLURAL_TO_SINGULAR.get(old, frozenset()):
        return "plural"
    if new in _SINGULAR_TO_PLURAL.get(old, set()):
        return "singular"
    return None


def pronoun_number_change(before: str, replacement: str, paragraph: str,
                          start: int, end: int) -> str | None:
    """Refuse a pronoun's number changed against the paragraph's own antecedent.

    Cooper, 2026-09-17: “Then they stood and left” became “he” where two people
    had just been at the table. The text before the run is not available here,
    so only what this paragraph supplies counts, and only the clear case is
    refused: a plural antecedent and no singular one in sight (or the mirror of
    that). Both kinds present means the reader may well be right — the models
    see the scene, this function sees a regex — and the edit stands.
    """
    number = _pronoun_swap(before, replacement)
    if number is None or not 0 <= start <= end <= len(paragraph):
        return None
    # An antecedent precedes its pronoun; scanning the head also keeps the
    # pronoun under edit from being read as evidence about itself.
    plural, singular = _antecedents(paragraph[:start])
    if number == "plural" and plural and not singular:
        return "a plural pronoun whose only antecedent in the paragraph is plural"
    if number == "singular" and singular and not plural:
        return "a singular pronoun whose only antecedent in the paragraph is singular"
    return None


# --- flat speech that is not a question --------------------------------------

# An adverb that reports the line was said WITHOUT inflection. A comma-closed
# line tagged this way is a statement on purpose, whatever its word order.
_FLAT_ADVERBS = ("mildly", "flatly", "dryly", "drily", "quietly", "evenly",
                 "softly", "blandly", "tonelessly", "simply", "calmly")
_FLAT_TAG = re.compile(r"[”\"’']?[\s ]*(?:he|she|they|it|we|you|I|[A-Z][\w’'\-]*)"
                       r"\s+said\s+(?:" + "|".join(_FLAT_ADVERBS) + r")\b")
_QUOTED = re.compile(r"[“\"]([^“”\"]*)[”\"]")
# Words a rhetorical repeat adds in front of the words it repeats.
_ECHO_LEAD = frozenset({"i", "he", "she", "they", "it", "we", "you", "so", "and",
                        "but", "did", "do", "does", "well", "oh", "yes", "no"})
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)


def _turns_into_question(before: str, replacement: str) -> bool:
    return (any(mark in replacement for mark in "?!")
            and not any(mark in before for mark in "?!")
            and any(mark in before for mark in ",."))


def _is_echo(paragraph: str, start: int) -> bool:
    """Is the quoted line under edit a verbatim repeat of the last speaker's tail?"""
    quotes = [(match.start(1), match.group(1)) for match in _QUOTED.finditer(paragraph)]
    here = next((i for i, (at, text) in enumerate(quotes)
                 if at <= start <= at + len(text)), None)
    if not here:                              # no quote, or the first one
        return False
    words = [w.casefold() for w in _WORD.findall(quotes[here][1])]
    earlier = [w.casefold() for w in _WORD.findall(quotes[here - 1][1])]
    while words and words[0] in _ECHO_LEAD and len(words) > 2:
        words = words[1:]
    return len(words) >= 2 and earlier[-len(words):] == words


def flat_tag_question(before: str, replacement: str, paragraph: str,
                      start: int, end: int) -> str | None:
    """Refuse a question mark forced onto a line the prose says was flat.

    Cooper, 2026-09-17: “Did he,” she said mildly became “Did he?”, and a
    deliberate flat “isn't she beautiful.” took a question mark. Two shapes are
    refused and no others: the flat reporting tag, and a rhetorical repeat of
    the previous speaker's own words.
    """
    if not _turns_into_question(before, replacement) or not 0 <= start <= end <= len(paragraph):
        return None
    if _FLAT_TAG.match(paragraph[end:]):
        return "a line the tag reports as flatly said keeps its statement mark"
    if _is_echo(paragraph, start):
        return "a verbatim repeat of the previous speaker's words is not a question"
    return None


# --- composition -------------------------------------------------------------

def _window(paragraph: str, start: int, end: int, replacement: str,
            pad: int = 48) -> tuple[str, str] | None:
    """The proposal widened to whole words on both sides, before and after.

    Minimal shrinking leaves a proposal as small as ``"" -> ","`` (the inserted
    comma before Sofia's ellipsis) or ``"18" -> "6"``. A guard comparing two
    surfaces needs the surrounding words to see what actually changed; the same
    bounds are used on both sides so unchanged context cancels out.
    """
    if not 0 <= start <= end <= len(paragraph):
        return None
    lo, hi = max(0, start - pad), min(len(paragraph), end + pad)
    while lo > 0 and not paragraph[lo - 1].isspace():
        lo -= 1
    while hi < len(paragraph) and not paragraph[hi].isspace():
        hi += 1
    return paragraph[lo:hi], paragraph[lo:start] + replacement + paragraph[end:hi]


def possessive_against_author(before: str, replacement: str, paragraph: str,
                              start: int, end: int, possessives) -> str | None:
    """Refuse converting a name's possessive away from the manuscript's own
    form (Immanuel, 2026-09-22: “Dolores’” 36 times, and seven scattered
    reader edits to “Dolores’s”). ``possessives`` is the run's
    docproof.consistency.PossessivePolicy, decided once from the original
    text; it is the one input here that is not the proposal itself."""
    if possessives is None or not getattr(possessives, "names", None):
        return None
    from docproof.consistency import possessive_conversion
    after = paragraph[:start] + replacement + paragraph[end:]
    return possessive_conversion(paragraph, after, possessives)


def proposal_problem(before: str, replacement: str, paragraph: str,
                     start: int, end: int, *, possessives=None) -> str | None:
    """Why this proposal must be refused, or None. Short enough to be a receipt."""
    if not all(isinstance(text, str) for text in (before, replacement, paragraph)):
        return None
    window = _window(paragraph, start, end, replacement)
    wide_before, wide_after = window if window else (before, replacement)
    for reason in (time_24h_conversion(wide_before, wide_after),
                   comma_adjacent_ellipsis(wide_before, wide_after),
                   pronoun_number_change(before, replacement, paragraph, start, end),
                   flat_tag_question(before, replacement, paragraph, start, end),
                   possessive_against_author(before, replacement, paragraph, start, end, possessives)):
        if reason:
            return reason
    return None
