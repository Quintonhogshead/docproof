"""app/warden/journal.py: the sqlite journal's API, with a fake clock so
every timestamp in an assertion is exact rather than "close to now"."""
from __future__ import annotations

import pytest

from app.warden.journal import Journal, RequestPending


class _Clock:
    """A settable clock: `Journal(path, now=clock)` reads `clock()`."""

    def __init__(self, start: str) -> None:
        self.value = start

    def __call__(self) -> str:
        return self.value


@pytest.fixture
def clock() -> _Clock:
    return _Clock("2026-09-22T12:00:00+00:00")


@pytest.fixture
def journal(tmp_path, clock) -> Journal:
    j = Journal(tmp_path / "journal.sqlite", now=clock)
    yield j
    j.close()


# -- findings -----------------------------------------------------------

def test_upsert_finding_creates_a_row(journal):
    f = journal.upsert_finding("agent-silent", "Kyler", "high",
                                "Kyler's run has gone quiet", {"age_s": 900})
    assert f.rule == "agent-silent"
    assert f.key == "Kyler"
    assert f.first_seen == f.last_seen == "2026-09-22T12:00:00+00:00"
    assert f.resolved_at is None
    assert f.notified_at is None
    assert f.evidence == {"age_s": 900}


def test_upsert_finding_dedupes_and_bumps_last_seen(journal, clock):
    f1 = journal.upsert_finding("agent-silent", "Kyler", "high", "silent", {})
    clock.value = "2026-09-22T12:20:00+00:00"
    f2 = journal.upsert_finding("agent-silent", "Kyler", "high",
                                 "still silent", {"age_s": 1200})
    assert f2.id == f1.id
    assert f2.first_seen == "2026-09-22T12:00:00+00:00"
    assert f2.last_seen == "2026-09-22T12:20:00+00:00"
    assert f2.summary == "still silent"
    assert len(journal.open_findings()) == 1


def test_upsert_finding_keeps_notified_at_across_reseeing(journal, clock):
    f1 = journal.upsert_finding("agent-silent", "Kyler", "high", "silent", {})
    journal.mark_notified(f1.id)
    clock.value = "2026-09-22T12:20:00+00:00"
    f2 = journal.upsert_finding("agent-silent", "Kyler", "high",
                                 "still silent", {})
    assert f2.notified_at == "2026-09-22T12:00:00+00:00"


def test_different_keys_are_different_findings(journal):
    journal.upsert_finding("agent-silent", "Kyler", "high", "s", {})
    journal.upsert_finding("agent-silent", "Cooper", "high", "s", {})
    assert len(journal.open_findings()) == 2


def test_resolve_missing_closes_absent_findings(journal):
    journal.upsert_finding("agent-silent", "Kyler", "high", "s1", {})
    journal.upsert_finding("book-unclaimed", "Cooper", "medium", "s2", {})

    resolved = journal.resolve_missing({("agent-silent", "Kyler")})

    assert [r.key for r in resolved] == ["Cooper"]
    assert resolved[0].resolved_at is not None
    remaining = journal.open_findings()
    assert [f.key for f in remaining] == ["Kyler"]


def test_resolve_missing_is_idempotent(journal):
    journal.upsert_finding("agent-silent", "Kyler", "high", "s1", {})
    journal.resolve_missing(set())
    resolved_again = journal.resolve_missing(set())
    assert resolved_again == []


def test_reopened_finding_gets_a_fresh_row(journal, clock):
    f1 = journal.upsert_finding("agent-silent", "Kyler", "high", "first", {})
    journal.resolve_missing(set())
    clock.value = "2026-09-22T13:00:00+00:00"
    f2 = journal.upsert_finding("agent-silent", "Kyler", "high", "second", {})

    assert f2.id != f1.id
    assert f2.first_seen == "2026-09-22T13:00:00+00:00"
    assert len(journal.open_findings()) == 1


# -- actions --------------------------------------------------------------

def test_record_action_returns_an_id(journal):
    aid = journal.record_action("galley-nudge", {"book": "Kyler"}, 0, False,
                                 {"before": 1}, {"after": 1}, True)
    assert isinstance(aid, int)


