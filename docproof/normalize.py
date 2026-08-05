"""The two edits that are made silently, and nothing else.

The house brief allows exactly two changes outside the tracked-changes system:
straight quotes become curly, and runs of spaces collapse to one. Neither is a
content change the author needs to review, so neither is tracked or logged
line by line.

That makes this the most dangerous module in the project. Every other edit
docproof makes lands in front of a person as a tracked change they can reject.
These do not. A wrongly-curled apostrophe ships.

So the rule here is *convert what is certain, and leave the rest alone*. A
straight apostrophe left straight is a small blemish a proofreader will catch.
An apostrophe curled the wrong way is a typographic error that looks
deliberate and survives to print. Where the direction cannot be determined
from the text, this module does nothing and counts what it skipped, so the
count reaches the report and a person can look.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .utils.xml_helpers import iter_text_elements, paragraph_text, set_text

log = logging.getLogger("docproof.normalize")

LEFT_DOUBLE, RIGHT_DOUBLE = "“", "”"
LEFT_SINGLE, RIGHT_SINGLE = "‘", "’"

# After one of these, a quote is opening; after anything else it is closing.
# The straight marks are in the set because every decision here is made
# against the original text: when a single quote follows a double one, as
# in «"'Tis always so."», that double quote has not been curled yet.
_OPENS_AFTER = set(" \t\n ([{<—–-“‘\"'/")

# Words that begin with a dropped letter, where the mark is an apostrophe and
# curls right — not an opening quote. This list is why "'tis" does not become
# "‘tis". It cannot be complete, and it is not meant to be: anything not on it
# and not otherwise certain is left straight rather than guessed at.
_ELISIONS = frozenset("""
tis twas twere til till bout cause em n round way fore neath gainst nother
ere im er is ave ad ow ello alf ope appen aven ain
""".split())

# How far ahead to look for the other half of a nested quotation. Long enough
# for a quoted phrase inside a line of dialogue, short enough that an
# unrelated possessive later in the paragraph is not mistaken for the partner.
_PAIR_WINDOW = 120


@dataclass(frozen=True)
class NormalizationReport:
    """Counts only. The brief asks for these two edits to go unlogged
    individually; it does not ask for them to go unmentioned."""
    quotes: int = 0          # straight marks curled
    spaces: int = 0          # runs of spaces collapsed
    paragraphs: int = 0      # paragraphs touched
    ambiguous: int = 0       # marks deliberately left straight
    ran: bool = False

    @property
    def total(self) -> int:
        return self.quotes + self.spaces


# --- the text rules ----------------------------------------------------------

def _word_after(text: str, i: int) -> str:
    j = i + 1
    while j < len(text) and text[j].isalpha():
        j += 1
    return text[i + 1:j].lower()


def _closes_nearby(text: str, i: int) -> bool:
    """Whether a straight mark within reach of `i` looks like the close of a
    quotation: preceded by a letter, digit or sentence punctuation, and not
    followed by a letter or digit.

    The punctuation half matters more than it looks. Dialogue almost always
    closes on a comma — "'Hello,' she said" — and a rule that only accepted a
    letter before the mark would leave that pair mismatched, one straight and
    one curled, which is worse than leaving both alone. A contraction fails
    the second test ("don't"); a possessive passes both ("the dogs' bark"),
    which is the residual risk, bounded by the window."""
    for j in range(i + 1, min(len(text), i + 1 + _PAIR_WINDOW)):
        if text[j] != "'":
            continue
        if ((text[j - 1].isalnum() or text[j - 1] in ",.!?;:")
                and (j + 1 >= len(text) or not text[j + 1].isalnum())):
            return True
    return False


def _quote_edits(text: str, *, single_primary: bool = False
                 ) -> tuple[list[tuple[int, int, str]], int]:
    """Straight marks that can be curled with confidence, and a count of the
    ones that cannot.

    `single_primary` is the U.K./Australian convention, where a single quote
    opens dialogue. It flips the default for a word-initial mark: in a U.S.
    manuscript that is more likely a dialect elision, and in a U.K. one it is
    more likely a line of dialogue starting."""
    edits: list[tuple[int, int, str]] = []
    ambiguous = 0
    for i, ch in enumerate(text):
        if ch not in "\"'":
            continue
        before = text[i - 1] if i else ""
        after = text[i + 1] if i + 1 < len(text) else ""

        if ch == '"':
            # Double quotes are safe: the character before one settles it, and
            # a double quote is never anything but a quotation mark.
            opening = (not before) or before in _OPENS_AFTER
            edits.append((i, i + 1, LEFT_DOUBLE if opening else RIGHT_DOUBLE))
            continue

        # A single mark between or after letters is an apostrophe —
        # contractions and possessives, the overwhelming majority.
        if before.isalnum():
            edits.append((i, i + 1, RIGHT_SINGLE))
        # "'60s", "'99"
        elif after.isdigit():
            edits.append((i, i + 1, RIGHT_SINGLE))
        # A dropped leading letter: "'tis", "'round", "'ere".
        elif ((not before) or before in _OPENS_AFTER) and after.isalpha() \
                and _word_after(text, i) in _ELISIONS:
            edits.append((i, i + 1, RIGHT_SINGLE))
        # A closing mark after sentence punctuation: "…and left,' she said."
        # Nothing else it can be — an apostrophe never follows a comma.
        elif before in ",.!?;:" and (not after or after in " \t \"”)]"):
            edits.append((i, i + 1, RIGHT_SINGLE))
        # A quotation opening. In a single-quote variant that is simply what a
        # word-initial mark usually is; in a double-quote one it has to be
        # earning its place, so look for the closing half nearby. Without that
        # check a U.S. pair would come out mismatched — one straight, one
        # curled — which reads worse than leaving both alone.
        elif ((not before) or before in _OPENS_AFTER) and (
                single_primary or _closes_nearby(text, i)):
            edits.append((i, i + 1, LEFT_SINGLE))
        else:
            # Could be a nested quotation, could be dialect this list does not
            # know. Guessing here is how a manuscript gets ‘ere instead of
            # ’ere, silently, with nothing to reject.
            ambiguous += 1
    return edits, ambiguous


# Two or more spaces that follow visible text: leading indentation is left
# alone, which is what the brief asks for.
_SPACE_RUN = re.compile(r"(?<=\S) {2,}")


def _space_edits(text: str) -> list[tuple[int, int, str]]:
    # A paragraph with no letters or digits is a scene divider — "*   *   *"
    # is spaced the way it is on purpose.
    if not any(c.isalnum() for c in text):
        return []
    return [(m.start(), m.end(), " ") for m in _SPACE_RUN.finditer(text)]


def normalize_text(text: str, *, quotes: bool = True, spaces: bool = True,
                   variant=None) -> str:
    """The text as this module would leave it. The readable statement of what
    normalization does, and what the tests assert against."""
    single = bool(variant and variant.opens_dialogue_with_single)
    edits, _ = (_quote_edits(text, single_primary=single) if quotes
                else ([], 0))
    if spaces:
        edits = edits + _space_edits(text)
    out, last = [], 0
    for start, end, replacement in sorted(edits):
        out.append(text[last:start])
        out.append(replacement)
        last = end
    out.append(text[last:])
    return "".join(out)


# --- applying them to the package --------------------------------------------

def _apply_untracked(p, edits: list[tuple[int, int, str]]) -> None:
    """Rewrite the paragraph's text elements in place, with no revision
    markup. Right to left, so an edit never moves the ground under the next
    one; spans are recomputed each time because the previous edit changed
    them."""
    for start, end, replacement in sorted(edits, key=lambda e: e[0],
                                          reverse=True):
        spans, off = [], 0
        for t in iter_text_elements(p):
            n = len(t.text or "")
            spans.append((t, off, off + n))
            off += n
        touched = [(t, lo, hi) for t, lo, hi in spans if hi > start and lo < end]
        for i, (t, lo, hi) in enumerate(touched):
            text = t.text or ""
            a, b = max(start, lo) - lo, min(end, hi) - lo
            # The replacement lands whole in the first element it touches; the
            # rest just lose their share of the span.
            set_text(t, text[:a] + (replacement if i == 0 else "") + text[b:])


def normalize_package(pkg, *, quotes: bool = True, spaces: bool = True,
                      variant=None) -> NormalizationReport:
    """Apply the two silent edits to every paragraph in the document.

    Runs before ingest, so everything downstream — the document model, the
    sweeps, the model passes, every anchor — measures against normalized text
    and no stage has to know this happened."""
    from .utils.xml_helpers import walk_package

    if not (quotes or spaces):
        return NormalizationReport(ran=False)

    single = bool(variant and variant.opens_dialogue_with_single)
    n_quotes = n_spaces = n_paras = ambiguous = 0
    parts: set[str] = set()
    for wp in walk_package(pkg):
        text = paragraph_text(wp.element)
        if not text:
            continue
        edits: list[tuple[int, int, str]] = []
        if quotes:
            found, amb = _quote_edits(text, single_primary=single)
            ambiguous += amb
            edits += found
            n_quotes += len(found)
        if spaces:
            found = _space_edits(text)
            edits += found
            n_spaces += len(found)
        if not edits:
            continue
        _apply_untracked(wp.element, edits)
        n_paras += 1
        parts.add(wp.part)

    for part in parts:
        pkg.mark_modified(part)

    report = NormalizationReport(quotes=n_quotes, spaces=n_spaces,
                                 paragraphs=n_paras, ambiguous=ambiguous,
                                 ran=True)
    log.info("Normalized silently: %d quote(s) curled, %d space run(s) "
             "collapsed across %d paragraph(s); %d mark(s) left straight as "
             "ambiguous.", n_quotes, n_spaces, n_paras, ambiguous)
    return report
