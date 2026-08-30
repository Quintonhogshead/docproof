"""docproof/cover/reality.py: distilling a manuscript sample into a compact
RealitySheet (the BRAIN wave's manuscript-grounding decision, 2026-08-29).

No network anywhere here — every provider is tests.fakes.FakeProvider,
scripted with a canned structured reply, or a tiny local stand-in that
raises outright (the same "call itself failed" idiom
tests/test_cover_direction.py uses for run_directions/revise_spec).

docproof.cover.pipeline's own test suite (tests/test_cover_pipeline.py)
covers the FALLBACK behavior (a distillation failure falling back to the
raw manuscript sample) — that's pipeline.py's call to make, not this
module's (see distill_reality's own docstring), so it lives with pipeline's
other run_job tests, not here.
"""
from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from docproof.cover.reality import (MAX_OUTPUT_TOKENS, REALITY_MODEL,
                                    RENDERED_WORD_CEILING, RealityResult,
                                    RealitySheet, RealitySheetError,
                                    distill_reality, render_reality_sheet)
from docproof.providers import ProviderResult

from .fakes import FakeProvider, USAGE

# -- fixtures -----------------------------------------------------------------

def _sheet_payload(**overrides) -> dict:
    data = dict(
        setting="a windswept lighthouse on a rocky Atlantic coast",
        era="Victorian",
        palette_cues=["brass and soot", "moonlit slate"],
        concrete_objects=["a lighthouse", "a pocket watch", "a rowboat"],
        motifs=["recurring storms", "a locked door"],
        atmosphere="Elegiac and wind-scoured, with a thread of hope.",
        never_show=["the shipwreck ending"])
    data.update(overrides)
    return data


class _ExplodingProvider:
    """Stands in for "the call itself failed" — a dropped connection, an SDK
    exception — as opposed to FakeProvider's "the call answered with
    something unusable"."""
    name = "fake-exploding"

    def complete_structured(self, **kwargs):
        raise RuntimeError("network is down")


# -- distill_reality: happy path ------------------------------------------------

def test_distill_reality_happy_path():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_sheet_payload(), usage=USAGE)])
    result = distill_reality("A ship sailed into the fog.", provider)

    assert isinstance(result, RealityResult)
    assert isinstance(result.sheet, RealitySheet)
    assert result.sheet.setting == "a windswept lighthouse on a rocky Atlantic coast"
    assert result.model == REALITY_MODEL
    assert result.cost is not None and result.cost > 0
    assert result.rendered == render_reality_sheet(result.sheet)

    call = provider.calls[0]
    assert call["schema_name"] == "cover_reality_sheet"
    assert call["max_tokens"] == MAX_OUTPUT_TOKENS
    assert call["model"] == REALITY_MODEL


def test_distill_reality_defaults_to_the_workhorse_model_but_accepts_an_override():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_sheet_payload(), usage=USAGE)])
    result = distill_reality("sample text", provider)
    assert result.model == REALITY_MODEL == "claude-sonnet-5"

    overridden = FakeProvider(
        results=[ProviderResult(parsed=_sheet_payload(), usage=USAGE)])
    result = distill_reality("sample text", overridden, model="claude-opus-5")
    assert result.model == "claude-opus-5"
    assert overridden.calls[0]["model"] == "claude-opus-5"


def test_distill_reality_user_prompt_includes_the_sample():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_sheet_payload(), usage=USAGE)])
    distill_reality("A ship sailed into the fog at dawn.", provider)
    assert "A ship sailed into the fog at dawn." in provider.calls[0]["user"]
    assert "MANUSCRIPT SAMPLE" in provider.calls[0]["user"]


def test_distill_reality_system_prompt_names_every_field():
    provider = FakeProvider(
        results=[ProviderResult(parsed=_sheet_payload(), usage=USAGE)])
    distill_reality("sample text", provider)
    system = provider.calls[0]["system"]
    for field in ("setting", "era", "palette_cues", "concrete_objects",
                 "motifs", "atmosphere", "never_show"):
        assert field in system
    assert "never invent" in system.lower()


def test_distill_reality_accepts_a_sheet_with_empty_list_fields():
    # "If the sample gives you nothing supportable for a list field, leave
    # that field empty" -- every field defaults to empty/"" and must still
    # validate cleanly.
    provider = FakeProvider(results=[ProviderResult(parsed={}, usage=USAGE)])
    result = distill_reality("sample text", provider)
    assert result.sheet == RealitySheet()


# -- failure paths: no fallback INSIDE this module, always RealitySheetError -

def test_distill_reality_raises_on_schema_mismatch():
    provider = FakeProvider(
        results=[ProviderResult(parsed={"concrete_objects": "not a list"}, usage=USAGE)])
    with pytest.raises(RealitySheetError, match="schema"):
        distill_reality("sample text", provider)


