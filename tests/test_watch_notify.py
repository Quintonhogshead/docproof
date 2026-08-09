"""The alert email, without sending an email.

`notify.send` is one Gmail request, built and inspected here: the account it
speaks as, the base64 message it carries. `summary` decides whether a pass is
worth an email at all, and `maybe_notify` is the one seam the tick calls —
silent without an address, and never loud when the mail server is."""
from __future__ import annotations

import base64
import json

from app.watch import notify
from app.watch.drive import DriveError
from app.watch.settings import WatchSettings
from app.watch.tick import TickReport


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener():
    calls: list = []

    def opener(request, timeout=60):
        calls.append(request)
        return _Resp(json.dumps({"id": "msg-1"}).encode())

    opener.calls = calls
    return opener


# --- send ---------------------------------------------------------------------

def test_send_posts_the_message_as_the_signed_in_account():
    opener = _opener()

    notify.send("at-1", "quinton@atmospherepress.com", "Subj", "The body.",
                opener=opener)

    request = opener.calls[0]
    assert request.full_url == notify.SEND_URL
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer at-1"
    raw = json.loads(request.data)["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: quinton@atmospherepress.com" in decoded
    assert "Subject: Subj" in decoded
    assert "The body." in decoded


# --- summary ------------------------------------------------------------------

def test_a_pass_with_nothing_to_report_is_no_email():
    assert notify.summary(TickReport()) is None


def test_the_summary_names_the_needs_human_and_the_failed():
    report = TickReport()
    report.needs_human.append(("Smith.docx", "two Projects are ready"))
    report.failed.append(("Jones.docx", "gave up after 3 tries"))

    subject, body = notify.summary(report)

    assert "2" in subject
    assert "Smith.docx" in body and "two Projects are ready" in body
    assert "Jones.docx" in body and "gave up after 3 tries" in body


# --- maybe_notify -------------------------------------------------------------

def test_maybe_notify_sends_when_configured_and_a_person_is_needed():
    opener = _opener()
    ws = WatchSettings(notify_email="quinton@atmospherepress.com")
    report = TickReport()
    report.needs_human.append(("Smith.docx", "two Projects are ready"))

    notify.maybe_notify("at-1", ws, report, opener=opener)

    assert len(opener.calls) == 1


def test_maybe_notify_is_silent_without_an_address():
    opener = _opener()
    report = TickReport()
    report.needs_human.append(("Smith.docx", "x"))

    notify.maybe_notify("at-1", WatchSettings(), report, opener=opener)

    assert opener.calls == []


def test_maybe_notify_is_silent_when_nothing_needs_a_person():
    opener = _opener()
    ws = WatchSettings(notify_email="quinton@atmospherepress.com")

    notify.maybe_notify("at-1", ws, TickReport(), opener=opener)

    assert opener.calls == []


def test_a_send_that_fails_is_swallowed_not_raised():
    def opener(request, timeout=60):
        raise DriveError("Gmail refused: insufficient scope")

    ws = WatchSettings(notify_email="quinton@atmospherepress.com")
    report = TickReport()
    report.needs_human.append(("Smith.docx", "x"))

    notify.maybe_notify("at-1", ws, report, opener=opener)   # must not raise
