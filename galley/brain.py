"""Build auditor and planner hooks for the Galley loop.

The auditor charges its structured read; the planner maps fresh hypotheses to
budgeted, non-duplicate targeted passes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from docproof.models import Usage
from docproof.providers import cost_of_usage

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

    key = _class_key(error_class)
    if key in KNOWN_TYPE_KEYS:
        return (key,)
    return CLASS_TO_TYPES.get(key, ())


def _class_key(error_class: str) -> str:
    return error_class.strip().lower().replace(" ", "_").replace("-", "_")


def _hypothesis_key(h: Hypothesis) -> tuple[int, str]:
    """What makes two hypotheses the same suspicion: where and what for.

    The free-text ``span_hint`` is deliberately not part of the key — the same
    chapter flagged for the same class with a re-quoted phrase is last wave's
    suspicion again, not a fresh one. The class is normalized the way
    :func:`resolve_error_types` reads it, so "Comma Splice" is "comma_splice".
    """
    return (h.chapter, _class_key(h.error_class))


def make_auditor(
    provider: Any,
    model: str,
    usage: Usage,
    *,
    n_samples: int = 6,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[[CaseFile, Manuscript], list[Hypothesis]]:
    """Build an ``Auditor`` closure over a provider, model, and shared usage.

    Densities use ``cf.findings`` directly. Previously recorded hypotheses are
    filtered out. The closure accepts the orchestrator's ``governor`` keyword;
    each call is charged as ``audit:wave<N>`` and added to shared usage.
    """

    def _audit(
        cf: CaseFile, ms: Manuscript, governor: Governor | None = None
    ) -> list[Hypothesis]:
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
            if governor is not None:
                # Price this call alone, then charge it to the same ledger the
                # detectors write into; the wave label names the wave whose
                # findings it read (the last closed one).
                call_usage = Usage()
                call_usage.add(result.usage, model=model)
                cost = cost_of_usage(call_usage, fallback_model=model) or 0.0
                if cost > 0:
                    governor.charge(
                        cost, f"audit:wave{governor.current_wave}",
                        allow_over_cap=True)
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


def _already_reread(cf: CaseFile) -> set[tuple[str, str]]:
    """The (target, error-type key) pairs a prior wave's single_pass re-read.

    ``target`` is ``"ch:<index>"`` for a chapter-scoped dispatch and
    ``"p:<para_id>"`` for a paragraph-scoped one, read from the wave actions
    the orchestrator records. A scope re-read once for a type is not fresh
    however the next audit words its suspicion.
    """

    seen: set[tuple[str, str]] = set()
    for wave in cf.waves:
        for action in wave.actions:
            if (not isinstance(action, dict)
                    or action.get("adapter") != "single_pass"
                    or "skipped" in action or "error" in action):
                continue
            scope = action.get("scope") or {}
            keys = list(scope.get("error_groups") or [])
            targets = [f"ch:{c}" for c in (scope.get("chapters") or [])]
            targets += [f"p:{p}" for p in (scope.get("para_ids") or [])]
            for target in targets:
                for key in keys:
                    seen.add((target, key))
    return seen


def make_planner(
    ms: Manuscript,
    *,
    model: str = "",
    min_confidence: str = "medium",
    est_usd_per_kword: float = 0.10,
    budget_headroom: float = 0.9,
    max_dispatches: int = 6,
    calibration: Any = None,
    n_samples: int = 6,
    max_hint_hits: int = 4,
) -> Callable[[list[Hypothesis], Governor, CaseFile], list[Dispatch]]:
    """Build a ``Planner``: fresh hypotheses -> budgeted single_pass dispatches.

    Groups hypotheses by chapter (or paragraph set for chapterless books),
    prices each dispatch by word count, and admits it within
    ``governor.remaining_usd * budget_headroom``. Prior (target, error-type)
    reads are skipped. Optional calibration supplies the per-kword rate.
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
    chapterless = not ms.chapters

    def _words(para_ids: tuple[str, ...]) -> int:
        return sum(len(ms.paragraphs.get(pid, "").split()) for pid in para_ids)

    def _paras_for(h: Hypothesis, sampled: tuple[str, ...]) -> tuple[str, ...]:
        """The paragraphs a hypothesis points at in a chapterless book."""
        hint = h.span_hint.strip()
        if hint in ms.paragraphs:
            return (hint,)
        if hint:
            low = hint.lower()
            hits = tuple(pid for pid in ms.order
                         if low in ms.paragraphs.get(pid, "").lower())
            if hits:
                return hits[:max_hint_hits]
        return sampled

    def _plan(
        hyps: list[Hypothesis], gov: Governor, cf: CaseFile
    ) -> list[Dispatch]:
        # Targets are ("ch", index) or ("p", pid, pid, ...); each becomes one
        # dispatch over the union of its suspected error-type keys.
        by_target: dict[tuple, set[str]] = {}
        sampled: tuple[str, ...] = ()
        if chapterless:
            findings = [{"para_id": g.span.para_id} for g in cf.findings]
            sampled = tuple(
                pid for pid, _text in sample_pages(
                    ms, chapter_densities(findings, ms), n_samples))
        for h in hyps:
            if _CONF_RANK.get(h.confidence, 1) < floor:
                continue
            keys = resolve_error_types(h.error_class)
            if not keys:
                log.info("brain plan: no error-type mapping for %r; skipped",
                         h.error_class)
                continue
            if chapterless:
                paras = _paras_for(h, sampled)
                if not paras:
                    log.info("brain plan: no paragraphs to scope for %r in a "
                             "chapterless book; skipped", h.error_class)
                    continue
                target: tuple = ("p", *paras)
            elif h.chapter in words_by_chapter:
                target = ("ch", h.chapter)
            else:
                log.info("brain plan: hypothesis names unknown chapter %s",
                         h.chapter)
                continue
            by_target.setdefault(target, set()).update(keys)

        # A scope already re-read for a type in an earlier wave is not fresh.
        already = _already_reread(cf)
        for target, keys in list(by_target.items()):
            labels = ([f"ch:{target[1]}"] if target[0] == "ch"
                      else [f"p:{pid}" for pid in target[1:]])
            fresh = {k for k in keys
                     if any((label, k) not in already for label in labels)}
            if fresh != keys:
                log.info("brain plan: %s already re-read for %s; not again",
                         target, sorted(keys - fresh))
            if fresh:
                by_target[target] = fresh
            else:
                del by_target[target]

        budget = gov.remaining_usd * budget_headroom
        dispatches: list[Dispatch] = []
        # Densest suspicion first: targets with more suspected classes lead.
        ranked = sorted(by_target.items(),
                        key=lambda kv: (-len(kv[1]), kv[0]))
        for target, keys in ranked:
            if target[0] == "ch":
                words = words_by_chapter[target[1]]
                scope = Scope(chapters=(target[1],),
                              error_groups=tuple(sorted(keys)), model=model)
            else:
                words = _words(target[1:])
                scope = Scope(para_ids=tuple(target[1:]),
                              error_groups=tuple(sorted(keys)), model=model)
            est = (words / 1000.0) * rate
            if est > budget:
                log.info(
                    "brain plan: re-read of %s (~$%.2f) over remaining "
                    "budget; skipped", target, est)
                continue
            budget -= est
            dispatches.append(Dispatch("single_pass", scope))
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
