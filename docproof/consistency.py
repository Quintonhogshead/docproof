"""One term, written more than one way.

The brief asks for compound-word consistency — *blood-cursed* against
*bloodcursed* against *blood cursed*, *safe keeping* against *safekeeping* —
and it is the one rule per-paragraph review structurally cannot do. A model
reading chunk 4 has no idea what chunk 40 said. Finding this needs the whole
document at once, which is exactly what a deterministic scan is for.

For terms it **asks and never corrects**, for a reason worth stating.
Detection here is mechanical: strip the hyphens and spaces, and two spellings
of one term collapse to the same key. But that same test cannot tell an
inconsistency from a distinction — *awhile* and *a while* mean different
things, as do *everyday* and *every day*. The known pairs are excluded by
name, and the rest go to the author as a question, because which form a book
uses is the author's to settle and getting it wrong silently would be worse
than not asking.

Two kinds of difference are grammar rather than spelling, and the scan folds
them away before it counts: capitalization (English capitalizes the first word
of every sentence) and a *form-final* possessive apostrophe (*mothers* and
*mother's* are different words that coexist, not one term written two ways). An
apostrophe *inside* a term — *farmer's market* against *farmers market* — is a
spelling of one term and is still asked about. See ``_structure``.

Proper names get one carefully-bounded exception. A capitalized word that
never appears lowercased and differs from another only in its diacritics —
*Rian* against *Rían*, *Zoe* against *Zoë* — is one name, not two words with
different meanings; English has no minimal pairs there the way it does for
compounds. When one spelling clearly owns the book (see ``find_name_drift``
for the exact bar), the strays are corrected as tracked changes, because the
author's accept/reject review is itself the human judgment the query channel
exists to request — a lopsided count answers the question before it is asked.
Anything short of that bar falls back to a question, same as the terms.

THE INVENTORY. Every scan here reads the whole book, runs offline, is
deterministic, and is silent on a book without its pattern. What each one finds,
and which channel it uses:

  ``find_inconsistencies``       one term written two ways         asks
  ``find_name_drift``            Rian / Rian with an accent        both
  ``find_spelling_variants``     grey / gray, via VarCon           asks
  ``find_variant_policy``        theatre throughout, on a US run   asks
  ``find_abbreviation_variants`` U.S. / US                         asks
  ``find_acronym_case``          NASA / Nasa                       asks
  ``find_accent_loanwords``      Si / Si with an accent            asks
  ``find_deity_pronouns``        he / He in a reverent book        asks
  ``find_time_style``            "at 8" in an "11:00 a.m." book    asks
  ``find_case_splits``           earth / Earth mid-sentence        corrects
  ``find_vessel_pronouns``       a ship called *she* and once *it* corrects
  ``find_dialect_variants``      dinnae / dinna inside dialect     both
  closed compounds               sat phone / satphone              corrects
  ``find_figure_drift``          282.6 deg x9 against 282.8 deg x1 asks
  ``callbacks.find_callbacks``   a remembered line, misquoted      both
  ``find_possessive_drift``      Dolores’ x36 against Dolores’s x1 corrects

The five above the last came out of the Cooper QA, where each was a miss no
per-paragraph review could structurally have caught; the last came out of the
Immanuel QA, where per-paragraph readers each applied Chicago's ’s at a few
sites of a name the author wrote the other way throughout. The ones that correct are
read by a caller that screens every proposal in context (Galley's fixed
workflow); the figure scan corrects nothing at all, because house policy is that
a numeric value is never changed to repair a contradiction.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from .models import Finding, ParagraphRef
from .spellscan import _dictionary, _sentence_initial
from .sweeps import sentence_window

log = logging.getLogger("docproof.consistency")

# The shipped data the mechanical scans read: the spelling-variant equivalence
# table and the Chicago/Merriam-Webster preference notes. Resolved next to the
# package the way the variant files are, so a pip-installed server finds them.
_CONSISTENCY_DIR = Path(__file__).resolve().parent.parent / "config" / "consistency"

# The key this type's findings carry. It is not an error type — nothing in
# config/error_types defines it — because there is no prompt to write: the
# whole thing is decided before any model sees the document.
CONSISTENCY_KEY = "term_consistency"

# The key a name correction carries. A separate key because the channel is
# decided per error type: CONSISTENCY_KEY findings ask, these correct. Like
# CONSISTENCY_KEY it lives outside config/error_types — no prompt to write.
NAME_KEY = "name_consistency"

# A word is a Unicode letter — [^\W\d_] — continued by letters, apostrophes,
# hyphens, or combining diacritics (U+0300–U+036F), so an accent that arrives
# decomposed cannot split its word. Both scans tokenize with this: an
# ASCII-only class would read Rían as R + an and fiancée as fianc + e,
# leaving every accented term invisible and spraying fragments that pair with
# their neighbours into phantom open compounds.
_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|[\u0300-\u036f'’-])*")


def _trim_quote(text: str, start: int, end: int) -> tuple[str, int, int]:
    """Give back a closing single quote that ``_WORD`` swallowed.

    ``_WORD`` allows a trailing ' or ’ so it can hold possessives and clitics,
    but that same rule grabs the closing quote of single-quoted dialogue
    (*cursed’*), and a bare word that appears once inside quotes would then flag
    against itself. A trailing quote is a closing quote — trim it and shorten
    the end offset to keep the occurrence anchored to the word — *unless* the
    character before it is an s, where it may be a real plural possessive
    (*mothers’*); that reading is left for ``_structure`` to fold away. (A
    dialect elision mark, *runnin’*, is trimmed too, which is harmless: the
    elided spelling keys apart from the full word regardless.)"""
    if text[-1] in "'’" and text[-2] not in "sS":
        return text[:-1], start, end - 1
    return text, start, end


# Pairs that collapse to the same key and are NOT inconsistencies: English
# distinguishes them. Flagging these would train the press to ignore this
# section, which is the only way a query channel really fails.
_LEGITIMATE = frozenset("""
awhile anymore sometime sometimes everyday anyway apart already altogether
maybe cannot into onto upon within without throughout however whatever
whenever wherever whoever nevertheless someday everyone anyone someone
everything anything something nothing indeed instead therefore moreover
""".split())


@dataclass(frozen=True)
class Occurrence:
    para_id: str
    start: int
    end: int
    form: str


# The key a casing-split finding carries: one term written lowercase in some
# places and capitalized in others outside sentence-initial position (earth /
# Earth, band-aid / Band-Aid). Lives outside config/error_types like the other
# consistency keys — deterministic, no prompt.
CASE_SPLIT_KEY = "case_split"


@dataclass(frozen=True)
class CaseSplit:
    """One 1–2-word term capitalized two ways in running prose. `clear` is the
    dominance test (the majority form leads `dominance`:1 over at least
    `min_total` uses); the minority occurrences are `outliers`."""
    key: str                              # case-folded term: "earth", "easy speed"
    counts: Counter                       # exact form -> mid-sentence uses
    dominant: str                         # exact form proposed at every outlier
    clear: bool
    outliers: tuple[Occurrence, ...]


@dataclass(frozen=True)
class Inconsistency:
    key: str
    counts: Counter                       # surface form -> times seen
    dominant: str
    outliers: tuple[Occurrence, ...]

    @property
    def minority_forms(self) -> tuple[str, ...]:
        return tuple(sorted({o.form for o in self.outliers}))


@dataclass(frozen=True)
class NameDrift:
    """One proper name, spelled with and without its diacritics."""
    key: str
    counts: Counter                       # representative form -> times seen
    dominant: str
    outliers: tuple[Occurrence, ...]
    # Whether the evidence clears the bar for correcting rather than asking.
    enforce: bool

    @property
    def minority_forms(self) -> tuple[str, ...]:
        return tuple(sorted({o.form for o in self.outliers}))


@dataclass(frozen=True)
class VariantGroup:
    """One word/abbreviation/acronym the manuscript writes more than one way,
    found by a deterministic table or casing scan rather than the compound-word
    key scan. Unlike the term scan this emits ONE query per group, anchored at
    the first minority occurrence: a spelling variant can recur on every page,
    and a margin comment per occurrence would bury the channel it lives in.

    `kind` is "spelling" | "abbreviation" | "acronym_case". `counts` maps the
    surface forms the book actually uses to their occurrence counts. `dominant`
    is the form the book uses most (the recommendation); `has_majority` is
    whether it leads the others decisively — when it does not, the query asks
    which form to settle on rather than naming a slip. `note` is the
    Chicago/Merriam-Webster preference phrasing, or "" ."""
    kind: str
    key: str
    counts: Counter                       # surface form -> times seen
    dominant: str
    has_majority: bool
    site: Occurrence                      # where the single query anchors
    minority_total: int
    note: str = ""

    @property
    def forms(self) -> list[tuple[str, int]]:
        """Surface forms with counts, most-used first — deterministic on ties."""
        return sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass(frozen=True)
class TimeStyleDrift:
    """A book that writes clock times with minutes (11:00 a.m., 8:30), and the
    bare-hour strays ("around 4", "at 8") the head proofreader was adding :00
    to by hand on the Purpura run. Queries only: a bare hour can be deliberate
    ("she was in bed by nine"-style books exist), so the book's own majority
    style is evidence to cite, never a rule to enforce silently."""
    with_minutes: int                     # H:MM times seen in the book
    example: str                          # a representative H:MM form
    outliers: tuple[Occurrence, ...]      # bare-hour sites; form is the digits


@dataclass(frozen=True)
class DeityPronounDrift:
    """A book that capitalizes pronouns referring to God, and the lowercase
    strays inside deity-anchored sentences. Queries only: pronoun reference is
    the author's to resolve, not a scan's."""
    capitalized: int                      # mid-sentence He/His/Him/Himself seen
    outliers: tuple[Occurrence, ...]      # lowercase strays, deity in sentence


# The key a vessel-pronoun correction carries. Like the other consistency keys
# it lives outside config/error_types — nothing about it needs a prompt.
VESSEL_KEY = "vessel_pronoun"

# The key a dialect-spelling correction carries (dinnae against dinna).
DIALECT_KEY = "dialect_spelling"

# The key a dictionary-decided compound correction carries (sat phone ->
# satphone). Separate from CONSISTENCY_KEY because the channel differs: the
# term scan asks, and this one corrects on the dictionary's authority.
COMPOUND_KEY = "compound_style"


@dataclass(frozen=True)
class VesselPronounDrift:
    """A named vessel the book pronouns as *she*, and the *it/its* strays in
    sentences that name her. The deity scan's shape with the polarity flipped:
    there the book's convention is a capital, here it is a gender, and in both
    cases the evidence is the book's own counts.

    `vessel` is the name as the book writes it; `feminine` and `neuter` are the
    pronoun counts in sentences naming her — the numbers the explanation
    cites."""
    vessel: str
    feminine: int
    neuter: int
    outliers: tuple[Occurrence, ...]


@dataclass(frozen=True)
class DialectVariants:
    """One dialect marker spelled more than one way inside dialect speech —
    *dinnae* ×20 against *dinna* ×6, *cannae* ×10 against *canna* ×1. Counted
    only over paragraphs that are plainly in dialect (see
    ``find_dialect_variants``), because every one of these spellings is a
    misspelling in ordinary prose and none of them is this scan's business
    there.

    `enforce` is the dominance test: a majority leading `min_dominance`:1 is the
    book answering the question itself, and the strays are corrected. A closer
    split is one query, at the first minority site."""
    key: str                              # the family: "dinnae", "ye", "no’"
    counts: Counter                       # spelling -> times seen in dialect
    dominant: str
    enforce: bool
    outliers: tuple[Occurrence, ...]
    kind: str = "dialect"


@dataclass(frozen=True)
class CompoundPreference:
    """A compound the dictionary closes (*satphone*, *website*) that this book
    also writes open or hyphenated. The one place the term scan's "ask, never
    correct" rule gives way: it asks because a key-folding scan cannot tell an
    inconsistency from a distinction, and here the dictionary has already told
    it. Dominance is not consulted — *sat phone* ×4 against *satphone* ×3 never
    reaches the term scan's bar, and the table settles it anyway."""
    key: str                              # folded key: "satphone"
    counts: Counter                       # representative form -> times seen
    preferred: str                        # the closed form the dictionary sets
    note: str                             # the dictionary phrasing to cite
    outliers: tuple[Occurrence, ...]      # every occurrence not already closed


@dataclass(frozen=True)
class FigureDrift:
    """One recurring figure written two ways — a bearing printed "282.6°" nine
    times and "282.8°" once, with "A perfect match." on the line after.

    Queries only, and not because the evidence is thin: house policy is that a
    numeric value is never changed to repair a contradiction. Which of two
    figures is the right one is not something a count can know, and quietly
    rewriting the rare one would destroy the only evidence the author has that
    the two disagree."""
    key: str                              # unit + integer part: "282°"
    counts: Counter                       # figure as written -> times seen
    majority: str
    outliers: tuple[Occurrence, ...]


