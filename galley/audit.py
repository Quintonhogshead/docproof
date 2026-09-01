"""The auditor — hypotheses about what a finished run missed (Track C1).

Read-only over a completed DocProof run. It computes, mechanically, a
findings-per-1,000-words density by chapter — a chapter that reads suspiciously
clean next to its neighbors is where a miss most likely hides — samples a few
pages from the lowest-density chapters, and asks the model for structured
hypotheses about missed errors against the frozen :class:`Hypothesis` schema.

This is the science the whole agentic premise rests on (see The Galley Plan,
Part 8): if the auditor cannot locate real misses better than chance, the agent
premise dies for the price of a memo. The single model call in :func:`audit_run`
is the only paid step; everything else — density, sampling, parsing — is
deterministic and unit-tested with a fake provider.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docproof.models import Usage

from galley.contracts import Hypothesis, Manuscript

log = logging.getLogger("galley.audit")

# The model is asked for at most this many hypotheses; a reply is capped to it so
# a runaway list can never blow the budget of whatever acts on them.
MAX_HYPOTHESES = 40
# Generous ceiling: a truncated structured reply parses as empty and is recorded
# as a loss (see the stop_reason check), so the ceiling is set not to trip.
DEFAULT_MAX_TOKENS = 8000

# Which findings.json rows count toward density. Only what the run actually
# delivered — an applied edit or a margin query — is evidence the chapter was
# read; a rejected_* / skipped_* row is a candidate the pipeline threw away, and
# counting it would make a chapter the validator gutted look well-covered. The
# app auditor (``galley.brain``) counts case-file findings, which are exactly
# these two channels, so the CLI must count the same or the two disagree.
COUNTED_STATUSES = frozenset({"validated", "query"})

# Sampling floors. A "page" worth reading is a BODY paragraph of at least
# ``MIN_SAMPLE_WORDS`` words (a heading or a one-line beat tells the model
# nothing about what the review missed), and a chapter may only rank as "quiet"
# once it holds ``MIN_QUIET_CHAPTER_WORDS`` — a 40-word epigraph unit is not a
# suspiciously clean chapter, it is a tiny one.
MIN_SAMPLE_WORDS = 40
MIN_QUIET_CHAPTER_WORDS = 500

# Words below which an unterminated line is read as a heading, not prose.
_HEADING_MAX_WORDS = 8
_TERMINAL = ".!?…:\u201d\u2019\"')"

_PROMPT_PATH = Path(__file__).with_name("prompts") / "audit.md"


# ---- mechanical density (no model) -------------------------------------

@dataclass(frozen=True)
class ChapterDensity:
    """A chapter's error density: findings per 1,000 words."""

    index: int
    title: str
    words: int
    findings: int

    @property
    def per_1k(self) -> float:
        return (self.findings / self.words * 1000.0) if self.words else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "words": self.words,
            "findings": self.findings,
            "per_1k": round(self.per_1k, 3),
        }


def read_findings(results_dir: str | Path) -> list[dict[str, Any]]:
    """Read the ``findings`` array from a finished run's ``findings.json``.

    Every row comes back (:func:`chapter_densities` decides which count); a
    missing or unreadable file yields an empty list — the auditor still runs on
    density-of-zero, it just has nothing to anchor its priors to.
    """

    path = Path(results_dir) / "findings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("audit: could not read %s; treating as no findings", path)
        return []
    findings = payload.get("findings", [])
    return [f for f in findings if isinstance(f, dict)]


def is_delivered(finding: dict[str, Any]) -> bool:
    """Does a findings.json row count toward density?

    A row whose ``status`` is in :data:`COUNTED_STATUSES` does; a
    ``rejected_*``/``skipped_*``/``pending`` row never reached the author and
    is not evidence the chapter was read. A row with no ``status`` at all (the
    bare ``{"para_id": ...}`` shape the app auditor builds from case-file
    findings) counts — it is already a delivered finding.
    """

    return "status" not in finding or finding.get("status") in COUNTED_STATUSES


def _chapter_of(ms: Manuscript) -> tuple[list[tuple[int, str, tuple[str, ...]]], dict[str, int]]:
    """Return (chapter rows, para_id -> chapter index).

    A book with no declared chapters is treated as one chapter over every
    paragraph; paragraphs no chapter covers become a trailing "(unplaced)" row so
    their findings and words are never silently dropped.
    """

    rows: list[tuple[int, str, tuple[str, ...]]] = []
    para_to_ch: dict[str, int] = {}
    if ms.chapters:
        covered: set[str] = set()
        for ch in ms.chapters:
            ids = tuple(p for p in ch.para_ids if p in ms.paragraphs)
            rows.append((ch.index, ch.title, ids))
            for p in ids:
                para_to_ch[p] = ch.index
                covered.add(p)
        leftover = tuple(p for p in ms.order if p in ms.paragraphs and p not in covered)
        if leftover:
            idx = max((r[0] for r in rows), default=-1) + 1
            rows.append((idx, "(unplaced)", leftover))
            for p in leftover:
                para_to_ch[p] = idx
    else:
        ids = tuple(ms.order) or tuple(ms.paragraphs)
        rows.append((0, "(whole book)", ids))
        for p in ids:
            para_to_ch[p] = 0
    return rows, para_to_ch


