"""app/warden/verbs.py: the claimed-book guard, galley-nudge's own
provably-dead guard, the HTTP-backed DocWatch verbs against a fake opener,
the HubSpot name verbs' verdict re-check, and that `run_verb` always
journals what happened, dry-run or not."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.warden import verbs
from app.warden.journal import Journal

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Records every command it was asked to run; returns a canned proc per
    call, defaulting to success."""

    def __init__(self, proc=None):
        self.calls: list[list[str]] = []
        self._proc = proc or FakeProc()

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return self._proc


class FakeSecrets:
    def __init__(self, **values):
        self._values = values

    def get(self, name):
        return self._values.get(name)


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
    def __init__(self, body: dict):
        self.calls: list = []
        self._body = json.dumps(body).encode()

    def __call__(self, request):
        self.calls.append(request)
        return _Response(self._body)


@pytest.fixture()
def journal(tmp_path):
    with Journal(tmp_path / "journal.sqlite", now=lambda: NOW.isoformat()) as j:
        yield j


def base_snapshot(**agent_overrides):
    agent = {"state": "idle", "book": "", "ledger": {"claimed": [], "pending_delivery": []}}
    agent.update(agent_overrides)
    return {
        "docwatch": {"agent": agent, "watch": {}},
        "fly": {"app": {"machines": [{"id": "app-machine-1"}]},
               "agent": {"machines": [{"id": "agent-machine-1"}]}},
        "native": {"agents": {}, "indesign": {}},
        "hubspot": {"projects": [], "form_submissions": []},
    }


@dataclass
class FakeConfig:
    fly_bin: str = "fly"
    fly_app: str = ""
    app_url: str = "https://warden.test"


def ctx(snapshot, *, run=None, opener=None, journal=None, secrets=None, config=None):
    return verbs.VerbContext(
        config=config or FakeConfig(), secrets=secrets or FakeSecrets(),
        journal=journal, snapshot=snapshot, run=run or FakeRun(), opener=opener,
        now=NOW)


# --- REGISTRY / run_verb dispatch -------------------------------------------

def test_unknown_verb_refuses_and_is_still_journaled(journal):
    c = ctx(base_snapshot(), journal=journal)
    result = verbs.run_verb(c, "no-such-verb", {}, False)
    assert result.ok is False
    actions = journal.recent_actions()
    assert actions[0].verb == "no-such-verb"
    assert actions[0].ok is False


def test_bad_arguments_refuse_cleanly_instead_of_raising(journal):
    c = ctx(base_snapshot(), journal=journal)
    result = verbs.run_verb(c, "docwatch-requeue", {}, False)  # missing file_id
    assert result.ok is False
    assert "bad arguments" in result.message


# --- the claimed-book guard --------------------------------------------------

def test_needs_idle_agent_refuses_while_a_book_is_running(journal):
    snap = base_snapshot(state="running")
    c = ctx(snap, journal=journal)
    result = verbs.run_verb(c, "fly-restart-app", {}, False)
    assert result.ok is False
    assert result.message == "a book is claimed"
    assert c.run.calls == []


def test_needs_idle_agent_refuses_while_the_ledger_has_a_claim(journal):
    snap = base_snapshot(state="idle", ledger={"claimed": [{"file_id": "x"}]})
    c = ctx(snap, journal=journal)
    result = verbs.run_verb(c, "fly-restart-app", {}, False)
    assert result.ok is False
    assert result.message == "a book is claimed"


def test_needs_idle_agent_allows_a_truly_idle_agent(journal):
    run = FakeRun()
    snap = base_snapshot(state="idle")
    c = ctx(snap, run=run, journal=journal)
    result = verbs.run_verb(c, "fly-restart-app", {}, False)
    assert result.ok is True
    assert run.calls == [["fly", "machine", "restart", "app-machine-1", "-a", ""]]


def test_galley_nudge_is_not_gated_by_needs_idle_agent(journal):
    """galley-nudge's whole job is un-sticking a claimed book, so the
    generic guard must not block it — only its own stall-finding check."""
    snap = base_snapshot(state="running", book="Dalton - Book 1.docx",
                         ledger={"claimed": [{"file_id": "f1", "name": "Dalton - Book 1.docx"}]})
    journal.upsert_finding("agent-silent", "Dalton - Book 1.docx", "high",
                           "stuck", {"book": "Dalton - Book 1.docx"})
    run = FakeRun()
    c = ctx(snap, run=run, journal=journal)
    result = verbs.run_verb(c, "galley-nudge", {"book": "Dalton - Book 1.docx"}, False)
    assert result.ok is True
    assert len(run.calls) == 2


