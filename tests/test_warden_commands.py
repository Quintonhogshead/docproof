"""app/warden/commands.py: the deterministic inbound vocabulary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.warden.commands import Reply, handle
from app.warden.config import WardenConfig
from app.warden.journal import Journal

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


class _Clock:
    """A settable clock for `Journal(path, now=clock)`, so a test can move
    the journal's own notion of "now" independently of the `now` passed to
    `handle()` -- the two are the same live clock in real use (tick.py
    constructs both from one call to `datetime.now`), but distinct here."""

    def __init__(self, start: str) -> None:
        self.value = start

    def __call__(self) -> str:
        return self.value


def _config(**overrides) -> WardenConfig:
    overrides.setdefault("owner_name", "Quinton")
    return WardenConfig(**overrides)


def _snapshot(**overrides):
    base = {
        "docwatch": {
            "agent": {"state": "running", "book": "Kyler", "phase": "settle",
                      "age_s": 125},
            "awaiting": [{"file_id": "1"}, {"file_id": "2"}],
            "last_tick": "2026-09-22T15:00:00+00:00",
            "files": [
                {"name": "Kyler - Book One.docx", "author_last": "Kyler",
                 "job_id": "job-1"},
            ],
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW.isoformat())


@pytest.fixture
def journal(tmp_path, clock):
    j = Journal(tmp_path / "journal.sqlite", now=clock)
    yield j
    j.close()


# -- status ---------------------------------------------------------------

def test_status_reports_agent_book_and_counts(journal):
    journal.upsert_finding("agent-silent", "Kyler", "high", "silent", {})
    journal.open_request("action", "restart", {})

    reply = handle("status", journal=journal, config=_config(),
                    snapshot=_snapshot(), now=NOW)

    assert isinstance(reply, Reply)
    assert reply.action is None
    assert "running" in reply.text
    assert "Kyler" in reply.text
    assert "settle" in reply.text
    assert "2 book(s) awaiting" in reply.text
    assert "1 open finding(s)" in reply.text
    assert "1 open request(s)" in reply.text


def test_status_is_case_insensitive(journal):
    reply = handle("Status", journal=journal, config=_config(),
                    snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert reply.text


def test_status_shows_paused(journal):
    journal.set("paused", "1")
    reply = handle("status", journal=journal, config=_config(),
                    snapshot=_snapshot(), now=NOW)
    assert "Paused" in reply.text


def test_status_survives_empty_snapshot(journal):
    reply = handle("status", journal=journal, config=_config(),
                    snapshot={}, now=NOW)
    assert reply.action is None
    assert "unknown" in reply.text
    assert "0 book(s) awaiting" in reply.text


# -- yes/no -----------------------------------------------------------------

def test_yes_runs_the_request(journal):
    req = journal.open_request(
        "action", "restart agent machine", {"verb": "fly-restart-agent"})
    reply = handle(f"yes {req.number}", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action == ("run_request", req.number)
    assert journal.get_request(req.number).status == "yes"
    assert journal.get_request(req.number).answered_by == "Quinton"


def test_short_form_yes_also_runs_the_request(journal):
    req = journal.open_request("action", "restart agent machine", {})
    reply = handle(f"y{req.number}", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action == ("run_request", req.number)


def test_no_leaves_it_alone(journal):
    req = journal.open_request("action", "restart agent machine", {})
    reply = handle(f"no {req.number}", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert journal.get_request(req.number).status == "no"


def test_yes_unknown_number_reports_not_open(journal):
    reply = handle("yes 999", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert "isn't open" in reply.text


def test_yes_uses_owner_name_from_config(journal):
    req = journal.open_request("action", "restart", {})
    handle(f"yes {req.number}", journal=journal,
           config=_config(owner_name="Someone Else"),
           snapshot=_snapshot(), now=NOW)
    assert journal.get_request(req.number).answered_by == "Someone Else"


# -- pause / resume ---------------------------------------------------------

def test_pause_sets_kv_and_replies(journal):
    reply = handle("pause", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert journal.get("paused") == "1"


def test_resume_clears_kv(journal):
    journal.set("paused", "1")
    reply = handle("resume", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert journal.get("paused") == "0"


def test_pause_resume_case_insensitive(journal):
    handle("PAUSE", journal=journal, config=_config(), snapshot=_snapshot(), now=NOW)
    assert journal.get("paused") == "1"
    handle("Resume", journal=journal, config=_config(), snapshot=_snapshot(), now=NOW)
    assert journal.get("paused") == "0"


# -- quiet --------------------------------------------------------------

def test_quiet_hours_sets_quiet_until(journal):
    reply = handle("quiet 3h", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert journal.get("quiet_until") == (NOW + timedelta(hours=3)).isoformat()


def test_quiet_minutes_sets_quiet_until(journal):
    handle("quiet 45m", journal=journal, config=_config(),
           snapshot=_snapshot(), now=NOW)
    assert journal.get("quiet_until") == (NOW + timedelta(minutes=45)).isoformat()


def test_quiet_accepts_full_unit_words(journal):
    handle("quiet 2 hours", journal=journal, config=_config(),
           snapshot=_snapshot(), now=NOW)
    assert journal.get("quiet_until") == (NOW + timedelta(hours=2)).isoformat()


# -- forget -------------------------------------------------------------

def test_forget_surname_returns_verb_action(journal):
    reply = handle("forget Kyler", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action == ("verb", "galley-nudge", {"book": "Kyler"})
    assert "Kyler" in reply.text


def test_forget_preserves_multi_word_names(journal):
    reply = handle("forget Van Der Berg", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action == ("verb", "galley-nudge", {"book": "Van Der Berg"})


# -- why ------------------------------------------------------------------

def test_why_reports_findings_actions_and_file(journal):
    journal.upsert_finding("agent-silent", "Kyler",
                            "high", "Kyler's run has gone quiet", {})
    journal.record_action("galley-nudge", {"book": "Kyler"}, 0, False, {}, {}, True)

    reply = handle("why Kyler", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)

    assert reply.action is None
    assert "Kyler" in reply.text
    assert "open finding" in reply.text
    assert "action(s)" in reply.text
    assert "job-1" in reply.text


def test_why_nothing_found_says_so(journal):
    reply = handle("why Nobody", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action is None
    assert "Nothing on Nobody" in reply.text


def test_why_ignores_findings_older_than_48h_actions(journal, clock):
    # An action outside the 48h window should not appear.
    journal.record_action("galley-nudge", {"book": "OldCase"}, 0, False, {}, {}, True)
    later = NOW + timedelta(hours=49)
    clock.value = later.isoformat()
    reply = handle("why OldCase", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=later)
    assert "Nothing on OldCase" in reply.text


# -- fallback ---------------------------------------------------------------

def test_unrecognised_text_goes_to_the_model(journal):
    reply = handle("what happened to Kyler's book?", journal=journal,
                   config=_config(), snapshot=_snapshot(), now=NOW)
    assert reply.text == ""
    assert reply.action == ("model", "what happened to Kyler's book?")


def test_empty_text_goes_to_the_model(journal):
    reply = handle("", journal=journal, config=_config(),
                   snapshot=_snapshot(), now=NOW)
    assert reply.action == ("model", "")
