"""Cheap-detector adapters: spellscan, LanguageTool, Sapling.

Three thin :class:`DetectorAdapter` wrappers over DocProof's own detector
functions. They share one shape — resolve the scope to paragraphs, build the
``ParagraphRef`` list those detectors read, run the detector, and hand back
located :class:`GFinding`\\s — but differ in what they cost and what they
promise:

* **spellscan** and **LanguageTool** run entirely *locally* (a bundled Hunspell
  dictionary; a local LanguageTool JVM). No network, no per-call billing, so
  ``cost_usd`` is ``0.0`` and no token bucket is threaded — only ``usage``'s
  call counter ticks, so the ledger records that a local pass ran.
* **Sapling** bills *per character*. This adapter never lets a scope past a hard
  character budget: it sums the scoped text up front and, if that exceeds the
  budget (or no API key is set), it *refuses* — empty findings, zero cost, a
  coverage note saying why — without ever calling the billed endpoint.

Every adapter is read-only with respect to both the manuscript and the DocProof
package: it calls DocProof's detector functions, it does not import a vendor SDK.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from docproof import languagetool, sapling, spellscan
from docproof.models import ParagraphRef, Usage

from galley.adapters import AdapterResult, Scope
from galley.contracts import GFinding, Manuscript, Provenance, Span

# --- shared plumbing ---------------------------------------------------------


def _refs(ms: Manuscript, para_ids: list[str]) -> list[ParagraphRef]:
    """The scoped paragraphs as ``ParagraphRef``s the detectors can read.

    Galley's ``Manuscript`` carries only ``para_id -> text``; the detector
    functions want a ``ParagraphRef``. Part/location/style get sensible body
    defaults, and every paragraph is reviewable — the scope already decided what
    to look at.
    """

    return [
        ParagraphRef(
            para_id=pid,
            part="word/document.xml",
            location="body",
            text=ms.text_of(pid),
            style="",
            reviewable=True,
        )
        for pid in para_ids
    ]


# --- spellscan ---------------------------------------------------------------

# spellscan reports *terms*, not spans, so a found word is located by searching
# the paragraph it was seen in. Apostrophe styles are folded together because
# spellscan normalises the curly form to a straight one in its surface strings.
def _locate(text: str, word: str) -> tuple[int, int] | None:
    """First occurrence of ``word`` in ``text`` as a ``(start, end)`` span.

    Word-boundary anchored so ``"teh"`` does not match inside ``"tether"``.
    Returns ``None`` when the term cannot be found (e.g. a surface that no
    longer slices its paragraph after normalisation), so the caller can record a
    coverage note rather than emit a bogus span.
    """

    if not word:
        return None
    # Fold ' and ’ so a normalised surface still matches the paragraph's text.
    pattern = re.escape(word).replace("'", "['’]").replace("’", "['’]")
    m = re.search(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", text)
    if m is None:
        return None
    return m.start(), m.end()


@dataclass
class SpellscanAdapter:
    """Wrap :func:`docproof.spellscan.scan` as a local, free detector.

    Emits one finding per flagged term (a spelling candidate, a repeated unknown
    word, or a demoted near-duplicate name), located at its first occurrence in
    the scoped text. Protected lexicon words — the author's own names — are never
    emitted; they are the do-not-flag list, not errors. spellscan proposes but
    never asserts a fix, so a finding's ``replace`` carries the dictionary's top
    guess when it has one and otherwise repeats ``find`` (a low-confidence flag,
    not a fabricated correction).
    """

    name: str = "spellscan"
    wave: int = 1
    dictionary: str = "en_US"
    model: str = ""

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        para_ids = scope.paragraph_ids(ms)
        refs = _refs(ms, para_ids)
        notes: list[str] = []
        if not refs:
            return AdapterResult(findings=[], coverage_notes=notes, cost_usd=0.0)

        scan = spellscan.scan(refs, dictionary=self.dictionary)
        # A local call was made; record it in the ledger without billing tokens.
        usage.api_calls += 1
        if not scan.available:
            notes.append(
                f"spellscan: no {self.dictionary} dictionary available, "
                "so the spelling scan did not run"
            )
            return AdapterResult(findings=[], coverage_notes=notes, cost_usd=0.0)

        text_of = ms.paragraphs
        findings: list[GFinding] = []
        n = 0

        def emit(word: str, para_ids_seen, error_type: str, replace: str) -> None:
            nonlocal n
            for pid in para_ids_seen:
                if pid not in text_of:
                    continue
                span = _locate(text_of[pid], word)
                if span is None:
                    continue
                start, end = span
                n += 1
                findings.append(
                    GFinding(
                        id=f"{self.name}-{self.wave}-{n:04d}",
                        error_type=error_type,
                        span=Span(pid, start, end),
                        find=word,
                        replace=replace or word,
                        note=f"Out-of-dictionary term: {word!r}.",
                        confidence="low",
                        provenance=Provenance(
                            detector=self.name,
                            wave=self.wave,
                            model=self.model,
                            cost_usd=0.0,
                        ),
                    )
                )
                return  # one finding per term, at its first location
            notes.append(f"spellscan: could not locate a span for {word!r}")

        for cand in scan.candidates:
            replace = cand.suggestions[0] if cand.suggestions else ""
            emit(cand.word, cand.para_ids, "spelling", replace)
        for cand in scan.recurring:
            replace = cand.suggestions[0] if cand.suggestions else ""
            emit(cand.word, cand.para_ids, "spelling", replace)
        for nd in scan.near_duplicates:
            # A rarer name one edit from a dominant twin: a variant, not a typo.
            para_ids_seen = tuple(
                pid for pid in para_ids if _locate(text_of.get(pid, ""), nd.word)
            )
            emit(nd.word, para_ids_seen, "name_variant", nd.suggestion)

        return AdapterResult(findings=findings, coverage_notes=notes, cost_usd=0.0)


# --- LanguageTool ------------------------------------------------------------


@dataclass
class LanguageToolAdapter:
    """Wrap :func:`docproof.languagetool.propose` as a local, free detector.

    LanguageTool runs as a local JVM server — no network, no per-call cost — so
    ``cost_usd`` is ``0.0``. The server is torn down in a ``finally`` after each
    run so a scan never leaves a JVM behind. Each proposed rewrite becomes one
    located finding; the rule category is not readily exposed on the candidate,
    so ``error_type`` is a flat ``"languagetool"``.
    """

    name: str = "languagetool"
    wave: int = 1
    dictionary: str = "en-US"
    model: str = ""

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        para_ids = scope.paragraph_ids(ms)
        refs = _refs(ms, para_ids)
        notes: list[str] = []
        if not refs:
            return AdapterResult(findings=[], coverage_notes=notes, cost_usd=0.0)

        if not languagetool.AVAILABLE:
            notes.append(
                "languagetool: not installed, so the mechanical-floor scan "
                "did not run"
            )
            return AdapterResult(findings=[], coverage_notes=notes, cost_usd=0.0)

        try:
            candidates = languagetool.propose(refs, dictionary=self.dictionary)
        finally:
            # Always stop the local server, even if propose() raised.
            languagetool.shutdown()

        usage.api_calls += 1  # a local scan ran; no tokens billed
        findings: list[GFinding] = []
        for n, cand in enumerate(candidates, start=1):
            findings.append(
                GFinding(
                    id=f"{self.name}-{self.wave}-{n:04d}",
                    error_type="languagetool",
                    span=Span(cand.para_id, cand.start, cand.end),
                    find=cand.original,
                    replace=cand.replacement,
                    note="LanguageTool mechanical-floor candidate.",
                    confidence="medium",
                    provenance=Provenance(
                        detector=self.name,
                        wave=self.wave,
                        model=self.model,
                        cost_usd=0.0,
                    ),
                )
            )

        return AdapterResult(findings=findings, coverage_notes=notes, cost_usd=0.0)


# --- Sapling (budget-fenced) -------------------------------------------------

# A module default cap on how much text one Sapling scope may cover, used when a
# scope names no char_budget of its own. Chosen well under a whole novel so an
# unbounded scope can never accidentally bill the whole book.
DEFAULT_CHAR_BUDGET = 40_000

# Sapling bills per character. This rate turns a dollar allowance into a
# character allowance so budget_usd caps the scope the same way char_budget
# does; deliberately conservative (~$0.06 per 1,000 characters).
USD_PER_CHAR = 6.25e-5


@dataclass
class SaplingAdapter:
    """Wrap :func:`docproof.sapling.check_paragraphs` behind a hard char budget.

    Sapling is the one billed detector here, so this adapter refuses before it
    can overspend. It resolves the effective character budget as the *minimum*
    of the scope's ``char_budget`` (or :data:`DEFAULT_CHAR_BUDGET`) and the
    characters ``budget_usd`` can afford, sums the scoped text, and if that total
    exceeds the budget it returns an empty result with a coverage note **without
    calling the billed endpoint**. With no API key it refuses the same way. Only
    a scope that is both affordable and keyed reaches ``check_paragraphs``.
    """

    name: str = "sapling"
    wave: int = 1
    api_key: str | None = None  # None -> read SAPLING_API_KEY at run time
    variety: str | None = None
    default_char_budget: int = DEFAULT_CHAR_BUDGET
    model: str = "sapling"

    def _key(self) -> str:
        if self.api_key is not None:
            return self.api_key
        return os.environ.get("SAPLING_API_KEY", "")

    def _char_budget(self, scope: Scope, budget_usd: float) -> int:
        scope_cap = (
            scope.char_budget
            if scope.char_budget is not None
            else self.default_char_budget
        )
        affordable = int(max(0.0, budget_usd) / USD_PER_CHAR)
        return min(scope_cap, affordable)

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        para_ids = scope.paragraph_ids(ms)
        pairs = [(pid, ms.text_of(pid)) for pid in para_ids]
        total_chars = sum(len(text) for _, text in pairs)
        char_budget = self._char_budget(scope, budget_usd)

        key = self._key()
        if not key:
            note = (
                "sapling: no SAPLING_API_KEY set, so the paid grammar pass was "
                "skipped (no call made)"
            )
            return AdapterResult(findings=[], coverage_notes=[note], cost_usd=0.0)

        if total_chars > char_budget:
            note = (
                f"sapling: declined scope — {total_chars} characters exceeds the "
                f"{char_budget}-character budget (no call made)"
            )
            return AdapterResult(findings=[], coverage_notes=[note], cost_usd=0.0)

        # Affordable and keyed: this is the only path that bills. Never reached
        # in a unit test (the no-key / over-budget refusals are what is tested).
        edits = sapling.check_paragraphs(pairs, key, variety=self.variety)
        cost = total_chars * USD_PER_CHAR
        usage.sapling_chars += total_chars
        usage.sapling_cost += cost

        text_of = ms.paragraphs
        findings: list[GFinding] = []
        for n, e in enumerate(edits, start=1):
            if e.para_id not in text_of:
                continue
            findings.append(
                GFinding(
                    id=f"{self.name}-{self.wave}-{n:04d}",
                    error_type=e.general_error_type or "grammar",
                    span=Span(e.para_id, e.start, e.end),
                    find=e.original,
                    replace=e.replacement,
                    note=sapling.describe(e),
                    confidence="medium",
                    provenance=Provenance(
                        detector=self.name,
                        wave=self.wave,
                        model=self.model,
                        cost_usd=cost,
                    ),
                )
            )

        return AdapterResult(findings=findings, coverage_notes=[], cost_usd=cost)


__all__ = [
    "SpellscanAdapter",
    "LanguageToolAdapter",
    "SaplingAdapter",
    "DEFAULT_CHAR_BUDGET",
    "USD_PER_CHAR",
]
