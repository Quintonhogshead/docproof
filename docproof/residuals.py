"""Residual-coverage queries: assert a rule finished, or say where it did not.

The comparison against a two-pass human proofread found the pattern that costs
the most reviewer trust (DP-002): a rule applied to MOST of its matches. The
model spelled out ~130 numbers and left "98%", "17 minutes" and "8 seconds"
as digits — including ~20 instances of the book's own central 1%/99% motif. A
rule absent is a scope decision; a rule applied to some-but-not-all matches
teaches the reviewer they cannot stop re-checking anything.

Deterministic sweeps already prove completion by re-scanning their own
patterns. The number rules cannot be sweeps — "a 9 mm round" and "Highway 1"
are judgment calls, so the conversion itself belongs to a read — but their
TRIGGER is a pattern, and a pattern can be re-scanned. So after validation
this module scans the reviewed text for every trigger site no validated edit
touched, and raises each as a margin query naming the rule and the spelled
form. The pass ends with the rule either executed or accounted for, never
silently partial.

Queries only. This module edits nothing, claims no spans, and a false
positive costs a margin note the author can wave off.
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
# meridiem (Chicago keeps digits with a.m./p.m.), a degree sign, an ordinal
# suffix (the ordinal rule owns those).
_NUMERAL_TAIL = re.compile(r"\s?(?:[ap]\.?m\.?\b|°|(?:st|nd|rd|th)\b)",
                           re.IGNORECASE)

_PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?%")

_ORDINAL = re.compile(r"(?<![\w.,:$€£§#/–—\-])(\d{1,2})(st|nd|rd|th)\b",
                      re.IGNORECASE)


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
        yield (m.start(), m.end(),
               f"House style spells out numbers up to one hundred — "
               f"“{word}” for “{m.group(1)}”. This one was not converted by "
               f"the pass; flagged so the rule is applied everywhere or "
               f"knowingly waived, never just mostly.")


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
               f"pass.")


def _ordinal_sites(text: str):
    for m in _ORDINAL.finditer(text):
        word = ordinal_word(int(m.group(1)))
        if word is None:
            continue
        yield (m.start(), m.end(),
               f"House style spells out ordinals — “{word}” for "
               f"“{m.group(0)}”. This one was not converted by the pass.")


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
    """Anchored spans per paragraph that some validated edit already covers. A
    trigger site inside one is being fixed; only the untouched sites are
    residue. Queries and rejected findings do not count — a question is not a
    conversion."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for f in validated:
        if f.status == "validated" and f.anchor is not None:
            spans.setdefault(f.para_id, []).append(
                (f.anchor.start, f.anchor.end))
    return spans


def residual_queries(paragraphs: Sequence[ParagraphRef],
                     validated: Sequence[Finding], *,
                     max_per_rule: int = 150) -> list[Finding]:
    """One margin query per trigger site the pass left as digits.

    `paragraphs` should be the reviewable paragraphs the run actually covered:
    headings keep their numerals ("Chapter 10" is typesetting, not prose), and
    a partial run must not question text it was not asked to read."""
    touched = _touched_spans(validated)
    findings: list[Finding] = []
    dropped: dict[str, int] = {}
    counts: dict[str, int] = {}
    n = 0
    for para in paragraphs:
        if not para.reviewable:
            continue
        spans = touched.get(para.para_id, ())
        for rule in _RULES:
            for start, end, why in rule.sites(para.text):
                if any(s < end and start < e for s, e in spans):
                    continue                     # an edit already has it
                if counts.get(rule.key, 0) >= max_per_rule:
                    dropped[rule.key] = dropped.get(rule.key, 0) + 1
                    continue
                counts[rule.key] = counts.get(rule.key, 0) + 1
                window, _lo, occurrence = sentence_window(para.text, start, end)
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
    if findings:
        log.info("Residual coverage: %d unconverted number-rule site(s) "
                 "queried (%s).", len(findings),
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
