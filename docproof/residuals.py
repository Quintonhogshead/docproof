"""Residual-coverage queries: assert a rule finished, or say where it did not.

The comparison against a two-pass human proofread found the pattern that costs
the most reviewer trust (DP-002): a rule applied to MOST of its matches. The
model spelled out ~130 numbers and left "98%", "17 minutes" and "8 seconds"
as digits — including ~20 instances of the book's own central 1%/99% motif. A
rule absent is a scope decision; a rule applied to some-but-not-all matches
teaches the reviewer they cannot stop re-checking anything.

Deterministic sweeps already prove completion by re-scanning their own
patterns. The number rules cannot be full sweeps — "a 9 mm round" and
"Highway 1" are judgment calls, so those conversions belong to a read — but
their TRIGGER is a pattern, and a pattern can be re-scanned. So after validation
this module scans the reviewed text for every trigger site no validated edit
touched. A site that is unambiguously running prose (a bare cardinal counted by
a plain noun, "42 people") is converted here as a silent tracked change; a site
that is a label, a measurement, or otherwise a judgment call is left as digits
and raised as a margin query naming the rule and the spelled form. Either way
the pass ends with the rule executed or accounted for, never silently partial.

The conversions are the plainest cases only, and each is a tracked change the
editor can reject, so a false apply costs a click and a false query costs a
margin note the author can wave off. The percent and ordinal rules stay queries
throughout — those are judgment calls more often than the bare cardinal is.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from .models import Finding, ParagraphRef
from .sweeps import cardinal_word, ordinal_word, sentence_window

log = logging.getLogger("docproof.residuals")

# Bare integers of one to three digits, standing alone: not part of a larger
# number (1,200 / 3.5 / 8:30), not glued to a word or hyphen (9mm, 5-year),
# not a percentage (the percent rule owns those), not preceded by a currency
# or section mark. Sentence-final digits still match — the follow guard only
# excludes a dot that continues into another digit.
_NUMERAL = re.compile(
    r"(?<![\w.,:$€£§#/–—\-])"
    r"(\d{1,3})"
    r"(?![\w/–—\-'’]|[.,:]\d|\s?%)")

# What follows a numeral that makes it not-a-spelled-word after all: a time's
# meridiem (the house keeps digits with AM/PM), a degree sign, an ordinal
# suffix (the ordinal rule owns those).
_NUMERAL_TAIL = re.compile(r"\s?(?:[ap]\.?m\.?\b|°|(?:st|nd|rd|th)\b)",
                           re.IGNORECASE)

_PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?%")

_ORDINAL = re.compile(r"(?<![\w.,:$€£§#/–—\-])(\d{1,2})(st|nd|rd|th)\b",
                      re.IGNORECASE)


# A word before a numeral that makes it a label or reference — "Chapter 3",
# "Room 15", "Highway 42" — not a counted quantity. These keep their digits
# even in prose, so a numeral that follows one is a judgment call left to the
# margin, never converted automatically. Lowercase-compared, so "page"/"Page"
# both match.
_LABEL_BEFORE = frozenset({
    "highway", "route", "interstate", "room", "suite", "apartment", "apt",
    "unit", "floor", "level", "gate", "exit", "chapter", "page", "figure",
    "fig", "table", "line", "verse", "act", "scene", "section", "part",
    "volume", "vol", "book", "no", "number", "size", "version", "step",
    "item", "question", "rule", "article", "paragraph", "model", "type",
    # "stage" stays a label: a cancer stage converts to ROMAN numerals
    # ("stage IV") in the number_style pass, so spelling it out here would
    # fight that rule — query, never convert.
    "stage", "phase", "district", "ward", "precinct", "channel", "aisle", "row",
    "seat", "track", "lane", "platform", "grade", "psalm", "sonnet", "note",
})

# A word after a numeral that makes it a measurement or identifier — "9 mm",
# "5 ft", "40 kg" — where the house keeps the digits. A plain common noun
# ("42 people") is the counted-quantity case this pass converts; these are not.
_UNIT_AFTER = frozenset({
    "mm", "cm", "m", "km", "kg", "g", "mg", "lb", "lbs", "oz", "ft", "foot",
    "feet", "in", "yd", "ml", "l", "mph", "kph", "hz", "khz", "mhz", "ghz",
    "kb", "mb", "gb", "tb", "px", "pt", "bc", "ad", "ce", "bce", "rpm",
    "cc", "hp", "kw", "v", "amp", "amps", "db",
})


def _word_before(text: str, i: int) -> str:
    """The alphabetic word immediately before position i (skipping one run of
    spaces), or "" if what precedes is punctuation, a digit, or the start."""
    j = i
    while j > 0 and text[j - 1] in " \t ":
        j -= 1
    k = j
    while k > 0 and (text[k - 1].isalpha() or text[k - 1] in "'’"):
        k -= 1
    return text[k:j]


def _word_after(text: str, i: int) -> str:
    """The alphabetic word immediately after position i (skipping one run of
    spaces), or "" if what follows is punctuation, a digit, or the end."""
    j = i
    while j < len(text) and text[j] in " \t ":
        j += 1
    k = j
    while k < len(text) and (text[k].isalpha() or text[k] in "'’"):
        k += 1
    return text[j:k]


def _prose_numeral(text: str, start: int, end: int) -> bool:
    """Whether a bare numeral is clearly a counted quantity in running prose —
    safe to spell out — rather than a label ("Room 15"), a measurement ("9 mm"),
    or a bare number the read has to judge. Conservative on purpose: it fires
    only on the plainest "<lowercase word> N <lowercase common noun>" shape, so
    "there were 42 people" converts while "Highway 42", "aged 8 a.m." (tail
    already excluded), a sentence-initial number, and anything after a proper
    noun or unit fall through to a margin query. Getting one wrong is a tracked
    change an editor rejects, but querying a clear case is the noise we are
    removing, so the bias is toward applying only what is unambiguous."""
    before = _word_before(text, start)
    if not before or before != before.lower() or before.lower() in _LABEL_BEFORE:
        return False                          # start-of-sentence, proper noun, label
    after = _word_after(text, end)
    if len(after) < 2 or after != after.lower() or after.lower() in _UNIT_AFTER:
        return False                          # a unit, an acronym, or nothing to count
    return True


def _numeral_sites(text: str):
    for m in _NUMERAL.finditer(text):
        n = int(m.group(1))
        if n > 100:
            continue
        if _NUMERAL_TAIL.match(text, m.end()):
            continue
        word = cardinal_word(n)
        if word is None:
            continue
        if _prose_numeral(text, m.start(), m.end()):
            # Clear prose: convert it as a tracked change rather than ask.
            yield (m.start(), m.end(),
                   f"House style spells out numbers up to one hundred.", word)
        else:
            # A label, a measurement, or a bare number the read must judge:
            # leave the digits and raise a question the way this pass always has.
            yield (m.start(), m.end(),
                   f"House style spells out numbers up to one hundred — "
                   f"“{word}” for “{m.group(1)}”. This one was not converted by "
                   f"the pass; flagged so the rule is applied everywhere or "
                   f"knowingly waived, never just mostly.", None)


def _percent_sites(text: str):
    for m in _PERCENT.finditer(text):
        try:
            word = cardinal_word(int(m.group(1)))
        except ValueError:                      # a decimal percentage
            word = None
        spelled = f"{word} percent" if word else f"{m.group(1)} percent"
        yield (m.start(), m.end(),
               f"House style writes the percent sign out — “{spelled}” for "
               f"“{m.group(0).strip()}”. This one was not converted by the "
               f"pass.", None)


def _ordinal_sites(text: str):
    for m in _ORDINAL.finditer(text):
        word = ordinal_word(int(m.group(1)))
        if word is None:
            continue
        yield (m.start(), m.end(),
               f"House style spells out ordinals — “{word}” for "
               f"“{m.group(0)}”. This one was not converted by the pass.", None)


@dataclass(frozen=True)
class _Rule:
    key: str
    sites: Callable[[str], object]


_RULES: tuple[_Rule, ...] = (
    _Rule("numeral", _numeral_sites),
    _Rule("percent", _percent_sites),
    _Rule("ordinal", _ordinal_sites),
)


def _touched_spans(validated: Sequence[Finding]) -> dict[str, list[tuple[int, int]]]:
    """Anchored spans per paragraph another finding already speaks for. A
    trigger site inside one is not residue; only the untouched sites are.

    Two kinds count. A validated edit is fixing the site. A query or a withheld
    edit (an anchored `query`/`skipped_low_confidence`/`rejected_oversized`) has
    already *raised* the site — the model asked about it, or a gate deliberately
    left it alone — so converting it here would both re-apply what a gate
    declined and second-guess a question already on the record. A bare rejection
    (no anchor, or a no-anchor/overlap/duplicate discard) claims no span and so
    never suppresses a residue site."""
    covered = {"validated", "query", "skipped_low_confidence",
               "rejected_oversized"}
    spans: dict[str, list[tuple[int, int]]] = {}
    for f in validated:
        if f.status in covered and f.anchor is not None:
            spans.setdefault(f.para_id, []).append(
                (f.anchor.start, f.anchor.end))
    return spans


def _heading_shape(text: str) -> bool:
    """A short line with no terminal punctuation reads as a title, subtitle,
    or TOC entry, whatever style it carries — authors hand-format headings as
    Normal all the time, so `reviewable` alone does not exclude them. Numerals
    in a heading are typesetting, not prose: the Purpura beta converted the
    chapter subtitle "Soul-sucking 8 to 5" into "Soul-sucking eight to 5" (on
    the TOC line AND the chapter page) before this guard existed."""
    s = text.strip()
    if not s:
        return True
    if len(s.split()) >= 8:
        return False
    tail = s.rstrip("\"”’')]")
    return not tail or tail[-1] not in ".!?…:—–-"


def residual_queries(paragraphs: Sequence[ParagraphRef],
                     validated: Sequence[Finding], *,
                     max_per_rule: int = 150) -> list[Finding]:
    """Residual number-rule coverage: convert the clear prose cases, ask about
    the rest.

    A bare numeral in plain prose ("there were 42 people") is spelled out as a
    tracked change here — a silent one, since the rule is mechanical and the
    change itself is the whole message. A numeral that is a label, a
    measurement, or otherwise a judgment call ("Highway 42", "9 mm") is left as
    digits and raised as a margin query the way this pass always has, so the
    rule still ends either applied or accounted for, never silently partial. The
    percent and ordinal rules stay queries throughout: those conversions are
    more often judgment calls than the plain cardinal is.

    `paragraphs` should be the reviewable paragraphs the run actually covered:
    headings keep their numerals ("Chapter 10" is typesetting, not prose), and
    a partial run must not question text it was not asked to read. The
    conversions are validated by the caller like any other edit, so an anchor
    that no longer matches is dropped there rather than written blind."""
    touched = _touched_spans(validated)
    findings: list[Finding] = []
    dropped: dict[str, int] = {}
    counts: dict[str, int] = {}
    applied = 0
    n = 0
    for para in paragraphs:
        if not para.reviewable:
            continue
        # Page furniture — running heads and folios — is typesetting, not prose:
        # a page-number "2" in a footer is not a spelled-out-numbers site, and
        # querying it ends the run on a comment no author can act on. Headings
        # are already excluded (they are swept-only, so not reviewable); header
        # and footer paragraphs are reviewable but must be held to the same rule.
        if para.location in ("header", "footer"):
            continue
        if _heading_shape(para.text):
            continue
        spans = touched.get(para.para_id, ())
        for rule in _RULES:
            for start, end, why, replacement in rule.sites(para.text):
                if any(s < end and start < e for s, e in spans):
                    continue                     # an edit already has it
                window, lo, occurrence = sentence_window(para.text, start, end)
                if replacement is not None:
                    # A clear prose conversion: a real correction, applied
                    # silently. Not capped — every one is a change that lands,
                    # so there is no truncated-list-reads-as-complete risk.
                    n += 1
                    applied += 1
                    corrected = window[:start - lo] + replacement + window[end - lo:]
                    findings.append(Finding(
                        finding_id=f"rs-{n:04d}",
                        chunk_id="residual",
                        para_id=para.para_id,
                        error_type="number_style",
                        original_text=window,
                        occurrence=occurrence,
                        corrected_text=corrected,
                        explanation=why,
                        confidence="high",
                        silent=True))
                    continue
                if counts.get(rule.key, 0) >= max_per_rule:
                    dropped[rule.key] = dropped.get(rule.key, 0) + 1
                    continue
                counts[rule.key] = counts.get(rule.key, 0) + 1
                n += 1
                findings.append(Finding(
                    finding_id=f"rs-{n:04d}",
                    chunk_id="residual",
                    para_id=para.para_id,
                    error_type="number_style",
                    original_text=window,
                    occurrence=occurrence,
                    corrected_text=window,
                    explanation=why,
                    confidence="medium",
                    force_query=True))
    queried = sum(counts.values())
    if applied or queried:
        log.info("Residual coverage: %d number site(s) converted, %d queried "
                 "(%s).", applied, queried,
                 ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for key, lost in sorted(dropped.items()):
        # Never a silent cap: a truncated list that says nothing reads as
        # "covered everything", which is the exact failure this module exists
        # to prevent.
        log.warning("Residual coverage: %s hit the %d-query cap; %d further "
                    "site(s) are uncounted in the margin (the rule is "
                    "partial beyond what the queries show).",
                    key, max_per_rule, lost)
    return findings
