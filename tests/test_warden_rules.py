"""app/warden/rules.py: every rule fires on its fixture and not on
healthy.json; thresholds are respected. Pure functions over recorded
snapshots — no I/O in this file at all."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.warden import rules

FIXTURES = Path(__file__).parent / "fixtures" / "warden"
NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


@dataclass
class Thresholds:
    agent_silent_factor: int = 3
    agent_stalled_min: int = 90
    unclaimed_extra_h: int = 1
    held_batch_h: int = 24
    skipped_ticks: int = 2
    request_expiry_h: int = 12
    tick_late_factor: int = 2
    fly_log_lines: int = 200


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def rule_names(findings) -> set[str]:
    return {f.rule for f in findings}


def test_healthy_fixture_fires_nothing():
    snapshot = load("healthy.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert findings == []


# -- agent-silent -----------------------------------------------------------

def test_agent_silent_fires_on_its_fixture():
    snapshot = load("agent_silent.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "agent-silent" in rule_names(findings)
    hit = next(f for f in findings if f.rule == "agent-silent")
    assert hit.tier == 0
    assert hit.verbs == ("galley-nudge",)
    assert hit.severity == "high"


def test_agent_silent_respects_the_factor_threshold():
    snapshot = load("agent_silent.json")
    # age_s is 1200, interval 60 -> 20x. A factor of 25 should not fire.
    findings = rules.evaluate(snapshot, Thresholds(agent_silent_factor=25), now=NOW)
    assert "agent-silent" not in rule_names(findings)


def test_agent_silent_does_not_fire_with_no_book_claimed():
    snapshot = load("agent_silent.json")
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = []
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "agent-silent" not in rule_names(findings)


# -- agent-stalled ------------------------------------------------------------

def test_agent_stalled_fires_when_history_shows_no_progress_for_90_minutes():
    snapshot = load("agent_stalled.json")
    history = []
    for minutes_ago in (100, 80, 60, 40, 20):
        prior = copy.deepcopy(snapshot)
        prior["at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        history.append(prior)
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=history)
    assert "agent-stalled" in rule_names(findings)
    hit = next(f for f in findings if f.rule == "agent-stalled")
    assert hit.verbs == ("galley-nudge",)
    assert hit.tier == 0


def test_agent_stalled_does_not_fire_when_turns_are_progressing():
    snapshot = load("agent_stalled.json")
    history = []
    for i, minutes_ago in enumerate((100, 80, 60, 40, 20)):
        prior = copy.deepcopy(snapshot)
        prior["at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        prior["docwatch"]["agent"]["turns"] = 40 - (i + 1) * 5  # different every time
        history.append(prior)
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=history)
    assert "agent-stalled" not in rule_names(findings)


def test_agent_stalled_does_not_fire_without_history():
    snapshot = load("agent_stalled.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=None)
    assert "agent-stalled" not in rule_names(findings)


# -- agent-dead-token ---------------------------------------------------------

def test_agent_dead_token_fires_on_credentials_error():
    snapshot = load("healthy.json")
    snapshot["docwatch"]["agent"]["credentials_error"] = "401 Unauthorized: token revoked"
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "agent-dead-token")
    assert hit.tier == 2
    assert hit.verbs == ()


def test_agent_dead_token_fires_on_a_401_in_the_agent_log():
    snapshot = load("healthy.json")
    snapshot["fly"]["logs"]["agent"] = ["2026-09-22T13:00:00Z 401 token rejected"]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "agent-dead-token" in rule_names(findings)


# -- agent-quota-frozen -------------------------------------------------------

def test_agent_quota_frozen_fires_on_its_fixture():
    snapshot = load("quota_frozen.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "agent-quota-frozen")
    assert hit.tier == 0
    assert hit.verbs == ("galley-resume",)


def test_agent_quota_frozen_does_not_fire_before_resume_time():
    snapshot = load("quota_frozen.json")
    early = datetime(2020, 1, 1, tzinfo=timezone.utc)
    findings = rules.evaluate(snapshot, Thresholds(), now=early)
    assert "agent-quota-frozen" not in rule_names(findings)


# -- book-unclaimed -----------------------------------------------------------

def test_book_unclaimed_fires_on_its_fixture():
    snapshot = load("book_unclaimed.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "book-unclaimed")
    assert hit.tier == 0
    assert hit.verbs == ("docwatch-requeue",)


def test_book_unclaimed_does_not_fire_while_the_agent_has_a_claim():
    snapshot = load("book_unclaimed.json")
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = [
        {"file_id": "x", "name": "x", "updated_at": NOW.isoformat()}]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "book-unclaimed" not in rule_names(findings)


def test_book_unclaimed_respects_the_extra_hours_threshold():
    snapshot = load("book_unclaimed.json")
    findings = rules.evaluate(snapshot, Thresholds(unclaimed_extra_h=100), now=NOW)
    assert "book-unclaimed" not in rule_names(findings)


# -- book-abandoned -----------------------------------------------------------

def test_book_abandoned_fires_when_ledger_claim_outlives_a_restart():
    snapshot = load("healthy.json")
    snapshot["docwatch"]["agent"]["book"] = ""
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = [
        {"file_id": "file-gone", "name": "Gunn - Book 1.docx",
         "updated_at": "2026-09-22T08:00:00+00:00"}]
    snapshot["fly"]["agent"]["machines"][0]["updated_at"] = "2026-09-22T09:00:00Z"
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "book-abandoned")
    assert hit.tier == 0
    assert hit.verbs == ("galley-nudge",)


def test_book_abandoned_does_not_fire_for_the_actively_claimed_book():
    snapshot = load("healthy.json")
    snapshot["docwatch"]["agent"]["book"] = "Gunn - Book 1.docx"
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = [
        {"file_id": "file-gone", "name": "Gunn - Book 1.docx",
         "updated_at": "2026-09-22T08:00:00+00:00"}]
    snapshot["fly"]["agent"]["machines"][0]["updated_at"] = "2026-09-22T09:00:00Z"
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "book-abandoned" not in rule_names(findings)


# -- watch-tick-missed --------------------------------------------------------

def test_watch_tick_missed_fires_on_its_fixture():
    snapshot = load("tick_missed.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "watch-tick-missed")
    assert hit.tier == 0
    assert "docwatch-run" in hit.verbs and "fly-restart-app" in hit.verbs


def test_watch_tick_missed_respects_the_late_factor():
    snapshot = load("tick_missed.json")
    # Clear the fixed-schedule half of the rule so only the "how late is the
    # last tick" branch, which the factor actually governs, is exercised.
    snapshot["docwatch"]["watch"]["next_tick_at"] = None
    findings = rules.evaluate(snapshot, Thresholds(tick_late_factor=1000), now=NOW)
    assert "watch-tick-missed" not in rule_names(findings)


def test_watch_tick_missed_off_when_auto_ticks_disabled():
    snapshot = load("tick_missed.json")
    snapshot["docwatch"]["watch"]["auto_ticks"] = False
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "watch-tick-missed" not in rule_names(findings)


# -- watch-signin-dead --------------------------------------------------------

def test_watch_signin_dead_fires_on_its_fixture():
    snapshot = load("signin_dead.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "watch-signin-dead")
    assert hit.tier == 2
    assert hit.verbs == ()


# -- watch-model-unavailable ---------------------------------------------------

def test_watch_model_unavailable_fires_on_its_fixture():
    snapshot = load("model_unavailable.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "watch-model-unavailable")
    assert hit.tier == 1


# -- watch-file-skipped-silently -----------------------------------------------

def test_watch_file_skipped_silently_fires_on_its_fixture():
    snapshot = load("file_skipped.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "watch-file-skipped-silently")
    assert hit.tier == 1


def test_watch_file_skipped_silently_respects_skipped_ticks():
    snapshot = load("file_skipped.json")
    findings = rules.evaluate(snapshot, Thresholds(skipped_ticks=1000), now=NOW)
    assert "watch-file-skipped-silently" not in rule_names(findings)


# -- native-worker-down ---------------------------------------------------------

def test_native_worker_down_fires_on_its_fixture():
    snapshot = load("native_down.json")
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hits = [f for f in findings if f.rule == "native-worker-down"]
    assert hits
    assert all(f.tier == 0 and f.verbs == ("native-kickstart",) for f in hits)
    keys = {f.key for f in hits}
    assert "com.docproof.interior-review-worker" in keys
    assert "indesign" in keys


def test_native_worker_down_silent_when_not_installed():
    snapshot = load("healthy.json")
    snapshot["native"] = {"installed": False, "agents": {}, "worker": None,
                          "intake": None, "indesign": {"alive": False, "version": "", "error": ""},
                          "batches": []}
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "native-worker-down" not in rule_names(findings)


# -- native-batch-held ----------------------------------------------------------

def test_native_batch_held_fires_when_a_batch_is_held():
    snapshot = load("healthy.json")
    snapshot["native"]["batches"] = [
        {"id": "batch-1", "state": "held", "book": "hs-99",
         "hold_reason": "author name does not match any folder",
         "updated_at": "2026-09-20T00:00:00+00:00"}]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "native-batch-held")
    assert hit.tier == 2
    assert hit.verbs == ()


def test_native_batch_held_silent_for_running_batches():
    snapshot = load("healthy.json")
    snapshot["native"]["batches"] = [
        {"id": "batch-1", "state": "running", "book": "hs-99",
         "hold_reason": "", "updated_at": "2026-09-20T00:00:00+00:00"}]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "native-batch-held" not in rule_names(findings)


# -- machine-oom -----------------------------------------------------------------

def test_machine_oom_fires_on_an_oom_log_line():
    snapshot = load("healthy.json")
    snapshot["fly"]["logs"]["agent"] = ["2026-09-22T13:00:00Z Out of memory: kill process 42"]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "machine-oom")
    assert hit.key == "agent"
    assert hit.verbs == ("fly-restart-agent",)
    assert hit.tier == 1


def test_machine_oom_matches_the_bare_oom_token():
    snapshot = load("healthy.json")
    snapshot["fly"]["logs"]["app"] = ["2026-09-22T13:00:00Z kernel: oom-killer invoked"]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "machine-oom")
    assert hit.key == "app"
    assert hit.verbs == ("fly-restart-app",)


# -- deploy-during-run ------------------------------------------------------------

def test_deploy_during_run_fires_when_release_lands_after_a_claim():
    snapshot = load("healthy.json")
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = [
        {"file_id": "file-x", "name": "Book X", "updated_at": "2026-09-22T08:00:00+00:00"}]
    snapshot["fly"]["agent"]["release"] = {"version": 999, "created_at": "2026-09-22T09:00:00+00:00"}
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    hit = next(f for f in findings if f.rule == "deploy-during-run")
    assert hit.tier == 2
    assert hit.verbs == ()


def test_deploy_during_run_silent_when_release_predates_the_claim():
    snapshot = load("healthy.json")
    snapshot["docwatch"]["agent"]["ledger"]["claimed"] = [
        {"file_id": "file-x", "name": "Book X", "updated_at": "2026-09-22T09:00:00+00:00"}]
    snapshot["fly"]["agent"]["release"] = {"version": 999, "created_at": "2026-09-22T08:00:00+00:00"}
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert "deploy-during-run" not in rule_names(findings)


# -- source-unreachable -----------------------------------------------------------

def test_source_unreachable_fires_after_three_consecutive_errors():
    snapshot = load("healthy.json")
    snapshot["hubspot"] = {"error": "HubSpot would not answer (503)."}
    history = [
        {**load("healthy.json"), "hubspot": {"error": "timeout"}},
        {**load("healthy.json"), "hubspot": {"error": "timeout"}},
    ]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=history)
    hit = next(f for f in findings if f.rule == "source-unreachable")
    assert hit.key == "hubspot"
    assert hit.tier == 2


def test_source_unreachable_silent_after_only_two_errors():
    snapshot = load("healthy.json")
    snapshot["hubspot"] = {"error": "HubSpot would not answer (503)."}
    history = [{**load("healthy.json"), "hubspot": {"error": "timeout"}}]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=history)
    assert "source-unreachable" not in rule_names(findings)


def test_source_unreachable_silent_once_the_source_recovers():
    snapshot = load("healthy.json")  # no error this tick
    history = [
        {**load("healthy.json"), "hubspot": {"error": "timeout"}},
        {**load("healthy.json"), "hubspot": {"error": "timeout"}},
    ]
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW, history=history)
    assert "source-unreachable" not in rule_names(findings)


# -- evaluate() never raises ----------------------------------------------------

def test_evaluate_never_raises_on_an_empty_snapshot():
    findings = rules.evaluate({}, Thresholds(), now=NOW)
    assert findings == []


def test_evaluate_never_raises_on_garbage_typed_fields():
    snapshot = {"docwatch": "not a dict", "fly": None, "native": 42,
               "hubspot": [], "email": "nope"}
    findings = rules.evaluate(snapshot, Thresholds(), now=NOW)
    assert findings == []
