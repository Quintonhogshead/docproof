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
from typing import Mapping, Sequence

from .models import ParagraphRef

log = logging.getLogger("docproof.spellscan")

# Words only: the sweeps own punctuation. Hyphenated compounds are checked as
# their parts, since no dictionary carries "blood-cursed" — holding the
# compound together is Phase 5's consistency problem, not this one.
_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")

# Email addresses and URLs are in-world artifacts, not vocabulary: tokenizing
# LBadgerbones@jhmi.edu into "LBadgerbones" invents a near-duplicate of a real
# character name, and the near-duplicate hygiene pass then DEMOTES the real
# name's protection. Mask them before tokenizing (length-preserving, so
# sentence-initial offsets keep meaning).
_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
                      r"|https?://\S+|www\.\S+")


def _mask_addresses(text: str) -> str:
    return _ADDRESS.sub(lambda m: " " * len(m.group(0)), text)


@dataclass(frozen=True)
class Candidate:
    word: str
    para_ids: tuple[str, ...]
    suggestions: tuple[str, ...] = ()
    stem: str = ""            # the known word it is a regular ending away from


@dataclass(frozen=True)
class NearDuplicate:
    """A protected name demoted because another protected name sits one edit
    away and decisively owns the book. The one failure mode name-protection
    has left: 'Hollingworth' earns the same protection 'Hollingsworth' does,
    and the misspelled character is then the one error the pipeline
    structurally cannot find."""
    word: str            # the rarer surface, as written
    count: int
    suggestion: str      # the dominant surface (for the plural-casing case,
                         # the dominant's casing carried onto the plural)
    of: str              # the dominant surface it is one edit from
    of_count: int


@dataclass(frozen=True)
class NamePair:
    """Two protected names one edit apart with counts too close to call.
    Demoting either would risk renaming a character, so this is a question for
    a person, not a candidate for a model."""
    a: str
    a_count: int
    b: str               # the rarer of the two
    b_count: int


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
    # Occurrence counts for the protected words, aligned with `lexicon`, so
    # the change log can render the do-not-flag list as a reviewable checklist
    # ("Hollingsworth (212)") instead of a truncated aside.
    lexicon_counts: tuple[int, ...] = ()
    near_duplicates: tuple[NearDuplicate, ...] = ()   # demoted: adjudicate each
    name_pairs: tuple[NamePair, ...] = ()             # too close: ask once

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
    # A line break (a Shift+Enter inside the paragraph) opens a line the
    # way a full stop opens a sentence: the capital after it is the
    # line's doing, not the author's.
    if i < 0 or text[i] in ".!?…\n":
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


# --- near-duplicate hygiene ---------------------------------------------------

def _one_edit_apart(a: str, b: str) -> bool:
    """One substitution, insertion, deletion or adjacent transposition apart.
    Both arguments lowercase; equal strings are zero edits, not one."""
    if a == b:
        return False
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        if a[i + 1:] == b[i + 1:]:                       # substitution
            return True
        return (i + 1 < len(a) and a[i] == b[i + 1]      # transposition
                and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:])
    return a[i:] == b[i + 1:]                            # insertion


