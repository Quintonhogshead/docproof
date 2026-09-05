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

    @property
    def _mechanical(self) -> tuple[VariantGroup, ...]:
        return self.variants + self.abbreviations + self.casings + self.accents

    @property
    def flagged(self) -> int:
        # Terms, non-enforced names, deity strays and time strays are
        # per-occurrence; the mechanical and policy scans are one query per
        # group.
        return (sum(len(t.outliers) for t in self.terms)
                + sum(len(n.outliers) for n in self.names if not n.enforce)
                + len(self._mechanical) + len(self.policy)
                + (len(self.deity.outliers) if self.deity else 0)
                + (len(self.times.outliers) if self.times else 0))

    @property
    def corrected(self) -> int:
        return sum(len(n.outliers) for n in self.names if n.enforce)


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
                         max_queries_per_kind: int = 40) -> ConsistencyReport:
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
    """
    if not enabled:
        return ConsistencyReport(ran=False)

    # Paragraph text by id, for the compound part-of-speech gate below (it reads
    # the word before each occurrence to tell a phrasal verb from its noun twin).
    _text_by_id = {p.para_id: p.text for p in paragraphs}
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
                if len(key) < min_length or key in _LEGITIMATE:
                    continue
                g = groups[key]
                g.counts[form] += 1
                g.where.append(Occurrence(para.para_id, start, end, form))

    terms: list[Inconsistency] = []
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
    report = ConsistencyReport(ran=True, terms=tuple(terms), names=drift,
                               variants=variants, abbreviations=abbrevs,
                               casings=cases, accents=accents, policy=policy,
                               deity=deity, times=times)
    log.info("Consistency scan: %d term(s), %d spelling-variant(s), "
             "%d abbreviation(s), %d acronym-case(s), %d accent(s), "
             "%d policy form(s), %d deity-pronoun stray(s), %d bare-hour "
             "time(s), and %d name(s) with diacritic drift — %d occurrence(s) "
             "to correct, %d to ask about",
             len(terms), len(variants), len(abbrevs), len(cases), len(accents),
             len(policy), len(deity.outliers) if deity else 0,
             len(times.outliers) if times else 0, len(drift),
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
                    f"starting here with “{first.form}”. Add “:00” to the bare "
                    f"hours to match?{others}"),
                confidence="medium",
            ))
            n += 1
    return findings
