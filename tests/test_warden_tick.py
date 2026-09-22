"""app/warden/tick.py: `run_tick`'s whole cycle against recorded snapshots
(`tests/fixtures/warden/*.json`) with `snapshot.collect` stubbed to hand one
back directly — no Fly, DocWatch, or HubSpot collection is under test here,
only what the tick does with the result. Covers: one Tier-0 action per tick,
quiet hours, the hourly text cap, Tier-1 request creation (and that it does
not repeat itself), expiry, `needs_model`, and the "yes N" round trip through
`listen()`."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.warden import config as config_module
from app.warden import tick as tick_module
from app.warden.journal import Journal
from app.warden.messaging import gmail as gmail_module
from app.warden.messaging.imessage import Inbound

FIXTURES = Path(__file__).parent / "fixtures" / "warden"
NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
OWNER = "+15551234567"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return FakeProc()


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, body=None):
        self.calls = []
        self._body = json.dumps(body if body is not None else {}).encode()

    def __call__(self, request):
        self.calls.append(request)
        return _Response(self._body)


class FakeSecrets:
    def __init__(self, **values):
        self._values = {"warden_token": "tok", "hubspot": "tok", **values}

    def get(self, name):
        return self._values.get(name)


def make_config(**overrides) -> config_module.WardenConfig:
    kwargs = dict(owner_handle=OWNER, imessage_enabled=True, harness="none",
                 team_emails=[], max_texts_per_hour=4)
    kwargs.update(overrides)
    return config_module.WardenConfig(**kwargs)


@pytest.fixture()
def journal(tmp_path):
    with Journal(tmp_path / "journal.sqlite", now=lambda: NOW.isoformat()) as j:
        yield j


def fake_send(sent: list):
    def _send(handle, text, *, run=None, journal=None, max_per_hour=None):
        if journal is not None and max_per_hour is not None:
            if journal.texts_sent_last_hour() >= max_per_hour:
                return False
        sent.append((handle, text))
        return True
    return _send


def stub_collect(monkeypatch, snap: dict):
    monkeypatch.setattr(tick_module.snapshot_module, "collect",
                        lambda *a, **k: json.loads(json.dumps(snap)))


def run_tick(monkeypatch, tmp_path, fixture, *, cfg=None, now=NOW, run=None,
            opener=None, journal_obj, sent=None):
    stub_collect(monkeypatch, load(fixture))
    monkeypatch.setattr(tick_module.imessage, "send", fake_send(sent if sent is not None else []))
    return tick_module.run_tick(
        cfg or make_config(), secrets=FakeSecrets(), journal=journal_obj,
        run=run or FakeRun(), opener=opener or FakeOpener(), now=now, home=tmp_path)


# --- one Tier-0 action per tick ---------------------------------------------

def test_fires_the_one_eligible_tier0_action(tmp_path, monkeypatch, journal):
    run = FakeRun()
    result = run_tick(monkeypatch, tmp_path, "quota_frozen.json", run=run,
                      journal_obj=journal)
    assert result.action is not None
    assert result.action["verb"] == "galley-resume"
    assert result.action["ok"] is True
    assert run.calls  # the fly ssh console command actually ran
    actions = journal.recent_actions()
    assert len(actions) == 1
    assert actions[0].verb == "galley-resume"


def test_only_one_action_fires_even_with_two_eligible_findings(tmp_path, monkeypatch, journal):
    run = FakeRun()
    result = run_tick(monkeypatch, tmp_path, "native_down.json", run=run,
                      journal_obj=journal)
    assert result.action is not None
    assert result.action["verb"] == "native-kickstart"
    assert result.action["args"] == {"label": "com.docproof.interior-review-worker"}
    # The InDesign-liveness finding is also fully actionable (indesign-restart,
    # no arguments needed) — it just lost the one-action lottery this tick, so
    # it must NOT show up as needing the model.
    assert result.needs_model == []
    assert len(journal.recent_actions()) == 1


def test_paused_takes_no_action_but_still_evaluates(tmp_path, monkeypatch, journal):
    journal.set("paused", "1")
    result = run_tick(monkeypatch, tmp_path, "quota_frozen.json", journal_obj=journal)
    assert result.action is None
    assert len(result.findings) == 1


# --- quiet hours -------------------------------------------------------

def test_a_medium_finding_is_not_texted_during_quiet_hours(tmp_path, monkeypatch, journal):
    # 2026-09-23T04:00 UTC is 2026-09-23T00:00 America/New_York — squarely
    # inside the default 23:00-07:00 quiet window.
    quiet_now = datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)
    sent: list = []
    result = run_tick(monkeypatch, tmp_path, "book_unclaimed.json", now=quiet_now,
                      journal_obj=journal, sent=sent)
    assert result.action is not None            # the fix itself still runs
    # Shifting "now" this far forward also trips watch-tick-missed (high,
    # texted regardless of quiet hours) on this fixture — the assertion is
    # about the medium book-unclaimed finding specifically, not "no text at
    # all went out".
    assert not any(text.startswith("[Medium]") for _, text in sent)


def test_a_high_finding_is_texted_even_during_quiet_hours(tmp_path, monkeypatch, journal):
    quiet_now = datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)
    sent: list = []
    run_tick(monkeypatch, tmp_path, "agent_silent.json", now=quiet_now,
            journal_obj=journal, sent=sent)
    assert sent, "a high-severity finding must bypass quiet hours"
    assert sent[0][0] == OWNER


# --- hourly text cap ---------------------------------------------------

def test_the_hourly_text_cap_is_respected_even_for_high_severity(tmp_path, monkeypatch, journal):
    cfg = make_config(max_texts_per_hour=1)
    journal.record_message("imessage", "out", OWNER, "an earlier text")
    sent: list = []
    run_tick(monkeypatch, tmp_path, "agent_silent.json", cfg=cfg,
             journal_obj=journal, sent=sent)
    assert sent == [], "the cap must hold even for a high-severity finding"


# --- Tier-1 requests: created once, texted once, then wait -------------

def test_tier1_finding_opens_exactly_one_request_and_does_not_repeat(tmp_path, monkeypatch, journal):
    sent: list = []
    r1 = run_tick(monkeypatch, tmp_path, "names_propose.json", journal_obj=journal, sent=sent)
    assert len(r1.requests) == 1
    assert r1.requests[0]["status"] == "open"
    assert len(sent) == 1
    assert sent[0][1].startswith(f"#{r1.requests[0]['number']} ")

    sent.clear()
    r2 = run_tick(monkeypatch, tmp_path, "names_propose.json", journal_obj=journal, sent=sent)
    assert len(r2.requests) == 1
    assert r2.requests[0]["number"] == r1.requests[0]["number"]
    assert sent == [], "must not re-ask about a request that's still open"


def test_tier1_finding_with_no_verb_falls_through_to_needs_model(tmp_path, monkeypatch, journal):
    result = run_tick(monkeypatch, tmp_path, "model_unavailable.json", journal_obj=journal)
    assert result.requests == []
    assert len(result.needs_model) == 1
    assert result.needs_model[0]["rule"] == "watch-model-unavailable"


def test_tier2_finding_has_no_verb_and_is_reported_not_acted_on(tmp_path, monkeypatch, journal):
    result = run_tick(monkeypatch, tmp_path, "signin_dead.json", journal_obj=journal)
    assert result.action is None
    assert result.requests == []
    assert len(result.needs_model) == 1
    assert result.needs_model[0]["rule"] == "watch-signin-dead"


# --- expiry --------------------------------------------------------------

def test_an_unanswered_request_expires_and_a_fresh_one_can_open(tmp_path, monkeypatch, journal):
    cfg = make_config()
    r1 = run_tick(monkeypatch, tmp_path, "names_propose.json", cfg=cfg, journal_obj=journal)
    number = r1.requests[0]["number"]
    assert journal.get_request(number).status == "open"

    later = NOW + timedelta(hours=cfg.thresholds.request_expiry_h + 1)
    r2 = run_tick(monkeypatch, tmp_path, "names_propose.json", cfg=cfg, now=later,
                 journal_obj=journal)
    assert journal.get_request(number).status == "expired"
    assert len(r2.requests) == 1
    assert r2.requests[0]["number"] != number


# --- needs_model -> harness ------------------------------------------------

def test_needs_model_invokes_the_harness_when_configured(tmp_path, monkeypatch, journal):
    calls = []
    monkeypatch.setattr(tick_module.harness, "run",
                        lambda *a, **k: calls.append((a, k)) or "handled it")
    cfg = make_config(harness="claude")
    result = run_tick(monkeypatch, tmp_path, "signin_dead.json", cfg=cfg, journal_obj=journal)
    assert result.harness_output == "handled it"
    assert len(calls) == 1
    messages = journal.messages_since("harness", "out", NOW - timedelta(hours=1))
    assert any(m["text"] == "handled it" for m in messages)


def test_harness_none_is_never_invoked(tmp_path, monkeypatch, journal):
    calls = []
    monkeypatch.setattr(tick_module.harness, "run", lambda *a, **k: calls.append(1))
    cfg = make_config(harness="none")
    run_tick(monkeypatch, tmp_path, "signin_dead.json", cfg=cfg, journal_obj=journal)
    assert calls == []


def test_no_model_flag_suppresses_the_harness_even_when_configured(tmp_path, monkeypatch, journal):
    calls = []
    monkeypatch.setattr(tick_module.harness, "run", lambda *a, **k: calls.append(1))
    stub_collect(monkeypatch, load("signin_dead.json"))
    monkeypatch.setattr(tick_module.imessage, "send", fake_send([]))
    cfg = make_config(harness="claude")
    tick_module.run_tick(cfg, secrets=FakeSecrets(), journal=journal, run=FakeRun(),
                         opener=FakeOpener(), now=NOW, home=tmp_path, allow_model=False)
    assert calls == []


# --- dry_run ---------------------------------------------------------------

def test_dry_run_never_calls_the_verbs_underlying_command(tmp_path, monkeypatch, journal):
    run = FakeRun()
    stub_collect(monkeypatch, load("quota_frozen.json"))
    monkeypatch.setattr(tick_module.imessage, "send", fake_send([]))
    result = tick_module.run_tick(make_config(), secrets=FakeSecrets(), journal=journal,
                                  run=run, opener=FakeOpener(), now=NOW, home=tmp_path,
                                  dry_run=True)
    assert result.action["ok"] is True
    assert run.calls == []


# --- resolution text -----------------------------------------------------

def test_a_resolved_finding_gets_one_text(tmp_path, monkeypatch, journal):
    sent: list = []
    run_tick(monkeypatch, tmp_path, "agent_silent.json", journal_obj=journal, sent=sent)
    sent.clear()
    run_tick(monkeypatch, tmp_path, "healthy.json", journal_obj=journal, sent=sent)
    assert len(sent) == 1
    assert sent[0][1].startswith("Resolved: ")


# --- listen(): the "yes N" round trip --------------------------------------

def test_yes_answers_a_tier1_request_and_runs_its_verb(tmp_path, monkeypatch, journal):
    cfg = make_config()
    sent: list = []
    r1 = run_tick(monkeypatch, tmp_path, "names_propose.json", cfg=cfg,
                 journal_obj=journal, sent=sent)
    number = r1.requests[0]["number"]

    inbound_at = NOW + timedelta(minutes=5)
    stub_collect(monkeypatch, load("names_propose.json"))
    monkeypatch.setattr(tick_module.snapshot_module, "save", lambda *a, **k: None)
    monkeypatch.setattr(tick_module.imessage, "read_since",
                        lambda since, allowed_handles: [Inbound(1, OWNER, f"yes {number}", inbound_at)])
    monkeypatch.setattr(tick_module.imessage, "send", fake_send(sent))
    monkeypatch.setattr(gmail_module, "replies", lambda *a, **k: [])
    opener = FakeOpener({})

    result = tick_module.listen(cfg, secrets=FakeSecrets(), journal=journal,
                                run=FakeRun(), opener=opener, now=inbound_at, home=tmp_path)

    assert len(result.handled) == 1
    req = journal.get_request(number)
    assert req.status == "yes"
    actions = [a for a in journal.recent_actions() if a.verb == "hubspot-set-name"]
    assert actions and actions[0].ok is True
    assert opener.calls, "expected the verb to actually PATCH HubSpot"


def test_a_stranger_cannot_answer_by_email(tmp_path, monkeypatch, journal):
    """Only iMessage from the owner's own number is ever treated as an
    approval — an email reply gets a status answer, never a yes."""
    cfg = make_config()
    r1 = run_tick(monkeypatch, tmp_path, "names_propose.json", cfg=cfg, journal_obj=journal)
    number = r1.requests[0]["number"]

    stub_collect(monkeypatch, load("names_propose.json"))
    monkeypatch.setattr(tick_module.imessage, "read_since", lambda *a, **k: [])
    sent_email = []
    monkeypatch.setattr(gmail_module, "replies",
                        lambda *a, **k: [{"id": "m1", "from": "someone@example.com",
                                          "subject": "re", "snippet": f"yes {number}"}])
    monkeypatch.setattr(gmail_module, "send_team",
                        lambda *a, **k: sent_email.append(a) or ["team@example.com"])

    tick_module.listen(cfg, secrets=FakeSecrets(), journal=journal, run=FakeRun(),
                       opener=FakeOpener(), now=NOW + timedelta(minutes=1), home=tmp_path)

    assert journal.get_request(number).status == "open", "email must never approve a request"


def test_the_same_unfixable_finding_does_not_wake_the_model_twice_in_six_hours(tmp_path, monkeypatch, journal):
    calls = []
    monkeypatch.setattr(tick_module.harness, "run",
                        lambda *a, **k: calls.append((a, k)) or "looked")
    cfg = make_config(harness="claude")
    run_tick(monkeypatch, tmp_path, "signin_dead.json", cfg=cfg, journal_obj=journal)
    second = run_tick(monkeypatch, tmp_path, "signin_dead.json", cfg=cfg, journal_obj=journal,
                      now=NOW + timedelta(minutes=20))
    assert len(calls) == 1
    assert second.needs_model == []
    third = run_tick(monkeypatch, tmp_path, "signin_dead.json", cfg=cfg, journal_obj=journal,
                     now=NOW + timedelta(hours=7))
    assert len(calls) == 2
    assert third.needs_model


def test_an_approved_code_request_runs_the_harness_in_code_mode_once(tmp_path, monkeypatch, journal):
    calls = []
    monkeypatch.setattr(tick_module.harness, "run",
                        lambda *a, **k: calls.append((a, k)) or "PR opened")
    cfg = make_config(harness="claude")
    req = journal.open_request("code", "Code fix for x", {"rule": "x", "files": "a.py", "fix": "f"})
    journal.answer_request(req.number, "yes", "Quinton")

    first = run_tick(monkeypatch, tmp_path, "healthy.json", cfg=cfg, journal_obj=journal)
    assert first.harness_output == "PR opened"
    assert calls[0][1]["mode"] == "code"
    assert '"rule": "x"' in calls[0][1]["question"]

    run_tick(monkeypatch, tmp_path, "healthy.json", cfg=cfg, journal_obj=journal,
             now=NOW + timedelta(minutes=20))
    assert len(calls) == 1, "a started code request is never re-run"
