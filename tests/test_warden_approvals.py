"""app/warden/approvals.py: rendering, parsing and applying "yes N"/"no N"."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.warden.approvals import apply_answer, expire, parse_answer, request_text
from app.warden.journal import Journal, Request


def test_request_text_format():
    req = Request(14, "2026-09-22T12:00:00+00:00", "action",
                  "restart the agent machine", {}, "open", None, None, None)
    assert request_text(req) == (
        "#14 restart the agent machine. Reply 'yes 14' or 'no 14'.")


@pytest.mark.parametrize("text,expected", [
    ("yes 14", ("yes", 14)),
    ("y14", ("yes", 14)),
    ("no 3", ("no", 3)),
    ("YES #14", ("yes", 14)),
    ("  no   #7  ", ("no", 7)),
    ("n9", ("no", 9)),
    ("No #2", ("no", 2)),
])
def test_parse_answer_accepts(text, expected):
    assert parse_answer(text) == expected


@pytest.mark.parametrize("text", [
    "status", "maybe 5", "yes", "14", "yes14x", "", "yes -5", "book 14 chapters",
])
def test_parse_answer_rejects(text):
    assert parse_answer(text) is None


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _journal(tmp_path, now=NOW):
    return Journal(tmp_path / "journal.sqlite", now=now.isoformat())


def test_apply_answer_yes_applies_and_returns_request(tmp_path):
    j = _journal(tmp_path)
    req = j.open_request("action", "restart agent machine", {"verb": "x"})
    result = apply_answer(j, f"yes {req.number}", "Quinton", NOW)
    assert result is not None
    assert result.status == "yes"
    assert result.answered_by == "Quinton"


def test_apply_answer_no_applies_and_returns_request(tmp_path):
    j = _journal(tmp_path)
    req = j.open_request("action", "restart agent machine", {})
    result = apply_answer(j, f"no {req.number}", "Quinton", NOW)
    assert result.status == "no"


def test_apply_answer_unparseable_text_returns_none(tmp_path):
    j = _journal(tmp_path)
    assert apply_answer(j, "hello there", "Quinton", NOW) is None


def test_apply_answer_unknown_number_returns_none(tmp_path):
    j = _journal(tmp_path)
    assert apply_answer(j, "yes 999", "Quinton", NOW) is None


def test_apply_answer_already_answered_returns_none(tmp_path):
    j = _journal(tmp_path)
    req = j.open_request("action", "restart", {})
    first = apply_answer(j, f"yes {req.number}", "Quinton", NOW)
    assert first is not None
    second = apply_answer(j, f"no {req.number}", "Quinton", NOW)
    assert second is None
    # The original "yes" is untouched by the rejected second reply.
    assert j.get_request(req.number).status == "yes"


def test_apply_answer_past_explicit_deadline_returns_none_and_expires_it(tmp_path):
    j = _journal(tmp_path, now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc))
    req = j.open_request(
        "action", "restart", {},
        expires_at=datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc))

    later = datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc)
    assert apply_answer(j, f"yes {req.number}", "Quinton", later) is None
    assert j.get_request(req.number).status == "expired"


def test_apply_answer_before_explicit_deadline_still_works(tmp_path):
    j = _journal(tmp_path, now=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc))
    req = j.open_request(
        "action", "restart", {},
        expires_at=datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc))

    soon = datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc)
    result = apply_answer(j, f"yes {req.number}", "Quinton", soon)
    assert result is not None
    assert result.status == "yes"


def test_expire_wrapper_uses_hours_against_request_at(tmp_path):
    opened_at = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    j = _journal(tmp_path, now=opened_at)
    req = j.open_request("action", "restart", {})  # no explicit expiry

    not_yet = expire(j, datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc), 12)
    assert not_yet == []
    assert j.get_request(req.number).status == "open"

    expired = expire(j, datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc), 12)
    assert [r.number for r in expired] == [req.number]


def test_expire_wrapper_also_honors_explicit_deadlines(tmp_path):
    opened_at = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    j = _journal(tmp_path, now=opened_at)
    req = j.open_request(
        "action", "restart", {},
        expires_at=datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc))

    expired = expire(j, datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc), hours=24)
    assert [r.number for r in expired] == [req.number]
