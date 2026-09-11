"""Quota exhaustion pauses the subscription, never the manuscript's verdict."""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone

import pytest

from docproof.subscription_limits import UsageLimitError, is_usage_limited, resume_after
from galley import agent as ga, driver as gd
from .test_agent import (BOOK, BOOK_2, FakeApp, FakeResult, Observed,
                         _downloader, _observed_agent, env, env_file)
from .test_driver import FIXTURE, _driver, _plan

LIMIT = "You've hit your session limit · resets 3:10am (UTC)"
NOW = datetime(2026, 9, 11, 1, 35, tzinfo=timezone.utc).timestamp()
RESET = datetime(2026, 9, 11, 3, 10, 30, tzinfo=timezone.utc).timestamp()


def test_reported_utc_reset_is_preserved():
    assert is_usage_limited(LIMIT)
    assert resume_after(LIMIT, now=NOW) == RESET
    assert not is_usage_limited("invalid token, run /login")
    assert not is_usage_limited("Reached max turns (100)")


@pytest.mark.parametrize("detail", ["Usage limit reached", "resets 3pm",
    "resets 99:99am (UTC)", "resets 3am (unknown-zone)"])
def test_unreadable_reset_is_a_short_recheck_not_a_guessed_time(detail):
    assert resume_after(detail, now=NOW) == NOW + 300


def test_midnight_rollover_and_recently_passed_reset():
    before = datetime(2026, 9, 10, 23, 0, tzinfo=timezone.utc).timestamp()
    assert resume_after(LIMIT, now=before) == RESET
    assert resume_after(LIMIT, now=RESET + 60) == RESET + 360


def test_success_subtype_with_error_flag_is_still_a_usage_failure():
    result = {"type": "result", "subtype": "success", "is_error": True,
              "num_turns": 1, "result": LIMIT}
    assert gd.session_limit(result, "") == "usage"
    assert gd.session_limit(None, LIMIT) == "usage"
    # Historical prose in a successful session is not an active quota error.
    assert gd.session_limit({**result, "is_error": False}, LIMIT) is None


def test_raw_result_only_keeps_the_reset_message(tmp_path):
    import sys

    stream = json.dumps({"type": "result", "subtype": "success",
        "is_error": True, "num_turns": 1, "result": LIMIT})
    spec = gd.PhaseSpec("verify", "prompt", tmp_path, tmp_path / "verify.log",
        [sys.executable, "-c", f"import sys; print({stream!r}); sys.exit(1)"], {})
    result = gd.spawn_claude(spec)
    assert result.limit == "usage" and result.tail == LIMIT


def test_an_old_snapshot_cannot_hide_a_rate_limited_reader(tmp_path, monkeypatch):
    book = tmp_path / "Ford - Book 1.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    calls = []
    def refused(spec):
        calls.append(spec.phase)
        return gd.PhaseResult(spec.phase, 1, spec.log_path, LIMIT,
                              limit="usage", num_turns=1)
    drv = _driver(book, tmp_path, spawn=refused, astra_review=True,
                  only_phases=["verify", "settle", "astra_review"])
    monkeypatch.setattr(drv, "_review_snapshot_available", lambda: True)
    monkeypatch.setattr(drv, "_run_astra_review", lambda *a: pytest.fail("review ran"))
    with pytest.raises(UsageLimitError, match="session limit"):
        drv.run()
    assert calls == ["verify"]
    ledger = json.loads((ws / "runs/driver/driver.json").read_text())
    assert ledger["stopped_at"] == "verify"
    assert not ledger["asked"] and not ledger["recovery_exhausted"]
    assert "settlement.json" not in ledger["reason"]
    assert not (ws / "runs/outcome.json").exists()


def test_boot_quota_is_not_reported_as_an_invalid_login(env, tmp_path):
    observed = Observed()
    checks, calls = [], []
    def check(values):
        checks.append(1)
        return LIMIT
    agent = _observed_agent(env, tmp_path, observed,
        opener=FakeApp([BOOK, BOOK_2]), preflight=check, wall_clock=lambda: NOW,
        run_driver=lambda **k: calls.append(k))
    agent.announce()
    agent.poll_once()
    agent.poll_once()
    assert checks == [1] and not calls
    assert agent._usage_pause()["resume_after"] == RESET
    assert "session limit" in observed.beats[-1]["last_error"]
    assert observed.beats[-1]["credentials_error"] == ""
    assert not any("token" in title for title, body in observed.alerts)
    assert not agent.ledger().books


def test_midbook_quota_survives_restart_and_resumes_both_books_at_reset(env, tmp_path):
    clock = {"now": NOW}
    calls, checks = [], []
    obs = Observed()
    def driver(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise UsageLimitError(LIMIT)
        return FakeResult()
    kwargs = dict(opener=FakeApp([BOOK, BOOK_2]), run_driver=driver,
                  download=_downloader(tmp_path), wall_clock=lambda: clock["now"],
                  preflight=lambda v: checks.append(1) or "")
    agent = _observed_agent(env, tmp_path, obs, **kwargs)
    assert agent.poll_once().outcome == "held"
    record = agent.ledger().claimed("drive-1")
    assert record["state"] == ga.CLAIMED
    assert record["operational_status"] == "waiting_for_usage"
    assert not agent.held_for_code("drive-1", agent.ledger())
    # Process restart does not lose the cooldown or probe the subscription.
    agent = _observed_agent(env, tmp_path, obs, **kwargs)
    agent.announce()
    agent.poll_once()
    assert len(calls) == 1 and not checks
    assert agent.ledger().state("drive-2") == ""
    clock["now"] = RESET + 1
    assert agent.poll_once().outcome == "done"
    assert checks == [1] and len(calls) == 2
    assert calls[0]["slug"] == calls[1]["slug"]
    assert not agent._usage_pause()
    assert agent.ledger().claimed("drive-1")["reason"] == "no open items"
    assert agent.poll_once().outcome == "done"
    assert agent.ledger().state("drive-2") == ga.FINISHED


def test_a_limit_still_active_after_reset_establishes_another_short_pause(env, tmp_path):
    clock = {"now": NOW}
    checks, calls = [], []
    agent = _observed_agent(env, tmp_path, Observed(), opener=FakeApp([BOOK]),
        wall_clock=lambda: clock["now"], preflight=lambda v: checks.append(1) or LIMIT,
        run_driver=lambda **k: calls.append(k))
    agent.pause_for_usage(LIMIT)
    clock["now"] = RESET + 1
    agent.poll_once()
    agent.poll_once()
    assert checks == [1] and not calls
    assert agent._usage_pause()["resume_after"] == clock["now"] + 300


def test_quota_preflight_keeps_the_reset_even_with_zero_exit_code():
    def run(*a, **k):
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "type": "result", "subtype": "success", "is_error": True, "result": LIMIT}))
    detail = ga.check_credentials({ga.OAUTH_KEY: "fake"}, runner=run)
    assert is_usage_limited(detail)
    assert resume_after(detail, now=NOW) == RESET