def _fold_diacritics(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


# Below this length, one edit is most of the word, and two short names a
# letter apart (Kai and Kay, Ana and Anna) are far more often two people than
# one misspelled. The plural-casing check has its own evidence — the shared
# stem — and ignores this floor.
_NEAR_DUP_MIN_LEN = 5


def _near_duplicate_pass(lexicon: list[str], counts: Mapping[str, int], *,
                         dominance: int
                         ) -> tuple[list[str], list[NearDuplicate],
                                    list[NamePair]]:
    """Split the protected list into what stays protected, what is demoted to
    adjudication, and what becomes a question.

    Three outcomes, by the shape of the evidence:
      * plural with the wrong casing ("Evtols" beside "EVTOL") — demoted with
        the dominant casing carried onto the plural ("EVTOLs"), however close
        the counts: a plural styles itself after its singular.
      * one edit apart and decisively lopsided (`dominance`-to-one) — the
        rarer is demoted with the dominant as its suggestion. The lopsided
        count answers the question before it is asked, the same bar the
        consistency scan's name corrections use.
      * one edit apart and close — a NamePair for the author. Demoting either
        would risk renaming a character, which is the one mistake this module
        exists to prevent.

    Diacritic-only pairs (Rian beside Rían) are left alone: the consistency
    scan's name-drift check owns those, with casing-aware machinery this
    module should not duplicate."""
    order = sorted(lexicon, key=lambda s: (-counts[s], s.lower()))
    out: list[str] = []
    demoted: dict[str, NearDuplicate] = {}
    pairs: list[NamePair] = []
    for i, a in enumerate(order):
        if a in demoted:
            continue
        al = a.lower()
        for b in order[i + 1:]:                  # b is never commoner than a
            if b in demoted:
                continue
            bl = b.lower()
            if bl == al + "s" or al == bl + "s":
                # The dominant form sets the casing: a rarer plural should be
                # the dominant singular plus s, a rarer singular the dominant
                # plural minus it.
                expected = (a + "s") if bl == al + "s" else a[:-1]
                if b != expected:
                    demoted[b] = NearDuplicate(
                        word=b, count=counts[b], suggestion=expected,
                        of=a, of_count=counts[a])
                continue                         # a cased-alike plural is fine
            if len(al) < _NEAR_DUP_MIN_LEN or len(bl) < _NEAR_DUP_MIN_LEN:
                continue
            if not _one_edit_apart(al, bl):
                continue
            if _fold_diacritics(al) == _fold_diacritics(bl):
                continue                         # the consistency scan's case
            if counts[a] >= dominance * counts[b]:
                demoted[b] = NearDuplicate(
                    word=b, count=counts[b], suggestion=a,
                    of=a, of_count=counts[a])
            else:
                pairs.append(NamePair(a, counts[a], b, counts[b]))
    for s in lexicon:
        if s not in demoted:
            out.append(s)
    return out, list(demoted.values()), pairs


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
         allowlist: Sequence[str] = (),
         denylist: Mapping[str, str] | None = None,
         dictionary: str = "en_US",
         near_duplicates: bool = True,
         near_duplicate_dominance: int = 5) -> SpellScan:
    """Read every paragraph, and sort what the dictionary does not know."""
    if not enabled:
        return SpellScan(available=False)
    dic = _dictionary(dictionary)
    if dic is None:
        return SpellScan(available=False, dictionary=dictionary)

    allowed = {w.lower() for w in allowlist}
    deny = {k.lower().strip(): v for k, v in (denylist or {}).items()}
    seen: dict[str, _Seen] = {}
    tokens = 0
    for para in paragraphs:
        masked = _mask_addresses(para.text)
        for m in _WORD.finditer(masked):
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
        entry = seen[word]
        if word in deny:
            # A house-banned spelling. Never the author's own, never merely
            # "noted": the correct form is often two words no dictionary
            # suggestion would reach (alot → a lot), so it is carried here and
            # shown as something to look at, whatever the casing or the count.
            candidates.append(Candidate(entry.surface, tuple(entry.para_ids),
                                        suggestions=(deny[word],)))
            continue
        if known(word):
            continue
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

    # Hygiene on the protection itself, before anyone is told to trust it: two
    # protected names one edit apart are more likely one character misspelled
    # than two characters named alike, and granting both immunity is how a
    # main character's surname ships wrong on every page. A decisively rarer
    # twin is demoted (the model will rule on it in context); a close split is
    # a question only the author can settle.
    demoted, pairs = [], []
    if near_duplicates:
        lexicon, demoted, pairs = _near_duplicate_pass(
            lexicon, {s: seen[s.lower()].count for s in lexicon},
            dominance=near_duplicate_dominance)
        for nd in demoted:
            candidates.append(Candidate(nd.word,
                                        tuple(seen[nd.word.lower()].para_ids),
                                        suggestions=(nd.suggestion,)))

    # suggest() costs about a quarter-second a word and is the only slow part
    # of this module, so it runs on a bounded prefix and never on the lexicon —
    # asking what "Kaelith" should have been is how the damage starts. A word
    # that already came apart into a stem or carries a suggestion (the
    # denylist's fix, a demoted near-duplicate's dominant twin) needs no guess.
    if suggestion_limit > 0:
        asked = 0
        for pile in (candidates, recurring):
            for i, c in enumerate(pile):
                if asked >= suggestion_limit:
                    break
                if c.stem or c.suggestions:      # already carries its fix
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
                       dictionary=dictionary,
                       lexicon_counts=tuple(seen[s.lower()].count
                                            for s in lexicon),
                       near_duplicates=tuple(demoted),
                       name_pairs=tuple(pairs))
    log.info("Spell scan: %d tokens, %d unique, %d unknown → %d protected as "
             "names, %d to look at, %d repeated", result.tokens, result.unique,
             result.unknown, len(result.lexicon), len(result.candidates),
             len(result.recurring))
    if demoted:
        log.info("Spell scan: %d near-duplicate name(s) demoted from "
                 "protection: %s", len(demoted),
                 "; ".join(f"{d.word} ({d.count}×) → {d.suggestion} "
                           f"({d.of_count}×)" for d in demoted))
    if pairs:
        log.info("Spell scan: %d near-identical name pair(s) raised to the "
                 "author: %s", len(pairs),
                 "; ".join(f"{p.a} ({p.a_count}×) / {p.b} ({p.b_count}×)"
                           for p in pairs))
    return result
