"""The practitioner brain: real ``audit`` and ``plan_wave`` hooks for the loop.

``run_galley`` ships with ``_no_audit``/``_no_plan`` stubs, so production only
ever runs wave one. This module supplies the working pair:

- :func:`make_auditor` wraps the paid audit read (``galley.audit``) to the
  orchestrator's ``Auditor`` signature — it projects the case file's own
  findings into the density table (no ``results_dir`` needed) and drops
  hypotheses the case file has already recorded, so a quiet loop iteration
  doesn't re-propose last wave's list.
- :func:`make_planner` turns fresh hypotheses into ``single_pass`` dispatches:
  one targeted re-read per suspect chapter, error classes mapped onto the
  shipped error-type keys, budgeted against the governor before any money
  moves.

Known limit: the audit call's own spend is metered on the shared ``Usage`` but
is not charged to the governor — the orchestrator only charges adapter costs.
Keep ``n_samples`` small; the audit is one structured call per loop iteration.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from docproof.models import Usage

from .adapters import Scope
from .orchestrator import Dispatch
from .audit import (
    DEFAULT_MAX_TOKENS,
    MAX_HYPOTHESES,
    _hypotheses_schema,
    _system_prompt,
    build_user_prompt,
    chapter_densities,
    parse_hypotheses,
    sample_pages,
)
from .casefile import CaseFile
from .contracts import Hypothesis, Manuscript
from .governor import Governor

log = logging.getLogger("docproof.galley.brain")

# Stop the wave loop when the last wave paid more than this per finding it
# added. A zero-yield wave is infinite marginal cost and always stops; the
# planner returning no dispatches is the graceful convergence signal.
DEFAULT_MARGINAL_STOP_USD = 2.0

# Audit hypothesis classes -> shipped error-type keys for a targeted re-read.
# Exact key matches pass through on their own; this table covers the audit
# prompt's suggested vocabulary where it differs from the config keys. A class
# that resolves to nothing is dropped with a log line — better an honest skip
# than a re-read with the wrong prompt.
CLASS_TO_TYPES: dict[str, tuple[str, ...]] = {
    "missing_comma": ("introductory_comma", "direct_address_comma",
                      "serial_comma"),
    "comma": ("introductory_comma", "direct_address_comma", "serial_comma",
              "unnecessary_comma"),
    "homophone": ("homophone_confusion",),
    "name_inconsistency": ("spelling", "capitalization"),
    "punctuation": ("terminal_mark", "quote_balance"),
    "grammar": ("subject_verb_agreement", "pronoun_agreement"),
}

# The shipped error-type keys a hypothesis class may pass through verbatim.
KNOWN_TYPE_KEYS = frozenset({
    "apostrophe_error", "capitalization", "comma_splice",
    "complex_list_semicolon", "compound_sentence_comma", "currency_style",
    "dialogue_tag", "dialogue_tag_punctuation", "direct_address_comma",
    "heading_sequence", "homophone_confusion", "introductory_comma",
    "list_intro_colon", "list_punctuation", "ly_adverb_hyphen", "missing_word",
    "number_style", "preposition_error", "pronoun_agreement", "quote_balance",
    "repeated_word", "run_on_sentence", "serial_comma", "speaker_change",
    "spelling", "subject_verb_agreement", "tag_question_comma", "tense_shift",
    "terminal_mark", "that_which", "that_who", "title_italics", "try_and",
    "unnecessary_comma", "word_echo",
})

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def resolve_error_types(error_class: str) -> tuple[str, ...]:
    """Map an audit hypothesis class to shipped error-type keys, or ``()``."""

    key = error_class.strip().lower().replace(" ", "_").replace("-", "_")
    if key in KNOWN_TYPE_KEYS:
        return (key,)
    return CLASS_TO_TYPES.get(key, ())


def _hypothesis_key(h: Hypothesis) -> tuple[int, str, str]:
    return (h.chapter, h.error_class.strip().lower(), h.span_hint.strip())


def make_auditor(
    provider: Any,
    model: str,
    usage: Usage,
    *,
    n_samples: int = 6,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[[CaseFile, Manuscript], list[Hypothesis]]:
    """Build an ``Auditor`` closure over a provider, model, and shared usage.

    Densities come from ``cf.findings`` directly (``chapter_densities`` reads
    only ``para_id``), so no run directory is needed. Hypotheses already in
    ``cf.hypotheses`` — the orchestrator accumulates them forever — are
    filtered out, which is what keeps the loop from replanning old suspicions.
    """

    def _audit(cf: CaseFile, ms: Manuscript) -> list[Hypothesis]:
        findings = [{"para_id": g.span.para_id} for g in cf.findings]
        densities = chapter_densities(findings, ms)
        if not densities:
            return []
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
                "brain audit: reply not ok (stop_reason=%s); zero hypotheses",
                result.stop_reason)
            return []

        seen = {_hypothesis_key(h) for h in cf.hypotheses}
        fresh: list[Hypothesis] = []
        for h in parse_hypotheses(result.parsed, cap=MAX_HYPOTHESES):
            key = _hypothesis_key(h)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(h)
        return fresh

    return _audit


def make_planner(
    ms: Manuscript,
    *,
    model: str = "",
    min_confidence: str = "medium",
    est_usd_per_kword: float = 0.10,
    budget_headroom: float = 0.9,
    max_dispatches: int = 6,
    calibration: Any = None,
) -> Callable[[list[Hypothesis], Governor, CaseFile], list[Dispatch]]:
    """Build a ``Planner``: fresh hypotheses -> budgeted single_pass dispatches.

    Hypotheses are grouped per chapter (one dispatch re-reads a chapter once
    with every suspected error type in a single combined pass). Each dispatch
    is priced by chapter word count at a per-kword rate and admitted only while
    the estimate fits inside ``governor.remaining_usd * budget_headroom``.
    Planning only ever consumes the ``hyps`` argument — the fresh batch from
    this iteration's audit — so an empty audit converges the loop.

    ``calibration``, if given (a ``galley.calibration.Calibration``, from
    :func:`galley.calibration.read_calibration`), makes the per-kword rate
    live: :func:`galley.calibration.est_usd_per_kword` looks up
    ``("single_pass", model)``'s observed rate, falling back to
    ``est_usd_per_kword`` when nothing has been calibrated yet. ``None`` (the
    default) keeps the frozen constant exactly as before.
    """

    rate = est_usd_per_kword
    if calibration is not None:
        from galley.calibration import est_usd_per_kword as _calibrated_rate

        rate = _calibrated_rate(calibration, "single_pass", model, est_usd_per_kword)

    words_by_chapter: dict[int, int] = {}
    for ch in ms.chapters:
        words_by_chapter[ch.index] = sum(
            len(ms.paragraphs.get(pid, "").split()) for pid in ch.para_ids)
    floor = _CONF_RANK.get(min_confidence, 1)

    def _plan(
        hyps: list[Hypothesis], gov: Governor, cf: CaseFile
    ) -> list[Dispatch]:
        by_chapter: dict[int, set[str]] = {}
        for h in hyps:
            if _CONF_RANK.get(h.confidence, 1) < floor:
                continue
            if h.chapter not in words_by_chapter:
                log.info("brain plan: hypothesis names unknown chapter %s",
                         h.chapter)
                continue
            keys = resolve_error_types(h.error_class)
            if not keys:
                log.info("brain plan: no error-type mapping for %r; skipped",
                         h.error_class)
                continue
            by_chapter.setdefault(h.chapter, set()).update(keys)

        budget = gov.remaining_usd * budget_headroom
        dispatches: list[Dispatch] = []
        # Densest suspicion first: chapters with more suspected classes lead.
        ranked = sorted(by_chapter.items(),
                        key=lambda kv: (-len(kv[1]), kv[0]))
        for chapter, keys in ranked:
            est = (words_by_chapter[chapter] / 1000.0) * rate
            if est > budget:
                log.info(
                    "brain plan: chapter %d re-read (~$%.2f) over remaining "
                    "budget; skipped", chapter, est)
                continue
            budget -= est
            dispatches.append(Dispatch(
                "single_pass",
                Scope(chapters=(chapter,), error_groups=tuple(sorted(keys)),
                      model=model),
            ))
            if len(dispatches) >= max_dispatches:
                break
        return dispatches

    return _plan


__all__ = [
    "CLASS_TO_TYPES",
    "DEFAULT_MARGINAL_STOP_USD",
    "KNOWN_TYPE_KEYS",
    "make_auditor",
    "make_planner",
    "resolve_error_types",
]
