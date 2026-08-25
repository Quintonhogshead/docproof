"""Three opt-in, whole-document, QUERY-ONLY scans a genre pack may turn on.

House policy draws a hard line the rest of DocProof already respects for
fact-checking and continuity: mechanics/proofreading are hammered the same in
every genre, but a check that reasons about the world OUTSIDE the manuscript
(what year a word entered use, what a real citation style looks like) carries
hallucination risk and can never silently rewrite the author's words. Every
finding this module produces sets `force_query=True` at construction — not as
a config default that a run could override, but on the object itself — so the
guarantee holds however this module is wired in.

The three scans:

* **anachronism** — flags vocabulary that reads as later than the book's own
  stated era, against a small curated word -> earliest-plausible-year table.
  Deliberately does NOT guess the era: `era` unset is a no-op even when the
  scan is enabled, because a wrong guessed era would raise wrong questions
  with total confidence. External-authority, so query-only, no exceptions.

* **citation_format** — a deterministic regex check for a non-fiction book
  mixing two citation styles (parenthetical author-year and numbered-bracket).
  No model, no external claim beyond "these two patterns disagree" — but it
  stays query-only anyway, because which style the book intends is an editing
  decision, not a mechanical fact the way a doubled space is.

* **reading_level** — flags paragraphs whose reading level sits far from the
  book's own target band, an internal-consistency check (the band defaults to
  the book's OWN median) that could in principle correct nothing (there is no
  "right" reading level) so it stays a question by the same logic smoothing
  and continuity do.

See docproof/config.py's GenreScansConfig for the knobs, and docproof/genre.py
for how a genre pack turns these on.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

from .config import GenreScansConfig
from .models import Finding, ParagraphRef
from .sweeps import sentence_window

log = logging.getLogger("docproof.genrescans")

GENRE_SCAN_KEYS = ("anachronism", "citation_format", "reading_level")

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
# Same shape as continuity.py's sentence splitter — a lookbehind on sentence-
# ending punctuation followed by whitespace. Redefined locally rather than
# imported so this module has no dependency on continuity's internals.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# --- anachronism ---------------------------------------------------------

# Curated word -> earliest plausible year AD its modern sense entered ordinary
# use, for the sense this scan cares about (a technology, an institution, a
# coinage) — not its first citation anywhere, which is often decades earlier
# and would make the table catch nothing. Small and deliberately unambiguous:
# every entry is a textbook anachronism-checker example, not a judgment call.
# Multi-word entries are matched on the whole phrase.
_ERA_WORDS: dict[str, int] = {
    "television": 1927, "radio": 1907, "telephone": 1876, "automobile": 1898,
    "airplane": 1907, "helicopter": 1922, "computer": 1946, "internet": 1983,
    "email": 1993, "smartphone": 2000, "laptop": 1983, "website": 1993,
    "okay": 1839, "teenager": 1941, "genocide": 1944, "robot": 1921,
    "escalator": 1900, "jeans": 1908, "radar": 1941, "nylon": 1938,
    "supermarket": 1933, "motel": 1925, "skyscraper": 1888,
    "photocopy": 1942, "microwave": 1937, "astronaut": 1929,
    "credit card": 1952, "zipper": 1925, "walkie-talkie": 1939,
    "dinosaur": 1841, "electricity": 1600, "vaccine": 1800,
    "photograph": 1839, "bicycle": 1868, "typewriter": 1868,
}


def _sane_era_words() -> dict[str, int]:
    """`_ERA_WORDS` filtered through wordfreq: a table entry that is not
    ordinary English (a typo in the curated list) has a zipf frequency near
    zero and would otherwise match nothing, silently. Failing the import
    entirely (wordfreq missing) is not fatal — the unfiltered table is used
    as-is rather than losing the scan."""
    try:
        from .adjudicate import zipf
    except Exception:                                  # pragma: no cover
        return dict(_ERA_WORDS)
    sane = {}
    for word, year in _ERA_WORDS.items():
        try:
            ok = zipf(word.split()[0]) > 1.0
        except Exception:                               # pragma: no cover
            ok = True
        if ok:
            sane[word] = year
        else:
            log.warning("genrescans: era word %r failed the wordfreq sanity "
                       "check and was dropped", word)
    return sane


def anachronism_findings(paragraphs: Sequence[ParagraphRef],
                         cfg, *, start_id: int = 1) -> list[Finding]:
    """Flag words from `_ERA_WORDS` that predate `cfg.era` is impossible for —
    i.e. whose earliest plausible year is AFTER the manuscript's own stated
    era. `cfg.era` unset is a deliberate no-op: guessing the era and flagging
    against the guess is exactly the hallucination risk this scan exists to
    avoid, so nothing is raised without an explicit year."""
    if not cfg.enabled or cfg.era is None:
        return []
    table = _sane_era_words()
    findings: list[Finding] = []
    n = start_id
    for para in paragraphs:
        low = para.text.lower()
        for word, year in table.items():
            if year <= cfg.era:
                continue
            for m in re.finditer(r"\b" + re.escape(word) + r"\b", low):
                if len(findings) >= cfg.max_queries:
                    break
                start, end = m.start(), m.end()
                window, lo, occurrence = sentence_window(para.text, start, end)
                findings.append(Finding(
                    finding_id=f"ga-{n:04d}",
                    chunk_id="genre_scan",
                    para_id=para.para_id,
                    error_type="anachronism",
                    original_text=window,
                    occurrence=occurrence,
                    corrected_text=window,        # a query changes nothing
                    explanation=(
                        f"“{para.text[start:end]}” in its familiar "
                        f"sense is not usually dated earlier than {year}, "
                        f"which is after this manuscript's stated setting "
                        f"({cfg.era}). Flagged as a question only — the book "
                        f"may use the word deliberately, or the setting may "
                        f"allow it."),
                    confidence="low",
                    force_query=True))
                n += 1
            if len(findings) >= cfg.max_queries:
                break
        if len(findings) >= cfg.max_queries:
            break
    return findings


# --- citation format -------------------------------------------------------

# Two citation-style families, matched independently. Neither claims to be a
# complete citation grammar — only common enough that two DIFFERENT styles
# both showing up repeatedly in one manuscript is a real signal of mixed
# convention, not a false positive from one style's own internal variety.
_CITATION_STYLES: dict[str, re.Pattern] = {
    "parenthetical author-year": re.compile(
        r"\([A-Z][A-Za-z\-]+(?:\s(?:&|and)\s[A-Z][A-Za-z\-]+)?,?\s(?:19|20)\d{2}"
        r"(?:,\s*p{1,2}\.\s*\d+)?\)"),
    "numbered bracket": re.compile(r"\[\d{1,3}(?:[-,]\s*\d{1,3})*\]"),
}


def citation_format_findings(paragraphs: Sequence[ParagraphRef],
                             cfg, *, start_id: int = 1) -> list[Finding]:
    """Flag a manuscript that uses BOTH citation-style families at least
    `min_occurrences` times each — a book using only one style consistently
    raises nothing, whatever that style is."""
    if not cfg.enabled:
        return []
    hits: dict[str, list[tuple[ParagraphRef, int, int]]] = {
        name: [] for name in _CITATION_STYLES}
    for para in paragraphs:
        for name, pattern in _CITATION_STYLES.items():
            for m in pattern.finditer(para.text):
                hits[name].append((para, m.start(), m.end()))
    present = {name: sites for name, sites in hits.items()
              if len(sites) >= cfg.min_occurrences}
    if len(present) < 2:
        return []
    findings: list[Finding] = []
    n = start_id
    counts = ", ".join(f"{name} ({len(sites)}x)"
                       for name, sites in sorted(present.items()))
    for name, sites in sorted(present.items()):
        para, start, end = sites[0]
        if len(findings) >= cfg.max_queries:
            break
        window, lo, occurrence = sentence_window(para.text, start, end)
        findings.append(Finding(
            finding_id=f"gc-{n:04d}",
            chunk_id="genre_scan",
            para_id=para.para_id,
            error_type="citation_format",
            original_text=window,
            occurrence=occurrence,
            corrected_text=window,
            explanation=(
                f"This manuscript mixes citation styles: {counts}. This is "
                f"the first {name} citation. Raised as a question only — "
                f"pick one style and apply it throughout, if that is the "
                f"intent."),
            confidence="medium",
            force_query=True))
        n += 1
    return findings


# --- reading level -----------------------------------------------------------

def _ari(text: str) -> float | None:
    """Automated Readability Index: 4.71*(chars/word) + 0.5*(words/sentence)
    - 21.43. No syllable counter needed, unlike Flesch-Kincaid — just
    characters, words, and sentences, all available from plain regex splits.
    None when there is not enough text to make the ratio meaningful."""
    words = _WORD.findall(text)
    if len(words) < 5:
        return None
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()] or [text]
    chars = sum(len(w) for w in words)
    return (4.71 * (chars / len(words))
           + 0.5 * (len(words) / len(sentences)) - 21.43)


def reading_level_findings(paragraphs: Sequence[ParagraphRef],
                           cfg, *, start_id: int = 1) -> list[Finding]:
    """Flag paragraphs whose ARI sits more than `tolerance` from the target
    band. With `target_ari` unset, the band centers on the book's own median
    scored paragraph — an internal-consistency check, not an outside opinion
    about what the genre should read like."""
    if not cfg.enabled:
        return []
    scored: list[tuple[ParagraphRef, float]] = []
    for para in paragraphs:
        if len(_WORD.findall(para.text)) < cfg.min_words:
            continue
        ari = _ari(para.text)
        if ari is not None:
            scored.append((para, ari))
    if not scored:
        return []
    if cfg.target_ari is not None:
        target = cfg.target_ari
    else:
        values = sorted(ari for _, ari in scored)
        mid = len(values) // 2
        target = (values[mid] if len(values) % 2
                 else (values[mid - 1] + values[mid]) / 2)
    findings: list[Finding] = []
    n = start_id
    for para, ari in scored:
        if len(findings) >= cfg.max_queries:
            break
        if abs(ari - target) <= cfg.tolerance:
            continue
        direction = "denser" if ari > target else "simpler"
        window = para.text
        findings.append(Finding(
            finding_id=f"gr-{n:04d}",
            chunk_id="genre_scan",
            para_id=para.para_id,
            error_type="reading_level",
            original_text=window,
            occurrence=1,
            corrected_text=window,
            explanation=(
                f"This paragraph reads noticeably {direction} than the "
                f"book's own target (readability score {ari:.1f} vs a "
                f"{target:.1f} band, tolerance {cfg.tolerance:.1f}). Raised "
                f"as a question only — a deliberate register shift is not "
                f"an error."),
            confidence="low",
            force_query=True))
        n += 1
    return findings


def run_genre_scans(paragraphs: Sequence[ParagraphRef],
                    cfg: GenreScansConfig) -> list[Finding]:
    """Every enabled genre scan, in a stable order, id-numbered to avoid
    colliding with any other finding source. Whole-document only — the
    caller (docproof/pipeline.py) gates this on `whole`, the same way the
    consistency scan is."""
    findings: list[Finding] = []
    findings += anachronism_findings(paragraphs, cfg.anachronism)
    findings += citation_format_findings(paragraphs, cfg.citation_format)
    findings += reading_level_findings(paragraphs, cfg.reading_level)
    return findings
