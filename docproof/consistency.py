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
from typing import Sequence

from .models import Finding, ParagraphRef
from .sweeps import sentence_window

log = logging.getLogger("docproof.consistency")

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
class ConsistencyReport:
    ran: bool = False
    terms: tuple[Inconsistency, ...] = ()
    names: tuple[NameDrift, ...] = ()

    @property
    def flagged(self) -> int:
        return (sum(len(t.outliers) for t in self.terms)
                + sum(len(n.outliers) for n in self.names if not n.enforce))

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


def find_inconsistencies(paragraphs: Sequence[ParagraphRef], *,
                         enabled: bool = True, min_length: int = 7,
                         min_dominance: int = 2, names: bool = True,
                         name_dominance: int = 5,
                         name_min_count: int = 20) -> ConsistencyReport:
    """Terms this manuscript writes more than one way.

    `min_length` keeps short words out — the shorter the key, the more likely
    two forms are unrelated English rather than one term. `min_dominance` is
    how many times the majority form must outnumber a minority one before the
    minority reads as a slip rather than a second, equally deliberate choice.

    `names` also runs the proper-name diacritic scan; `name_dominance` and
    `name_min_count` set its bar for correcting rather than asking. See
    ``find_name_drift``.
    """
    if not enabled:
        return ConsistencyReport(ran=False)

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
        outliers = tuple(o for o in g.where
                         if _structure(o.form) in minority_structs)
        if outliers:
            counts = Counter({reps[s]: totals[s] for s in totals})
            terms.append(Inconsistency(key, counts, reps[dom_struct], outliers))

    drift = (find_name_drift(paragraphs, min_dominance=name_dominance,
                             min_count=name_min_count) if names else ())
    report = ConsistencyReport(ran=True, terms=tuple(terms), names=drift)
    log.info("Consistency scan: %d term(s) written more than one way and "
             "%d name(s) with diacritic drift — %d occurrence(s) to correct, "
             "%d to ask about", len(terms), len(drift), report.corrected,
             report.flagged)
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
                corrected_text=window,          # a query changes nothing
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
                    corrected_text=window,          # a query changes nothing
                    explanation=(
                        f"This manuscript spells what may be one name more "
                        f"than one way: “{o.form}” here, and elsewhere "
                        f"{others}. Is the difference deliberate? If not, "
                        f"“{drift.dominant}” is the form used most."),
                    confidence="high",
                ))
                n += 1
    return findings
