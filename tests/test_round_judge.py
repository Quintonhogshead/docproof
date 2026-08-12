"""The multi-round review judge (design doc stage D).

A strong model rules on every model-generated correction between rounds:
approve (becomes a tracked change and rewrites what the next round reads), query
(a margin question that changes nothing), or reject (dropped, remembered). These
tests pin the routing, the fail-open discipline, the decision memory that stops a
rejected fix being re-judged, and that the judge prompt is an editable field.
"""
from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from docproof.config import Config, RoundsConfig
from docproof.models import Finding, Usage
from docproof.providers import ProviderResult
from docproof.verifier import (RoundJudge, adjudicate_round,
                               default_judge_prompt, rejection_key)
from tests.fakes import USAGE


def _f(fid, para, original, corrected, etype="comma_splice"):
    return Finding(fid, "chunk-000", para, etype, original, 1, corrected,
                   "because", "high")


PARA = {"body-0000": "He ran, he fell.", "body-0001": "The dog barked."}


class _JudgeProvider:
    """Scripted verdicts keyed by finding_id (approve for anything unset). Reads
    the rendered prompt so concurrent per-paragraph calls each answer for exactly
    the finding_ids they were asked about, order-independent."""

    name = "fake-judge"

    def __init__(self, script=None, *, stop_reason="ok"):
        self.script = script or {}
        self.stop_reason = stop_reason
        self.seen: list[str] = []          # finding_ids actually sent to the model
        self.calls: list[dict] = []

    def complete_structured(self, *, user, **kwargs):
        ids = re.findall(r"finding_id=(\S+)", user)
        self.seen += ids
        self.calls.append({"user": user, **kwargs})
        if self.stop_reason != "ok":
            return ProviderResult(parsed=None, stop_reason=self.stop_reason,
                                  usage=USAGE)
        verdicts = [{"finding_id": fid,
                     "verdict": self.script.get(fid, ("approve", ""))[0],
                     "reason": self.script.get(fid, ("approve", ""))[1]}
                    for fid in ids]
        return ProviderResult(parsed={"verdicts": verdicts}, usage=USAGE)


# --- routing -----------------------------------------------------------------

def test_all_approved_become_edits():
    f = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    u = Usage()
    res = adjudicate_round([f], PARA, {}, _JudgeProvider(),
                           model="claude-opus-5", usage=u)
    assert [x.finding_id for x in res.edits] == ["f-1"]
    assert res.queries == [] and res.rejected == []
    assert u.api_calls == 1


def test_approve_query_reject_route_and_remember():
    f1 = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    f2 = _f("f-2", "body-0001", "The dog barked.", "The dog barked!")
    f3 = _f("f-3", "body-0001", "dog", "dogs")
    j = _JudgeProvider({"f-2": ("query", "Maybe emphasis?"),
                        "f-3": ("reject", "Singular is right.")})
    res = adjudicate_round([f1, f2, f3], PARA, {}, j, model="m", usage=Usage())

    assert [x.finding_id for x in res.edits] == ["f-1"]
    q = res.queries[0]
    assert q.finding_id == "f-2" and q.force_query
    assert q.explanation == "Maybe emphasis?"          # reason becomes the margin note
    r = res.rejected[0]
    assert r.finding_id == "f-3" and r.status == "rejected_by_verifier"
    assert r.explanation == "Singular is right."
    assert rejection_key(f3) in res.new_rejections
    assert rejection_key(f1) not in res.new_rejections   # approvals aren't remembered


# --- decision memory ---------------------------------------------------------

def test_prior_rejection_auto_rejects_without_a_model_call():
    f = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    j = _JudgeProvider()
    res = adjudicate_round([f], PARA, {}, j, model="m",
                           prior_rejections=frozenset({rejection_key(f)}),
                           usage=Usage())
    assert [x.finding_id for x in res.rejected] == ["f-1"]
    assert res.edits == [] and res.queries == []
    assert j.seen == []                                # the judge was never asked
    assert rejection_key(f) in res.new_rejections      # still carried forward


def test_rejection_key_is_coordinate_and_wording_free():
    a = _f("f-a", "body-0000", "the cat sat", "the dog sat")
    b = _f("f-b", "body-0000", "cat", "dog")           # same edit, narrower quote
    assert rejection_key(a) == rejection_key(b)
    elsewhere = _f("f-c", "body-0001", "cat", "dog")   # different paragraph
    assert rejection_key(a) != rejection_key(elsewhere)


def test_memory_round_trips_across_rounds():
    f = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    r1 = adjudicate_round([f], PARA, {},
                          _JudgeProvider({"f-1": ("reject", "Not an error.")}),
                          model="m", usage=Usage())
    # a later round re-proposes the same fix; the carried memory refuses it
    j2 = _JudgeProvider()
    r2 = adjudicate_round([f], PARA, {}, j2, model="m",
                          prior_rejections=r1.new_rejections, usage=Usage())
    assert [x.finding_id for x in r2.rejected] == ["f-1"]
    assert j2.seen == []


# --- fail open ---------------------------------------------------------------

def test_no_answer_keeps_the_correction():
    f = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    res = adjudicate_round([f], PARA, {},
                           _JudgeProvider(stop_reason="refusal"),
                           model="m", usage=Usage())
    assert [x.finding_id for x in res.edits] == ["f-1"]   # kept, not dropped
    assert res.rejected == []


def test_finding_without_a_home_paragraph_is_kept():
    f = _f("f-1", "body-9999", "x", "y")               # para not in PARA
    j = _JudgeProvider()
    res = adjudicate_round([f], PARA, {}, j, model="m", usage=Usage())
    assert [x.finding_id for x in res.edits] == ["f-1"]
    assert j.seen == []


# --- the editable prompt -----------------------------------------------------

def test_instructions_and_context_thread_into_the_system_prompt():
    f = _f("f-1", "body-0000", "He ran, he fell.", "He ran; he fell.")
    j = _JudgeProvider()
    adjudicate_round([f], PARA, {}, j, model="m",
                     instructions="HOUSE RULE: keep Oxford commas.",
                     context="Conventions: US English.", usage=Usage())
    system = j.calls[0]["system"]
    assert system.startswith("HOUSE RULE: keep Oxford commas.")
    assert "Conventions: US English." in system


def test_empty_instructions_fall_back_to_the_default():
    judge = RoundJudge({}, _JudgeProvider(), "m")      # no instructions
    assert judge.system_prompt == default_judge_prompt()
    assert RoundsConfig().judge_prompt == ""           # panel field defaults empty


# --- config ------------------------------------------------------------------

def test_rounds_config_is_inert_by_default():
    cfg = Config()
    assert cfg.rounds.count == 1
    assert cfg.rounds.judge_model == "gpt-5.6-sol"
    assert cfg.rounds.judge_effort == "high"


def test_bad_judge_model_only_validated_when_active():
    RoundsConfig(count=1, judge_model="not-a-real-model")   # inert: tolerated
    with pytest.raises(ValidationError):
        RoundsConfig(count=2, judge_model="not-a-real-model")
