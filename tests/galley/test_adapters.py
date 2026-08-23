"""A2 — DetectorAdapter protocol, Scope resolution, registry, and the fake."""

import pytest

from docproof.models import Usage

from galley.adapters import (
    ADAPTERS,
    AdapterResult,
    DetectorAdapter,
    Scope,
    get,
    register,
)
from galley.contracts import Manuscript

from tests.galley.fakes import FakeDetector, gfinding, make_manuscript


def test_fake_satisfies_protocol_isinstance():
    assert isinstance(FakeDetector(), DetectorAdapter)


def test_non_adapter_fails_isinstance():
    class NotAnAdapter:
        name = "nope"

    assert not isinstance(NotAnAdapter(), DetectorAdapter)  # missing run()


def test_registry_lookup():
    det = FakeDetector(name="unit-registry")
    try:
        register(det)
        assert ADAPTERS["unit-registry"] is det
        assert get("unit-registry") is det
    finally:
        ADAPTERS.pop("unit-registry", None)


def test_register_rejects_non_adapter():
    class Bad:
        pass

    with pytest.raises(TypeError):
        register(Bad())  # type: ignore[arg-type]


def test_get_missing_raises():
    with pytest.raises(KeyError):
        get("no-such-adapter")


def test_scope_whole_book_is_reading_order():
    ms = make_manuscript("a", "b", "c")
    assert Scope().paragraph_ids(ms) == ["body-0001", "body-0002", "body-0003"]


def test_scope_by_chapters_and_paras_union_in_order():
    ms = make_manuscript("a", "b", "c", "d", chapter_size=2)
    # chapter 1 = body-0003, body-0004; add body-0001 explicitly
    scope = Scope(chapters=(1,), para_ids=("body-0001",))
    assert scope.paragraph_ids(ms) == ["body-0001", "body-0003", "body-0004"]


def test_scope_drops_unknown_ids():
    ms = make_manuscript("a", "b")
    scope = Scope(para_ids=("body-0002", "ghost-9999"))
    assert scope.paragraph_ids(ms) == ["body-0002"]


def test_fake_returns_only_scoped_findings():
    ms = make_manuscript("alpha", "beta", "gamma")
    scripted = [
        gfinding("g-1", "body-0001", "alpha", "Alpha"),
        gfinding("g-2", "body-0003", "gamma", "Gamma"),
    ]
    det = FakeDetector(scripted=scripted, cost_usd=0.5)
    usage = Usage()
    result = det.run(ms, Scope(para_ids=("body-0001",)), budget_usd=10.0, usage=usage)
    assert isinstance(result, AdapterResult)
    assert [f.id for f in result.findings] == ["g-1"]
    assert result.cost_usd == 0.5


def test_fake_threads_usage():
    ms = make_manuscript("alpha")
    det = FakeDetector(model="fake-model", tokens_in=1000, tokens_out=200)
    usage = Usage()
    det.run(ms, Scope(), budget_usd=1.0, usage=usage)
    assert usage.by_model["fake-model"]["input_tokens"] == 1000
    assert usage.by_model["fake-model"]["output_tokens"] == 200
    assert usage.api_calls == 1


def test_fake_declines_when_over_budget():
    ms = make_manuscript("alpha")
    det = FakeDetector(scripted=[gfinding("g-1", "body-0001", "alpha", "A")], cost_usd=5.0)
    usage = Usage()
    result = det.run(ms, Scope(), budget_usd=1.0, usage=usage)
    assert result.findings == []
    assert result.cost_usd == 0.0
    assert result.coverage_notes and "over budget" in result.coverage_notes[0]


def test_fake_records_calls():
    ms = make_manuscript("alpha")
    det = FakeDetector()
    det.run(ms, Scope(passes=2), budget_usd=1.0, usage=Usage())
    assert len(det.calls) == 1
    scope, budget = det.calls[0]
    assert scope.passes == 2 and budget == 1.0
