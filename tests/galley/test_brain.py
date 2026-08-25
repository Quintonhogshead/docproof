"""The practitioner brain: auditor projection/dedupe and planner budgeting.

Money-fenced: the provider is always the scripted fake from test_audit's
pattern; no test here may construct a real provider.
"""

from __future__ import annotations

from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.brain import (
    DEFAULT_MARGINAL_STOP_USD,
    make_auditor,
    make_planner,
    resolve_error_types,
)
from galley.casefile import BudgetLedger, CaseFile
from galley.contracts import Hypothesis
from galley.governor import Caps, Governor

from .fakes import gfinding, make_manuscript

_U = NormalizedUsage(input_tokens=100, output_tokens=50)


class _Scripted:
    name = "fake"

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        self._last_kwargs = kwargs
        return self._result


def _row(chapter=0, error_class="comma_splice", span_hint="", conf="medium"):
    return {"chapter": chapter, "error_class": error_class,
            "why": "test", "span_hint": span_hint, "confidence": conf}


def _gov(total=100.0, max_waves=6):
    gov = Governor(BudgetLedger(),
                   Caps(total_usd=total, per_wave_usd=total,
                        max_waves=max_waves, max_panel_calls=8))
    gov.open_wave()
    return gov


# ---- resolve_error_types ------------------------------------------------

def test_resolve_exact_key_passes_through():
    assert resolve_error_types("comma_splice") == ("comma_splice",)
    assert resolve_error_types("Tense Shift") == ("tense_shift",)


def test_resolve_alias_and_unknown():
    assert "homophone_confusion" in resolve_error_types("homophone")
    assert resolve_error_types("vibes") == ()


# ---- make_auditor -------------------------------------------------------

def test_auditor_projects_casefile_and_dedupes():
    ms = make_manuscript(*["word " * 30] * 4, chapter_size=2)
    cf = CaseFile(book="b")
    cf.findings.append(gfinding("f1", "body-0001", "teh", "the"))
    # Already-recorded hypothesis: the same row must not come back as fresh.
    cf.hypotheses.append(Hypothesis(chapter=0, error_class="comma_splice",
                                    why="", span_hint="old", confidence="low"))
    provider = _Scripted(ProviderResult(
        parsed={"hypotheses": [
            _row(0, "comma_splice", span_hint="old"),
            _row(1, "spelling", span_hint="fresh"),
        ]},
        usage=_U))
    usage = Usage()
    auditor = make_auditor(provider, "claude-opus-5", usage, n_samples=2)

    fresh = auditor(cf, ms)
    assert [h.error_class for h in fresh] == ["spelling"]
    assert provider.calls == 1
    assert usage.input_tokens == _U.input_tokens
    assert "claude-opus-5" in usage.by_model


def test_auditor_truncated_reply_is_zero_hypotheses():
    ms = make_manuscript(*["word " * 30] * 2, chapter_size=2)
    cf = CaseFile(book="b")
    cf.findings.append(gfinding("f1", "body-0001", "teh", "the"))
    provider = _Scripted(ProviderResult(
        parsed=None, usage=_U, stop_reason="max_tokens"))
    auditor = make_auditor(provider, "m", Usage(), n_samples=1)
    assert auditor(cf, ms) == []


# ---- make_planner -------------------------------------------------------

def test_planner_groups_by_chapter_and_maps_types():
    ms = make_manuscript(*["word " * 100] * 4, chapter_size=2)
    planner = make_planner(ms, est_usd_per_kword=0.10)
    hyps = [
        Hypothesis(chapter=0, error_class="comma_splice", why="", span_hint=""),
        Hypothesis(chapter=0, error_class="homophone", why="", span_hint=""),
        Hypothesis(chapter=1, error_class="spelling", why="", span_hint=""),
    ]
    dispatches = planner(hyps, _gov(), CaseFile(book="b"))
    assert len(dispatches) == 2
    # Chapter 0 has two suspected classes, so it ranks first.
    first = dispatches[0]
    assert first.adapter == "single_pass"
    assert first.scope.chapters == (0,)
    assert set(first.scope.error_groups) == {"comma_splice",
                                             "homophone_confusion"}
    assert dispatches[1].scope.chapters == (1,)
    assert dispatches[1].scope.error_groups == ("spelling",)


def test_planner_drops_low_confidence_unknown_chapter_and_unmapped():
    ms = make_manuscript(*["word " * 100] * 2, chapter_size=2)
    planner = make_planner(ms)
    hyps = [
        Hypothesis(chapter=0, error_class="comma_splice", why="",
                   span_hint="", confidence="low"),
        Hypothesis(chapter=9, error_class="spelling", why="", span_hint=""),
        Hypothesis(chapter=0, error_class="vibes", why="", span_hint=""),
    ]
    assert planner(hyps, _gov(), CaseFile(book="b")) == []


def test_planner_respects_budget():
    # 100k words in one chapter at $0.10/kword = $10 estimated; $5 remaining.
    ms = make_manuscript(*["word " * 1000] * 100, chapter_size=100)
    planner = make_planner(ms, est_usd_per_kword=0.10, budget_headroom=1.0)
    hyps = [Hypothesis(chapter=0, error_class="spelling", why="", span_hint="")]
    assert planner(hyps, _gov(total=5.0), CaseFile(book="b")) == []
    assert len(planner(hyps, _gov(total=50.0), CaseFile(book="b"))) == 1


def test_planner_empty_hypotheses_converges():
    ms = make_manuscript(*["word " * 30] * 2, chapter_size=2)
    planner = make_planner(ms)
    assert planner([], _gov(), CaseFile(book="b")) == []


def test_stop_threshold_default_is_finite():
    assert 0 < DEFAULT_MARGINAL_STOP_USD < float("inf")
