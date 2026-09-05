"""Do the book's own pointers resolve? A $0, report-only referee.

DocProof already asks whether citations are *styled* consistently
(genrescans.citation_format flags a book mixing author-year with numbered
brackets). Nothing asks the cheaper and more damning question a copyeditor
answers with a highlighter on a printout: does every "(Smith, 2020)" have a
Smith 2020 in the reference list, does every reference entry get cited, and
does "see chapter 9" point at a chapter that exists? Those are internal
bookkeeping failures — both halves of every check live inside the manuscript,
so a deterministic scan can referee them with no model, no network, and no
external claim about the world.

Hard lines this module holds, in the same spirit as genrescans and toccheck:

* **Report-only.** It produces a CiteReport, never a Finding, never an edit.
  Which half of a mismatch is wrong — the citation or the entry, the cross-ref
  or the chapter count — is the author's to settle; the operator (or an agent
  reading the report) raises margin queries. It does not fact-check whether
  Smith 2020 exists in the world, and it does not restyle anything.

* **Conservative by design.** A false "unresolved" query spends author
  attention on nothing, so wherever parsing is uncertain the scan stays
  silent: reference-list paragraphs that don't parse as author-year entries
  are never flagged (bibliographic formatting variety is huge), citation
  candidates whose "surname" is a common sentence word are dropped via a
  stop-list, and a year that differs from an entry only by a disambiguating
  letter (2020 vs 2020a) counts as matched.

* **Auto-skip is a normal result.** A book with no reference list gets only
  the cross-reference checks — has_references=False and zero citation issues
  is what a novel looks like, not an error. Likewise a book with no numbered
  chapter headings skips chapter-ref checking, and a book that never captions
  its figures (or tables) skips figure-ref (table-ref) checking: you cannot
  fail to resolve against a numbering scheme the book doesn't use.

Pure stdlib (re, dataclasses, typing, logging) so it can run anywhere the
pipeline does, including under the test suite's no-network guarantee.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Sequence

from .models import ParagraphRef

log = logging.getLogger("docproof.citecheck")

# Locations whose prose carries citations and cross-references. Footnotes and
# endnotes are included on purpose — academic books put half their citations
# there — headers/footers/tables are not (running heads repeat, table cells
# rarely cite in resolvable author-year form).
_PROSE_LOCATIONS = ("body", "footnote", "endnote")

_TERMINAL = ".!?…"
_SHORT_LINE = 60          # a heading-shaped line is shorter than this
_ENTRY_PREVIEW = 80       # how much of a reference entry an issue quotes
_RENDER_CAP = 40          # issue lines render() prints before "+N more"
_MIN_ENTRIES = 3          # a "reference section" must parse at least this many


_REF_HEADING = re.compile(
    r"^(?:references|bibliography|works\s+cited|reference\s+list|sources)\s*:?$",
    re.IGNORECASE)

# A surname token: capitalized, at least two letters, apostrophes and hyphens
# allowed (O'Brien, Smith-Jones). Deliberately excludes initials like "J." so
# "Smith, J. (2020)" never yields a bogus second author.
_NAME = r"[A-Z][A-Za-z'’\-]+"
# Plausible publication years, 1500-2099, with an optional disambiguating
# letter (2020a). The floor keeps page-like numbers ("p. 1423" is fine but
# "1423" alone predates movable type in English printing anyway).
_YEAR = r"(?:1[5-9]\d\d|20\d\d)[a-z]?"

_ENTRY_HEAD = re.compile(rf"^({_NAME}),\s")
_YEAR_ANY = re.compile(rf"\b({_YEAR})\b")


_PAREN = re.compile(r"\(([^()]*)\)")
# One citation inside a parenthetical, after splitting on ";". Allows an
# optional connective prefix, one "& / and" co-author, "et al.", an optional
# comma before the year, and an optional page tail.
_SEGMENT = re.compile(
    rf"^(?:(?:[Ss]ee\s+[Aa]lso|[Ss]ee|[Ee]\.g\.,?|[Cc]f\.)\s+)?"
    rf"({_NAME})"
    rf"(?:\s+(?:&|and)\s+{_NAME})?"
    rf"(?:\s+et\s+al\.?)?"
    rf"\s*,?\s+"
    rf"({_YEAR})"
    rf"(?:\s*,\s*pp?\.?\s*\d+(?:\s*[-–—]\s*\d+)?)?"
    rf"$")
_NARRATIVE = re.compile(
    rf"\b({_NAME})(?:\s+(?:and|&)\s+{_NAME})?(?:\s+et\s+al\.?)?"
    rf"\s+\(({_YEAR})\)")

# Words that satisfy the surname shape but are ordinary sentence material —
# "(See 2020)" is a stage direction, not a citation. Compared casefolded with
# trailing periods stripped, so "e.g." and "E.g" both land.
_STOPLIST = frozenset((
    "the", "in", "see", "according", "after", "since", "as", "by", "from",
    "for", "when", "with", "e.g", "i.e", "cf",
))


_CH_HEADING = re.compile(r"^chapter\s+([A-Za-z0-9]+(?:-[A-Za-z]+)?)\b",
                         re.IGNORECASE)
_CH_REF = re.compile(r"\b(?:see\s+)?chapters?\s+(\d{1,4}|[A-Za-z]+(?:-[A-Za-z]+)?)\b",
                     re.IGNORECASE)

_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _word_number(token: str) -> int | None:
    """"three" -> 3, "twenty" -> 20, "twenty-three" -> 23; None otherwise."""
    t = token.casefold()
    if t in _UNITS:
        return _UNITS[t]
    if t in _TENS:
        return _TENS[t]
    if "-" in t:
        tens, _, unit = t.partition("-")
        if tens in _TENS and unit in _UNITS and _UNITS[unit] < 10:
            return _TENS[tens] + _UNITS[unit]
    return None


def _roman_number(token: str) -> int | None:
    t = token.casefold()
    if not t or any(c not in _ROMAN for c in t):
        return None
    total = 0
    for i, c in enumerate(t):
        v = _ROMAN[c]
        if i + 1 < len(t) and _ROMAN[t[i + 1]] > v:
            total -= v
        else:
            total += v
    return total


def _chapter_number(token: str, *, allow_roman: bool) -> int | None:
    """Parse a chapter designator. Roman numerals are trusted only for
    HEADINGS (where "Chapter IV" is a convention); in running prose too many
    ordinary words are technically roman ("mix" = 1009), so cross-refs parse
    digits and number words only."""
    if token.isdigit():
        n = int(token)
    else:
        n = _word_number(token)
        if n is None and allow_roman:
            n = _roman_number(token)
    if n is None or n <= 0 or n > 400:      # 400: sanity, not doctrine
        return None
    return n



# Case-sensitive on purpose: captions and formal cross-refs capitalize the
# label; lowercase "the table below" prose must not match.
_CAPTION = re.compile(r"^(Figure|Fig\.|Table)\s+(\d+(?:\.\d+)*)")
_FT_REF = re.compile(r"\b(?:see\s+)?(Figure|Fig\.|Table)\s+(\d+(?:\.\d+)*)\b")



@dataclass(frozen=True)
class CiteIssue:
    kind: str        # unmatched_citation | unmatched_entry | chapter_ref | figure_ref | table_ref
    para_id: str     # where it was seen — every issue lives in a paragraph
    text: str        # the citation/reference/cross-ref surface as it appears
    detail: str      # what didn't resolve and what it was compared against


@dataclass(frozen=True)
class CiteReport:
    has_references: bool
    in_text_citations: int
    reference_entries: int
    chapters_found: int
    figures_found: int
    tables_found: int
    issues: tuple[CiteIssue, ...]

    def to_json(self) -> dict:
        return {
            "has_references": self.has_references,
            "in_text_citations": self.in_text_citations,
            "reference_entries": self.reference_entries,
            "chapters_found": self.chapters_found,
            "figures_found": self.figures_found,
            "tables_found": self.tables_found,
            "issues": [
                {"kind": i.kind, "para_id": i.para_id,
                 "text": i.text, "detail": i.detail}
                for i in self.issues
            ],
        }



def _is_heading_shaped(p: ParagraphRef) -> bool:
    t = p.text.strip()
    if not t:
        return False
    if p.style.startswith("Heading"):
        return True
    return len(t) < _SHORT_LINE and t[-1] not in _TERMINAL


def _is_ref_heading(p: ParagraphRef) -> bool:
    return (p.location == "body" and _is_heading_shaped(p)
            and bool(_REF_HEADING.match(p.text.strip())))


def _parse_entries(paras, start: int, end: int):
    """(para_index, (surname, year), preview) for each section paragraph that
    parses as an author-year entry. Non-parsing paragraphs are skipped in
    silence — flagging bibliographic formatting we don't understand would be
    all noise."""
    entries = []
    for i in range(start, end):
        p = paras[i]
        if p.location != "body":
            continue
        t = p.text.strip()
        head = _ENTRY_HEAD.match(t)
        if not head:
            continue
        year = _YEAR_ANY.search(t)
        if not year:
            continue
        key = (head.group(1).casefold(), year.group(1))
        entries.append((i, key, t[:_ENTRY_PREVIEW]))
    return entries


def _base(key):
    """(smith, 2020a) -> (smith, 2020): the letter only disambiguates within
    one author-year pair, so matching across it is never a real mismatch."""
    name, year = key
    return (name, year[:4])


def _stopped(name: str) -> bool:
    return name.casefold().rstrip(".") in _STOPLIST


def _find_citations(paras, ref_start: int):
    """Every author-date citation in the prose before the reference section.
    Yields (key, display_name, para_index, char_offset, surface)."""
    out = []
    for i in range(ref_start):
        p = paras[i]
        if p.location not in _PROSE_LOCATIONS or not p.text:
            continue
        for pm in _PAREN.finditer(p.text):
            inner = pm.group(1)
            pos = pm.start(1)
            for seg in inner.split(";"):
                stripped = seg.strip()
                sm = _SEGMENT.match(stripped)
                if sm and not _stopped(sm.group(1)):
                    off = pos + seg.index(stripped) if stripped else pos
                    out.append(((sm.group(1).casefold(), sm.group(2)),
                                sm.group(1), i, off, stripped))
                pos += len(seg) + 1
        for nm in _NARRATIVE.finditer(p.text):
            if _stopped(nm.group(1)):
                continue
            out.append(((nm.group(1).casefold(), nm.group(2)),
                        nm.group(1), i, nm.start(), nm.group(0)))
    return out


def check(paragraphs: Sequence[ParagraphRef]) -> CiteReport:
    paras = list(paragraphs)

    # Prefer the first candidate heading whose section parses like a real
    # reference list; a mid-book short line that merely reads "Sources" then
    # cannot shadow the actual References at the back.
    section = None
    fallback = None
    for c, p in enumerate(paras):
        if not _is_ref_heading(p):
            continue
        end = next((j for j in range(c + 1, len(paras))
                    if paras[j].style.startswith("Heading")), len(paras))
        entries = _parse_entries(paras, c + 1, end)
        if fallback is None:
            fallback = (c, entries)
        if len(entries) >= _MIN_ENTRIES:
            section = (c, entries)
            break
    if section is None:
        section = fallback
    if section is not None:
        ref_start, entries = section
    else:
        ref_start, entries = len(paras), []
    has_references = len(entries) >= _MIN_ENTRIES
    if section is not None:
        log.info("citecheck: reference section at %s with %d parsed entries",
                 paras[ref_start].para_id, len(entries))

    citations = _find_citations(paras, ref_start)

    # kind -> list of (para_index, offset, seq, CiteIssue); merged and sorted
    # into document order at the end.
    raw_issues: list[tuple[int, int, int, CiteIssue]] = []
    seq = 0

    if has_references:
        entry_keys = {key for _, key, _ in entries}
        entry_bases = {_base(k) for k in entry_keys}
        cite_keys = {c[0] for c in citations}
        cite_bases = {_base(k) for k in cite_keys}

        # one unmatched_citation per KEY, at its first occurrence
        first: dict[tuple, tuple] = {}
        counts: dict[tuple, int] = {}
        for key, disp, i, off, surface in citations:
            counts[key] = counts.get(key, 0) + 1
            if key not in first:
                first[key] = (disp, i, off, surface)
        for key in first:
            if key in entry_keys or _base(key) in entry_bases:
                continue
            disp, i, off, surface = first[key]
            n = counts[key]
            raw_issues.append((i, off, seq, CiteIssue(
                "unmatched_citation", paras[i].para_id, surface,
                f"cited {n}x in the text but none of the "
                f"{len(entries)} reference entries matches "
                f"{disp} {key[1]}")))
            seq += 1

        seen_entry_keys = set()
        for i, key, preview in entries:
            if key in seen_entry_keys:
                continue          # duplicate entry; one verdict is enough
            seen_entry_keys.add(key)
            if key in cite_keys or _base(key) in cite_bases:
                continue
            raw_issues.append((i, 0, seq, CiteIssue(
                "unmatched_entry", paras[i].para_id, preview,
                f"listed in the references but never cited in the text "
                f"(compared against {len(cite_keys)} distinct in-text "
                f"citation keys)")))
            seq += 1

    heading_paras = set()
    chapter_numbers = []
    for i, p in enumerate(paras):
        if p.location != "body" or not _is_heading_shaped(p):
            continue
        m = _CH_HEADING.match(p.text.strip())
        if not m:
            continue
        n = _chapter_number(m.group(1), allow_roman=True)
        if n is not None:
            heading_paras.add(i)
            chapter_numbers.append(n)
    chapters_found = max(chapter_numbers, default=0)

    if chapter_numbers:   # a book without numbered chapters is normal: skip
        first_ref: dict[int, tuple] = {}
        ref_counts: dict[int, int] = {}
        for i in range(ref_start):
            p = paras[i]
            if p.location not in _PROSE_LOCATIONS or i in heading_paras:
                continue
            for m in _CH_REF.finditer(p.text):
                n = _chapter_number(m.group(1), allow_roman=False)
                if n is None or n <= chapters_found:
                    continue
                ref_counts[n] = ref_counts.get(n, 0) + 1
                if n not in first_ref:
                    first_ref[n] = (i, m.start(), m.group(0))
        for n, (i, off, surface) in first_ref.items():
            raw_issues.append((i, off, seq, CiteIssue(
                "chapter_ref", paras[i].para_id, surface,
                f"refers to chapter {n} ({ref_counts[n]}x) but the highest "
                f"chapter heading found is chapter {chapters_found}")))
            seq += 1

    captions = {"figure": set(), "table": set()}
    caption_counts = {"figure": 0, "table": 0}
    caption_paras = set()
    for i, p in enumerate(paras):
        if p.location != "body":
            continue
        m = _CAPTION.match(p.text.strip())
        if not m:
            continue
        kind = "table" if m.group(1) == "Table" else "figure"
        captions[kind].add(m.group(2))
        caption_counts[kind] += 1
        caption_paras.add(i)

    for kind, label in (("figure", "Figure"), ("table", "Table")):
        if not captions[kind]:   # the book doesn't label these: skip
            continue
        first_ft: dict[str, tuple] = {}
        ft_counts: dict[str, int] = {}
        for i in range(ref_start):
            p = paras[i]
            if p.location not in _PROSE_LOCATIONS or i in caption_paras:
                continue
            for m in _FT_REF.finditer(p.text):
                mkind = "table" if m.group(1) == "Table" else "figure"
                if mkind != kind:
                    continue
                ident = m.group(2)
                if ident in captions[kind]:
                    continue
                # "Figure 4" against captions Figure 4.1, 4.2 is a reference
                # to the group, not a dangling pointer — stay silent.
                if any(c.startswith(ident + ".") for c in captions[kind]):
                    continue
                ft_counts[ident] = ft_counts.get(ident, 0) + 1
                if ident not in first_ft:
                    first_ft[ident] = (i, m.start(), m.group(0))
        for ident, (i, off, surface) in first_ft.items():
            raw_issues.append((i, off, seq, CiteIssue(
                f"{kind}_ref", paras[i].para_id, surface,
                f"no {label} {ident} caption found; the book captions "
                f"{caption_counts[kind]} {kind}(s): "
                f"{label} {', '.join(sorted(captions[kind]))}")))
            seq += 1

    raw_issues.sort(key=lambda t: (t[0], t[1], t[2]))
    return CiteReport(
        has_references=has_references,
        in_text_citations=len(citations),
        reference_entries=len(entries),
        chapters_found=chapters_found,
        figures_found=caption_counts["figure"],
        tables_found=caption_counts["table"],
        issues=tuple(t[3] for t in raw_issues),
    )



_KIND_ORDER = ("unmatched_citation", "unmatched_entry",
               "chapter_ref", "figure_ref", "table_ref")


def render(r: CiteReport) -> str:
    lines = ["citecheck — citations & cross-references"]
    lines.append("has_references: "
                 + ("yes" if r.has_references else
                    "no (citation matching skipped — normal for a book "
                    "without a reference list)"))
    lines.append(f"in-text citations: {r.in_text_citations}   "
                 f"reference entries: {r.reference_entries}")
    lines.append(f"chapters found: {r.chapters_found}   "
                 f"figures found: {r.figures_found}   "
                 f"tables found: {r.tables_found}")
    if not r.issues:
        lines.append("issues: none")
    else:
        lines.append(f"issues: {len(r.issues)}")
        shown = 0
        overflow = 0
        for kind in _KIND_ORDER:
            group = [i for i in r.issues if i.kind == kind]
            if not group:
                continue
            lines.append(f"{kind} ({len(group)}):")
            for iss in group:
                if shown >= _RENDER_CAP:
                    overflow += 1
                    continue
                lines.append(f"  {iss.para_id}  {iss.text}  — {iss.detail}")
                shown += 1
        if overflow:
            lines.append(f"  +{overflow} more")
    lines.append("report-only: nothing was edited; queries are the "
                 "operator's to raise. This check does not fact-check "
                 "sources and does not restyle citations.")
    return "\n".join(lines)