@dataclass(frozen=True)
class ConsistencyReport:
    ran: bool = False
    terms: tuple[Inconsistency, ...] = ()
    names: tuple[NameDrift, ...] = ()
    variants: tuple[VariantGroup, ...] = ()        # spelling variants (VarCon)
    abbreviations: tuple[VariantGroup, ...] = ()   # U.S. vs US, a.m. vs AM
    casings: tuple[VariantGroup, ...] = ()         # NASA vs Nasa
    accents: tuple[VariantGroup, ...] = ()         # si vs sí, senor vs señor
    policy: tuple[VariantGroup, ...] = ()          # non-US forms, policy "us"
    deity: DeityPronounDrift | None = None         # he->He in a reverent book
    times: TimeStyleDrift | None = None            # "at 8" in an "11:00" book
    case_splits: tuple[CaseSplit, ...] = ()        # earth/Earth outside sentence starts
    vessels: tuple[VesselPronounDrift, ...] = ()   # "Its wings" on a ship called she
    dialect: tuple[DialectVariants, ...] = ()      # dinnae/dinna, ye/yeh
    compounds: tuple[CompoundPreference, ...] = ()  # sat phone -> satphone
    figures: tuple[FigureDrift, ...] = ()          # 282.6° ×9 against 282.8° ×1
    callbacks: tuple = ()                          # callbacks.Callback rows

    @property
    def _mechanical(self) -> tuple[VariantGroup, ...]:
        return self.variants + self.abbreviations + self.casings + self.accents

    @property
    def flagged(self) -> int:
        # Terms, non-enforced names, deity strays, time strays and figure
        # strays are per-occurrence; the mechanical, policy and dialect scans
        # are one query per group.
        return (sum(len(t.outliers) for t in self.terms)
                + sum(len(n.outliers) for n in self.names if not n.enforce)
                + len(self._mechanical) + len(self.policy)
                + (len(self.deity.outliers) if self.deity else 0)
                + (len(self.times.outliers) if self.times else 0)
                + sum(len(c.outliers) for c in self.case_splits if not c.clear)
                + sum(1 for d in self.dialect if not d.enforce)
                + sum(len(f.outliers) for f in self.figures)
                + sum(1 for c in self.callbacks if c.kind != "misquote"))

    @property
    def corrected(self) -> int:
        return (sum(len(n.outliers) for n in self.names if n.enforce)
                + sum(len(c.outliers) for c in self.case_splits if c.clear)
                + sum(len(v.outliers) for v in self.vessels)
                + sum(len(d.outliers) for d in self.dialect if d.enforce)
                + sum(len(c.outliers) for c in self.compounds)
                + sum(1 for c in self.callbacks if c.kind == "misquote"))


def _key(form: str) -> str:
    # NFC first: composed and decomposed spellings of one accent must be one
    # key, and one length for min_length. The accents themselves stay — café
    # against cafe may be a loanword against its anglicization, two deliberate
    # choices, so the term scan never accent-folds. Whether an accent
    # difference is drift is find_name_drift's question, asked only of proper
    # names, where English has no such minimal pairs.
    form = unicodedata.normalize("NFC", form)
    return re.sub(r"[-\s’']", "", form).lower()


def _structure(form: str) -> str:
    """Case- and apostrophe-folded, but otherwise structure-preserving.

    Unlike ``_key`` this *keeps* spaces and hyphens, so *blood cursed*,
    *blood-cursed* and *bloodcursed* stay three distinct structures while
    *You should* and *you should* — and *ANIMALS* and *animals* — become one.
    That is the whole distinction this scan is allowed to flag: a structural
    difference between spellings of a term, never a capitalization or a
    straight-versus-curly apostrophe difference. English capitalizes the first
    word of every sentence, so almost any common word appears both lowercased
    and sentence-initial; folding case here is what keeps that from reading as
    an inconsistency. Normalization form is folded for the same reason: a
    composed and a decomposed café render identically, and a difference no
    reader can see is not a difference this scan may report.

    A *form-final* possessive apostrophe is folded away for the same reason.
    *mothers* against *mother's* against *mothers'* is a plural against a
    possessive — grammatically different words that legitimately coexist, not
    one term written two ways — so they collapse to a single structure and the
    group never reaches the dominance test. An apostrophe *internal* to the
    term, anchored by a following word (*Krebs' Cycle* against *Krebs Cycle*,
    *farmer's market* against *farmers market*), is one fixed term written two
    ways; it stays a distinct structure and still flags. The fold runs at the
    very end of the whole form, which may carry spaces or hyphens, so *the
    mother's* folds to *the mothers* while *krebs' cycle* is left untouched.

    Known limitation: *Krebs's Cycle* against *Krebs' Cycle* — the s's-versus-s'
    style choice — is not caught, because the extra s lands the two forms in
    different ``_key`` buckets before this ever runs."""
    s = unicodedata.normalize("NFC", form).lower().replace("’", "'")
    # Fold a form-final possessive: mother's and mothers' both become mothers.
    # s' before 's, so a possessive that also carries a trailing closing quote
    # (mother's’ from dialogue) folds all the way rather than stalling halfway.
    s = re.sub(r"s'$", "s", s)
    s = re.sub(r"'s$", "s", s)
    return s


def _fold_accents(s: str) -> str:
    """Diacritics stripped: Rían and Rian fold to the same key."""
    return "".join(ch for ch in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(ch))


# A trailing possessive is not part of the name: Rían's and Rían are one name,
# and trimming it here both merges their counts and keeps a correction from
# touching the clitic.
_POSSESSIVE = re.compile(r"(?:[’']s|[’'])$")