def test_distill_reality_raises_when_the_model_returns_no_usable_result():
    provider = FakeProvider(results=[ProviderResult(
        stop_reason="max_tokens", error="output truncated", parsed=None)])
    with pytest.raises(RealitySheetError, match="truncated"):
        distill_reality("sample text", provider)


def test_distill_reality_raises_when_the_provider_call_itself_fails():
    with pytest.raises(RealitySheetError, match="network is down"):
        distill_reality("sample text", _ExplodingProvider())


def test_distill_reality_raises_on_a_list_field_exceeding_its_cap():
    provider = FakeProvider(results=[ProviderResult(
        parsed=_sheet_payload(concrete_objects=[f"object {i}" for i in range(9)]),
        usage=USAGE)])
    with pytest.raises(RealitySheetError, match="schema"):
        distill_reality("sample text", provider)


# -- RealitySheet model: caps + extra="forbid" ---------------------------------

def test_reality_sheet_enforces_list_length_caps():
    with pytest.raises(ValidationError):
        RealitySheet(concrete_objects=[f"o{i}" for i in range(9)])
    with pytest.raises(ValidationError):
        RealitySheet(motifs=[f"m{i}" for i in range(7)])
    with pytest.raises(ValidationError):
        RealitySheet(never_show=[f"n{i}" for i in range(5)])


def test_reality_sheet_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RealitySheet(**_sheet_payload(), extra_field="nope")


def test_reality_sheet_defaults_to_all_empty():
    sheet = RealitySheet()
    assert sheet.setting == "" and sheet.era == "" and sheet.atmosphere == ""
    assert sheet.palette_cues == [] and sheet.concrete_objects == []
    assert sheet.motifs == [] and sheet.never_show == []


# -- render_reality_sheet: labeled sections + the word ceiling ----------------

def test_render_reality_sheet_labels_each_populated_section():
    sheet = RealitySheet(**_sheet_payload())
    rendered = render_reality_sheet(sheet)
    assert "Setting: a windswept lighthouse on a rocky Atlantic coast" in rendered
    assert "Era: Victorian" in rendered
    assert "Palette cues: brass and soot, moonlit slate" in rendered
    assert "Concrete objects: a lighthouse, a pocket watch, a rowboat" in rendered
    assert "Motifs: recurring storms, a locked door" in rendered
    assert "Atmosphere: Elegiac and wind-scoured, with a thread of hope." in rendered
    assert "Never show: the shipwreck ending" in rendered


def test_render_reality_sheet_omits_empty_fields():
    sheet = RealitySheet(setting="a harbor", era="", atmosphere="Quiet.")
    rendered = render_reality_sheet(sheet)
    assert "Setting: a harbor" in rendered
    assert "Atmosphere: Quiet." in rendered
    assert "Era:" not in rendered
    assert "Palette cues:" not in rendered
    assert "Never show:" not in rendered


def test_render_reality_sheet_empty_sheet_renders_to_an_empty_string():
    assert render_reality_sheet(RealitySheet()) == ""


def test_render_reality_sheet_stays_under_the_word_ceiling_by_construction():
    # A sheet built at every field's own cap (8/6/4 items, each a short
    # phrase) comfortably clears the ceiling without ever needing the
    # truncation branch -- the common case.
    sheet = RealitySheet(
        setting="a", era="b",
        palette_cues=[f"cue phrase {i}" for i in range(6)],
        concrete_objects=[f"object phrase {i}" for i in range(8)],
        motifs=[f"motif phrase {i}" for i in range(6)],
        atmosphere="A short atmosphere sentence with a handful of words in it.",
        never_show=[f"never show phrase {i}" for i in range(4)])
    rendered = render_reality_sheet(sheet)
    assert len(rendered.split()) <= RENDERED_WORD_CEILING
    assert "…" not in rendered                  # no truncation needed


def test_render_reality_sheet_enforces_the_ceiling_when_fields_run_long():
    # A pathologically verbose model answer (long phrases even within the
    # capped item counts) is still capped in code, not merely requested of
    # the model.
    sheet = RealitySheet(
        atmosphere=" ".join(f"word{i}" for i in range(500)))
    rendered = render_reality_sheet(sheet)
    assert len(rendered.split()) == RENDERED_WORD_CEILING + 1   # + the "…" marker
    assert rendered.endswith("…")


def test_render_reality_sheet_leaves_a_short_sheet_untouched_including_line_breaks():
    sheet = RealitySheet(setting="a harbor", era="Victorian")
    rendered = render_reality_sheet(sheet)
    assert rendered == "Setting: a harbor\nEra: Victorian"


# -- RealityResult: frozen dataclass -------------------------------------------

def test_reality_result_is_frozen():
    result = RealityResult(sheet=RealitySheet(), rendered="", model=REALITY_MODEL,
                           cost=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.model = "other"
