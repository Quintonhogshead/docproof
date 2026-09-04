"""The sequential Luna loops fanned out (Georgis, 2026-09-04): the chapter
sweep's windows, the change verifier's batches, the finished-text walk's
reads, and settle's judge calls run up to `concurrency` at once through
`docproof.fanout.fan_out`, fold their results in submission order, and are
byte-identical to the old for-loop at concurrency 1.

The provider here sleeps, records each call's (start, end) on a monotonic
clock, and answers by CONTENT rather than by call order — with calls in
flight at once, order of arrival is the one thing a test cannot script.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
import yaml

import docproof.__main__ as m
from docproof.__main__ import main
from docproof.fanout import fan_out, fold_usage
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import NormalizedUsage, ProviderResult

DELAY = 0.15


class _LatencyProvider:
    """Sleeps DELAY per call, records (start, end) per call, and answers via
    `answer(schema_name, user)` — deterministic whatever the arrival order."""

    def __init__(self, answer, delay: float = DELAY):
        self._answer = answer
        self.delay = delay
        self.spans: list[tuple[float, float]] = []
        self.users: list[str] = []
        self._lock = threading.Lock()

    def complete_structured(self, **kw):
        t0 = time.monotonic()
        time.sleep(self.delay)
        body = self._answer(kw["schema_name"], kw["user"])
        t1 = time.monotonic()
        with self._lock:
            self.spans.append((t0, t1))
            self.users.append(kw["user"])
        return ProviderResult(parsed=body, usage=NormalizedUsage(10, 5),
                              stop_reason="ok")

    def overlapped(self) -> bool:
        s = sorted(self.spans)
        return any(s[i][1] > s[i + 1][0] for i in range(len(s) - 1))


# --- the helper itself --------------------------------------------------------

def test_fan_out_keeps_item_order_and_only_overlaps_above_width_one():
    spans: dict[int, tuple[float, float]] = {}

    def fetch(i):
        t0 = time.monotonic()
        time.sleep(DELAY)
        spans[i] = (t0, time.monotonic())
        return i * i

    assert [r for _i, r in fan_out(range(4), fetch, concurrency=1)] == \
        [0, 1, 4, 9]
    s = sorted(spans.values())
    assert all(s[i][1] <= s[i + 1][0] for i in range(3))     # sequential
    spans.clear()
    assert [r for _i, r in fan_out(range(4), fetch, concurrency=4)] == \
        [0, 1, 4, 9]                                           # still ordered
    s = sorted(spans.values())
    assert any(s[i][1] > s[i + 1][0] for i in range(3))       # overlapped


def test_fan_out_cancels_unstarted_calls_when_a_fetch_raises():
    started: list[int] = []

    def fetch(i):
        started.append(i)
        time.sleep(0.05)
        if i == 0:
            raise RuntimeError("boom")
        return i

    with pytest.raises(RuntimeError):
        list(fan_out(range(50), fetch, concurrency=2))
    assert len(started) < 50            # the tail was never bought


def test_fold_usage_sums_every_counter_and_bucket():
    a, b = Usage(), Usage()
    a.add(NormalizedUsage(10, 5), model="m")
    b.add(NormalizedUsage(1, 1), model="m")
    b.add(NormalizedUsage(2, 2), model="n")
    fold_usage(a, b)
    assert a.api_calls == 3 and a.input_tokens == 13
    assert a.by_model["m"]["api_calls"] == 2 and "n" in a.by_model


# --- chapter sweep ------------------------------------------------------------

def _paras(n: int) -> list[ParagraphRef]:
    return [ParagraphRef(f"p{i}", "word/document.xml", "body",
                         f"Window {i} has a wrod in it. " * 3, "Normal")
            for i in range(n)]


def _sweep_answer(schema_name, user):
    n = int(re.search(r"\[P(\d+)\]", user).group(1))
    return {"findings": [{"para": n, "quote": "wrod", "correction": "word",
                          "note": "typo"}]}


@pytest.mark.parametrize("concurrency,expect_overlap", [(1, False), (4, True)])
def test_chapter_sweep_windows_fan_out_in_document_order(concurrency,
                                                         expect_overlap, caplog):
    from docproof.chaptersweep import propose
    paras = _paras(4)
    prov = _LatencyProvider(_sweep_answer)
    usage = Usage()
    stats: dict = {}
    with caplog.at_level("INFO", logger="docproof.chaptersweep"):
        cands = propose(paras, prov, model="m", max_output_tokens=100,
                        usage=usage, window_chars=10, stats=stats,
                        concurrency=concurrency)
    assert [c.para_id for c in cands] == ["p0", "p1", "p2", "p3"]
    assert stats["windows"] == 4 and usage.api_calls == 4
    assert prov.overlapped() is expect_overlap
    lines = [r.message for r in caplog.records if "Chapter sweep window" in r.message]
    assert lines[:2] == ["Chapter sweep window 1/4: 1 candidate(s)",
                         "Chapter sweep window 2/4: 1 candidate(s)"]


# --- the change verifier and the walk ---------------------------------------

def _verify_answer(schema_name, user):
    if schema_name == "problems":
        return {"problems": [{"index": 1, "verdict": "wrong_rule",
                              "detail": "d", "fix": "f"}]}
    pid = re.search(r"\[(p\d+)\]", user).group(1)
    return {"findings": [{"para_id": pid, "quote": "q", "problem": "x",
                          "suggestion": "y", "severity": "low"}]}


@pytest.mark.parametrize("concurrency,expect_overlap", [(1, False), (4, True)])
def test_verify_batches_and_walk_reads_fan_out_in_order(concurrency,
                                                        expect_overlap, caplog):
    from galley import verify
    edits = [{"para_id": f"p{i}", "original_text": f"a{i}",
              "corrected_text": f"b{i}", "error_type": "t"} for i in range(4)]
    accepted = {f"p{i}": f"b{i} text" for i in range(4)}
    prov = _LatencyProvider(_verify_answer)
    usage = Usage()
    with caplog.at_level("INFO", logger="galley.verify"):
        problems = verify.verify_changes(edits, accepted, prov, "m", usage,
                                         batch_size=1, concurrency=concurrency)
    assert [p.para_id for p in problems] == ["p0", "p1", "p2", "p3"]
    assert usage.api_calls == 4 and prov.overlapped() is expect_overlap
    assert any("change verifier: batch 2/4 done" in r.message
               for r in caplog.records)

    prov = _LatencyProvider(_verify_answer)
    usage = Usage()
    with caplog.at_level("INFO", logger="galley.verify"):
        found = verify.walk_finished_text(accepted, prov, "m", usage,
                                          char_budget=1, concurrency=concurrency)
    assert [r.para_id for r in found] == ["p0", "p1", "p2", "p3"]
    assert usage.api_calls == 4 and prov.overlapped() is expect_overlap
    assert any("finished-text walk: read 3/4 done" in r.message
               for r in caplog.records)


# --- settle: the judge calls of a round ----------------------------------------

def _judge_answer(schema_name, user):
    if schema_name == "decision":
        if "'recieve'" in user:
            return {"action": "add", "replacement": "receive", "reason": "",
                    "question": ""}
        return {"action": "drop", "replacement": "", "reason": "voice",
                "question": ""}
    return {"problems": []} if schema_name == "problems" else {"findings": []}


@pytest.mark.parametrize("concurrency,expect_overlap", [(1, False), (2, True)])
def test_settle_judge_calls_fan_out_and_settle_in_item_order(
        tmp_path, monkeypatch, concurrency, expect_overlap):
    from galley.settle import Settlement
    from galley.verify import paragraph_views, residual_id
    from .galley.test_settle import (_build, _manuscript, _para_ids,
                                    _replay_config, _walk)
    src = _manuscript(tmp_path)
    ids, _doc = _para_ids(src)
    p0, p1, p2 = ids[0], ids[1], ids[2]
    run = _build(tmp_path, src, [
        {"para_id": p0, "original_text": "teh", "corrected_text": "the",
         "confidence": "high"}])
    # two residuals with no suggestion: both go to the judge
    _walk(run, [{"para_id": p1, "quote": "recieve", "problem": "misspelling",
                 "suggestion": "", "severity": "high"},
                {"para_id": p2, "quote": "sure the total", "problem": "?",
                 "suggestion": "", "severity": "low"}])
    cfgpath = Path(_replay_config(tmp_path))
    cfg = yaml.safe_load(cfgpath.read_text("utf-8"))
    cfg["api"]["concurrency"] = concurrency
    cfgpath.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    prov = _LatencyProvider(_judge_answer)
    monkeypatch.setattr(m, "_resolve_engine",
                        lambda args, cfg, default_model=None:
                        ("provider", prov, "fake-model"))
    rc = main(["galley", "settle", str(run), "--source", str(src),
               "--config", str(cfgpath), "--engine", "provider"])
    assert rc == 0
    judge_spans = [prov.spans[i] for i, u in enumerate(prov.users)
                   if "FLAGGED SPAN" in u]
    assert len(judge_spans) == 2
    a, b = sorted(judge_spans)
    assert (a[1] > b[0]) is expect_overlap
    st = Settlement.load(run)
    recs = {r.residual_id: r for r in st.latest().values()}
    assert recs[residual_id(p1, "recieve")].action == "add"
    assert recs[residual_id(p2, "sure the total")].action == "drop"
    assert "receive the letter" in paragraph_views(run)[1][p1]
    assert st.open == []


def test_lane_concurrency_follows_the_config_and_caps_the_subagent_lane():
    from docproof.config import load_config
    cfg = load_config("config/default.yaml")
    assert m._lane_concurrency(cfg, "none", "") == 1
    assert m._lane_concurrency(cfg, "provider", cfg.api.model) == \
        cfg.concurrency_for(cfg.api.model)
    assert 1 <= m._lane_concurrency(cfg, "subagent", "subagent:claude-opus-5") \
        <= m.SUBAGENT_MAX_INFLIGHT
    cfg.api.concurrency = 1
    assert m._lane_concurrency(cfg, "provider", cfg.api.model) == 1
    assert m._lane_concurrency(cfg, "subagent", "x") == 1
