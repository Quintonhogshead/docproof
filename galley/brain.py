"""The practitioner brain: real ``audit`` and ``plan_wave`` hooks for the loop.

``run_galley`` ships with ``_no_audit``/``_no_plan`` stubs, so production only
ever runs wave one. This module supplies the working pair:

- :func:`make_auditor` wraps the paid audit read (``galley.audit``) to the
  orchestrator's ``Auditor`` signature — it projects the case file's own
  findings into the density table (no ``results_dir`` needed), drops
  hypotheses the case file has already recorded (keyed on chapter and error
  class, so a re-worded locator is not a new suspicion), and charges its own
  read to the governor the orchestrator hands it, labelled ``audit:wave<N>``.
- :func:`make_planner` turns fresh hypotheses into ``single_pass`` dispatches:
  one targeted re-read per suspect chapter — or, in a book with no chapters,
  per suspect paragraph set, located from the hypothesis's ``span_hint`` or
  the audit's own sample — error classes mapped onto the shipped error-type
  keys, budgeted against the governor before any money moves, and never
  re-dispatching a (target, error type) a prior wave already re-read.

Keep ``n_samples`` small; the audit is one structured call per loop iteration.
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

    Densities come from ``cf.findings`` directly (``chapter_densities`` reads
    only ``para_id``), so no run directory is needed. Hypotheses already in
    ``cf.hypotheses`` — the orchestrator accumulates them forever — are
    filtered out, which is what keeps the loop from replanning old suspicions.

    The closure declares a ``governor`` keyword: the orchestrator passes its
    governor to it, and each audit call's cost (``cost_of_usage`` over the
    call's own metered usage) is charged as ``audit:wave<N>`` — always, past
    the cap included, because the read has already happened by the time its
    price is known. The shared ``usage`` accretes the same tokens for the job
    record.
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

    Hypotheses are grouped per chapter (one dispatch re-reads a chapter once
    with every suspected error type in a single combined pass). Each dispatch
    is priced by word count at a per-kword rate and admitted only while the
    estimate fits inside ``governor.remaining_usd * budget_headroom``.
    Planning only ever consumes the ``hyps`` argument — the fresh batch from
    this iteration's audit — so an empty audit converges the loop.

    A book with no chapters is not a book with nothing to re-read: there the
    scope is a paragraph set — the paragraph ``span_hint`` names (an id, or a
    phrase found in at most ``max_hint_hits`` paragraphs), else the pages the
    audit itself sampled (``n_samples`` must match the auditor's; the sample
    is deterministic, so the planner can replay it from ``cf.findings``).

    A (target, error type) pair a prior wave's single_pass already re-read is
    dropped before budgeting: the audit may keep suspecting it, but a second
    read of the same scope for the same type is churn, not coverage.

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
