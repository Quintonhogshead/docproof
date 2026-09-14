"""Explicit Sonnet/Luna votes are the only route to Opus adjudication."""
import copy
from collections import Counter
import json
import threading

import pytest

from galley.fixed_screening import PAIR, is_pair_disagreement, packet, windows
from galley.fixed_workflow import DECISIONS, LUNA, OPUS, SONNET, FixedWorkflow, FixedWorkflowError, _candidate, _windows
from tests.test_galley_fixed_workflow import Readers, _flow, finding, make_book, ruling


@pytest.mark.parametrize("stage", ["typed", "numbers", "local_completion", "ensemble_sweep", "fable", "astra"])
@pytest.mark.parametrize("action", ["apply", "drop"])
def test_pair_agreement_never_calls_opus(make_book, tmp_path, stage, action):
    def answer(name, model, payload, kwargs):
        assert name == stage + "_screen" and model in PAIR
        return {"decisions": [{**ruling(s, action, s["proposals"][0]["replacement"]),
                               "reason": "Different explanation from " + model} for s in payload["sites"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=answer))
    rows = [_candidate(finding("p", "waited", "waits"), flow.current, "local:heuristic")]
    flow._apply(stage, flow._adjudicate(stage, rows, force=stage == "local_completion"))
    assert flow.current["p"] == ("He waits for someone." if action == "apply" else flow.original["p"])
    assert len(flow.calls.events) == 2
    assert flow.questions == []


def test_only_actual_pair_disagreements_reach_opus_and_pair_runs_concurrently(make_book, tmp_path):
    barrier = threading.Barrier(2)
    seen = []
    def answer(stage, model, payload, kwargs):
        if stage == "typed_screen":
            barrier.wait(timeout=3)
            return {"decisions": [ruling(s, "apply" if s["before"] == "someone" and model == SONNET else "drop",
                                         s["proposals"][0]["replacement"]) for s in payload["sites"]]}
        assert stage == "typed_disputes" and model == OPUS
        assert all(is_pair_disagreement(s) for s in payload["sites"])
        seen.extend(s["before"] for s in payload["sites"])
        return {"decisions": [ruling(s, "drop") for s in payload["sites"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=answer))
    rows = [_candidate(finding("p", a, b), flow.current, "local:heuristic")
            for a, b in (("waited", "waits"), ("someone", "Mary"))]
    assert flow._adjudicate("typed", rows) == []
    assert seen == ["someone"]


@pytest.mark.parametrize("defect", ["missing", "unsafe", "empty_query"])
@pytest.mark.parametrize("failed_model", PAIR)
def test_unusable_vote_is_dropped_without_opus(make_book, tmp_path, defect, failed_model):
    def answer(stage, model, payload, kwargs):
        assert stage == "typed_screen"
        if model == failed_model:
            if defect == "missing":
                return {"_skipped_read": {"reason": "Provider unavailable"}}
            return {"decisions": [ruling(s, "query" if defect == "empty_query" else "apply", "bad\x00text")
                                  for s in payload["sites"]]}
        return {"decisions": [ruling(s, "apply", s["proposals"][0]["replacement"]) for s in payload["sites"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=answer))
    rows = [_candidate(finding("p", "waited", "waits"), flow.current, SONNET)]
    assert flow._adjudicate("typed", rows, (SONNET, LUNA)) == []
    assert flow.current == flow.original and flow.questions == []
    assert any(h.get("usable") is False for h in flow.history)
    if defect != "missing":
        assert any(h.get("rejected_proposal", {}).get("model") == failed_model for h in flow.history)


@pytest.mark.parametrize("sonnet_verdict", ["reject", "unavailable"])
def test_luna_rejection_needs_explicit_sonnet_approval_before_opus(make_book, tmp_path, sonnet_verdict):
    def answer(stage, model, payload, kwargs):
        assert model != OPUS
        if model == SONNET and sonnet_verdict == "unavailable":
            return {"_skipped_read": {"reason": "Unavailable"}}
        return {"decisions": [{"id": s["id"], "verdict": "reject", "reason": "Not a clear correction"}
                              for s in payload["changes"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=answer))
    before = flow._apply("typed", [_candidate(finding("p", "waited", "waits"), flow.current, SONNET)])
    flow._checks("checks", before)
    assert flow.current == before and not flow.questions
    assert [e["model"] for e in flow.calls.events] == [LUNA, SONNET]


@pytest.mark.parametrize("reviews", [{}, {SONNET: {"id": "x", "action": "drop"}},
    {SONNET: {"id": "x", "action": "drop"}, LUNA: {"id": "x", "action": "drop"}},
    {SONNET: {"id": "x", "action": "drop"}, LUNA: {"id": "x", "verdict": "approve"}},
    {SONNET: {"id": "x", "action": "drop"}, LUNA: {"id": "x", "action": "apply", "replacement": "a", "origin": "code"}},
    {SONNET: {}, LUNA: None}])
def test_opus_guard_rejects_non_disagreements_before_transport(make_book, tmp_path, reviews):
    flow = _flow(make_book, tmp_path)
    with pytest.raises(FixedWorkflowError, match="explicit Sonnet and Luna disagreement"):
        flow._ask("typed_disputes", OPUS, "Review", {"sites": [{"id": "x", "screening": reviews}]}, DECISIONS)
    assert flow.calls.events == []


def test_bulk_local_flags_share_context_and_never_reach_opus_when_pair_drops(make_book, tmp_path):
    flow = _flow(make_book, tmp_path)
    paragraph = "He waited, watching the door. " * 100
    proposal = {"id": "nested", "start": 9, "end": 10, "before": ",", "replacement": "",
                "category": "grammar", "action": "edit", "reason": "Examine this comma boundary.",
                "models": ["local:heuristic"], "related_paragraphs": {"neighbour": "Context preserved."},
                "local_evidence": {"diagnostics": "Repeated detector metadata." * 20}}
    sites = [{"id": f"d-{i}", "para_id": "p", "paragraph": paragraph, "source": paragraph,
              "start": 9, "end": 10, "before": ",", "proposals": [proposal]} for i in range(9537)]
    batches = list(windows(sites))
    assert len(batches) < len(list(_windows(sites, 20000))) / 5
    for batch in batches:
        compact = packet(batch)
        assert compact["paragraphs"] == {"p": {"text": paragraph}}
        assert compact["context"] == {"neighbour": "Context preserved."}
        assert "id" not in compact["sites"][0]["proposals"][0]
        assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) <= 20000
    agreed, disputed = flow._screen_candidates("typed", sites)
    assert len(agreed) == 9537 and disputed == []
    assert {e["model"] for e in flow.calls.events} == set(PAIR)
    for model in PAIR:
        assert Counter(s["id"] for e in flow.calls.events if e["model"] == model for s in e["payload"]["sites"]) == Counter(s["id"] for s in sites)


def test_old_adjudication_receipts_cannot_resume_under_new_policy(make_book, tmp_path):
    source = make_book("Text.")
    directory = tmp_path / "run"
    flow = FixedWorkflow(source, directory, calls=Readers())
    identity = copy.deepcopy(flow.identity)
    del identity["adjudication_policy"]
    flow.manifest.write_text(json.dumps({"identity": identity, "execution_mode": "fixed", "status": "pending"}))
    with pytest.raises(FixedWorkflowError):
        FixedWorkflow(source, directory, calls=Readers())


def test_windows_hold_at_most_max_sites_even_when_the_packet_is_small():
    from galley.fixed_screening import MAX_SITES
    sites = [{"id": f"d-{i}", "para_id": "p", "paragraph": "Short.", "source": "Short.",
              "start": 0, "end": 1, "before": "S", "proposals": []} for i in range(60)]
    batches = list(windows(sites))
    assert [len(b) for b in batches] == [MAX_SITES, MAX_SITES, 10]
    assert [len(b) for b in windows(sites, max_sites=40)] == [40, 20]
    with pytest.raises(ValueError):
        list(windows(sites, max_sites=0))