def test_recent_actions_window(journal, clock):
    journal.record_action("galley-nudge", {"book": "Kyler"}, 0, False, {}, {}, True)
    clock.value = "2026-09-22T20:00:00+00:00"  # 8h later
    journal.record_action("galley-nudge", {"book": "Cooper"}, 0, False, {}, {}, True)

    assert len(journal.recent_actions(hours=24)) == 2
    recent = journal.recent_actions(hours=6)
    assert len(recent) == 1
    assert recent[0].args == {"book": "Cooper"}


def test_last_action_by_verb_and_key(journal, clock):
    journal.record_action("galley-nudge", {"book": "Kyler"}, 0, False, {}, {}, True)
    clock.value = "2026-09-22T12:05:00+00:00"
    journal.record_action("galley-nudge", {"book": "Cooper"}, 0, False, {}, {}, True)

    assert journal.last_action("galley-nudge").args == {"book": "Cooper"}
    assert journal.last_action("galley-nudge", key="Kyler").args == {"book": "Kyler"}
    assert journal.last_action("unknown-verb") is None
    assert journal.last_action("galley-nudge", key="Nobody") is None


def test_action_records_dry_run_and_ok_flags(journal):
    journal.record_action("fly-restart-app", {}, 0, True, {}, {}, False)
    a = journal.recent_actions(hours=24)[0]
    assert a.dry_run is True
    assert a.ok is False
    assert a.tier == 0


# -- messages -------------------------------------------------------------

def test_texts_sent_last_hour(journal, clock):
    journal.record_message("imessage", "out", "+15551234567", "hello")
    clock.value = "2026-09-22T12:30:00+00:00"
    journal.record_message("imessage", "out", "+15551234567", "world")
    clock.value = "2026-09-22T14:00:00+00:00"  # outside the 1h window now
    assert journal.texts_sent_last_hour() == 0


def test_messages_since_filters_channel_direction_and_time(journal, clock):
    journal.record_message("imessage", "out", "+1", "sent")
    journal.record_message("imessage", "in", "+1", "received")
    clock.value = "2026-09-22T12:30:00+00:00"
    journal.record_message("imessage", "out", "+1", "sent later")

    since = journal.messages_since("imessage", "out", "2026-09-22T12:15:00+00:00")
    assert [m["text"] for m in since] == ["sent later"]


# -- requests ---------------------------------------------------------

def test_open_request_numbers_are_monotonic(journal):
    r1 = journal.open_request("action", "restart agent machine", {})
    r2 = journal.open_request("action", "requeue Kyler", {})
    assert r1.number == 1
    assert r2.number == 2


def test_only_one_open_code_request_at_a_time(journal):
    journal.open_request("code", "fix the timeout", {"files": ["a.py"]})
    with pytest.raises(RequestPending):
        journal.open_request("code", "fix something else", {})
    # But other kinds are unaffected.
    journal.open_request("action", "restart", {})


def test_code_request_reopens_after_the_first_is_answered(journal):
    first = journal.open_request("code", "fix the timeout", {})
    journal.answer_request(first.number, "no", "Quinton")
    # No longer open, so a second code request is allowed.
    journal.open_request("code", "fix something else", {})


def test_answer_request_updates_status_and_who(journal, clock):
    req = journal.open_request("action", "restart agent machine", {})
    clock.value = "2026-09-22T12:05:00+00:00"
    answered = journal.answer_request(req.number, "yes", "Quinton")
    assert answered.status == "yes"
    assert answered.answered_by == "Quinton"
    assert answered.answered_at == "2026-09-22T12:05:00+00:00"


def test_answer_request_rejects_bad_answer_word(journal):
    req = journal.open_request("action", "restart", {})
    with pytest.raises(ValueError):
        journal.answer_request(req.number, "maybe", "Quinton")


def test_answer_request_unknown_number_raises_keyerror(journal):
    with pytest.raises(KeyError):
        journal.answer_request(999, "yes", "Quinton")


