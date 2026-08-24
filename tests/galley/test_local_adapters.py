"""B3 — cheap-detector adapters: spellscan (offline), LanguageTool (offline or
skip), and the budget-fenced Sapling adapter that must refuse WITHOUT ever
making a live, billed call.

Money is fenced here: spellscan and LanguageTool run locally with no network;
the Sapling adapter is only ever exercised on its refusal paths, and a guard
(monkeypatching ``sapling.check_paragraphs`` to raise) proves the billed
endpoint is never reached.
"""

from __future__ import annotations

import pytest

from docproof import languagetool, sapling, spellscan
from docproof.models import Usage

from galley.adapters import AdapterResult, DetectorAdapter, Scope
from galley.adapters.local import (
    DEFAULT_CHAR_BUDGET,
    USD_PER_CHAR,
    LanguageToolAdapter,
    SaplingAdapter,
    SpellscanAdapter,
)

from tests.galley.fakes import make_manuscript


# --- protocol conformance ----------------------------------------------------


@pytest.mark.parametrize(
    "adapter",
    [SpellscanAdapter(), LanguageToolAdapter(), SaplingAdapter()],
)
def test_adapters_satisfy_protocol(adapter):
    assert isinstance(adapter, DetectorAdapter)


# --- spellscan (offline) -----------------------------------------------------


def _ref(pid, text):
    from docproof.models import ParagraphRef

    return ParagraphRef(pid, "", "body", text, "")


def _spylls_available() -> bool:
    return spellscan.scan([_ref("body-0001", "hello world")]).available


def test_spellscan_locates_a_misspelling_offline():
    if not _spylls_available():
        pytest.skip("spylls / en_US dictionary not available in this environment")

    ms = make_manuscript(
        "The knight rode out at dawn.",
        "He drew teh sword and waited for the signal.",
    )
    adapter = SpellscanAdapter(wave=2)
    usage = Usage()
    result = adapter.run(ms, Scope(), budget_usd=0.0, usage=usage)

    assert isinstance(result, AdapterResult)
    assert result.cost_usd == 0.0
    # The planted misspelling "teh" must come back as a located finding.
    teh = [f for f in result.findings if f.find.lower() == "teh"]
    assert teh, f"expected a finding for 'teh', got {[f.find for f in result.findings]}"
    f = teh[0]
    assert f.span.para_id == "body-0002"
    # The span must actually slice the word out of the paragraph text.
    para = ms.text_of(f.span.para_id)
    assert para[f.span.start : f.span.end].lower() == "teh"
    assert f.provenance and f.provenance.detector == "spellscan"
    assert f.provenance.wave == 2
    # A local pass ran: the call counter ticked, no tokens were billed.
    assert usage.api_calls == 1
    assert usage.input_tokens == 0


def test_spellscan_respects_scope():
    if not _spylls_available():
        pytest.skip("spylls / en_US dictionary not available in this environment")

    ms = make_manuscript(
        "He drew teh sword.",  # body-0001
        "A perfectly ordinary sentence.",  # body-0002
    )
    adapter = SpellscanAdapter()
    # Scope only the clean paragraph; the misspelling is out of scope.
    result = adapter.run(
        ms, Scope(para_ids=("body-0002",)), budget_usd=0.0, usage=Usage()
    )
    assert all(f.span.para_id == "body-0002" for f in result.findings)
    assert not any(f.find.lower() == "teh" for f in result.findings)


# --- LanguageTool (offline, or skip cleanly) ---------------------------------


def test_languagetool_offline_or_skip():
    if not languagetool.AVAILABLE:
        pytest.skip("language_tool_python not installed")

    ms = make_manuscript(
        "She went to the the store yesterday.",  # doubled 'the'
        "He could of gone home earlier.",
    )
    adapter = LanguageToolAdapter()
    usage = Usage()
    try:
        result = adapter.run(ms, Scope(), budget_usd=0.0, usage=usage)
    except Exception as e:  # noqa: BLE001 - JVM/jar/network unavailable in env
        pytest.skip(f"LanguageTool server could not start offline: {e}")

    assert isinstance(result, AdapterResult)
    assert result.cost_usd == 0.0
    # shutdown() must have cleared the server cache (called in the finally).
    assert languagetool._tool_cache == {}
    # Findings, when produced, are located and slice their paragraph.
    for f in result.findings:
        para = ms.text_of(f.span.para_id)
        assert para[f.span.start : f.span.end] == f.find
        assert f.provenance and f.provenance.detector == "languagetool"


