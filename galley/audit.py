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

    A missing or unreadable file yields an empty list — the auditor still runs on
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
    """The findings-per-1,000-words table, by chapter, in chapter order."""

    rows, para_to_ch = _chapter_of(ms)
    counts: dict[int, int] = {r[0]: 0 for r in rows}
    for f in findings:
        pid = f.get("para_id", "")
        ch = para_to_ch.get(pid)
        if ch is not None:
            counts[ch] += 1

    out: list[ChapterDensity] = []
    for index, title, ids in rows:
        words = sum(len(ms.paragraphs.get(p, "").split()) for p in ids)
        out.append(ChapterDensity(index, title, words, counts[index]))
    return out


def sample_pages(
    ms: Manuscript, densities: list[ChapterDensity], n: int
) -> list[tuple[str, str]]:
    """Deterministically sample up to ``n`` (para_id, text) pages to read.

    Drawn from the lowest-density chapters first — where a miss most likely hides
    — one paragraph per chapter in a round-robin so the sample spreads rather than
    piling into a single quiet chapter. Deterministic given the inputs.
    """

    if n <= 0:
        return []
    _, para_to_ch = _chapter_of(ms)
    by_ch: dict[int, list[str]] = {}
    for pid in ms.order:
        if pid in ms.paragraphs:
            by_ch.setdefault(para_to_ch.get(pid, 0), []).append(pid)

    # Chapters, quietest first (ties broken by index for determinism).
    order = sorted(densities, key=lambda d: (d.per_1k, d.index))
    cursors = {d.index: 0 for d in densities}
    picked: list[str] = []
    while len(picked) < n:
        advanced = False
        for d in order:
            ids = by_ch.get(d.index, [])
            c = cursors[d.index]
            if c < len(ids):
                picked.append(ids[c])
                cursors[d.index] = c + 1
                advanced = True
                if len(picked) >= n:
                    break
        if not advanced:
            break
    return [(p, ms.paragraphs.get(p, "")) for p in picked]


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
    lines += ["", "## Sampled pages", ""]
    for pid, text in samples:
        lines.append(f'<page id="{pid}">{text}</page>')
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


def audit_run(
    results_dir: str | Path,
    ms: Manuscript,
    provider,
    model: str,
    usage: Usage,
    *,
    n_samples: int = 6,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[Hypothesis]:
    """Audit a finished run for missed errors and return located hypotheses.

    The one paid step is the single ``complete_structured`` call. Its usage is
    threaded onto the shared ``Usage``; a reply that did not come back clean
    (``stop_reason`` other than ``"ok"`` — a refusal or a token-ceiling
    truncation) is recorded as a loss and yields no hypotheses, never a parse of a
    half-answer.
    """

    findings = read_findings(results_dir)
    densities = chapter_densities(findings, ms)
    samples = sample_pages(ms, densities, n_samples)

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
            "audit: reply not ok (stop_reason=%s); recording zero hypotheses as a "
            "loss rather than parsing a truncated answer", result.stop_reason)
        return []

    return parse_hypotheses(result.parsed, cap=MAX_HYPOTHESES)


__all__ = [
    "ChapterDensity",
    "MAX_HYPOTHESES",
    "audit_run",
    "build_user_prompt",
    "chapter_densities",
    "parse_hypotheses",
    "read_findings",
    "sample_pages",
]
