"""A dictionary scan that never corrects anything.

Out-of-dictionary is evidence, not a verdict. The proofreading brief's own
worst case is a coined word "spell-checked" into a standard English one, and
the dictionary makes that easy to do: asked to suggest for *Kaelith* it offers
*Lilith*, and for *Vorrenth* it offers *Torrent*. A checker that acted on that
would quietly rename a character.

So this module classifies instead. Every word the dictionary does not know is
sorted into one of three piles:

  * words written as names — capitalized where no rule of English would
    capitalize them. These become a lexicon of the manuscript's own vocabulary,
    handed to the model as a do-not-flag list. This is the half that pays for
    itself: it attacks the exact false positive the error types are written to
    avoid, with document-specific evidence no static list could carry.

  * words to look at: used seldom, or coming apart into an ordinary word plus
    an ending English does not give it. Both are what a typo looks like, so
    they are handed over as things to read — never as things to change.

  * words used throughout. Repetition is evidence of vocabulary, and it used
    to be treated as proof: any unknown word seen twice was protected outright.
    That is the wrong way round for the words it matters most on. A coinage
    repeats because the author invented it; *growed* repeats because the author
    believes in it, and protecting it makes a misspelling on every page of a
    manuscript the one thing this pipeline structurally cannot find. So the
    repetition is passed on as what it is — evidence — and the model reads the
    word where it falls.

Protection is the strong claim, so it takes the strong signal. Name-casing is
that signal, because renaming a character is the one error an author cannot
undo by reading the change log.

Nothing here edits the manuscript. The output is context for the model passes,
and counts for the change log.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from .models import ParagraphRef

log = logging.getLogger("docproof.spellscan")

# Words only: the sweeps own punctuation. Hyphenated compounds are checked as
# their parts, since no dictionary carries "blood-cursed" — holding the
# compound together is Phase 5's consistency problem, not this one.
_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")


@dataclass(frozen=True)
class Candidate:
    word: str
    para_ids: tuple[str, ...]
    suggestions: tuple[str, ...] = ()
    stem: str = ""            # the known word it is a regular ending away from


@dataclass(frozen=True)
class SpellScan:
    """What the dictionary found, and nothing it decided."""
    lexicon: tuple[str, ...] = ()        # written as names: protect
    candidates: tuple[Candidate, ...] = ()   # unknown, not a name: look at
    recurring: tuple[Candidate, ...] = ()    # unknown and repeated: read it
    tokens: int = 0
    unique: int = 0
    unknown: int = 0
    available: bool = True               # False when no dictionary was loadable
    dictionary: str = ""                 # which set actually answered

    def prompt_section(self) -> str:
        """The document vocabulary, as the model should read it. Empty when
        there is nothing worth saying, so a clean manuscript adds no tokens."""
        parts: list[str] = []
        if self.lexicon:
            parts.append(
                "WORDS THIS AUTHOR OWNS\n"
                "A dictionary scan of this manuscript found these words "
                "written as names — coined terms, invented places and peoples, "
                "characters. They are CORRECT. Never report one as a "
                "misspelling, and never change one into the standard English "
                "word it resembles:\n"
                + ", ".join(self.lexicon))
        if self.candidates:
            parts.append(
                "WORDS TO LOOK AT\n"
                "These are not written as names and are not in the dictionary "
                "— which is what a typo looks like. Where one falls in a "
                "paragraph you are reviewing, read it and decide. If it reads "
                "as deliberate, leave it. A word shown as “x → y” came apart "
                "as an ordinary word plus an ending English does not give it, "
                "which is usually a misspelling and occasionally dialect the "
                "author means. The parenthesised words are the dictionary's "
                "guesses, not instructions:\n"
                + "; ".join(_describe(c) for c in self.candidates))
        if self.recurring:
            parts.append(
                "WORDS USED THROUGHOUT\n"
                "These are not in the dictionary either, and appear more than "
                "once. Repetition usually means a coined term the author "
                "means — but a misspelling the author believes in repeats just "
                "as faithfully, so this is evidence and not a verdict. Read "
                "each where it falls: if it belongs to this book's vocabulary, "
                "leave it alone; if it is simply the wrong spelling of an "
                "ordinary word, it is wrong every time it appears:\n"
                + "; ".join(_describe(c) for c in self.recurring))
        return "\n\n".join(parts)


def _describe(c: Candidate) -> str:
    """One word as the model should see it: what it came apart into, and what
    the dictionary would have guessed, neither of them as an instruction."""
    out = c.word
    if c.stem:
        out += f" → {c.stem}"
    if c.suggestions:
        out += f" (perhaps: {', '.join(c.suggestions)})"
    return out


@lru_cache(maxsize=8)
def _dictionary(name: str):
    """A Hunspell dictionary by name or path, or None if none can be loaded.

    Offline on purpose: the tests must not reach the network and the watcher
    runs unattended, so a dictionary that needs downloading is no dictionary
    at all. Only `en_US` ships with spylls, so a U.K., Canadian or Australian
    run needs the press to supply its own .aff/.dic pair and point
    `spellcheck.dictionary` at it. Rather than quietly checking British
    spelling against an American word list — which would file *colour* and
    *organise* as unknown words — the scan declines to run and says why."""
    try:
        import spylls
        from spylls.hunspell import Dictionary
    except ImportError:
        log.warning("spylls is not installed, so the spell scan is skipped. "
                    "The model passes still run; they simply do not get the "
                    "manuscript's own vocabulary as a do-not-flag list.")
        return None

    candidate = Path(name)
    if candidate.parent != Path("."):            # an explicit path
        paths = [candidate]
    else:
        paths = [Path(spylls.__file__).parent / "hunspell" / "data" / "en" / name]
    for path in paths:
        try:
            return Dictionary.from_files(str(path))
        except Exception:                        # missing or broken data files
            continue
    log.warning(
        "No %s dictionary available, so the spell scan is skipped for this "
        "run. spylls ships en_US only; put an .aff/.dic pair somewhere and "
        "set spellcheck.dictionary to its path (without the extension) to "
        "scan this variant. Checking it against en_US instead would file "
        "every correct non-U.S. spelling as an unknown word.", name)
    return None


def _sentence_initial(text: str, pos: int) -> bool:
    """Is this word standing where any word would be capitalized anyway?

    Getting this wrong is expensive in one direction: a word wrongly read as
    capitalized mid-sentence is read as a name, and names are protected. So a
    quotation counts, even though the mark before it is usually a comma —
    `he said, "Growed like a weed"` opens a sentence as surely as a full stop
    does, and it is the commonest construction in fiction there is."""
    i = pos - 1
    quoted = False
    while i >= 0 and text[i] in " \t \"“”'‘’(":
        quoted = quoted or text[i] in "\"“‘'("
        i -= 1
    if i < 0 or text[i] in ".!?…":
        return True
    return quoted and text[i] in ",:;—–-"


# Endings English adds by rule. Longest first, so "rised" is read as "rise"
# plus -ed rather than "rise" plus -d.
_REGULAR_ENDINGS = ("ing", "est", "ed", "es", "en", "er", "ly", "s", "d")


def _regular_form(word: str, dic) -> str:
    """The known word this one is a regular ending away from, if there is one.

    This is the shape of a misspelling the occurrence count cannot argue with:
    *growed*, *teached*, *layed*, *tooken*, *partys*, *messyer*. Each is an
    ordinary English word wearing an ending English does not give it, and each
    repeats — the author believes in it — so counting occurrences reads it as
    vocabulary and protects it, which is exactly backwards.

    Taking the word apart tells the two cases apart. A coinage is almost never
    a standard word plus a regular ending: strip *bloodcursed* or *starship*
    and no English word is left, while stripping *growed* leaves *grow*.

    Returns the stem, or "" when the word does not come apart this way."""
    w = word.lower()
    for ending in _REGULAR_ENDINGS:
        if not w.endswith(ending):
            continue
        stem = w[:-len(ending)]
        if len(stem) < 3:
            continue
        guesses = [stem, stem + "e"]              # layed → lay; rised → rise
        if stem[-1] == stem[-2]:
            guesses.append(stem[:-1])             # runned → run
        if stem.endswith("i"):
            guesses.append(stem[:-1] + "y")       # tryed → try
        for guess in guesses:
            if dic.lookup(guess):
                return guess
    return ""


@dataclass
class _Seen:
    count: int = 0
    proper: int = 0        # capitalized somewhere other than a sentence start
    lower: int = 0         # written in lower case at least this often
    forms: Counter = field(default_factory=Counter)   # how it is actually written
    para_ids: list[str] = field(default_factory=list)

    @property
    def surface(self) -> str:
        """The spelling the author uses most. Showing a character's name back
        to the model in lower case would be its own kind of wrong — and might
        read as an invitation to "fix" the capitalization."""
        return min(self.forms.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def scan(paragraphs: Sequence[ParagraphRef], *, enabled: bool = True,
         min_occurrences: int = 2, suggestion_limit: int = 25,
         allowlist: Sequence[str] = (), dictionary: str = "en_US") -> SpellScan:
    """Read every paragraph, and sort what the dictionary does not know."""
    if not enabled:
        return SpellScan(available=False)
    dic = _dictionary(dictionary)
    if dic is None:
        return SpellScan(available=False, dictionary=dictionary)

    allowed = {w.lower() for w in allowlist}
    seen: dict[str, _Seen] = {}
    tokens = 0
    for para in paragraphs:
        for m in _WORD.finditer(para.text):
            word = m.group(0).replace("’", "'")
            tokens += 1
            entry = seen.setdefault(word.lower(), _Seen())
            entry.count += 1
            entry.forms[word] += 1
            if not word[0].isupper():
                entry.lower += 1
            elif not _sentence_initial(para.text, m.start()):
                entry.proper += 1
            if para.para_id not in entry.para_ids:
                entry.para_ids.append(para.para_id)

    def known(word: str) -> bool:
        return (word.lower() in allowed
                or dic.lookup(word) or dic.lookup(word.lower())
                or dic.lookup(word.capitalize()))

    lexicon: list[str] = []
    candidates: list[Candidate] = []
    recurring: list[Candidate] = []
    for word in sorted(seen):
        if known(word):
            continue
        entry = seen[word]
        stem = _regular_form(word, dic)
        if entry.proper:
            # Written as a name. This is the signal strong enough to earn
            # protection outright, because it guards the one mistake an author
            # cannot undo by reading the change log: a renamed character.
            lexicon.append(entry.surface)
        elif not entry.lower and entry.count >= min_occurrences and not stem:
            # Never once written in lower case, and used again: a name that
            # happens only ever to have fallen at the start of a sentence.
            # Weaker evidence, so it yields to a word that comes apart — a
            # capital on "Layed" is the sentence's doing, not the author's.
            lexicon.append(entry.surface)
        elif stem or entry.count < min_occurrences:
            # Used seldom, or an ordinary word wearing an ending English does
            # not give it. Either way, something to look at.
            candidates.append(Candidate(entry.surface, tuple(entry.para_ids),
                                        stem=stem))
        else:
            # Repeated, which is evidence of vocabulary but not proof of it.
            # Said to the model as evidence rather than acted on here.
            recurring.append(Candidate(entry.surface, tuple(entry.para_ids)))

    # suggest() costs about a quarter-second a word and is the only slow part
    # of this module, so it runs on a bounded prefix and never on the lexicon —
    # asking what "Kaelith" should have been is how the damage starts. A word
    # that already came apart into a stem needs no guess: it carries its own.
    if suggestion_limit > 0:
        asked = 0
        for pile in (candidates, recurring):
            for i, c in enumerate(pile):
                if asked >= suggestion_limit:
                    break
                if c.stem:
                    continue
                asked += 1
                try:
                    picks = tuple(list(dic.suggest(c.word))[:3])
                except Exception:                # a suggester that gives up
                    picks = ()
                pile[i] = Candidate(c.word, c.para_ids, picks, c.stem)

    result = SpellScan(lexicon=tuple(lexicon), candidates=tuple(candidates),
                       recurring=tuple(recurring), tokens=tokens,
                       unique=len(seen),
                       unknown=len(lexicon) + len(candidates) + len(recurring),
                       dictionary=dictionary)
    log.info("Spell scan: %d tokens, %d unique, %d unknown → %d protected as "
             "names, %d to look at, %d repeated", result.tokens, result.unique,
             result.unknown, len(result.lexicon), len(result.candidates),
             len(result.recurring))
    return result