# The sentence boundary ``sweeps.sentence_window`` quotes by, repeated here so a
# span these scans measure and the window a finding quotes cannot disagree about
# where a sentence ends. (Kept as a copy rather than an import of a private
# name; the one-line pattern is the contract.)
_SENTENCE_END = re.compile(r"[.!?…][\"”’')\]]*\s+")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Every sentence in `text` as a (start, end) pair, trailing whitespace
    excluded, split exactly where ``sentence_window`` splits. A scan that needs
    to count what shares a sentence — pronouns against a vessel's name, a
    remembered line against the line it remembers — needs the sentences
    themselves, not one window at a time."""
    bounds = [0] + [m.end() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    spans: list[tuple[int, int]] = []
    for lo, hi in zip(bounds, bounds[1:]):
        end = lo + len(text[lo:hi].rstrip())
        if end > lo:
            spans.append((lo, end))
    return spans


def _match_case(target: str, source: str) -> str:
    """`target` set the way `source` was, so a correction at a sentence start
    keeps its capital and a shouted one keeps its shout."""
    if not source:
        return target
    if source.isupper() and len(source) > 1:
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


@dataclass
class _Group:
    counts: Counter = field(default_factory=Counter)
    where: list[Occurrence] = field(default_factory=list)


def find_name_drift(paragraphs: Sequence[ParagraphRef], *,
                    min_dominance: int = 5,
                    min_count: int = 20) -> tuple[NameDrift, ...]:
    """Proper names spelled with and without their diacritics.

    A candidate is a capitalized word of three letters or more that never
    appears lowercased anywhere in the manuscript — a word that does is
    ordinary English (*exposé* the noun against *expose* the verb), and
    ordinary English is not this scan's to touch. Two candidates that differ
    only in their diacritics are one name spelled two ways.

    The strays are corrected rather than asked about when the evidence is
    lopsided enough to answer the question itself: the dominant spelling
    appears at least `min_count` times, outnumbers every minority spelling
    `min_dominance` times over, and never shares a sentence with a stray —
    sharing one is the pattern of two similarly-named characters interacting,
    and the signal to ask. A group short of that bar carries enforce=False
    and goes to the author as a question, same as the terms."""
    by_id = {p.para_id: p for p in paragraphs}
    groups: dict[str, _Group] = defaultdict(_Group)
    lowercased: set[str] = set()
    for para in paragraphs:
        for m in _WORD.finditer(para.text):
            form, start = m.group(0), m.start()
            form = _POSSESSIVE.sub("", form)
            if len(form) < 3:
                continue
            key = _fold_accents(_structure(form))
            if form[0].islower():
                lowercased.add(key)
                continue
            g = groups[key]
            g.counts[form] += 1
            g.where.append(Occurrence(para.para_id, start,
                                      start + len(form), form))

    names: list[NameDrift] = []
    for key, g in sorted(groups.items()):
        if key in lowercased:
            continue
        # Sub-bucket by structure, exactly as the term scan does: RIAN in a
        # heading and Rian in prose are one spelling, not two. Two structures
        # under one accent-folded key differ in their diacritics and nothing
        # else — the key construction guarantees it, and that guarantee is
        # the whole reason correcting is safe here.
        buckets: dict[str, Counter] = defaultdict(Counter)
        for form, n in g.counts.items():
            buckets[_structure(form)][form] += n
        if len(buckets) < 2:
            continue
        totals = {s: sum(c.values()) for s, c in buckets.items()}
        reps = {s: min(c, key=lambda f, c=c: (-c[f], f))
                for s, c in buckets.items()}
        dom_struct = max(totals, key=lambda s: (totals[s], s))
        dom_total = totals[dom_struct]
        minority = set(totals) - {dom_struct}
        outliers = tuple(o for o in g.where
                         if _structure(o.form) in minority)
        dom_forms = tuple(buckets[dom_struct])
        enforce = (dom_total >= min_count
                   and all(dom_total >= totals[s] * min_dominance
                           for s in minority)
                   and not _share_a_sentence(outliers, dom_forms, by_id))
        counts = Counter({reps[s]: totals[s] for s in totals})
        names.append(NameDrift(key, counts, reps[dom_struct],
                               outliers, enforce))
    return tuple(names)


def _share_a_sentence(outliers: Sequence[Occurrence],
                      dom_forms: Sequence[str], by_id: dict) -> bool:
    """Whether any stray spelling sits in one sentence with the dominant one."""
    for o in outliers:
        para = by_id.get(o.para_id)
        if para is None:
            continue
        window, lo, _ = sentence_window(para.text, o.start, o.end)
        rest = window[:o.start - lo] + window[o.end - lo:]
        if any(f in rest for f in dom_forms):
            return True
    return False


#
# These three are what a key-folding compound scan structurally cannot do:
# grey/gray differ by a letter, not by hyphenation, so _key never groups them;
# U.S./US differ by punctuation the word tokenizer discards; NASA/Nasa differ by
# a capital that _structure deliberately folds away. Each reads the whole book,
# counts every spelling of one thing, and — like the term scan — only ASKS,
# never corrects: which variant a book uses is the author's to settle.


@lru_cache(maxsize=1)
def _load_varcon() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """(form -> cluster id, cluster id -> members). The cluster id is the
    American spelling (the table's first column). Returns empty maps, and warns
    once, if the table is missing — the scan then simply finds nothing."""
    forms: dict[str, str] = {}
    members: dict[str, tuple[str, ...]] = {}
    try:
        text = (_CONSISTENCY_DIR / "varcon.tsv").read_text(encoding="utf-8")
    except OSError:
        log.warning("No varcon.tsv found at %s; the spelling-variant scan is "
                    "skipped. Run tools/build_varcon.py to generate it.",
                    _CONSISTENCY_DIR)
        return {}, {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cluster = tuple(f.lower() for f in line.split("\t") if f)
        if len(cluster) < 2:
            continue
        cid = cluster[0]
        members[cid] = cluster
        for f in cluster:
            forms.setdefault(f, cid)
    return forms, members


@lru_cache(maxsize=1)
def _load_chicago() -> tuple[dict, dict]:
    """(class notes, per-form notes) from chicago.yaml, or empty maps."""
    try:
        import yaml
        data = yaml.safe_load(
            (_CONSISTENCY_DIR / "chicago.yaml").read_text(encoding="utf-8"))
    except Exception:                             # missing or malformed: not fatal
        return {}, {}
    data = data or {}
    return (data.get("classes") or {}), (data.get("forms") or {})


@lru_cache(maxsize=1)
def _load_closed_compounds() -> dict[str, tuple[str, str]]:
    """The compounds the dictionary closes, keyed the way ``_key`` keys a term:
    folded key -> (closed spelling, the note a correction cites).

    Two tables feed it. ``closed_compounds.yaml``, whose whole subject this is,
    and — narrowly — a single-word entry in ``chicago.yaml``'s `forms` map whose
    note actually says the word is set closed, so a preference already recorded
    there needs no second home. The narrowness matters: most of that map is
    British-against-American letter variants (gray, toward), which are the query
    channel's business and not a spacing question at all. A missing or malformed
    file leaves the map empty and the scan silent."""
    out: dict[str, tuple[str, str]] = {}
    for form, note in (_load_chicago()[1] or {}).items():
        form, note = str(form), str(note)
        if (form and not re.search(r"[-\s]", form)
                and re.search(r"\b(closed|one word|solid)\b", note, re.I)):
            out.setdefault(_key(form), (form.lower(), note))
    try:
        import yaml
        data = yaml.safe_load(
            (_CONSISTENCY_DIR / "closed_compounds.yaml").read_text(
                encoding="utf-8")) or {}
    except Exception:                             # missing or malformed: not fatal
        data = {}
    for form, note in (data.get("forms") or {}).items():
        form = str(form)
        if form and not re.search(r"[-\s]", form):
            out[_key(form)] = (form.lower(), str(note))
    return out


def _closed_compound(key: str, structures: Sequence[str]) -> tuple[str, str] | None:
    """The closed spelling the dictionary sets for this group, or None.

    The group has to be a spacing/hyphenation contest and nothing else: every
    structure must reduce to `key` by deleting spaces and hyphens alone. An
    apostrophe in the mix (*farmer's market*) is a different question and is left
    to the query channel, where the term scan already asks it."""
    known = _load_closed_compounds().get(key)
    if known is None:
        return None
    if any(re.sub(r"[-\s]", "", s) != key for s in structures):
        return None
    return known


def _variant_class(american: str, british: str) -> str:
    """Which regular British/American family a two-spelling cluster belongs to,
    inferred from the spellings themselves so no class tag has to be stored.
    "" for the irregular one-off pairs, which carry their note in chicago.yaml's
    `forms` map instead."""
    a, b = american, british
    if a.endswith("or") and b == a[:-2] + "our":
        return "our"
    if a.endswith("ize") and b == a[:-3] + "ise":
        return "ize"
    if a.endswith("yze") and b == a[:-3] + "yse":
        return "yze"
    if a.endswith("se") and b == a[:-2] + "ce":
        return "ce"
    if a.endswith("er") and b == a[:-2] + "re":
        return "re"
    if b == a + "ue":
        return "ogue"
    if len(b) == len(a) + 1:                      # single-l vs doubled-l
        i = 0
        while i < len(a) and a[i] == b[i]:
            i += 1
        if b[i:i + 1] == "l" and b[i + 1:] == a[i:]:
            return "ll"
    return ""


def _chicago_note(cid: str, members: tuple[str, ...]) -> str:
    classes, forms = _load_chicago()
    if cid in forms:
        return forms[cid]
    other = next((m for m in members if m != cid), "")
    return classes.get(_variant_class(cid, other), "")


def _skip_caps_context(para: ParagraphRef) -> bool:
    """A paragraph whose capitals are styling, not spelling: a heading, or a
    line set mostly in capitals. An all-caps token here says nothing about how
    the author capitalizes the word in running prose."""
    style = (para.style or "").lower()
    if "head" in style or "title" in style:
        return True
    letters = [c for c in para.text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        return True
    return False


def _pick_dominant(counts: Mapping[str, int], *, prefer: str = "") -> str:
    """The most-used key, ties broken toward `prefer` then alphabetically, so
    the recommendation is deterministic run to run."""
    return max(counts, key=lambda k: (counts[k], k == prefer, k))


def _has_majority(counts: Mapping[str, int], dom: str, min_dominance: int) -> bool:
    return all(counts[dom] >= n * min_dominance
               for k, n in counts.items() if k != dom)


def _cap(groups: list[VariantGroup], kind: str, limit: int) -> list[VariantGroup]:
    if limit and len(groups) > limit:
        log.info("Consistency %s queries capped at %d (%d found); raise "
                 "consistency.max_queries_per_kind to see the rest.",
                 kind, limit, len(groups))
        return groups[:limit]
    return groups


def find_spelling_variants(paragraphs: Sequence[ParagraphRef], *,
                           min_dominance: int = 2,
                           respell: Mapping[str, str] | None = None,
                           protected: Sequence[str] = (),
                           chicago: bool = True,
                           max_queries: int = 40) -> tuple[VariantGroup, ...]:
    """Different-letter spellings of one word (grey/gray, toward/towards),
    grouped through the VarCon table.

    A cluster whose forms the active variant already enforces (its respell map
    converts, e.g. grey->gray on a U.S. run) is skipped: that site is the
    adjudication pass's to correct, and asking about it too would be a question
    beside a change. An occurrence the spell scan protected as the author's own
    word is skipped, and a form capitalized mid-sentence is treated as a name
    (Mr. Grey, Earl Grey), not a spelling of the common word."""
    forms_map, members_map = _load_varcon()
    if not forms_map:
        return ()
    respell_keys = {k.lower() for k in (respell or {})}
    protected_l = {w.lower() for w in protected}

    by_cluster: dict[str, dict] = {}
    for para in paragraphs:
        for m in _WORD.finditer(para.text):
            raw = m.group(0)
            w = raw.lower().strip("’'")
            cid = forms_map.get(w)
            if cid is None:
                continue
            g = by_cluster.setdefault(
                cid, {"counts": Counter(), "sites": [], "skip": False})
            if w in respell_keys:
                g["skip"] = True
            if w in protected_l:
                continue
            if raw[:1].isupper() and not _sentence_initial(para.text, m.start()):
                continue
            g["counts"][w] += 1
            g["sites"].append(
                Occurrence(para.para_id, m.start(), m.start() + len(raw), raw))

    out: list[VariantGroup] = []
    for cid in sorted(by_cluster):
        g = by_cluster[cid]
        counts: Counter = g["counts"]
        if g["skip"] or len(counts) < 2:
            continue
        dom = _pick_dominant(counts, prefer=cid)
        minority = {f for f in counts if f != dom}
        site = next((o for o in g["sites"]
                     if o.form.lower().strip("’'") in minority), g["sites"][0])
        note = _chicago_note(cid, members_map.get(cid, ())) if chicago else ""
        out.append(VariantGroup(
            "spelling", cid, Counter(counts), dom,
            _has_majority(counts, dom, min_dominance), site,
            sum(counts[f] for f in minority), note))
    return tuple(_cap(out, "spelling-variant", max_queries))


def find_variant_policy(paragraphs: Sequence[ParagraphRef], *,
                        respell: Mapping[str, str] | None = None,
                        protected: Sequence[str] = (),
                        chicago: bool = True,
                        max_queries: int = 40) -> tuple[VariantGroup, ...]:
    """Words this book spells the British way THROUGHOUT (theatre, colour) —
    the case the mixed-usage scan structurally cannot see, raised only when
    ``consistency.variant_policy`` is "us".

    One query per cluster, proposing the American spelling. Restricted to the
    regular British/American families (_variant_class: -our, -ise, -re, …), so
    a form Merriam-Webster accepts in U.S. prose anyway (towards, grey as a
    name) is never flagged as policy. A cluster the book uses BOTH ways is the
    mixed-usage scan's to ask about, not this one's — two queries about one
    word is one too many."""
    forms_map, members_map = _load_varcon()
    if not forms_map:
        return ()
    respell_keys = {k.lower() for k in (respell or {})}
    protected_l = {w.lower() for w in protected}

    by_cluster: dict[str, dict] = {}
    for para in paragraphs:
        for m in _WORD.finditer(para.text):
            raw = m.group(0)
            w = raw.lower().strip("’'")
            cid = forms_map.get(w)
            if cid is None:
                continue
            g = by_cluster.setdefault(
                cid, {"counts": Counter(), "sites": [], "skip": False})
            if w in respell_keys or w in protected_l:
                g["skip"] = True
                continue
            if raw[:1].isupper() and not _sentence_initial(para.text, m.start()):
                continue
            g["counts"][w] += 1
            g["sites"].append(
                Occurrence(para.para_id, m.start(), m.start() + len(raw), raw))

    out: list[VariantGroup] = []
    for cid in sorted(by_cluster):
        g = by_cluster[cid]
        counts: Counter = g["counts"]
        # Policy speaks only where the book never uses the American form at
        # all; mixed usage already gets the mixed-usage query.
        if g["skip"] or not counts or cid in counts:
            continue
        other = next(iter(counts))
        if not _variant_class(cid, other):
            continue
        note = _chicago_note(cid, members_map.get(cid, ())) if chicago else ""
        out.append(VariantGroup(
            "policy", cid, Counter(counts), cid, True, g["sites"][0],
            sum(counts.values()), note))
    return tuple(_cap(out, "variant-policy", max_queries))



# The pronouns reverent capitalization applies to, and the names that anchor a
# sentence to God plainly enough for a query to be worth the margin space.
_DEITY_PRONOUNS = frozenset({"he", "his", "him", "himself"})
_DEITY_CAPS = frozenset({"He", "His", "Him", "Himself"})
_DEITY_NAME = re.compile(
    r"\b(?:God|Lord|Jesus|Christ|Almighty|Savior|Saviour|Messiah|"
    r"Holy Spirit|Heavenly Father)\b")


def find_deity_pronouns(paragraphs: Sequence[ParagraphRef], *,
                        min_capitalized: int = 8,
                        max_queries: int = 25) -> DeityPronounDrift | None:
    """Lowercase he/his/him in a book that capitalizes pronouns referring to
    God — "He also sees the struggles we go through, and he knows every
    decision" — raised as queries, because only the author can say which
    pronouns are His.

    Self-gating: the scan speaks only when the book plainly follows reverent
    capitalization (at least ``min_capitalized`` mid-sentence He/His/Him and a
    few explicit deity names), and only flags a lowercase pronoun whose own
    PARAGRAPH names God — or whose own sentence carries a mid-sentence
    capitalized deity pronoun; anything farther from an anchor is guesswork.
    ("God is patient with us! He also sees…, and he knows every decision" —
    the name is a sentence back, the paragraph is the anchor that catches it.)
    Chicago itself lowercases deity
    pronouns; this scan enforces nothing, it keeps the book consistent with
    the convention the book already chose."""
    capitalized = 0
    names = 0
    candidates: list[Occurrence] = []
    for para in paragraphs:
        text = para.text
        if _skip_caps_context(para):
            continue
        names += len(_DEITY_NAME.findall(text))
        for m in _WORD.finditer(text):
            w = m.group(0)
            if w in _DEITY_CAPS and not _sentence_initial(text, m.start()):
                capitalized += 1
            elif w in _DEITY_PRONOUNS:
                candidates.append(
                    Occurrence(para.para_id, m.start(), m.start() + len(w), w))
    if capitalized < min_capitalized or names < 3:
        return None

    by_id = {p.para_id: p for p in paragraphs}
    outliers: list[Occurrence] = []
    for o in candidates:
        text = by_id[o.para_id].text
        sentence, lo, _occ = sentence_window(text, o.start, o.end)
        anchored = bool(_DEITY_NAME.search(text)) or any(
            m.group(0) in _DEITY_CAPS
            and not _sentence_initial(text, lo + m.start())
            for m in _WORD.finditer(sentence))
        if anchored:
            outliers.append(o)
    if not outliers:
        return None
    if len(outliers) > max_queries:
        log.info("Deity-pronoun queries capped at %d (%d found).",
                 max_queries, len(outliers))
        outliers = outliers[:max_queries]
    return DeityPronounDrift(capitalized, tuple(outliers))



# The same shape as the deity scan, one convention over: a ship the book calls
# *she* and once or twice calls *it*.
_FEMININE_PRONOUNS = frozenset({"she", "her", "hers", "herself"})
_NEUTER_PRONOUNS = frozenset({"it", "its", "itself"})
# What each stray becomes. "it" is the only one that depends on its position:
# subject "it" is *she*, an object or a preposition's complement is *her*.
_NEUTER_FIX = {"its": "her", "itself": "herself"}
# What a subject pronoun can follow — nothing (a sentence start), a coordinator,
# or a subordinator. Anything else (a preposition, a verb) makes the pronoun an
# object, where the feminine form is "her" rather than "she".
_SUBJECT_LEADERS = frozenset("""
and but or so yet then that which who because if when while whilst though
although since until unless before after as where whether nor
""".split())
# What a person does and a ship does not. A candidate the book ever puts in front
# of one of these is somebody, and somebody is never an "it" — which makes this
# the cheapest guard available against correcting the pronouns of "the Captain".
_PERSON_VERBS = frozenset("""
said says asked asks replied replies answered answers shouted whispered muttered
murmured laughed smiled nodded shrugged sighed grinned frowned agreed admitted
wondered thought knew believed remembered decided explained added continued
told asked demanded insisted repeated promised swore
""".split())


def find_vessel_pronouns(paragraphs: Sequence[ParagraphRef], *,
                         min_feminine: int = 5,
                         min_mentions: int = 8,
                         max_queries: int = 25) -> tuple[VesselPronounDrift, ...]:
    """A named vessel the book pronouns as *she*, and the *it/its* strays.

    The Cooper QA: a ship called the Dutchwoman carries "Her vast sails" and
    "She hung in the center of the lens" for four hundred pages, and then "Its
    wings curved forward" and "Its systems couldn't reconcile" — invisible to a
    per-paragraph read, which sees a perfectly ordinary neuter pronoun beside a
    perfectly ordinary noun.

    Self-gating in three steps, because the whole risk here is a pronoun that
    refers to something else in the same sentence. A candidate is a capitalized
    word the book introduces with "the" at least `min_mentions` times away from
    a sentence start — the shape of a named vessel (*the Dutchwoman*), not of a
    person. A candidate is feminine only when the sentences that name it carry
    at least `min_feminine` she/her/hers/herself AND outnumber their it/its by
    three to one; anything closer is a book that pronouns the thing both ways
    on purpose, or a candidate that is not a vessel at all. Then every
    it/its/itself in a sentence naming her is a stray.

    Corrections, like the case-split scan's: the counts answer the question a
    query would ask, and the caller that reads this (Galley's fixed workflow)
    screens every proposal in context before any of it reaches the document —
    which is also the screen for the one thing this scan cannot see, an "it"
    that means the console rather than the ship."""
    usable = [(p.para_id, p.text, _sentence_spans(p.text))
              for p in paragraphs if not _skip_caps_context(p)]
    mentions: Counter = Counter()
    people: set[str] = set()
    for _pid, text, _spans in usable:
        for m in _WORD.finditer(text):
            form, start, end = _trim_quote(m.group(0), m.start(), m.end())
            form = _POSSESSIVE.sub("", form)
            end = start + len(form)
            if (len(form) < 4 or _case_shape(form) != "title"
                    or _sentence_initial(text, start)):
                continue
            if _word_before(text, start) != "the":
                continue
            mentions[form] += 1
            # "the Captain said", "the Commodore nodded" — a candidate that acts
            # like a person is a person, and no person is ever an "it".
            after = _WORD.search(text[end:end + 24] or "")
            if after and after.group(0).lower() in _PERSON_VERBS:
                people.add(form)
    candidates = [f for f, n in mentions.items()
                  if n >= min_mentions and f not in people]
    if not candidates:
        return ()

    drifts: list[VesselPronounDrift] = []
    budget = max_queries
    for vessel in sorted(candidates):
        named = re.compile(r"\b" + re.escape(vessel) + r"(?:[’']s)?\b")
        feminine = neuter = 0
        strays: list[Occurrence] = []
        for para_id, text, spans in usable:
            for lo, hi in spans:
                sentence = text[lo:hi]
                if not named.search(sentence):
                    continue
                for m in _WORD.finditer(sentence):
                    w = m.group(0).lower()
                    if w in _FEMININE_PRONOUNS:
                        feminine += 1
                    elif w in _NEUTER_PRONOUNS:
                        neuter += 1
                        strays.append(Occurrence(
                            para_id, lo + m.start(),
                            lo + m.end(), m.group(0)))
        if feminine < min_feminine or feminine < neuter * 3 or not strays:
            continue
        if budget <= 0:
            log.info("Vessel-pronoun corrections capped at %d.", max_queries)
            break
        strays = strays[:budget]
        budget -= len(strays)
        drifts.append(VesselPronounDrift(vessel, feminine, neuter,
                                         tuple(strays)))
    # One stray belongs to one vessel: when two candidates share a sentence the
    # first by name wins, so the same span is never corrected twice.
    seen: set[tuple[str, int]] = set()
    kept: list[VesselPronounDrift] = []
    for d in drifts:
        strays = tuple(o for o in d.outliers
                       if (o.para_id, o.start) not in seen)
        seen.update((o.para_id, o.start) for o in strays)
        if strays:
            kept.append(VesselPronounDrift(d.vessel, d.feminine, d.neuter,
                                           strays))
    return tuple(kept)


def vessel_fix(text: str, o: Occurrence) -> str:
    """The feminine form a stray becomes: *its*->her, *itself*->herself, and
    *it*->she when it is a subject, *her* when it is not. Case is carried over,
    so a sentence-initial "It" becomes "She"."""
    lower = o.form.lower()
    if lower in _NEUTER_FIX:
        return _match_case(_NEUTER_FIX[lower], o.form)
    subject = (_sentence_initial(text, o.start)
               or _word_before(text, o.start) in _SUBJECT_LEADERS)
    return _match_case("she" if subject else "her", o.form)



# An H:MM time anywhere in the book: the style evidence.
_TIME_WITH_MINUTES = re.compile(r"\b\d{1,2}:[0-5]\d\b")
# A bare hour with a meridiem attached ("11 a.m.", "2 PM") — the digits must
# not continue an H:MM form, so ":00 a.m." never matches its own minutes.
_BARE_HOUR_MERIDIEM = re.compile(
    r"(?<![\d:.])\b(\d{1,2})[  ]*(?=(?:[ap]\.m\.|[AP]\.?M\.?|[ap]m\b))")
# A bare hour a time preposition introduces ("around 4", "at 8"). The digits
# must not open an H:MM form, a number range, an ordinal, or a counted noun.
_BARE_HOUR_PREP = re.compile(
    r"\b(?:at|around|by|until|till|before|after|past)[  ]+(\d{1,2})\b"
    r"(?![  ]*[:%\d–-])(?!(?:st|nd|rd|th))")
# The word after the digits that says "this is a quantity, not a clock":
# "after 10 minutes", "by 5 percent", "at 8 years old", "at 2 o'clock" (a
# deliberate spelled style this scan must not fight).
_HOUR_NOT_CLOCK = re.compile(
    r"^[  ]*(?:minutes?|mins?|hours?|hrs?|seconds?|days?|weeks?|months?|"
    r"years?|miles?|blocks?|percent|dollars?|bucks?|cents?|pounds?|kids?|"
    r"people|times?|more|of|o['’]clock)\b", re.IGNORECASE)


def find_time_style(paragraphs: Sequence[ParagraphRef], *,
                    min_with_minutes: int = 3,
                    max_queries: int = 25) -> TimeStyleDrift | None:
    """Bare clock hours in a book whose own style writes times with minutes.

    The Purpura head-proofreader pass added ":00" by hand to "around 4"-style
    hours because the book writes "11:00 a.m." everywhere else — an
    inconsistency only a whole-book read can see, which is exactly what this
    module is for. Self-gating: the scan speaks only when the book carries at
    least `min_with_minutes` H:MM times, and every catch is a query — a bare
    hour can be a deliberate register, so the book's own majority style is
    cited as evidence, never enforced silently."""
    with_minutes = 0
    example = ""
    candidates: list[Occurrence] = []
    seen_spans: set[tuple[str, int]] = set()
    for para in paragraphs:
        text = para.text
        for m in _TIME_WITH_MINUTES.finditer(text):
            with_minutes += 1
            example = example or m.group(0)
        for pat, group in ((_BARE_HOUR_MERIDIEM, 1), (_BARE_HOUR_PREP, 1)):
            for m in pat.finditer(text):
                digits = m.group(group)
                if not 1 <= int(digits) <= 12:
                    continue
                start = m.start(group)
                if (para.para_id, start) in seen_spans:
                    continue
                if (pat is _BARE_HOUR_PREP
                        and _HOUR_NOT_CLOCK.match(text[m.end(group):])):
                    continue
                seen_spans.add((para.para_id, start))
                candidates.append(Occurrence(
                    para.para_id, start, start + len(digits), digits))
    if with_minutes < min_with_minutes or not candidates:
        return None
    if len(candidates) > max_queries:
        log.info("Time-style queries capped at %d (%d found).",
                 max_queries, len(candidates))
        candidates = candidates[:max_queries]
    return TimeStyleDrift(with_minutes, example, tuple(candidates))



# A figure with a unit attached, or a currency amount. The unit alternation is
# longest-first so "km/h" and "kHz" are not read as "km" and "k". Case matters:
# "N" is newtons and "m" is metres, while "degrees" and "seconds" are words.
_FIGURE_UNITS = (
    "°", "degrees", "%", "km/h", "kHz", "MHz", "Hz", "km", "kg", "mph",
    "μrad", "µrad", "urad", "µN", "uN", "AU", "ly", "seconds", "secs", "sec",
    "N", "m", "g", "s",
)
_FIGURE = re.compile(
    r"(?<![\w.,])(?P<currency>[$€£])?(?P<sign>[-−])?"
    r"(?P<number>\d{1,4}(?:,\d{3})*(?:\.\d+)?)"
    r"(?:[    ]?(?P<unit>" + "|".join(_FIGURE_UNITS) + r"))?"
    r"(?!\w)")
# One spelling per unit, so "degrees" and "°" — or "sec" and "s" — are one
# recurring figure rather than two.
_UNIT_CANON = {"degrees": "°", "seconds": "s", "secs": "s", "sec": "s",
               "urad": "μrad", "µrad": "μrad", "uN": "µN"}


def find_figure_drift(paragraphs: Sequence[ParagraphRef], *,
                      min_majority: int = 3,
                      max_queries: int = 25) -> tuple[FigureDrift, ...]:
    """A recurring figure whose decimal wanders once.

    The Cooper QA: a bearing printed "282.6°" and "77.4°" nine times, and once
    "282.8°" and "77.2°" — followed, two words later, by "A perfect match." Two
    numbers that disagree are the single hardest thing for a per-paragraph read
    to see, because each of them is a perfectly well-formed number.

    Queries, always. House policy is that a numeric value is never changed to
    repair a contradiction: a count cannot know which of two figures is the
    right one, and silently rewriting the rare one would delete the only
    evidence the author has that they disagree. Self-gating on repetition — the
    majority figure must appear at least `min_majority` times and the stray
    fewer than a third as often, which is the shape of a slip rather than of two
    real measurements."""
    groups: dict[str, dict] = {}
    for para in paragraphs:
        if _skip_caps_context(para):
            continue
        for m in _FIGURE.finditer(para.text):
            unit, currency = m.group("unit"), m.group("currency")
            if not unit and not currency:
                continue
            number = m.group("number")
            if unit == "s" and re.fullmatch(r"[12]\d{3}", number):
                continue                      # "the 1980s" is not a duration
            unit = _UNIT_CANON.get(unit or "", unit or "")
            integer, _, decimal = number.replace(",", "").partition(".")
            sign = m.group("sign") or ""
            key = f"{currency or ''}{sign}{integer}{unit}"
            written = para.text[m.start():m.end()]
            g = groups.setdefault(key, {"counts": Counter(), "sites": [],
                                        "decimals": {}})
            g["counts"][written] += 1
            g["decimals"][written] = decimal
            g["sites"].append(Occurrence(
                para.para_id, m.start(), m.end(), written))

    out: list[FigureDrift] = []
    budget = max_queries
    for key in sorted(groups):
        g = groups[key]
        counts: Counter = g["counts"]
        if len(counts) < 2:
            continue
        majority = _pick_dominant(counts)
        maj_n = counts[majority]
        if maj_n < min_majority:
            continue
        # A stray is a figure whose DECIMAL differs and which is rare beside the
        # majority. A form that differs only in its thousands separator is a
        # number-style question, not a contradiction, and is left alone.
        # Both figures must carry a decimal. "6 m" beside "6.5 m" is two
        # measurements a reader cannot confuse; "282.6°" beside "282.8°" is one
        # measurement that moved, and that is the only shape worth a question.
        strays = {f for f, n in counts.items()
                  if f != majority and n * 3 < maj_n
                  and g["decimals"][f] and g["decimals"][majority]
                  and g["decimals"][f] != g["decimals"][majority]}
        if not strays:
            continue
        outliers = tuple(o for o in g["sites"] if o.form in strays)
        if budget <= 0:
            log.info("Figure-drift queries capped at %d.", max_queries)
            break
        outliers = outliers[:budget]
        budget -= len(outliers)
        out.append(FigureDrift(key, Counter(counts), majority, outliers))
    return tuple(out)



# Loanwords whose unaccented spelling is not an English word of its own, mapped
# to the accented form Merriam-Webster sets. Deliberately short: a pair where
# the bare spelling is accepted English (cafe, naive, resume) is a style choice
# this scan has no business flagging, and "ole" (good ole boy) is dialect.
# One query per word, at its first bare occurrence.
_ACCENT_LOANWORDS = {
    "si": "sí",
    "senor": "señor",
    "senora": "señora",
    "senorita": "señorita",
    "adios": "adiós",
    "manana": "mañana",
    "jalapeno": "jalapeño",
    "jalapenos": "jalapeños",
    "pinata": "piñata",
    "pinatas": "piñatas",
    "quinceanera": "quinceañera",
    "voila": "voilà",
    "touche": "touché",
    "fiance": "fiancé",
    "fiancee": "fiancée",
}


def find_accent_loanwords(paragraphs: Sequence[ParagraphRef], *,
                          protected: Sequence[str] = (),
                          max_queries: int = 40) -> tuple[VariantGroup, ...]:
    """A loanword written without the accent it wears in the dictionary —
    "Si!" for "Sí!", "senor" for "señor". The human pass restored the accent
    the model passes glided over (Purpura: Si -> Sí); this scan asks instead
    of correcting, because a bare spelling can be a romanization choice.
    A word in `protected` (the manuscript's own lexicon — "Si" as a name) is
    left alone. Counts of the accented spelling, when the book also uses it,
    ride along in the query as evidence."""
    protected_l = {w.lower() for w in protected}
    accented_of = dict(_ACCENT_LOANWORDS)
    bare_of = {v: k for k, v in _ACCENT_LOANWORDS.items()}
    groups: dict[str, dict] = {}
    for para in paragraphs:
        for m in _WORD.finditer(para.text):
            raw = m.group(0)
            w = unicodedata.normalize("NFC", raw.lower().strip("’'"))
            if w in accented_of and w not in protected_l:
                key = accented_of[w]
                g = groups.setdefault(key, {"counts": Counter(), "sites": []})
                g["counts"][w] += 1
                g["sites"].append(Occurrence(
                    para.para_id, m.start(), m.start() + len(raw), raw))
            elif w in bare_of:
                key = w
                g = groups.setdefault(key, {"counts": Counter(), "sites": []})
                g["counts"][w] += 1

    out: list[VariantGroup] = []
    for key in sorted(groups):
        g = groups[key]
        counts: Counter = g["counts"]
        bare = bare_of[key]
        if not counts.get(bare) or not g["sites"]:
            continue                      # only the accented form appears
        out.append(VariantGroup(
            "accent", key, Counter(counts), key,
            True, g["sites"][0], counts[bare]))
    return tuple(_cap(out, "accent-loanword", max_queries))



# Dialect. Every spelling below is a misspelling in ordinary prose, which is
# exactly why the scan first has to prove it is looking at dialect: two distinct
# markers in the paragraph, and only then does it count anything. Outside that
# gate an "o’" is a possessive artifact and a "ye" is Ye Olde signage.
_DIALECT_MARKER_WORDS = """
dinnae dinna dinny cannae canna tae ye yer aye wee ken isnae isna wasnae wasna
didnae didna wouldnae wouldna couldnae couldna havenae havena ain bairn yersel
yerself
""".split()
# Markers written with an elision mark: no’ (not), o’ (of), wi’ (with), an’ (and).
_DIALECT_MARKER_ELIDED = ("no", "o", "wi", "an")
_DIALECT_MARKER = re.compile(
    r"\b(?:" + "|".join(sorted(_DIALECT_MARKER_WORDS, key=len, reverse=True))
    + r")\b|\b(?:" + "|".join(_DIALECT_MARKER_ELIDED) + r")[’'](?![A-Za-z])",
    re.IGNORECASE)

# Variant spellings of ONE marker. Deliberately conservative: "canny" is a real
# English word and is not in the cannae family, and a family is only listed when
# both spellings are unambiguously the same dialect word.
_DIALECT_FAMILIES: dict[str, tuple[str, ...]] = {
    "dinnae": ("dinnae", "dinna", "dinny"),
    "cannae": ("cannae", "canna"),
    "havenae": ("havenae", "havena"),
    "isnae": ("isnae", "isna"),
    "wasnae": ("wasnae", "wasna"),
    "didnae": ("didnae", "didna"),
    "wouldnae": ("wouldnae", "wouldna"),
    "couldnae": ("couldnae", "couldna"),
    "ye": ("ye", "yeh"),
    "yersel": ("yersel", "yerself"),
}
# The elided-not family, whose two spellings are not two spellings of a word but
# a mark that is there or missing: "It’s no’ bad" against "It’s no bad".
_NO_FAMILY = "no’"
_NO_ELIDED = re.compile(r"\bno[’'](?![A-Za-z])")
# A bare "no" standing where the elided "not" would: after a copula, before a
# modifier. The word after is what tells a dialect "no" from an ordinary one.
_NO_BARE = re.compile(
    r"(?:\b(?:is|was|were|are|am)|[’'](?:s|re|m))\s+(no)\b\s+([A-Za-z’']+)",
    re.IGNORECASE)
# After "was no ___", these make the "no" ordinary English rather than an elided
# "not": a quantifier, a determiner, a pronoun, or one of the fixed phrases.
_NO_IS_ORDINARY = frozenset("""
one longer more matter doubt need use idea sign room question point harm trouble
sense man woman place name reason choice chance hope escape mistake friend help
business word words sound sign sight thing things body kind sort time times way
ways good telling knowing mistaking a an the my your his her its our their this
that these those them it him you me us stranger fool
""".split())


@lru_cache(maxsize=None)
def _dialect_family_re(family: str) -> re.Pattern:
    forms = sorted(_DIALECT_FAMILIES[family], key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(forms) + r")\b", re.IGNORECASE)


def _dialect_markers(text: str) -> set[str]:
    return {m.group(0).lower().replace("’", "'")
            for m in _DIALECT_MARKER.finditer(text)}


def find_dialect_variants(paragraphs: Sequence[ParagraphRef], *,
                          min_dominance: int = 3,
                          max_queries: int = 40) -> tuple[DialectVariants, ...]:
    """One dialect marker spelled more than one way inside dialect speech.

    The Cooper QA, all of it in Scots dialogue: *dinnae* ×20 against *dinna* ×6,
    *cannae* ×10 against *canna* ×1, *ye* everywhere against one *yeh*,
    *havenae* against *havena*. No per-paragraph read can see it — each spelling
    is fine on its own page — and no spell scan can either, because all of them
    are outside the dictionary.

    The scan counts nothing until a paragraph proves it is in dialect: at least
    two distinct markers from ``_DIALECT_MARKER``. Inside that gate, a family
    whose majority leads `min_dominance`:1 has answered the question itself and
    its strays become tracked edits (the fixed workflow screens every one); a
    closer split is a single query at the first minority site, because which
    spelling a dialect uses is the author's ear, not a scan's."""
    counts: dict[str, Counter] = defaultdict(Counter)
    sites: dict[str, list[tuple[str, Occurrence]]] = defaultdict(list)

    def record(family: str, para_id: str, start: int, end: int, raw: str,
               form: str) -> None:
        counts[family][form] += 1
        sites[family].append((form, Occurrence(para_id, start, end, raw)))

    for para in paragraphs:
        text = para.text
        if _skip_caps_context(para) or len(_dialect_markers(text)) < 2:
            continue
        for family in _DIALECT_FAMILIES:
            for m in _dialect_family_re(family).finditer(text):
                raw = m.group(0)
                record(family, para.para_id, m.start(), m.end(), raw,
                       raw.lower())
        for m in _NO_ELIDED.finditer(text):
            record(_NO_FAMILY, para.para_id, m.start(), m.end(), m.group(0),
                   "no’")
        for m in _NO_BARE.finditer(text):
            if m.group(2).lower().strip("’'") in _NO_IS_ORDINARY:
                continue
            record(_NO_FAMILY, para.para_id, m.start(1), m.end(1),
                   m.group(1), "no")

    out: list[DialectVariants] = []
    for family in sorted(counts):
        c = counts[family]
        if len(c) < 2:
            continue                          # one spelling: nothing to settle
        dominant = _pick_dominant(c, prefer=family)
        minority = {f for f in c if f != dominant}
        enforce = _has_majority(c, dominant, min_dominance)
        outliers = tuple(o for form, o in sites[family] if form in minority)
        if not outliers:
            continue
        out.append(DialectVariants(family, Counter(c), dominant, enforce,
                                   outliers))
    if max_queries and len(out) > max_queries:
        log.info("Dialect-variant findings capped at %d (%d found).",
                 max_queries, len(out))
        out = out[:max_queries]
    return tuple(out)


# A run of letter-then-dot (U.S., a.m., Ph.D.) with no spaces between the units,
# so spaced personal initials ("J. R. R.") never match as one token.
_DOTTED = re.compile(r"(?:[A-Za-z]\.){2,}")
# An undotted all-caps token, optionally pluralized (US, NASA, URLs). Lowercase
# is excluded on purpose, so the pronoun "us" and the verb "am" never join an
# abbreviation group.
_CAPS = re.compile(r"\b[A-Z]{2,6}s?\b")


def _abbr_key(form: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", form).lower()
    return letters[:-1] if len(letters) > 2 and letters.endswith("s") else letters


def find_abbreviation_variants(paragraphs: Sequence[ParagraphRef], *,
                               min_dominance: int = 2,
                               protected: Sequence[str] = (),
                               max_queries: int = 40) -> tuple[VariantGroup, ...]:
    """One abbreviation set two ways — dotted against undotted (U.S. / US),
    dotted-lowercase against capitals (a.m. / AM). A group is raised only when
    BOTH a dotted and an undotted spelling of the same letters occur, so a book
    that only ever writes "US" is left alone; the minority style is the query."""
    protected_l = {w.lower() for w in protected}
    groups: dict[str, dict] = {}
    for para in paragraphs:
        shout = _skip_caps_context(para)
        for pat, structure in ((_DOTTED, "dotted"), (_CAPS, "caps")):
            if structure == "caps" and shout:
                continue
            for m in pat.finditer(para.text):
                raw = m.group(0)
                key = _abbr_key(raw)
                if len(key) < 2:
                    continue
                g = groups.setdefault(
                    key, {"struct": defaultdict(Counter),
                          "sites": defaultdict(list)})
                g["struct"][structure][raw] += 1
                g["sites"][structure].append(
                    Occurrence(para.para_id, m.start(),
                               m.start() + len(raw), raw))

    out: list[VariantGroup] = []
    for key in sorted(groups):
        if key in protected_l:                    # the author's own term
            continue
        struct = groups[key]["struct"]
        if len(struct) < 2:                       # needs both dotted and caps
            continue
        totals = {s: sum(c.values()) for s, c in struct.items()}
        dom_struct = _pick_dominant(totals)
        minority_structs = {s for s in struct if s != dom_struct}
        counts = Counter({_rep(struct[s]): totals[s] for s in struct})
        dom_form = _rep(struct[dom_struct])
        site = groups[key]["sites"][min(minority_structs)][0]
        out.append(VariantGroup(
            "abbreviation", key, counts, dom_form,
            _has_majority(totals, dom_struct, min_dominance), site,
            sum(totals[s] for s in minority_structs)))
    return tuple(_cap(out, "abbreviation", max_queries))


def _rep(surfaces: Counter) -> str:
    """The most common surface spelling of one structure, for display."""
    return max(surfaces, key=lambda s: (surfaces[s], s))


_CAPS_TOKEN = re.compile(r"\b[A-Z]{2,6}\b")           # NASA
_TITLE_TOKEN = re.compile(r"\b[A-Z][a-z]{1,5}\b")     # Nasa
_LOWER_TOKEN = re.compile(r"\b[a-z]{2,6}\b")


def find_acronym_case(paragraphs: Sequence[ParagraphRef], *,
                      min_dominance: int = 2, dictionary: str = "en_US",
                      protected: Sequence[str] = (),
                      max_queries: int = 40) -> tuple[VariantGroup, ...]:
    """An initialism set in capitals in one place and as a title-cased word in
    another (NASA / Nasa). Safe to detect deterministically because the two
    spellings differ PAST the first letter — sentence position explains only the
    first capital, which is exactly why the term scan's case-fold hides this.

    The dictionary decides what is an acronym rather than a word: a key whose
    lowercasing is an ordinary English word (AIDS/aids, MASS/Mass) is left alone,
    because its title case may be that word at a sentence start. The scan needs
    the dictionary for that judgment, so with none loadable it declines."""
    dic = _dictionary(dictionary)
    if dic is None:
        return ()
    protected_l = {w.lower() for w in protected}

    caps: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, Counter] = defaultdict(Counter)
    lowers: set[str] = set()
    sites: dict[str, dict] = defaultdict(lambda: {"caps": [], "title": []})
    for para in paragraphs:
        shout = _skip_caps_context(para)
        for m in _LOWER_TOKEN.finditer(para.text):
            lowers.add(m.group(0))
        for m in _TITLE_TOKEN.finditer(para.text):
            key = m.group(0).lower()
            titles[key][m.group(0)] += 1
            sites[key]["title"].append(
                Occurrence(para.para_id, m.start(),
                           m.start() + len(m.group(0)), m.group(0)))
        if shout:
            continue
        for m in _CAPS_TOKEN.finditer(para.text):
            key = m.group(0).lower()
            caps[key][m.group(0)] += 1
            sites[key]["caps"].append(
                Occurrence(para.para_id, m.start(),
                           m.start() + len(m.group(0)), m.group(0)))

    out: list[VariantGroup] = []
    for key in sorted(caps):
        cap_total = sum(caps[key].values())
        title_total = sum(titles.get(key, {}).values())
        if cap_total < 2 or title_total < 1:
            continue
        if key in protected_l:                    # a name the spell scan owns
            continue
        if key in lowers or dic.lookup(key) or dic.lookup(key.capitalize()):
            continue                              # an ordinary word, not an acronym
        totals = {"caps": cap_total, "title": title_total}
        dom_struct = _pick_dominant(totals, prefer="caps")
        counts = Counter({_rep(caps[key]): cap_total,
                          _rep(titles[key]): title_total})
        dom_form = _rep(caps[key] if dom_struct == "caps" else titles[key])
        minority = "title" if dom_struct == "caps" else "caps"
        site = sites[key][minority][0]
        out.append(VariantGroup(
            "acronym_case", key, counts, dom_form,
            _has_majority(totals, dom_struct, min_dominance), site,
            totals[minority]))
    return tuple(_cap(out, "acronym-case", max_queries))


# A cheap, deterministic part-of-speech gate for the one compound false-positive
# the term scan floods on: an OPEN two-word compound used as a phrasal verb
# ("check in", "follow up") against its HYPHENATED twin used as a noun/modifier
# ("check-in", "follow-up"). Both are correct; they are not one term spelled two
# ways, and asking about each is noise (Purpura beta: ~24 such queries). A full
# POS tagger is not in the base install, so this reads the word immediately
# before each occurrence: a verb leader before the open form marks it a verb, a
# determiner before the hyphen form marks it a noun.
_VERB_LEADERS = frozenset("""
i you we they he she it who to please let will would can could shall should may
might must do does did been be being am is are was were and or then just gonna
wanna
""".split())
_DETERMINERS = frozenset("""
a an the this that these those my your our his her its their no any some each
every another one first second last next new same whole
""".split())


def _word_before(text: str, start: int) -> str:
    j = start
    while j > 0 and not text[j - 1].isalpha():
        j -= 1
    k = j
    while k > 0 and (text[k - 1].isalpha() or text[k - 1] in "'’-"):
        k -= 1
    return text[k:j].lower()


def _compound_pos_split(structs: dict, text_by_id: dict) -> bool:
    """True when the competing structures are exactly an OPEN vs HYPHENATED
    compound that reads as a verb/noun pair (check in / check-in) — a legitimate
    coexistence the scan must not flag. `structs` maps a `_structure` string to
    its list of Occurrences."""
    open_s = [s for s in structs if " " in s and "-" not in s]
    hyph_s = [s for s in structs if "-" in s and " " not in s]
    # Only the clean two-way open/hyphen contest — a third (closed) form could be
    # a real typo, so leave anything else to the ordinary dominance test.
    if len(structs) != 2 or not open_s or not hyph_s:
        return False
    verbal = any(_word_before(text_by_id.get(o.para_id, ""), o.start)
                 in _VERB_LEADERS
                 for s in open_s for o in structs[s])
    nominal = any(_word_before(text_by_id.get(o.para_id, ""), o.start)
                  in _DETERMINERS
                  for s in hyph_s for o in structs[s])
    return verbal and nominal


_CASE_DETERMINERS = frozenset(
    "a an the my your his her its our their this that these those".split())


def _case_shape(word: str) -> str | None:
    """"lower" for an all-lowercase word, "title" for a word capitalized only
    at its start (and after an internal hyphen or apostrophe: Band-Aid,
    O’Brien), None for anything else — ALLCAPS, camelCase, McCoy — which says
    nothing about how the author capitalizes the word in prose."""
    if word == word.lower():
        return "lower"
    if not word[0].isupper():
        return None
    for i in range(1, len(word)):
        if word[i].isupper() and word[i - 1] not in "-‐‑’'":
            return None
    return "title"


# Lowercase words that sit inside a capitalized name phrase without breaking
# it: "Atlas the Elephant", "Carve Surf & Coffee", "The Little Mermaid".
_NAME_CONNECTORS = frozenset("the of and for de la du von van".split())
_CONNECTOR_GAP = re.compile(r"\A (?:& )?\Z")


def _in_name_phrase(tokens, i, text) -> bool:
    """A Capitalized token whose neighbour — directly, or across one lowercase
    connector or an ampersand — is another Capitalized token that is not
    itself sentence-initial reads as part of a name phrase (Easy Speed, Aunt
    May, Atlas the Elephant, Carve Surf & Coffee). "The Earth" at a sentence
    start still counts Earth: the preceding capital is the sentence's."""
    form, start, end, shape, initial = tokens[i]

    def adjacent(a_end, b_start):
        return _CONNECTOR_GAP.match(text[a_end:b_start]) is not None

    def title_at(j, *, initial_ok):
        if not 0 <= j < len(tokens):
            return False
        jform, jstart, jend, jshape, jinitial = tokens[j]
        return jshape == "title" and (initial_ok or not jinitial)

    # Directly before / after.
    if i > 0 and title_at(i - 1, initial_ok=False) and adjacent(tokens[i - 1][2], start):
        return True
    if i + 1 < len(tokens) and title_at(i + 1, initial_ok=True) and adjacent(end, tokens[i + 1][1]):
        return True
    # Across one connector ("the", "of", "&").
    if i > 1:
        cform, cstart, cend, cshape, _ = tokens[i - 1]
        if (cform.lower() in _NAME_CONNECTORS and adjacent(cend, start)
                and title_at(i - 2, initial_ok=False) and adjacent(tokens[i - 2][2], cstart)):
            return True
    if i + 2 < len(tokens):
        cform, cstart, cend, cshape, _ = tokens[i + 1]
        if (cform.lower() in _NAME_CONNECTORS and adjacent(end, cstart)
                and title_at(i + 2, initial_ok=True) and adjacent(cend, tokens[i + 2][1])):
            return True
    return False


def find_case_splits(paragraphs: Sequence[ParagraphRef], *,
                     dominance: int = 3, min_total: int = 5,
                     proper_min_total: int = 3,
                     min_length: int = 3, max_ngram: int = 2,
                     sentence_initial_excluded: bool = True,
                     determiner_guard: float = 0.8,
                     protected: Sequence[str] = (),
                     exclude: Sequence[str] = (),
                     max_groups: int = 40) -> tuple[CaseSplit, ...]:
    """Terms the book capitalizes two ways outside sentence-initial position.

    The term scan folds case on purpose (English capitalizes the first word of
    every sentence); this scan looks only at the positions where casing is the
    author's choice, and only at the two shapes that choice takes — lowercase
    against Capitalized. ALLCAPS, camelCase and mid-word capitals are ignored
    (`_case_shape`), so OK/okay is a spelling question, not a casing one.

    Guards, in order: headings and shouted lines say nothing (`_skip_caps_context`);
    a Capitalized word standing next to another Capitalized word is a name
    phrase (Easy Speed, Aunt May) and is counted only as the bigram, never as
    its parts; a possessive clitic is stripped so Earth's counts as Earth;
    function words never split (he/He belongs to the deity-pronoun scan); and a
    term whose lowercase uses are led by a determiner while its capitalized
    uses stand bare (my mom / Mom, the coach / Coach) is the one legitimate
    casing coexistence in English, and is skipped when `determiner_guard` of
    each side agrees.

    `dominance` and `min_total` decide `clear`: a clear split proposes the
    majority form as a correction; a closer split is reported with its counts
    for a reader to judge.

    `proper_min_total` is a lower bar for a term hung off a PROPER NOUN — a word
    the book capitalizes everywhere away from a sentence start and never
    lowercases at all. "Atacama plateau" once against "Atacama Plateau" three
    times is four uses in total, well under `min_total`, and it is still a split
    a reader wants: the proper noun rules out the ordinary reason a word appears
    both ways (a common noun that is also somebody's name). Such a bigram is
    counted even when its two words disagree in case, which is the only way the
    lowercase half of "Atacama plateau" is visible at all."""
    protected_l = {str(p).lower() for p in protected} | {str(e).lower() for e in exclude}
    from .function_words import FUNCTION_WORDS
    groups: dict[str, dict] = {}

    # Pass one, over the whole book: which words are proper nouns. A word seen
    # lowercased anywhere is not one, and a word only ever seen at a sentence
    # start says nothing — English capitalizes those regardless.
    lowered: set[str] = set()
    titled: set[str] = set()
    for para in paragraphs:
        if _skip_caps_context(para):
            continue
        for m in _WORD.finditer(para.text):
            form, start, _end = _trim_quote(m.group(0), m.start(), m.end())
            form = _POSSESSIVE.sub("", form)
            if not form:
                continue
            shape = _case_shape(form)
            if shape == "lower":
                lowered.add(form.lower())
            elif shape == "title" and not (sentence_initial_excluded
                                           and _sentence_initial(para.text, start)):
                titled.add(form.lower())
    proper = titled - lowered

    def bucket(key):
        return groups.setdefault(key, {"lower": [], "title": [], "det_lower": 0,
                                       "det_title": 0, "counts": Counter()})

    def add(key, occurrence, shape, determined):
        g = bucket(key)
        g[shape].append(occurrence)
        g["counts"][occurrence.form] += 1
        if determined:
            g["det_" + shape] += 1

    for para in paragraphs:
        if _skip_caps_context(para):
            continue
        text = para.text
        tokens = []
        for m in _WORD.finditer(text):
            form, start, end = _trim_quote(m.group(0), m.start(), m.end())
            form = _POSSESSIVE.sub("", form)
            end = start + len(form)
            if not form:
                continue
            tokens.append((form, start, end, _case_shape(form),
                           sentence_initial_excluded and _sentence_initial(text, start)))
        for i, (form, start, end, shape, initial) in enumerate(tokens):
            if shape is None:
                continue
            determined = _word_before(text, start) in _CASE_DETERMINERS
            # A bigram joined by one space. Two same-shaped words are one term
            # capitalized one way ("solar system" / "Solar System"). A
            # Capitalized PROPER noun followed by a word of either shape is one
            # too — "Atacama plateau" against "Atacama Plateau" — and there the
            # shape that matters is the SECOND word's, since the first is
            # capitalized either way.
            if max_ngram >= 2 and i + 1 < len(tokens) and not initial:
                nform, nstart, nend, nshape, _ = tokens[i + 1]
                joined = text[end:nstart] == " " and nshape is not None
                bigram_shape = None
                if joined and nshape == shape:
                    bigram_shape = shape
                elif joined and shape == "title" and form.lower() in proper:
                    bigram_shape = nshape
                if bigram_shape is not None:
                    add(form.lower() + " " + nform.lower(),
                        Occurrence(para.para_id, start, nend, text[start:nend]),
                        bigram_shape, determined)
            if initial:
                continue
            if shape == "title" and _in_name_phrase(tokens, i, text):
                continue              # part of a capitalized phrase, not a stray
            add(form.lower(), Occurrence(para.para_id, start, end, form), shape, determined)

    splits: list[CaseSplit] = []
    for key in sorted(groups):
        g = groups[key]
        if (len(key) < min_length or key in protected_l
                or (" " not in key and key in FUNCTION_WORDS)
                or not g["lower"] or not g["title"]):
            continue
        total = len(g["lower"]) + len(g["title"])
        # A term hung off a proper noun clears a lower bar — see the docstring.
        floor = (min(min_total, proper_min_total)
                 if any(w in proper for w in key.split(" ")) else min_total)
        if total < floor:
            continue
        lo = g["det_lower"] / len(g["lower"])
        ti = g["det_title"] / len(g["title"])
        if lo >= determiner_guard and ti <= 1 - determiner_guard:
            continue
        dom_shape = "lower" if len(g["lower"]) >= len(g["title"]) else "title"
        min_shape = "title" if dom_shape == "lower" else "lower"
        dom_forms = Counter(o.form for o in g[dom_shape])
        dominant = min(dom_forms, key=lambda f: (-dom_forms[f], f))
        dom_n, min_n = len(g[dom_shape]), len(g[min_shape])
        splits.append(CaseSplit(key, g["counts"], dominant, dom_n >= dominance * min_n,
                                tuple(g[min_shape])))
    if max_groups and len(splits) > max_groups:
        log.info("Consistency case-split findings capped at %d (%d found); raise "
                 "consistency.max_queries_per_kind to see the rest.", max_groups, len(splits))
        splits = splits[:max_groups]
    return tuple(splits)


def find_inconsistencies(paragraphs: Sequence[ParagraphRef], *,
                         enabled: bool = True, min_length: int = 7,
                         min_dominance: int = 2, names: bool = True,
                         name_dominance: int = 5,
                         name_min_count: int = 20,
                         spelling_variants: bool = True,
                         abbreviations: bool = True,
                         acronym_case: bool = True,
                         chicago_notes: bool = True,
                         respell: Mapping[str, str] | None = None,
                         protected: Sequence[str] = (),
                         dictionary: str = "en_US",
                         variant_policy: str = "off",
                         deity_pronouns: bool = True,
                         deity_min_capitalized: int = 8,
                         time_style: bool = True,
                         time_min_with_minutes: int = 3,
                         accent_loanwords: bool = True,
                         max_queries_per_kind: int = 40,
                         case_splits: bool = False,
                         case_split_dominance: int = 3,
                         case_split_min_total: int = 5,
                         case_split_proper_min_total: int = 3,
                         case_split_exclude: Sequence[str] = (),
                         vessel_pronouns: bool = False,
                         vessel_min_feminine: int = 5,
                         dialect_variants: bool = False,
                         dialect_dominance: int = 3,
                         closed_compounds: bool = False,
                         figure_drift: bool = True,
                         figure_min_majority: int = 3,
                         callbacks: bool = False,
                         callback_min_tokens: int = 8,
                         callback_near: float = 0.80) -> ConsistencyReport:
    """Terms this manuscript writes more than one way.

    `min_length` keeps short words out — the shorter the key, the more likely
    two forms are unrelated English rather than one term. `min_dominance` is
    how many times the majority form must outnumber a minority one before the
    minority reads as a slip rather than a second, equally deliberate choice.

    `names` also runs the proper-name diacritic scan; `name_dominance` and
    `name_min_count` set its bar for correcting rather than asking. See
    ``find_name_drift``.

    `spelling_variants`, `abbreviations` and `acronym_case` run the three
    mechanical scans a key-folding compound scan cannot (grey/gray, U.S./US,
    NASA/Nasa). `respell` and `protected` come from the run's variant and spell
    scan, so an enforced or author-owned form is not also asked about; `chicago_notes`
    adds the Merriam-Webster preference phrasing; `max_queries_per_kind` bounds
    each scan's output so a dialect-mixed book cannot flood the query channel.

    `case_splits` runs ``find_case_splits`` — earth/Earth outside sentence
    starts. Off by default: its findings are tracked edits meant for a caller
    that screens every proposal (Galley's fixed workflow); the legacy pipeline
    has no such screen and does not ask for it. `case_split_exclude` lists
    keys the run has already decided by an accepted edit.

    The five whole-book scans the Cooper QA added, each silent on a book without
    the pattern: `vessel_pronouns` (a ship called *she* and once *it*),
    `dialect_variants` (dinnae against dinna inside dialect speech),
    `closed_compounds` (sat phone against satphone, where the dictionary
    decides), `figure_drift` (282.6° nine times and 282.8° once) and `callbacks`
    (a remembered line that misquotes the line it remembers; see
    ``docproof/callbacks.py``).

    Four of those five can propose a tracked edit, and they are off here for the
    same reason `case_splits` is: the caller that asks for them is the one that
    screens every proposal in context. The shipped config turns them on, so
    Galley's fixed workflow — which reads this signature and passes the config
    through by name — gets all of them; the legacy pipeline, which enumerates
    what it wants and has no screen, keeps the behaviour it had. `figure_drift`
    is on by default because it corrects nothing at all: a numeric value is never
    changed to repair a contradiction, so its worst case is a question.
    """
    if not enabled:
        return ConsistencyReport(ran=False)

    # Paragraph text by id, for the compound part-of-speech gate below (it reads
    # the word before each occurrence to tell a phrasal verb from its noun twin).
    _text_by_id = {p.para_id: p.text for p in paragraphs}
    closed_keys = _load_closed_compounds() if closed_compounds else {}
    groups: dict[str, _Group] = defaultdict(_Group)
    for para in paragraphs:
        # Trim closing-quote artifacts up front, so both the single-token form
        # and the two-word window below see the word without its stray quote.
        tokens = [_trim_quote(m.group(0), m.start(), m.end())
                  for m in _WORD.finditer(para.text)]
        for i, (wtext, wstart, wend) in enumerate(tokens):
            forms = [(wtext, wstart, wend)]
            # The open-compound spelling of the same term is two words, so a
            # scan that only looked at single tokens would miss exactly the
            # case the brief names first. The gap is measured from the *trimmed*
            # end, so a closing quote sitting between the words (cursed’ blood)
            # blocks the join instead of fusing two unrelated words.
            if i + 1 < len(tokens):
                ntext, nstart, nend = tokens[i + 1]
                if para.text[wend:nstart] == " ":
                    forms.append((para.text[wstart:nend], wstart, nend))
            for form, start, end in forms:
                key = _key(form)
                # `min_length` is a guard against accidental key collisions
                # between unrelated short words. A key the closed-compound table
                # names is not an accident — the dictionary put it there — so
                # "email" and "online" are counted despite being short.
                if key in _LEGITIMATE:
                    continue
                if len(key) < min_length and key not in closed_keys:
                    continue
                g = groups[key]
                g.counts[form] += 1
                g.where.append(Occurrence(para.para_id, start, end, form))

    terms: list[Inconsistency] = []
    compounds: list[CompoundPreference] = []
    for key, g in sorted(groups.items()):
        # Collapse the surface forms into their structures before deciding
        # anything. Two spellings that differ only in letter case (or in the
        # apostrophe glyph) share one structure, so a term written one way but
        # sometimes at the start of a sentence — the overwhelming majority of
        # what a naive surface-form comparison flags — never reaches the
        # dominance test at all.
        buckets: dict[str, Counter] = defaultdict(Counter)
        for form, n in g.counts.items():
            buckets[_structure(form)][form] += n
        if len(buckets) < 2:
            continue
        totals = {s: sum(c.values()) for s, c in buckets.items()}
        # One representative surface form per structure: the spelling used
        # most, breaking ties toward the plain lowercase form so the recommended
        # spelling never carries an incidental sentence-initial capital.
        reps = {s: min(c, key=lambda f, c=c: (-c[f], f != f.lower(), f))
                for s, c in buckets.items()}
        # The dictionary decides before dominance gets a vote. "sat phone" ×4
        # against "satphone" ×3 never reaches the bar below and never will; the
        # table already knows which of the two is the word.
        closed = (_closed_compound(key, tuple(buckets))
                  if closed_compounds else None)
        if closed is not None:
            preferred, note = closed
            strays = tuple(o for o in g.where
                           if _structure(o.form) != preferred)
            if strays:
                counts = Counter({reps[s]: totals[s] for s in totals})
                compounds.append(CompoundPreference(key, counts, preferred,
                                                    note, strays))
            continue
        dom_struct = max(totals, key=lambda s: (totals[s], s))
        dom_total = totals[dom_struct]
        minority_structs = {s for s, t in totals.items()
                            if s != dom_struct and dom_total >= t * min_dominance}
        if not minority_structs:
            # No structure clearly dominates, so this is two deliberate choices
            # or a word this scan should not be guessing about.
            continue
        # Suppress a legitimate verb/noun compound split (check in / check-in)
        # before it becomes a query — part-of-speech, not one term two ways.
        occs_by_struct: dict[str, list] = defaultdict(list)
        for o in g.where:
            occs_by_struct[_structure(o.form)].append(o)
        if _compound_pos_split(occs_by_struct, _text_by_id):
            continue
        outliers = tuple(o for o in g.where
                         if _structure(o.form) in minority_structs)
        if outliers:
            counts = Counter({reps[s]: totals[s] for s in totals})
            terms.append(Inconsistency(key, counts, reps[dom_struct], outliers))

    drift = (find_name_drift(paragraphs, min_dominance=name_dominance,
                             min_count=name_min_count) if names else ())
    variants = (find_spelling_variants(
        paragraphs, min_dominance=min_dominance, respell=respell,
        protected=protected, chicago=chicago_notes,
        max_queries=max_queries_per_kind) if spelling_variants else ())
    abbrevs = (find_abbreviation_variants(
        paragraphs, min_dominance=min_dominance, protected=protected,
        max_queries=max_queries_per_kind) if abbreviations else ())
    cases = (find_acronym_case(
        paragraphs, min_dominance=min_dominance, dictionary=dictionary,
        protected=protected,
        max_queries=max_queries_per_kind) if acronym_case else ())
    policy = (find_variant_policy(
        paragraphs, respell=respell, protected=protected,
        chicago=chicago_notes,
        max_queries=max_queries_per_kind) if variant_policy == "us" else ())
    deity = (find_deity_pronouns(
        paragraphs, min_capitalized=deity_min_capitalized,
        max_queries=max_queries_per_kind) if deity_pronouns else None)
    times = (find_time_style(
        paragraphs, min_with_minutes=time_min_with_minutes,
        max_queries=max_queries_per_kind) if time_style else None)
    accents = (find_accent_loanwords(
        paragraphs, protected=protected,
        max_queries=max_queries_per_kind) if accent_loanwords else ())
    splits = (find_case_splits(
        paragraphs, dominance=case_split_dominance, min_total=case_split_min_total,
        proper_min_total=case_split_proper_min_total,
        protected=protected, exclude=case_split_exclude,
        max_groups=max_queries_per_kind) if case_splits else ())
    vessels = (find_vessel_pronouns(
        paragraphs, min_feminine=vessel_min_feminine,
        max_queries=max_queries_per_kind) if vessel_pronouns else ())
    dialect = (find_dialect_variants(
        paragraphs, min_dominance=dialect_dominance,
        max_queries=max_queries_per_kind) if dialect_variants else ())
    figures = (find_figure_drift(
        paragraphs, min_majority=figure_min_majority,
        max_queries=max_queries_per_kind) if figure_drift else ())
    echoes: tuple = ()
    if callbacks:
        from .callbacks import find_callbacks
        echoes = find_callbacks(paragraphs, min_tokens=callback_min_tokens,
                                near=(callback_near, 0.999),
                                max_queries=max_queries_per_kind)
    report = ConsistencyReport(ran=True, terms=tuple(terms), names=drift,
                               variants=variants, abbreviations=abbrevs,
                               casings=cases, accents=accents, policy=policy,
                               deity=deity, times=times, case_splits=splits,
                               vessels=vessels, dialect=dialect,
                               compounds=tuple(compounds), figures=figures,
                               callbacks=echoes)
    log.info("Consistency scan: %d term(s), %d spelling-variant(s), "
             "%d abbreviation(s), %d acronym-case(s), %d accent(s), "
             "%d policy form(s), %d deity-pronoun stray(s), %d bare-hour "
             "time(s), %d name(s) with diacritic drift, %d vessel(s) pronouned "
             "both ways, %d dialect spelling(s), %d dictionary-decided "
             "compound(s), %d drifting figure(s), and %d verbatim callback(s) "
             "— %d occurrence(s) to correct, %d to ask about",
             len(terms), len(variants), len(abbrevs), len(cases), len(accents),
             len(policy), len(deity.outliers) if deity else 0,
             len(times.outliers) if times else 0, len(drift), len(vessels),
             len(dialect), len(compounds), len(figures), len(echoes),
             report.corrected, report.flagged)
    return report


def to_findings(report: ConsistencyReport, paragraphs: Sequence[ParagraphRef],
                start_id: int = 1) -> list[Finding]:
    """One finding per outlier occurrence, anchored to the sentence it sits in.

    Term outliers are queries — which spelling a book uses is the author's
    decision, and that scan cannot tell a slip from a distinction. Name
    outliers whose group cleared the enforcement bar are corrections, and go
    down the tracked-change channel like any other edit; the rest are queries
    too."""
    by_id = {p.para_id: p for p in paragraphs}
    findings: list[Finding] = []
    n = start_id
    for term in report.terms:
        for o in term.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, _, occurrence = sentence_window(para.text, o.start, o.end)
            # term.counts now holds one representative spelling per structure,
            # so the list reads "over consume" vs "overconsume", not a dozen
            # case variants of one word. Exclude this occurrence's own
            # structure by structure, not by exact spelling: a sentence-initial
            # outlier still names the other forms, not itself.
            o_struct = _structure(o.form)
            others = ", ".join(
                f"“{f}” ({c})" for f, c in term.counts.most_common()
                if _structure(f) != o_struct)
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This manuscript writes this term more than one way: "
                    f"“{o.form}” here, and elsewhere {others}. Is the "
                    f"difference deliberate? If not, “{term.dominant}” is the "
                    f"form used most."),
                confidence="high",
            ))
            n += 1

    c = 1
    for drift in report.names:
        for o in drift.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, lo, occurrence = sentence_window(para.text, o.start, o.end)
            dom_count = drift.counts[drift.dominant]
            if drift.enforce:
                # An all-caps stray (a heading) keeps its setting; everything
                # else takes the dominant spelling verbatim.
                fix = (drift.dominant.upper()
                       if o.form.isupper() and len(o.form) > 1
                       else drift.dominant)
                findings.append(Finding(
                    finding_id=f"n-{c:04d}",
                    chunk_id="consistency",
                    para_id=o.para_id,
                    error_type=NAME_KEY,
                    original_text=window,
                    occurrence=occurrence,
                    corrected_text=(window[:o.start - lo] + fix
                                    + window[o.end - lo:]),
                    explanation=(
                        f"This manuscript spells this name "
                        f"“{drift.dominant}” {dom_count} time(s) but "
                        f"“{o.form}” here. Corrected to the spelling the "
                        f"book uses; reject if the two spellings are "
                        f"different characters."),
                    confidence="high",
                ))
                c += 1
            else:
                others = ", ".join(
                    f"“{f}” ({cnt})" for f, cnt in drift.counts.most_common()
                    if _structure(f) != _structure(o.form))
                findings.append(Finding(
                    finding_id=f"c-{n:04d}",
                    chunk_id="consistency",
                    para_id=o.para_id,
                    error_type=CONSISTENCY_KEY,
                    original_text=window,
                    occurrence=occurrence,
                    corrected_text=window,
                    explanation=(
                        f"This manuscript spells what may be one name more "
                        f"than one way: “{o.form}” here, and elsewhere "
                        f"{others}. Is the difference deliberate? If not, "
                        f"“{drift.dominant}” is the form used most."),
                    confidence="high",
                ))
                n += 1

    # The mechanical scans: one query per group, at the first minority site.
    for vg in report._mechanical:
        para = by_id.get(vg.site.para_id)
        if para is None:
            continue
        window, _, occurrence = sentence_window(
            para.text, vg.site.start, vg.site.end)
        forms = ", ".join(f"“{f}” ({c})" for f, c in vg.forms)
        note = f" {vg.note}" if vg.note else ""
        if vg.kind == "accent":
            # The recommendation is the dictionary's accented form, not the
            # book's majority, so the shared majority template does not fit.
            both = (f" The book itself also writes “{vg.dominant}” "
                    f"({vg.counts[vg.dominant]} time(s))."
                    if vg.counts.get(vg.dominant) else "")
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=vg.site.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"“{vg.site.form}” is a loanword the dictionary sets "
                    f"with its accent: “{vg.dominant}”.{both} Change here "
                    f"(and anywhere else it appears bare), unless the plain "
                    f"spelling is deliberate?"),
                confidence="high",
            ))
            n += 1
            continue
        if vg.kind == "spelling":
            lead = "This manuscript spells one word more than one way"
        elif vg.kind == "abbreviation":
            lead = "This abbreviation is written more than one way"
        else:
            lead = "This is capitalized more than one way"
        if vg.has_majority:
            tail = (f" If that isn't deliberate, “{vg.dominant}” is the form "
                    f"the book uses most.")
        else:
            tail = (f" The book uses both about equally; “{vg.dominant}” is the "
                    f"form to settle on unless the split is deliberate.")
        findings.append(Finding(
            finding_id=f"c-{n:04d}",
            chunk_id="consistency",
            para_id=vg.site.para_id,
            error_type=CONSISTENCY_KEY,
            original_text=window,
            occurrence=occurrence,
            corrected_text=window,
            explanation=f"{lead}: {forms}.{note}{tail}",
            confidence="high",
        ))
        n += 1

    # Policy: one query per cluster, at the first occurrence, proposing the
    # American spelling the book never uses.
    for vg in report.policy:
        para = by_id.get(vg.site.para_id)
        if para is None:
            continue
        window, _, occurrence = sentence_window(
            para.text, vg.site.start, vg.site.end)
        forms = ", ".join(f"“{f}” ({c})" for f, c in vg.forms)
        note = f" {vg.note}" if vg.note else ""
        findings.append(Finding(
            finding_id=f"c-{n:04d}",
            chunk_id="consistency",
            para_id=vg.site.para_id,
            error_type=CONSISTENCY_KEY,
            original_text=window,
            occurrence=occurrence,
            corrected_text=window,
            explanation=(
                f"House style prefers the U.S. spelling “{vg.dominant}”; "
                f"this book uses {forms} throughout.{note} Change to "
                f"“{vg.dominant}” everywhere?"),
            confidence="high",
        ))
        n += 1

    # Deity pronouns: one query per stray, because each needs its own eyes —
    # only the author knows which pronouns are His.
    if report.deity:
        for o in report.deity.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, _, occurrence = sentence_window(para.text, o.start, o.end)
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This book capitalizes pronouns referring to God "
                    f"({report.deity.capitalized} mid-sentence uses of "
                    f"He/His/Him). If this “{o.form}” refers to God, the "
                    f"book's own convention makes it "
                    f"“{o.form[:1].upper()}{o.form[1:]}”; if it refers to "
                    f"someone else, please ignore this note."),
                confidence="medium",
            ))
            n += 1

    # Bare clock hours: ONE book-level question, not one per site. Whether to
    # write minutes on the bare hours is a single style decision for the whole
    # book — asking it once per site buried the Purpura margin under 25 near-
    # identical queries (P1-7). The one query anchors at the first bare hour and
    # lists the rest, so the author still sees every site but answers once.
    if report.times and report.times.outliers:
        sites = [o for o in report.times.outliers if by_id.get(o.para_id)]
        if sites:
            first = sites[0]
            para = by_id[first.para_id]
            window, _, occurrence = sentence_window(para.text, first.start, first.end)
            forms = []
            for o in sites:
                if o.form not in forms:
                    forms.append(o.form)
            others = (f" The other bare hour(s): {', '.join(forms[1:])}."
                      if len(forms) > 1 else "")
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=first.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This book writes clock times with minutes — "
                    f"“{report.times.example}”, {report.times.with_minutes} "
                    f"time(s) — but {len(sites)} clock hour(s) stand bare, "
                    f"starting here with “{first.form}”. House style spells a "
                    f"bare hour out (“around four”) rather than adding “:00”; "
                    f"is any of these meant as a clock reading with minutes?{others}"),
                confidence="medium",
            ))
            n += 1
    k = 1
    for split in report.case_splits:
        forms = " vs ".join(f"“{f}” ×{c}" for f, c in split.counts.most_common())
        min_n = len(split.outliers)
        dom_n = sum(split.counts.values()) - min_n
        for o in split.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, lo, occurrence = sentence_window(para.text, o.start, o.end)
            corrected = window[:o.start - lo] + split.dominant + window[o.end - lo:]
            if split.clear:
                explanation = (
                    f"{forms} outside sentence-initial position; dominant form "
                    f"“{split.dominant}” leads {dom_n} to {min_n}, so this “{o.form}” "
                    f"is changed to match. Drop if this use is a different sense.")
            else:
                explanation = (
                    f"{forms} outside sentence-initial position; no form clearly "
                    f"dominates. “{split.dominant}” is the form used more and is "
                    f"proposed for consistency only; drop if the split is deliberate.")
            findings.append(Finding(
                finding_id=f"k-{k:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=CASE_SPLIT_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=corrected,
                explanation=explanation,
                confidence="high" if split.clear else "medium",
            ))
            k += 1

    # Vessel pronouns: one correction per stray, carrying the counts that make
    # the book's own convention the evidence.
    v = 1
    for vessel in report.vessels:
        for o in vessel.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, lo, occurrence = sentence_window(para.text, o.start, o.end)
            fix = vessel_fix(para.text, o)
            findings.append(Finding(
                finding_id=f"v-{v:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=VESSEL_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window[:o.start - lo] + fix + window[o.end - lo:],
                explanation=(
                    f"This book pronouns the {vessel.vessel} as “she” — "
                    f"{vessel.feminine} feminine pronoun(s) in sentences naming "
                    f"her against {vessel.neuter} neuter — so this “{o.form}” "
                    f"is changed to “{fix}”. Drop it if the pronoun refers to "
                    f"something else in the sentence."),
                confidence="medium",
            ))
            v += 1

    # Dialect: corrections when the majority spelling is decisive, otherwise one
    # query at the first minority site. A spelling that recurs on every page of
    # dialogue cannot have a margin note per occurrence.
    d = 1
    for group in report.dialect:
        forms = ", ".join(f"“{f}” ({c})" for f, c in group.counts.most_common())
        sites = [o for o in group.outliers if by_id.get(o.para_id)]
        if not sites:
            continue
        if not group.enforce:
            first = sites[0]
            para = by_id[first.para_id]
            window, _, occurrence = sentence_window(
                para.text, first.start, first.end)
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=first.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This dialect word is spelled more than one way in "
                    f"dialogue: {forms}. No spelling clearly dominates, so "
                    f"which one to settle on is your ear — “{group.dominant}” "
                    f"is the one used more."),
                confidence="medium",
            ))
            n += 1
            continue
        for o in sites:
            para = by_id[o.para_id]
            window, lo, occurrence = sentence_window(para.text, o.start, o.end)
            fix = _match_case(group.dominant, o.form)
            findings.append(Finding(
                finding_id=f"d-{d:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=DIALECT_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window[:o.start - lo] + fix + window[o.end - lo:],
                explanation=(
                    f"This dialogue spells one dialect word more than one way: "
                    f"{forms}. Changed to the spelling the book uses; reject if "
                    f"this speaker's spelling is deliberately different."),
                confidence="high",
            ))
            d += 1

    # Dictionary-decided compounds: corrections, because the table already
    # answered the question the term scan would have asked.
    m = 1
    for pref in report.compounds:
        forms = ", ".join(f"“{f}” ({c})" for f, c in pref.counts.most_common())
        for o in pref.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, lo, occurrence = sentence_window(para.text, o.start, o.end)
            fix = _match_case(pref.preferred, o.form)
            findings.append(Finding(
                finding_id=f"m-{m:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=COMPOUND_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window[:o.start - lo] + fix + window[o.end - lo:],
                explanation=(
                    f"{pref.note} This manuscript writes it {forms}, so this "
                    f"one is closed up to match the dictionary rather than the "
                    f"count. Reject if the open spelling is deliberate."),
                confidence="high",
            ))
            m += 1

    # Figures: one query per stray, and never an edit — a numeric value is not
    # changed to repair a contradiction.
    for figure in report.figures:
        maj_n = figure.counts[figure.majority]
        for o in figure.outliers:
            para = by_id.get(o.para_id)
            if para is None:
                continue
            window, _, occurrence = sentence_window(para.text, o.start, o.end)
            findings.append(Finding(
                finding_id=f"c-{n:04d}",
                chunk_id="consistency",
                para_id=o.para_id,
                error_type=CONSISTENCY_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This figure is “{figure.majority}” at {maj_n} other "
                    f"place(s) and “{o.form}” here. Is the difference "
                    f"deliberate? Nothing has been changed either way — which "
                    f"figure is the right one is yours to say."),
                confidence="high",
            ))
            n += 1

    if report.callbacks:
        from .callbacks import callback_findings
        findings.extend(callback_findings(report.callbacks, paragraphs))

    return findings