# --- galley-nudge's own guard -------------------------------------------

def test_galley_nudge_refuses_without_an_open_stall_finding(journal):
    snap = base_snapshot()
    c = ctx(snap, journal=journal)
    result = verbs.run_verb(c, "galley-nudge", {"book": "Nobody - Book 1.docx"}, False)
    assert result.ok is False
    assert "provably dead" in result.message
    assert c.run.calls == []


def test_galley_nudge_dry_run_does_not_touch_run(journal):
    journal.upsert_finding("agent-stalled", "Georgis - Book 1.docx", "high",
                           "stalled", {"book": "Georgis - Book 1.docx"})
    snap = base_snapshot(book="Georgis - Book 1.docx")
    run = FakeRun()
    c = ctx(snap, run=run, journal=journal)
    result = verbs.run_verb(c, "galley-nudge", {"book": "Georgis - Book 1.docx"}, True)
    assert result.ok is True
    assert result.result["dry_run"] is True
    assert run.calls == []
    action = journal.recent_actions()[0]
    assert action.dry_run is True


# --- galley-resume -----------------------------------------------------

def test_galley_resume_refuses_before_the_pause_elapses(journal):
    snap = base_snapshot(usage_pause={"resume_after": NOW.timestamp() + 3600,
                                      "reason": "usage limit"})
    c = ctx(snap, journal=journal)
    result = verbs.run_verb(c, "galley-resume", {}, False)
    assert result.ok is False
    assert c.run.calls == []


def test_galley_resume_succeeds_once_past_due(journal):
    snap = base_snapshot(usage_pause={"resume_after": NOW.timestamp() - 60,
                                      "reason": "usage limit"})
    run = FakeRun()
    c = ctx(snap, run=run, journal=journal)
    result = verbs.run_verb(c, "galley-resume", {}, False)
    assert result.ok is True
    assert run.calls[0][-1] == "rm -f /data/galley-workspaces/.subscription-pause.json"


def test_galley_resume_refuses_with_no_pause_at_all(journal):
    c = ctx(base_snapshot(usage_pause=None), journal=journal)
    result = verbs.run_verb(c, "galley-resume", {}, False)
    assert result.ok is False


# --- HTTP-backed DocWatch verbs ------------------------------------------

def test_docwatch_requeue_resets_the_flag_then_runs(journal):
    opener = FakeOpener({"started": True})
    c = ctx(base_snapshot(), opener=opener, journal=journal,
           secrets=FakeSecrets(warden_token="tok"))
    result = verbs.run_verb(c, "docwatch-requeue", {"file_id": "drive-1"}, False)
    assert result.ok is True
    assert len(opener.calls) == 2
    first_body = json.loads(opener.calls[0].data)
    assert first_body == {"file_id": "drive-1", "stage": "proof"}
    assert opener.calls[0].full_url.endswith("/api/watch/warden/flags/reset")
    assert opener.calls[1].full_url.endswith("/api/watch/warden/run")


def test_docwatch_requeue_dry_run_makes_no_http_call(journal):
    opener = FakeOpener({})
    c = ctx(base_snapshot(), opener=opener, journal=journal,
           secrets=FakeSecrets(warden_token="tok"))
    result = verbs.run_verb(c, "docwatch-requeue", {"file_id": "drive-1"}, True)
    assert result.ok is True
    assert opener.calls == []


def test_docwatch_run_fails_cleanly_with_no_token(journal):
    c = ctx(base_snapshot(), opener=FakeOpener({}), journal=journal,
           secrets=FakeSecrets())
    result = verbs.run_verb(c, "docwatch-run", {}, False)
    assert result.ok is False
    assert "no warden_token" in result.result.get("error", "") or not result.ok