def chapter_densities(
    findings: list[dict[str, Any]], ms: Manuscript
) -> list[ChapterDensity]:
    """The findings-per-1,000-words table, by chapter, in chapter order.

    Only delivered rows count (:func:`is_delivered`), so the CLI auditor —
    reading a full ``findings.json`` — and the app auditor — reading case-file
    findings — agree on what a chapter's density is.
    """

    rows, para_to_ch = _chapter_of(ms)
    counts: dict[int, int] = {r[0]: 0 for r in rows}
    for f in findings:
        if not is_delivered(f):
            continue
        pid = f.get("para_id", "")
        ch = para_to_ch.get(pid)
        if ch is not None:
            counts[ch] += 1

    out: list[ChapterDensity] = []
    for index, title, ids in rows:
        words = sum(len(ms.paragraphs.get(p, "").split()) for p in ids)
        out.append(ChapterDensity(index, title, words, counts[index]))
    return out


def is_heading_text(text: str) -> bool:
    """Whether a paragraph reads as a heading (or furniture) rather than prose.

    DocProof's chapter units keep their heading paragraph as the first line
    (``docproof.continuity.chapters``), and the manuscript contract carries no
    style, so the test is structural: an empty line, a line the continuity
    pass's own ``looks_like_chapter_heading`` recognises ("Chapter Nine",
    "Prologue", a bare number), or a short line with no terminal punctuation
    (a title, a part divider, a dateline).
    """

    t = text.strip()
    if not t:
        return True
    try:
        from docproof.continuity import looks_like_chapter_heading
        from docproof.models import ParagraphRef

        if looks_like_chapter_heading(
            ParagraphRef("", "", "body", t, "")
        ):
            return True
    except Exception:  # noqa: BLE001 - the structural test below still applies
        pass
    if len(t.split()) >= _HEADING_MAX_WORDS:
        return False
    return t[-1] not in _TERMINAL


class SampledPage(tuple):
    """A sampled ``(para_id, text)`` pair that also knows where it came from.

    A plain 2-tuple to every existing consumer (``for pid, text in samples``),
    plus ``chapter`` and ``control`` so the prompt can label the control
    chapter's page and the result can record it.
    """

    chapter: int
    control: bool

    def __new__(cls, para_id: str, text: str, chapter: int = 0,
                control: bool = False) -> "SampledPage":
        self = super().__new__(cls, (para_id, text))
        self.chapter = chapter
        self.control = control
        return self


@dataclass(frozen=True)
class SamplePlan:
    """What :func:`plan_sample` chose: the pages, and which chapter is the control.

    ``control_chapter`` is ``None`` when the book offers no non-quiet chapter to
    control against (one eligible chapter, or every chapter at zero density).
    """

    pages: tuple[SampledPage, ...] = ()
    quiet_chapters: tuple[int, ...] = ()
    control_chapter: int | None = None
    seed: int = 0