# --- possessives of names ending in s -----------------------------------------

# The key a possessive conformation carries. Like NAME_KEY it lives outside
# config/error_types: the book's own count decides it, not a prompt.
POSSESSIVE_KEY = "possessive_s"

# The bar for an author preference: at least this many countable possessives,
# at least this share of them in one form. Immanuel (2026-09-22) wrote
# "Dolores’" 28 times and "Dolores’s" twice, 93%.
POSSESSIVE_MIN_SITES = 3
POSSESSIVE_DOMINANCE = 0.75

# A word ending in s, then an apostrophe, then an optional s, then no letter.
# Both apostrophes are read; a conformed site keeps the one it was written with.
_POSSESSIVE_SITE = re.compile(
    r"(?<![\w'’\-‐‑])(?P<name>[^\W\d_]+s)(?P<mark>[’'])(?P<s>s?)(?![^\W\d_'’])", re.UNICODE)
# A capitalized word ending in s, anywhere, for the name and plural evidence.
_S_WORD = re.compile(r"(?<![\w'’\-‐‑])[^\W\d_]+s(?![^\W\d_])", re.UNICODE)
_CAP_WORD = re.compile(r"(?<![\w'’\-‐‑])[^\W\d_]+(?![^\W\d_])", re.UNICODE)
_NEXT_WORD = re.compile(r"[\s ]+([^\W\d_]+)", re.UNICODE)
# After "Dolores’s" these words read "Dolores is/has" as readily as a
# possessive ("Dolores’s been", "Dolores’s a nurse"); so does any -ing word
# ("Dolores’s singing"). Such a site is neither counted nor conformed.
_CONTRACTION_NEXT = frozenset("""
a an the been got gotten gone not never always just still already really so too
very here there gonna in on at out up down off back over like right probably
definitely only also all no
""".split())
# Fixed expressions that keep the bare apostrophe whatever the book does.
_BARE_IDIOMS = {"achilles": {"heel", "heels", "tendon", "tendons"}}
_HONORIFICS_S = frozenset({"mrs", "ms", "messrs"})
# "the Petters’ window", "The McCoys’ Hang on Sloopy", "Los Almendros’": a name
# after an article is a family, a band or a people, a PLURAL possessive, and
# a plural possessive is never Petters’s.
_PLURAL_ARTICLES = frozenset({"the", "los", "las", "les"})
_WORD_BEFORE = re.compile(r"([^\W\d_]+)[\s ]+\Z", re.UNICODE)