def test_answer_request_already_answered_raises_valueerror(journal):
    req = journal.open_request("action", "restart", {})
    journal.answer_request(req.number, "yes", "Quinton")
    with pytest.raises(ValueError):
        journal.answer_request(req.number, "no", "Quinton")


def test_open_requests_excludes_answered(journal):
    r1 = journal.open_request("action", "restart", {})
    r2 = journal.open_request("action", "requeue", {})
    journal.answer_request(r1.number, "yes", "Quinton")
    assert [r.number for r in journal.open_requests()] == [r2.number]


def test_get_request_returns_none_for_unknown(journal):
    assert journal.get_request(999) is None


def test_expire_requests_with_explicit_deadline(journal):
    req = journal.open_request(
        "action", "restart", {}, expires_at="2026-09-22T13:00:00+00:00")

    not_yet = journal.expire_requests("2026-09-22T12:30:00+00:00")
    assert not_yet == []
    assert journal.get_request(req.number).status == "open"

    expired = journal.expire_requests("2026-09-22T14:00:00+00:00")
    assert [r.number for r in expired] == [req.number]
    assert journal.get_request(req.number).status == "expired"


def test_expire_requests_default_hours_uses_request_at(journal):
    req = journal.open_request("action", "restart", {})  # opened at 12:00, no deadline

    untouched = journal.expire_requests("2026-09-22T13:00:00+00:00")
    assert untouched == []  # no default_hours given -> never expires on its own

    expired = journal.expire_requests(
        "2026-09-22T23:00:00+00:00", default_hours=6)
    assert [r.number for r in expired] == [req.number]


# -- kv -----------------------------------------------------------------

def test_kv_get_default_and_set(journal):
    assert journal.get("paused") is None
    assert journal.get("paused", "0") == "0"
    journal.set("paused", "1")
    assert journal.get("paused") == "1"
    journal.set("paused", "0")
    assert journal.get("paused") == "0"


# -- summary ------------------------------------------------------------

def test_summary_shape(journal):
    journal.upsert_finding("agent-silent", "Kyler", "high", "s", {})
    journal.open_request("action", "restart", {})
    journal.record_action("galley-nudge", {}, 0, False, {}, {}, True)
    journal.set("paused", "1")
    journal.set("quiet_until", "2026-09-23T00:00:00+00:00")
    journal.set("last_tick_at", "2026-09-22T12:00:00+00:00")

    s = journal.summary()

    assert set(s) == {"open_findings", "open_requests", "recent_actions",
                       "paused", "quiet_until", "last_tick_at"}
    assert s["paused"] is True
    assert s["quiet_until"] == "2026-09-23T00:00:00+00:00"
    assert s["last_tick_at"] == "2026-09-22T12:00:00+00:00"
    assert len(s["open_findings"]) == 1
    assert len(s["open_requests"]) == 1
    assert len(s["recent_actions"]) == 1
    assert s["open_findings"][0]["rule"] == "agent-silent"


def test_summary_defaults_when_nothing_recorded(tmp_path):
    j = Journal(tmp_path / "empty.sqlite", now="2026-09-22T12:00:00+00:00")
    s = j.summary()
    assert s["open_findings"] == []
    assert s["open_requests"] == []
    assert s["recent_actions"] == []
    assert s["paused"] is False
    assert s["quiet_until"] is None
    assert s["last_tick_at"] is None
    j.close()


# -- lifecycle ------------------------------------------------------------

def test_context_manager_persists_across_reopen(tmp_path):
    path = tmp_path / "journal.sqlite"
    with Journal(path, now="2026-09-22T12:00:00+00:00") as j:
        j.set("k", "v")
        j.upsert_finding("r", "k", "low", "s", {})

    with Journal(path, now="2026-09-22T12:00:00+00:00") as j2:
        assert j2.get("k") == "v"
        assert len(j2.open_findings()) == 1


def test_journal_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "journal.sqlite"
    j = Journal(path, now="2026-09-22T12:00:00+00:00")
    assert path.is_file()
    j.close()