def plan_sample(
    ms: Manuscript,
    densities: list[ChapterDensity],
    n: int,
    *,
    seed: int = 0,
    min_words: int = MIN_SAMPLE_WORDS,
    min_chapter_words: int = MIN_QUIET_CHAPTER_WORDS,
) -> SamplePlan:
    """Choose up to ``n`` body paragraphs to read, plus one control chapter.

    Drawn from the lowest-density chapters first — where a miss most likely
    hides — one paragraph per chapter in a round-robin so the sample spreads
    rather than piling into a single quiet chapter. Only BODY paragraphs of at
    least ``min_words`` words qualify (never a heading), only chapters of at
    least ``min_chapter_words`` may rank as quiet (the "(unplaced)" bucket never
    does), and within a chapter the paragraph is seeded-random rather than
    always the first, so repeated audits read different pages. Deterministic
    given the inputs and ``seed``.

    One page always comes from a CONTROL chapter — the densest eligible chapter,
    where the review demonstrably read carefully — so a hit rate above chance is
    measurable: hypotheses should land in the quiet chapters, not the control.
    """

    if n <= 0:
        return SamplePlan(seed=seed)
    _, para_to_ch = _chapter_of(ms)
    by_ch: dict[int, list[str]] = {}
    for pid in ms.order:
        text = ms.paragraphs.get(pid)
        if text is None or is_heading_text(text):
            continue
        by_ch.setdefault(para_to_ch.get(pid, 0), []).append(pid)

    def eligible(index: int, floor: int) -> list[str]:
        return [p for p in by_ch.get(index, ())
                if len(ms.paragraphs[p].split()) >= floor]

    candidates = [d for d in densities
                  if d.title != "(unplaced)" and d.words >= min_chapter_words
                  and eligible(d.index, min_words)]
    if not candidates:
        # A book of tiny chapters or short paragraphs: relax the floors rather
        # than hand the auditor nothing (the longest prose is still the best
        # page to read), and skip the control — there is nothing to rank.
        pages: list[SampledPage] = []
        longest = sorted(
            (p for ids in by_ch.values() for p in ids),
            key=lambda p: (-len(ms.paragraphs[p].split()), p))
        for p in longest[:n]:
            pages.append(SampledPage(p, ms.paragraphs[p], para_to_ch.get(p, 0)))
        return SamplePlan(pages=tuple(pages), seed=seed)

    # Chapters, quietest first (ties broken by index for determinism). The
    # control is the densest chapter, and only counts as one when it is
    # genuinely denser than the quietest — all-zero densities have no control.
    order = sorted(candidates, key=lambda d: (d.per_1k, d.index))
    control: ChapterDensity | None = None
    if len(order) >= 2 and n >= 2:
        densest = max(order, key=lambda d: (d.per_1k, -d.index))
        if densest.per_1k > order[0].per_1k:
            control = densest
            order = [d for d in order if d.index != densest.index]

    def shuffled(index: int) -> list[str]:
        ids = eligible(index, min_words)
        random.Random(f"{seed}:{index}").shuffle(ids)
        return ids

    pools = {d.index: shuffled(d.index) for d in order}
    quota = n - (1 if control is not None else 0)
    picked: list[SampledPage] = []
    while len(picked) < quota:
        advanced = False
        for d in order:
            pool = pools[d.index]
            if pool:
                pid = pool.pop(0)
                picked.append(SampledPage(pid, ms.paragraphs[pid], d.index))
                advanced = True
                if len(picked) >= quota:
                    break
        if not advanced:
            break
    if control is not None:
        pid = shuffled(control.index)[0]
        picked.append(SampledPage(pid, ms.paragraphs[pid], control.index, True))
    return SamplePlan(
        pages=tuple(picked),
        quiet_chapters=tuple(d.index for d in order),
        control_chapter=control.index if control is not None else None,
        seed=seed,
    )


def sample_pages(
    ms: Manuscript, densities: list[ChapterDensity], n: int, *, seed: int = 0,
    **floors: int,
) -> list[SampledPage]:
    """Deterministically sample up to ``n`` (para_id, text) pages to read.

    The list form of :func:`plan_sample` — the same pages, each a 2-tuple that
    also carries ``chapter`` and ``control``. See :func:`plan_sample` for the
    rules (body paragraphs only, word floors, seeded choice, one control).
    """

    return list(plan_sample(ms, densities, n, seed=seed, **floors).pages)


# ---- prompt + parsing --------------------------------------------------

def _system_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        log.warning("audit: prompt file %s missing; using a terse fallback",
                    _PROMPT_PATH)
        return (
            "You are a senior proofreader auditing a finished automated review "
            "for MISSED errors. Return only structured hypotheses about likely "
            "misses; do not restate errors already found."
        )


def build_user_prompt(
    densities: list[ChapterDensity], samples: list[tuple[str, str]]
) -> str:
    """The read-only evidence block: the density table and the sampled pages."""

    lines = ["## Error density by chapter (findings per 1,000 words)", ""]
    for d in densities:
        lines.append(
            f"- Chapter {d.index} — {d.title}: {d.findings} findings / "
            f"{d.words} words = {d.per_1k:.2f} per 1k"
        )
    control = next(
        (getattr(p, "chapter", None) for p in samples if getattr(p, "control", False)),
        None)
    lines += ["", "## Sampled pages", ""]
    if control is not None:
        lines += [
            f"Chapter {control} is the CONTROL: the review read it closely and "
            "it is not suspected of anything. Its page is here so you have a "
            "baseline; reporting nothing for it is the expected answer unless "
            "the page itself shows a real miss.",
            "",
        ]
    for page in samples:
        pid, text = page[0], page[1]
        attrs = f'id="{pid}"'
        chapter = getattr(page, "chapter", None)
        if chapter is not None:
            attrs += f' chapter="{chapter}"'
        if getattr(page, "control", False):
            attrs += ' control="true"'
        lines.append(f"<page {attrs}>{text}</page>")
    lines += [
        "",
        "Name where and what kind of error was most likely MISSED. One "
        "hypothesis per suspected miss; skip chapters that read as genuinely "
        "clean. Do not repeat errors the review already found.",
    ]
    return "\n".join(lines)