@dataclass(frozen=True)
class PossessiveSite:
    """One countable possessive of a name ending in s. ``start``..``end`` is
    the whole possessive, name included (``Dolores’`` or ``Dolores’s``)."""
    start: int
    end: int
    name: str
    form: str           # "bare" (Dolores’) or "s" (Dolores’s)
    mark: str           # the apostrophe as written


@dataclass(frozen=True)
class PossessivePreference:
    name: str
    form: str           # the form the book's text is conformed to
    basis: str          # "name" | "book" | "chicago"
    bare: int           # this name's countable bare possessives
    s: int              # and its 's possessives


@dataclass(frozen=True)
class PossessivePolicy:
    """The manuscript's own answer to Dolores’ versus Dolores’s, per name.
    Decided once from the ORIGINAL text; every later stage reads it."""
    names: Mapping[str, PossessivePreference]
    book_bare: int
    book_s: int

    def form(self, name: str) -> str | None:
        pref = self.names.get(name)
        return pref.form if pref else None

    def as_dict(self) -> dict:
        return {"book": {"bare": self.book_bare, "s": self.book_s},
                "names": {n: {"form": p.form, "basis": p.basis, "bare": p.bare, "s": p.s}
                          for n, p in sorted(self.names.items())}}


def _closing_quote(text: str, at: int) -> bool:
    """Is the ’ at ``at`` (a word-final one) the close of single-quoted
    speech rather than a possessive? UK dialogue ends ‘Hi, Dolores’ with the
    same glyph the possessive uses. Walk the paragraph tracking open ‘ marks
    the way the sweeps do; when one is open here, the ’ is a possessive only
    if another closer follows before the next opener (‘Is that Dolores’
    car?’). Otherwise it is, or may be, the closer, and is left alone."""
    if text[at] != "’":
        return False
    open_count = 0
    for i in range(at):
        ch = text[i]
        if ch == "‘":
            open_count += 1
        elif ch == "’" and open_count:
            prev, nxt = text[i - 1] if i else "", text[i + 1] if i + 1 < len(text) else ""
            if not (prev.isalpha() and nxt.isalpha()):
                open_count -= 1
    if not open_count:
        return False
    for j in range(at + 1, len(text)):
        ch = text[j]
        if ch == "‘":
            return True
        if ch == "’":
            prev, nxt = text[j - 1], text[j + 1] if j + 1 < len(text) else ""
            if not (prev.isalpha() and nxt.isalpha()):
                return False
    return True


