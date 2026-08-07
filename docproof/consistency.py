"""One term, written more than one way.

The brief asks for compound-word consistency — *blood-cursed* against
*bloodcursed* against *blood cursed*, *safe keeping* against *safekeeping* —
and it is the one rule per-paragraph review structurally cannot do. A model
reading chunk 4 has no idea what chunk 40 said. Finding this needs the whole
document at once, which is exactly what a deterministic scan is for.

It **asks and never corrects**, for a reason worth stating. Detection here is
mechanical: strip the hyphens and spaces, and two spellings of one term
collapse to the same key. But that same test cannot tell an inconsistency from
a distinction — *awhile* and *a while* mean different things, as do *everyday*
and *every day*. The known pairs are excluded by name, and the rest go to the
author as a question, because which form a book uses is the author's to settle
and getting it wrong silently would be worse than not asking.
"""
from __future__ import annotations

import logging
import re
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

_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")

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
class ConsistencyReport:
    ran: bool = False
    terms: tuple[Inconsistency, ...] = ()

    @property
    def flagged(self) -> int:
        return sum(len(t.outliers) for t in self.terms)


def _key(form: str) -> str:
    return re.sub(r"[-\s’']", "", form).lower()


def _structure(form: str) -> str:
    """Case- and apostrophe-glyph-folded, but otherwise structure-preserving.

    Unlike ``_key`` this *keeps* spaces and hyphens, so *blood cursed*,
    *blood-cursed* and *bloodcursed* stay three distinct structures while
    *You should* and *you should* — and *ANIMALS* and *animals* — become one.
    That is the whole distinction this scan is allowed to flag: a structural
    difference between spellings of a term, never a capitalization or a
    straight-versus-curly apostrophe difference. English capitalizes the first
    word of every sentence, so almost any common word appears both lowercased
    and sentence-initial; folding case here is what keeps that from reading as
    an inconsistency."""
    return form.lower().replace("’", "'")


@dataclass
class _Group:
    counts: Counter = field(default_factory=Counter)
    where: list[Occurrence] = field(default_factory=list)


def find_inconsistencies(paragraphs: Sequence[ParagraphRef], *,
                         enabled: bool = True, min_length: int = 7,
                         min_dominance: int = 2) -> ConsistencyReport:
    """Terms this manuscript writes more than one way.

    `min_length` keeps short words out — the shorter the key, the more likely
    two forms are unrelated English rather than one term. `min_dominance` is
    how many times the majority form must outnumber a minority one before the
    minority reads as a slip rather than a second, equally deliberate choice.
    """
    if not enabled:
        return ConsistencyReport(ran=False)

    groups: dict[str, _Group] = defaultdict(_Group)
    for para in paragraphs:
        words = list(_WORD.finditer(para.text))
        for i, m in enumerate(words):
            forms = [(m.group(0), m.start(), m.end())]
            # The open-compound spelling of the same term is two words, so a
            # scan that only looked at single tokens would miss exactly the
            # case the brief names first.
            if i + 1 < len(words):
                nxt = words[i + 1]
                if para.text[m.end():nxt.start()] == " ":
                    forms.append((para.text[m.start():nxt.end()],
                                  m.start(), nxt.end()))
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

    report = ConsistencyReport(ran=True, terms=tuple(terms))
    log.info("Consistency scan: %d term(s) written more than one way, "
             "%d occurrence(s) to ask about", len(terms), report.flagged)
    return report


def to_findings(report: ConsistencyReport, paragraphs: Sequence[ParagraphRef],
                start_id: int = 1) -> list[Finding]:
    """One query per outlier occurrence, anchored to the sentence it sits in.

    Queries, not corrections: which spelling a book uses is the author's
    decision, and this scan cannot tell a slip from a distinction."""
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
    return findings
