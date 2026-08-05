"""A dictionary scan that never corrects anything.

Out-of-dictionary is evidence, not a verdict. The proofreading brief's own
worst case is a coined word "spell-checked" into a standard English one, and
the dictionary makes that easy to do: asked to suggest for *Kaelith* it offers
*Lilith*, and for *Vorrenth* it offers *Torrent*. A checker that acted on that
would quietly rename a character.

So this module classifies instead. Every word the dictionary does not know is
sorted into one of two piles:

  * words the author clearly means — repeated, or capitalized mid-sentence.
    These become a lexicon of the manuscript's own vocabulary, handed to the
    model as a do-not-flag list. This is the half that pays for itself: it
    attacks the exact false positive the error types are written to avoid,
    with document-specific evidence no static list could carry.

  * words used exactly once and not written as a name. That is what a typo
    looks like, so they are handed over as things to look at — never as things
    to change.

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


@dataclass(frozen=True)
class SpellScan:
    """What the dictionary found, and nothing it decided."""
    lexicon: tuple[str, ...] = ()        # the author's own vocabulary: protect
    candidates: tuple[Candidate, ...] = ()   # used once, unknown: look at
    tokens: int = 0
    unique: int = 0
    unknown: int = 0
    available: bool = True               # False when no dictionary was loadable

    def prompt_section(self) -> str:
        """The document vocabulary, as the model should read it. Empty when
        there is nothing worth saying, so a clean manuscript adds no tokens."""
        parts: list[str] = []
        if self.lexicon:
            parts.append(
                "WORDS THIS AUTHOR OWNS\n"
                "A dictionary scan of this manuscript found these words used "
                "as the author's own — coined terms, invented places and "
                "peoples, character names. They are CORRECT. Never report one "
                "as a misspelling, and never change one into the standard "
                "English word it resembles:\n"
                + ", ".join(self.lexicon))
        if self.candidates:
            listed = []
            for c in self.candidates:
                if c.suggestions:
                    listed.append(f"{c.word} (perhaps: {', '.join(c.suggestions)})")
                else:
                    listed.append(c.word)
            parts.append(
                "WORDS TO LOOK AT\n"
                "These appear exactly once, are not written as a name, and are "
                "not in the dictionary — which is what a typo looks like. "
                "Where one falls in a paragraph you are reviewing, read it and "
                "decide. If it reads as deliberate, leave it. The parenthesised "
                "words are the dictionary's guesses, not instructions:\n"
                + "; ".join(listed))
        return "\n\n".join(parts)


@lru_cache(maxsize=4)
def _dictionary(name: str):
    """The bundled Hunspell dictionary, or None if spylls is not installed.

    Offline on purpose: the tests must not reach the network and the watcher
    runs unattended, so a dictionary that needs downloading is no dictionary
    at all."""
    try:
        import spylls
        from spylls.hunspell import Dictionary
    except ImportError:
        log.warning("spylls is not installed, so the spell scan is skipped. "
                    "The model passes still run; they simply do not get the "
                    "manuscript's own vocabulary as a do-not-flag list.")
        return None
    path = Path(spylls.__file__).parent / "hunspell" / "data" / "en" / name
    try:
        return Dictionary.from_files(str(path))
    except Exception as e:                       # a missing or broken data file
        log.warning("Could not load the %s dictionary (%s); skipping the "
                    "spell scan.", name, e)
        return None


def _sentence_initial(text: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and text[i] in " \t \"“”'‘’(":
        i -= 1
    return i < 0 or text[i] in ".!?…"


@dataclass
class _Seen:
    count: int = 0
    proper: int = 0        # capitalized somewhere other than a sentence start
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
        return SpellScan(available=False)

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
            if word[0].isupper() and not _sentence_initial(para.text, m.start()):
                entry.proper += 1
            if para.para_id not in entry.para_ids:
                entry.para_ids.append(para.para_id)

    def known(word: str) -> bool:
        return (word.lower() in allowed
                or dic.lookup(word) or dic.lookup(word.lower())
                or dic.lookup(word.capitalize()))

    lexicon: list[str] = []
    candidates: list[Candidate] = []
    for word in sorted(seen):
        if known(word):
            continue
        entry = seen[word]
        # A word the author uses more than once, or writes as a name, is the
        # author's. A word used once in lower case is worth a second look.
        if entry.proper or entry.count >= min_occurrences:
            lexicon.append(entry.surface)
        else:
            candidates.append(Candidate(entry.surface, tuple(entry.para_ids)))

    # suggest() costs about a quarter-second a word and is the only slow part
    # of this module, so it runs on a bounded prefix and never on the lexicon —
    # asking what "Kaelith" should have been is how the damage starts.
    if suggestion_limit > 0 and candidates:
        enriched = []
        for i, c in enumerate(candidates):
            if i < suggestion_limit:
                try:
                    picks = tuple(list(dic.suggest(c.word))[:3])
                except Exception:                # a suggester that gives up
                    picks = ()
                enriched.append(Candidate(c.word, c.para_ids, picks))
            else:
                enriched.append(c)
        candidates = enriched

    result = SpellScan(lexicon=tuple(lexicon), candidates=tuple(candidates),
                       tokens=tokens, unique=len(seen),
                       unknown=len(lexicon) + len(candidates))
    log.info("Spell scan: %d tokens, %d unique, %d unknown → %d protected as "
             "the author's own, %d to look at", result.tokens, result.unique,
             result.unknown, len(result.lexicon), len(result.candidates))
    return result