def _possessive_sites(text: str, names) -> list[PossessiveSite]:
    """The countable possessives in one paragraph of the names in ``names``.
    A site that may be a closing quotation mark, an 's contraction, a plural
    (after "the") or a fixed expression is not a site: it is neither counted
    nor changed."""
    sites = []
    for m in _POSSESSIVE_SITE.finditer(text):
        name = m.group("name")
        if name not in names:
            continue
        before = _WORD_BEFORE.search(text, max(0, m.start() - 12), m.start())
        if before and before.group(1).lower() in _PLURAL_ARTICLES:
            continue
        nxt = _NEXT_WORD.match(text, m.end())
        following = nxt.group(1).lower() if nxt else ""
        if following and following in _BARE_IDIOMS.get(name.lower(), ()):
            continue
        if following == "sake":
            continue                   # for Jesus’ sake: an idiom, not a count
        if m.group("s"):
            if following in _CONTRACTION_NEXT or following.endswith("ing"):
                continue
            form = "s"
        else:
            if _closing_quote(text, m.start("mark")):
                continue
            form = "bare"
        sites.append(PossessiveSite(m.start(), m.end(), name, form, m.group("mark")))
    return sites


def _possessive_names(texts: Mapping[str, str]) -> set[str]:
    """Capitalized words ending in s that the book uses as singular names.
    A name is a word the book capitalizes somewhere mid-sentence (so a
    sentence-initial "Thanks’" is not one). A word whose s is a plural — the
    book also has the word without it (Smiths beside Smith, Joneses beside
    Jones) — is left out, because Smiths’ must never become Smiths’s."""
    caps, named = set(), set()
    for text in texts.values():
        text = text or ""
        for m in _CAP_WORD.finditer(text):
            word = m.group(0)
            if word[:1].isupper():
                caps.add(word)
        for m in _S_WORD.finditer(text):
            word = m.group(0)
            if (len(word) >= 3 and word[:1].isupper() and not word.isupper()
                    and word.lower() not in _HONORIFICS_S
                    and not _sentence_initial(text, m.start())):
                named.add(word)
    return {w for w in named
            if w[:-1] not in caps and not (w.endswith("es") and w[:-2] in caps)}