def parse_hypotheses(parsed: Any, *, cap: int = MAX_HYPOTHESES) -> list[Hypothesis]:
    """Turn a structured model reply into ``Hypothesis`` objects, capped.

    Rows that are not objects are skipped, never fatal. ``parsed`` may be the
    ``{"hypotheses": [...]}`` envelope or a bare list.
    """

    if isinstance(parsed, dict):
        rows = parsed.get("hypotheses", [])
    elif isinstance(parsed, list):
        rows = parsed
    else:
        rows = []
    out: list[Hypothesis] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(Hypothesis.from_json(row))
        if len(out) >= cap:
            break
    return out


def _hypotheses_schema() -> tuple[dict[str, Any], str]:
    """The strict JSON schema for the model call and its schema name."""
    from pydantic import BaseModel

    from docproof.providers import strict_json_schema

    class _Row(BaseModel):
        chapter: int
        error_class: str
        why: str
        span_hint: str
        confidence: str

    class _Hypotheses(BaseModel):
        hypotheses: list[_Row]

    return strict_json_schema(_Hypotheses), "hypotheses"


class AuditResult(list):
    """The hypotheses of one audit, plus what was sampled to get them.

    A plain ``list[Hypothesis]`` to every existing caller; ``control_chapter``
    (``None`` when no control could be drawn), ``sample_ids`` and ``seed``
    record the experiment so a hit rate against the control is measurable
    after the fact.
    """

    control_chapter: int | None
    sample_ids: tuple[str, ...]
    seed: int

    def __init__(self, hypotheses=(), *, plan: SamplePlan | None = None):
        super().__init__(hypotheses)
        plan = plan or SamplePlan()
        self.control_chapter = plan.control_chapter
        self.sample_ids = tuple(p[0] for p in plan.pages)
        self.seed = plan.seed

    def to_json(self) -> dict[str, Any]:
        return {
            "hypotheses": [h.to_json() for h in self],
            "control_chapter": self.control_chapter,
            "sample_ids": list(self.sample_ids),
            "seed": self.seed,
        }


def audit_run(
    results_dir: str | Path,
    ms: Manuscript,
    provider,
    model: str,
    usage: Usage,
    *,
    n_samples: int = 6,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed: int = 0,
) -> AuditResult:
    """Audit a finished run for missed errors and return located hypotheses.

    The one paid step is the single ``complete_structured`` call. Its usage is
    threaded onto the shared ``Usage``; a reply that did not come back clean
    (``stop_reason`` other than ``"ok"`` — a refusal or a token-ceiling
    truncation) is recorded as a loss and yields no hypotheses, never a parse of a
    half-answer. ``seed`` varies which pages are read; the returned
    :class:`AuditResult` records the control chapter and the sampled ids.
    """

    findings = read_findings(results_dir)
    densities = chapter_densities(findings, ms)
    plan = plan_sample(ms, densities, n_samples, seed=seed)
    samples = list(plan.pages)

    schema, schema_name = _hypotheses_schema()
    result = provider.complete_structured(
        model=model,
        system=_system_prompt(),
        user=build_user_prompt(densities, samples),
        schema=schema,
        schema_name=schema_name,
        max_tokens=max_tokens,
    )
    if result.usage is not None:
        usage.add(result.usage, model=model)

    if result.stop_reason != "ok":
        log.warning(
            "audit: reply not ok (stop_reason=%s%s); recording zero hypotheses "
            "as a loss rather than parsing a truncated answer",
            result.stop_reason,
            f" — {result.error}" if getattr(result, "error", None) else "")
        return AuditResult(plan=plan)

    return AuditResult(parse_hypotheses(result.parsed, cap=MAX_HYPOTHESES), plan=plan)


__all__ = [
    "AuditResult",
    "COUNTED_STATUSES",
    "ChapterDensity",
    "MAX_HYPOTHESES",
    "MIN_QUIET_CHAPTER_WORDS",
    "MIN_SAMPLE_WORDS",
    "SamplePlan",
    "SampledPage",
    "audit_run",
    "build_user_prompt",
    "chapter_densities",
    "is_delivered",
    "is_heading_text",
    "parse_hypotheses",
    "plan_sample",
    "read_findings",
    "sample_pages",
]
