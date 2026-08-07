"""Score findings against the corpus labels.

Scoring is per error type and within-type: when we score `tense_shift`, only
`tense_shift` findings on `tense_shift` cases count. A finding of some other
type on a paragraph we labeled for `tense_shift` is neither credited nor
penalized — that paragraph might carry a real issue of the other type we simply
didn't label, and guessing would corrupt the number.

Two lenses, on purpose:

  * recall is case-level — of the seeded errors of this type, how many did the
    pipeline catch (land at least one correct flag on).
  * precision is finding-level — of the flags of this type the pipeline made on
    this type's cases, how many were on a real seeded span rather than a trap or
    the wrong span.

The confidence threshold is applied in post, so one low-gate run yields the
whole precision/recall curve across low/medium/high.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import CONFIDENCE_RANK, Finding
from ..providers import estimate_cost
from .runner import EvalRun

# Error types that legitimately claim each other's spans. Empty today; when the
# run_on_sentence / comma_splice boundary or similar needs to count as a catch
# for either label, add it here rather than smearing it through the matcher.
ALIASES: dict[str, set[str]] = {}

# A finding has "acted" — reached the author — when validated or written as a
# margin query. Everything else (rejected_*, skipped_*) changed nothing.
ACTED = frozenset({"validated", "query"})


def _family(t: str) -> set[str]:
    return {t} | ALIASES.get(t, set())


def _fold(s: str) -> str:
    """Quote- and whitespace-fold for locating a label span in canonical text.
    Each replacement is one char for one char, so offsets are preserved."""
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))


def _locate(span: str, text: str) -> tuple[int, int] | None:
    i = _fold(text).find(_fold(span))
    return (i, i + len(span)) if i >= 0 else None


def _finding_range(f: Finding, text: str) -> tuple[int, int] | None:
    """The span of canonical text a finding touches. The validator's anchor is
    the minimal shrunk edit; fall back to the whole quoted sentence for findings
    that never anchored (a rejected_no_anchor still tells us where the model was
    looking)."""
    if f.anchor is not None:
        return (f.anchor.start, f.anchor.end)
    return _locate(f.original_text, text)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    (a0, a1), (b0, b1) = a, b
    # A pure insertion is zero-width; widen it by one so "insert a comma right
    # after this word" still counts as touching the word's span.
    if a0 == a1:
        a1 = a0 + 1
    if b0 == b1:
        b1 = b0 + 1
    return a0 < b1 and b0 < a1


@dataclass
class TypeScore:
    error_type: str
    seeded: int = 0            # seeded-error cases of this type
    traps: int = 0            # clean cases of this type
    caught: int = 0          # seeded cases with >=1 correct flag
    correction_exact: int = 0  # of caught, how many produced the expected fix
    tp_flags: int = 0        # flags on a real seeded span
    fp_flags: int = 0        # flags on a trap, or on a seeded case off-span
    trap_cases_flagged: int = 0

    @property
    def precision(self) -> float | None:
        d = self.tp_flags + self.fp_flags
        return self.tp_flags / d if d else None

    @property
    def recall(self) -> float | None:
        return self.caught / self.seeded if self.seeded else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class Scorecard:
    model: str
    threshold: str
    types: dict[str, TypeScore] = field(default_factory=dict)
    anchor_failures: int = 0
    total_findings: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    # -- aggregates -------------------------------------------------------
    @property
    def micro(self) -> tuple[float | None, float | None, float | None]:
        tp = sum(t.tp_flags for t in self.types.values())
        fp = sum(t.fp_flags for t in self.types.values())
        caught = sum(t.caught for t in self.types.values())
        seeded = sum(t.seeded for t in self.types.values())
        p = tp / (tp + fp) if (tp + fp) else None
        r = caught / seeded if seeded else None
        f = (2 * p * r / (p + r)) if p and r else None
        return p, r, f

    @property
    def macro(self) -> tuple[float | None, float | None, float | None]:
        def mean(vals):
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None
        return (mean(t.precision for t in self.types.values()),
                mean(t.recall for t in self.types.values()),
                mean(t.f1 for t in self.types.values()))

    @property
    def trap_fp_rate(self) -> float | None:
        flagged = sum(t.trap_cases_flagged for t in self.types.values())
        traps = sum(t.traps for t in self.types.values())
        return flagged / traps if traps else None

    @property
    def anchor_failure_rate(self) -> float | None:
        return (self.anchor_failures / self.total_findings
                if self.total_findings else None)


def score(run: EvalRun, threshold: str = "low") -> Scorecard:
    """Score one run at one confidence threshold. Call repeatedly with
    'low'/'medium'/'high' for the curve."""
    text_of = {p.para_id: p.text for p in run.doc.paragraphs}
    by_para: dict[str, list[Finding]] = {}
    for f in run.findings:
        by_para.setdefault(f.para_id, []).append(f)

    card = Scorecard(model=run.model, threshold=threshold)
    card.total_findings = len(run.findings)
    card.anchor_failures = sum(1 for f in run.findings
                               if f.status == "rejected_no_anchor")
    card.input_tokens = run.usage.input_tokens
    card.output_tokens = run.usage.output_tokens
    card.cost = estimate_cost(run.model, input_tokens=run.usage.input_tokens,
                              output_tokens=run.usage.output_tokens)

    gate = CONFIDENCE_RANK[threshold]

    def acted_of_type(para_id: str, types: set[str]) -> list[Finding]:
        return [f for f in by_para.get(para_id, [])
                if f.error_type in types and f.status in ACTED
                and CONFIDENCE_RANK[f.confidence] >= gate]

    for case in run.cases:
        para_id = next((pid for pid, cid in run.built.para_to_case.items()
                        if cid == case.id), None)
        if para_id is None:
            continue
        ts = card.types.setdefault(case.error_type, TypeScore(case.error_type))
        fam = _family(case.error_type)
        flags = acted_of_type(para_id, fam)
        text = text_of.get(para_id, "")

        if case.is_clean:
            ts.traps += 1
            ts.fp_flags += len(flags)
            if flags:
                ts.trap_cases_flagged += 1
            continue

        ts.seeded += 1
        target = _locate(case.span, text) if case.span else None
        on_span, off_span = [], []
        for f in flags:
            fr = _finding_range(f, text)
            if target is not None and fr is not None and _overlaps(fr, target):
                on_span.append(f)
            else:
                off_span.append(f)
        ts.tp_flags += len(on_span)
        ts.fp_flags += len(off_span)     # right type, wrong place — a false alarm
        if on_span:
            ts.caught += 1
            if case.correction and any(
                    _fold(" ".join(f.corrected_text.split()))
                    == _fold(" ".join(case.correction.split()))
                    or case.correction in f.corrected_text
                    for f in on_span):
                ts.correction_exact += 1

    return card


def score_curve(run: EvalRun) -> dict[str, Scorecard]:
    """The three-threshold sweep from a single run."""
    return {t: score(run, t) for t in ("low", "medium", "high")}