def possessive_policy(texts: Mapping[str, str], *, min_sites: int = POSSESSIVE_MIN_SITES,
                      dominance: float = POSSESSIVE_DOMINANCE) -> PossessivePolicy:
    """Decide, from the manuscript as written, which possessive each name
    ending in s takes. A name the author writes one way clearly (at least
    ``min_sites`` countable possessives, at least ``dominance`` of them in one
    form) keeps that form. Otherwise the book's pooled count over every such
    name decides by the same bar. Only a book with no clear preference gets
    Chicago's default, ’s. Chicago accepts both forms; the house rule that the
    author's consistent choice is never an error is what makes this a count
    and not a correction."""
    names = _possessive_names(texts)
    counts: dict[str, Counter] = defaultdict(Counter)
    for text in texts.values():
        for site in _possessive_sites(text or "", names):
            counts[site.name][site.form] += 1
    book = Counter()
    for c in counts.values():
        book.update(c)

    def clear(c: Counter) -> str | None:
        total = c["bare"] + c["s"]
        if total < min_sites:
            return None
        top = "bare" if c["bare"] >= c["s"] else "s"
        return top if c[top] / total >= dominance else None

    book_form = clear(book)
    prefs = {}
    for name, c in counts.items():
        own = clear(c)
        form, basis = ((own, "name") if own else (book_form, "book") if book_form else ("s", "chicago"))
        prefs[name] = PossessivePreference(name, form, basis, c["bare"], c["s"])
    return PossessivePolicy(prefs, book["bare"], book["s"])