def test_languagetool_calls_shutdown(monkeypatch):
    """propose() may raise; shutdown() must still run (the try/finally)."""

    calls = {"shutdown": 0}

    def boom(*a, **k):
        raise RuntimeError("simulated JVM failure")

    def note_shutdown():
        calls["shutdown"] += 1

    monkeypatch.setattr(languagetool, "AVAILABLE", True)
    monkeypatch.setattr(languagetool, "propose", boom)
    monkeypatch.setattr(languagetool, "shutdown", note_shutdown)

    ms = make_manuscript("Any text at all.")
    with pytest.raises(RuntimeError):
        LanguageToolAdapter().run(ms, Scope(), budget_usd=0.0, usage=Usage())
    assert calls["shutdown"] == 1


# --- Sapling (budget-fenced, NEVER a live call) ------------------------------


def _guard_no_live_call(monkeypatch):
    """Make any call to the billed endpoint an immediate, loud failure."""

    def forbidden(*a, **k):  # pragma: no cover - the point is it never runs
        raise AssertionError("sapling.check_paragraphs was called in a test!")

    monkeypatch.setattr(sapling, "check_paragraphs", forbidden)


def test_sapling_refuses_over_budget_scope_without_calling(monkeypatch):
    _guard_no_live_call(monkeypatch)

    # A manuscript whose text far exceeds a tiny char budget.
    ms = make_manuscript("x" * 500, "y" * 500)
    adapter = SaplingAdapter(api_key="unit-test-key")  # keyed, so budget is the gate
    usage = Usage()
    result = adapter.run(
        ms, Scope(char_budget=100), budget_usd=1000.0, usage=usage
    )

    assert result.findings == []
    assert result.cost_usd == 0.0
    assert result.coverage_notes and "declined scope" in result.coverage_notes[0]
    # Nothing billed.
    assert usage.sapling_chars == 0 and usage.sapling_cost == 0.0


def test_sapling_refuses_when_dollar_budget_too_small(monkeypatch):
    _guard_no_live_call(monkeypatch)

    ms = make_manuscript("z" * 2000)
    # char_budget is generous, but $0 can afford 0 characters -> refuse.
    adapter = SaplingAdapter(api_key="unit-test-key")
    result = adapter.run(
        ms, Scope(char_budget=1_000_000), budget_usd=0.0, usage=Usage()
    )
    assert result.findings == []
    assert result.cost_usd == 0.0
    assert result.coverage_notes and "declined scope" in result.coverage_notes[0]


def test_sapling_refuses_without_key(monkeypatch):
    _guard_no_live_call(monkeypatch)
    monkeypatch.delenv("SAPLING_API_KEY", raising=False)

    ms = make_manuscript("short text well under any budget")
    adapter = SaplingAdapter(api_key=None)  # falls back to the (absent) env key
    result = adapter.run(ms, Scope(), budget_usd=1000.0, usage=Usage())

    assert result.findings == []
    assert result.cost_usd == 0.0
    assert result.coverage_notes and "SAPLING_API_KEY" in result.coverage_notes[0]


def test_sapling_budget_math():
    """The effective budget is min(scope cap, dollars-afford)."""
    adapter = SaplingAdapter()
    scope = Scope(char_budget=10_000)
    # $1 at USD_PER_CHAR affords 1/USD_PER_CHAR chars; min with the 10k cap.
    afford = int(1.0 / USD_PER_CHAR)
    assert adapter._char_budget(scope, 1.0) == min(10_000, afford)
    # No scope cap -> the module default is the ceiling.
    assert adapter._char_budget(Scope(), 1e9) == DEFAULT_CHAR_BUDGET
