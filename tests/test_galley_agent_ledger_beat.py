"""The ledger's own readout, and the heartbeat carrying it.

The Warden reads the agent's heartbeat (`GET /api/watch/warden`, whose
`docwatch.agent` is this same JSON) to tell a stuck claim from an idle
machine, so every beat now carries `Ledger.summary()` and the current usage
pause, not only whatever this poll cycle happened to be doing. Bounded,
because the server enforces its own ceiling on what a heartbeat may be
(`app.routes.watch.AGENT_STATUS_MAX_BYTES`) and a heartbeat the server
refuses for its size says nothing at all.
"""
from __future__ import annotations

import json

import pytest

from galley import agent as ga

APP = "https://atmosphere-docproof.fly.dev"


@pytest.fixture()
def env() -> ga.AgentEnv:
    return ga.AgentEnv(app_url=APP, token="tok", oauth_token="oauth")


def _agent(env, tmp_path, **kw) -> ga.Agent:
    kw.setdefault("workspace_root", tmp_path / "ws")
    kw.setdefault("log", lambda _m: None)
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("heartbeat", lambda payload: None)
    kw.setdefault("heartbeat_interval_s", 0)
    return ga.Agent(env=env, **kw)


# --- Ledger.summary() ----------------------------------------------------

def test_summary_shape_with_a_mix_of_states(tmp_path):
    ledger = ga.Ledger.load(tmp_path / ".agent-state.json")
    ledger.record("claimed-1", ga.CLAIMED, name="A - Book 1.docx")
    ledger.record("claimed-2", ga.CLAIMED, name="B - Book 1.docx")
    ledger.record("pending-1", ga.PENDING_DELIVERY, name="C - Book 1.docx")
    ledger.record("done-1", ga.FINISHED, name="D - Book 1.docx")
    ledger.record("failed-1", ga.FAILED, name="E - Book 1.docx")

    summary = ledger.summary()

    assert set(summary) == {"claimed", "pending_delivery", "counts"}
    assert {row["file_id"] for row in summary["claimed"]} == {
        "claimed-1", "claimed-2"}
    assert {row["file_id"] for row in summary["pending_delivery"]} == {
        "pending-1"}
    for row in summary["claimed"] + summary["pending_delivery"]:
        assert set(row) == {"file_id", "name", "state", "updated_at"}
    assert summary["counts"] == {
        ga.CLAIMED: 2, ga.PENDING_DELIVERY: 1, ga.FINISHED: 1, ga.FAILED: 1}


def test_summary_is_empty_and_bounded_on_a_fresh_ledger(tmp_path):
    ledger = ga.Ledger.load(tmp_path / "nothing-here.json")
    summary = ledger.summary()
    assert summary == {"claimed": [], "pending_delivery": [], "counts": {}}


def test_summary_caps_each_list_at_twenty(tmp_path):
    ledger = ga.Ledger.load(tmp_path / ".agent-state.json")
    for i in range(35):
        ledger.record(f"claimed-{i:02d}", ga.CLAIMED, name=f"Book {i}.docx")
    for i in range(35):
        ledger.record(f"pending-{i:02d}", ga.PENDING_DELIVERY,
                      name=f"Pending {i}.docx")

    summary = ledger.summary()

    assert len(summary["claimed"]) == ga.Ledger.SUMMARY_CAP == 20
    assert len(summary["pending_delivery"]) == 20
    # The counts are the true totals, not the capped list lengths — a
    # monitoring agent needs to know 35 are stuck even if it only gets 20
    # named.
    assert summary["counts"][ga.CLAIMED] == 35
    assert summary["counts"][ga.PENDING_DELIVERY] == 35


# --- present in the heartbeat --------------------------------------------

def test_the_beat_carries_the_ledger_and_the_usage_pause(env, tmp_path):
    beats = []
    agent = _agent(env, tmp_path, heartbeat=beats.append)
    agent.ledger().record("drive-1", ga.CLAIMED, name="Test - Book 1.docx")

    payload = agent._beat(state="running", book="Test - Book 1.docx")

    assert beats[-1] is payload
    assert "ledger" in payload and "usage_pause" in payload
    assert payload["ledger"]["claimed"][0]["file_id"] == "drive-1"
    assert payload["usage_pause"] is None


def test_the_usage_pause_rides_the_beat_once_set(env, tmp_path):
    beats = []
    agent = _agent(env, tmp_path, heartbeat=beats.append,
                   wall_clock=lambda: 1_000_000.0)
    agent.pause_for_usage("Claude usage limit reached")

    payload = agent._beat()

    assert payload["usage_pause"]
    assert payload["usage_pause"]["reason"] == "Claude usage limit reached"
    assert payload["usage_pause"]["resume_after"] > 1_000_000.0


def test_rest_beat_still_means_idle_with_the_new_keys_riding_along(env, tmp_path):
    """`_rest_beat` drops every run-specific key so "idle" still means idle;
    the ledger and the usage pause are not run-specific, so they ride along
    on an idle beat exactly as they do on a running one."""
    beats = []
    agent = _agent(env, tmp_path, heartbeat=beats.append)
    agent._beat(state="running", phase="settle", book="Test - Book 1.docx",
               turns=4)
    agent.ledger().record("drive-1", ga.CLAIMED, name="Test - Book 1.docx")

    payload = agent._rest_beat(state="idle")

    assert payload["state"] == "idle"
    for stale in ("phase", "book", "turns"):
        assert stale not in payload
    assert payload["ledger"]["claimed"][0]["file_id"] == "drive-1"
    assert payload["usage_pause"] is None


def test_the_ledger_and_usage_pause_are_empty_and_none_when_idle_and_unclaimed(
        env, tmp_path):
    beats = []
    agent = _agent(env, tmp_path, heartbeat=beats.append)

    payload = agent._rest_beat(state="idle", awaiting=0)

    assert payload["ledger"] == {"claimed": [], "pending_delivery": [],
                                 "counts": {}}
    assert payload["usage_pause"] is None


# --- bounded ----------------------------------------------------------------

def test_the_heartbeat_stays_under_the_servers_ceiling_with_a_huge_ledger(
        env, tmp_path):
    from app.routes.watch import AGENT_STATUS_MAX_BYTES

    assert ga.HEARTBEAT_MAX_BYTES == AGENT_STATUS_MAX_BYTES

    beats = []
    agent = _agent(env, tmp_path, heartbeat=beats.append)
    ledger = agent.ledger()
    long_name = "A" * 500 + " - Book 1.docx"
    for i in range(400):
        ledger.record(f"claimed-{i:04d}", ga.CLAIMED, name=long_name)
    for i in range(400):
        ledger.record(f"pending-{i:04d}", ga.PENDING_DELIVERY, name=long_name)

    payload = agent._beat(state="running", book="Test - Book 1.docx")

    size = len(json.dumps(payload).encode("utf-8"))
    assert size <= ga.HEARTBEAT_MAX_BYTES, size