def test_resend_completion_posts_the_file_id(journal):
    opener = FakeOpener({"sent": True, "file_id": "drive-1", "job_id": "job-1"})
    c = ctx(base_snapshot(), opener=opener, journal=journal,
           secrets=FakeSecrets(warden_token="tok"))
    result = verbs.run_verb(c, "resend-completion", {"file_id": "drive-1"}, False)
    assert result.ok is True
    assert json.loads(opener.calls[0].data) == {"file_id": "drive-1"}


# --- HubSpot name verbs --------------------------------------------------

def _snapshot_for_fill():
    snap = base_snapshot()
    snap["hubspot"]["projects"] = [{"id": "hs-1", "author_first": "", "author_last": ""}]
    snap["docwatch"]["files"] = [{"hubspot_id": "hs-1", "subfolder_name": "Casey, Jordan",
                                  "byline": "Jordan Casey"}]
    return snap


def _snapshot_for_propose():
    snap = base_snapshot()
    snap["hubspot"]["projects"] = [{"id": "hs-2", "author_first": "Jordn",
                                    "author_last": "Casey"}]
    snap["docwatch"]["files"] = [{"hubspot_id": "hs-2", "subfolder_name": "Casey, Jordan",
                                  "byline": "Jordan Casey"}]
    return snap


def test_hubspot_fill_name_writes_when_the_verdict_is_fill(journal):
    opener = FakeOpener({})
    snap = _snapshot_for_fill()
    c = ctx(snap, opener=opener, journal=journal, secrets=FakeSecrets(hubspot="tok"))
    result = verbs.run_verb(
        c, "hubspot-fill-name",
        {"project_id": "hs-1", "first": "Jordan", "last": "Casey"}, False)
    assert result.ok is True
    assert opener.calls, "expected a PATCH to HubSpot"


def test_hubspot_fill_name_refuses_when_the_verdict_is_not_fill(journal):
    snap = _snapshot_for_propose()
    c = ctx(snap, journal=journal, secrets=FakeSecrets(hubspot="tok"))
    result = verbs.run_verb(
        c, "hubspot-fill-name",
        {"project_id": "hs-2", "first": "Jordan", "last": "Casey"}, False)
    assert result.ok is False
    assert "not a 'fill' case" in result.message


def test_hubspot_set_name_requires_propose_verdict(journal):
    snap = _snapshot_for_propose()
    opener = FakeOpener({})
    c = ctx(snap, opener=opener, journal=journal, secrets=FakeSecrets(hubspot="tok"))
    result = verbs.run_verb(
        c, "hubspot-set-name",
        {"project_id": "hs-2", "first": "Jordan", "last": "Casey"}, False)
    assert result.ok is True
    assert opener.calls


def test_hubspot_set_name_dry_run_never_calls_hubspot(journal):
    snap = _snapshot_for_propose()
    opener = FakeOpener({})
    c = ctx(snap, opener=opener, journal=journal, secrets=FakeSecrets(hubspot="tok"))
    result = verbs.run_verb(
        c, "hubspot-set-name",
        {"project_id": "hs-2", "first": "Jordan", "last": "Casey"}, True)
    assert result.ok is True
    assert opener.calls == []


# --- native / indesign ---------------------------------------------------

def test_native_kickstart_runs_launchctl(journal):
    run = FakeRun()
    c = ctx(base_snapshot(), run=run, journal=journal)
    result = verbs.run_verb(c, "native-kickstart",
                            {"label": "com.docproof.interior-review-worker"}, False)
    assert result.ok is True
    assert run.calls[0][:3] == ["launchctl", "kickstart", "-k"]


def test_indesign_restart_runs_all_three_steps(journal):
    run = FakeRun()
    c = ctx(base_snapshot(), run=run, journal=journal)
    result = verbs.run_verb(c, "indesign-restart", {}, False)
    assert result.ok is True
    assert len(run.calls) == 3


# --- every call is journaled ----------------------------------------------

def test_every_verb_call_is_journaled_with_before_and_result(journal):
    run = FakeRun()
    c = ctx(base_snapshot(state="idle"), run=run, journal=journal)
    verbs.run_verb(c, "fly-restart-app", {}, False, finding_id=42)
    action = journal.recent_actions()[0]
    assert action.verb == "fly-restart-app"
    assert action.finding_id == 42
    assert action.tier == 0
    assert action.before.get("group") == "app"