def _possessive_reason(pref: PossessivePreference, policy: PossessivePolicy, target: str) -> str:
    if pref.basis == "name":
        return (f"The manuscript writes this name's possessive as “{target}” "
                f"({pref.bare if pref.form == 'bare' else pref.s} of {pref.bare + pref.s} times); "
                f"this one is brought in line with the author's form.")
    if pref.basis == "book":
        n = policy.book_bare if pref.form == "bare" else policy.book_s
        style = "a bare apostrophe" if pref.form == "bare" else "’s"
        return (f"The manuscript writes the possessive of names ending in s with {style} "
                f"({n} of {policy.book_bare + policy.book_s} times); “{target}” follows the author's form.")
    return (f"The manuscript shows no consistent form for possessives of names ending in s, "
            f"so Chicago's default applies: “{target}”.")


def find_possessive_drift(paragraphs: Sequence[ParagraphRef],
                          policy: PossessivePolicy) -> list[Finding]:
    """A tracked edit for every countable possessive not in its name's
    decided form (``possessive_policy``), quoted by its sentence like the
    other house sweeps. The set is one decision: a caller screening the sites
    should move them together (Galley's fixed workflow does)."""
    findings: list[Finding] = []
    for para in paragraphs:
        if not getattr(para, "reviewable", True) or not para.text:
            continue
        for site in _possessive_sites(para.text, policy.names):
            pref = policy.names[site.name]
            if site.form == pref.form:
                continue
            target = site.name + site.mark + ("s" if pref.form == "s" else "")
            window, lo, occurrence = sentence_window(para.text, site.start, site.end)
            corrected = window[:site.start - lo] + target + window[site.end - lo:]
            findings.append(Finding(
                finding_id=f"possessive-{len(findings) + 1}", chunk_id="house",
                para_id=para.para_id, error_type=POSSESSIVE_KEY,
                original_text=window, occurrence=occurrence, corrected_text=corrected,
                explanation=_possessive_reason(pref, policy, target),
                confidence="high", status="validated"))
    return findings


def possessive_conversion(before: str, after: str, policy: PossessivePolicy) -> str | None:
    """Why a change from paragraph ``before`` to ``after`` converts a name's
    possessive AWAY from its decided form, or None. Read on raw shapes, not
    countable sites: adding an s after a closing quote is no better an edit.
    Only a conversion counts (one shape up, the other down), so an added
    closing quote or a restored missing word is not refused."""
    for name, pref in policy.names.items():
        if name not in before and name not in after:
            continue
        s_shape = re.compile(r"(?<![\w'’\-‐‑])" + re.escape(name) + r"[’']s(?![^\W\d_'’])")
        bare_shape = re.compile(r"(?<![\w'’\-‐‑])" + re.escape(name) + r"[’'](?![^\W\d_'’])")
        d_s = len(s_shape.findall(after)) - len(s_shape.findall(before))
        d_bare = len(bare_shape.findall(after)) - len(bare_shape.findall(before))
        wrong = d_s > 0 and d_bare < 0 if pref.form == "bare" else d_bare > 0 and d_s < 0
        if wrong:
            kept = f"{name}’" if pref.form == "bare" else f"{name}’s"
            count = (f"{pref.bare} of {pref.bare + pref.s} times" if pref.basis == "name" and pref.form == "bare"
                     else f"{pref.s} of {pref.bare + pref.s} times" if pref.basis == "name"
                     else "names ending in s generally" if pref.basis == "book"
                     else "Chicago's default, the book having no preference")
            return (f"the possessive of {name} is “{kept}” in this book ({count}); "
                    f"a single site is never converted against the author's form")
    return None
