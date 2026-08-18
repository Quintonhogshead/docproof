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

    assert subject.startswith("[DocProof][High][Action]")   # inbox-sortable
    assert "2" in subject
    assert "Smith.docx" in body and "two Projects are ready" in body
    assert "Jones.docx" in body and "gave up after 3 tries" in body


def test_the_summary_names_a_ready_author_missing_its_book_original():
    report = TickReport()
    report.missing_source.append(
        ("Quinton Johnson", "flagged 'Ready for Formatting' but the folder has "
                            "no manuscript in it yet."))

    subject, body = notify.summary(report)

    assert "1" in subject
    assert "Quinton Johnson" in body
    assert "no manuscript" in body


def test_a_pass_with_only_a_missing_source_still_emails():
    report = TickReport()
    report.missing_source.append(("Quinton Johnson", "no Book Original yet."))
    assert notify.summary(report) is not None


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


# --- the completion log -------------------------------------------------------
#
# A full record for a finished book. Built here against a populated Job and a
# sample prep.json, so every group renders, the estimated cost is labelled, and
# a missing prep.json degrades to an email rather than an exception.

import json as _json
from pathlib import Path

from app.jobs import Job
from app.watch.drive import DOCX_MIME, DriveError, DriveFile
from app.watch.state import FileRecord

PREP = {
    "counts": {"paragraphs": 4200, "blank_lines": 120,
               "scene_breaks_inserted": 8, "scene_breaks_from_author": 2,
               "unanswered": 0, "untouched_outside_body": 5},
    "styles": {"body": 4000, "chapter": 30},
    "usage": {"input_tokens": 100000, "output_tokens": 5000, "api_calls": 13},
    "style_sheet": {"name": "house", "version": 7},
    "tagging_prompt": {"name": "prep", "version": 3},
}


def _job(results_dir=None, **over):
    fields = dict(
        id="job-1", filename="Johnson - Book Original.docx", source_path="/x",
        model="claude-opus-4-8", mode="now", state="done", effort="high",
        kind="prep", done=12, total=12, api_calls=13, input_tokens=100000,
        output_tokens=5000, cache_read_tokens=80000, cache_write_tokens=2000,
        cost=4.13, words=90000, tagged=1200, flags=3, verified=True,
        created_at="2026-08-09T10:00:00+00:00",
        updated_at="2026-08-09T10:07:30+00:00",
        results_dir=str(results_dir) if results_dir else None)
    fields.update(over)
    return Job(**fields)


def _rec(**over):
    fields = dict(file_id="m-1", name="Johnson - Book Original.docx",
                  hubspot_id="hs-Johnson", author_first="Quinton",
                  author_last="Johnson", subfolder_id="sf-1",
                  subfolder_name="Quinton Johnson",
                  uploaded={"Johnson - book 0.docx": "up-1",
                            "Johnson - book 0 - notes.md": "up-2"})
    fields.update(over)
    return FileRecord(**fields)


def _file():
    return DriveFile(id="m-1", name="Johnson - Book Original.docx",
                     mime_type=DOCX_MIME)


def _ws(**over):
    fields = dict(notify_email="quinton@atmospherepress.com",
                  notify_on_complete=True, hubspot_object="0-970")
    fields.update(over)
    return WatchSettings(**fields)


def _with_prep(tmp_path):
    (tmp_path / "prep.json").write_text(_json.dumps(PREP), encoding="utf-8")
    return tmp_path


def test_completion_renders_every_group(tmp_path):
    job = _job(_with_prep(tmp_path))
    subject, text, html = notify.completion(
        _ws(), job, _file(), _rec(), ["Johnson - book 0.docx"], "sf-1")

    assert subject.startswith("[DocProof][Low][Done]")     # inbox-sortable
    assert "Quinton Johnson" in subject and "Book Original" in subject
    # the tags sort the inbox but stay out of the body a person reads
    assert not text.startswith("[DocProof]")
    # routing, model, cost, tokens, quality, timing, detail
    assert "Quinton Johnson" in text            # author / subfolder
    assert "hs-Johnson" in text                 # the record
    assert "claude-opus-4-8" in text and "high" in text
    assert "$4.13" in text and "estimated" in text.lower()
    assert "100,000" in text                    # input tokens, grouped
    assert "7m 30s" in text                     # elapsed, derived
    assert "Johnson - book 0.docx" in text      # an output name
    assert "https://drive.google.com/drive/folders/sf-1" in text
    assert "4,200" in text                      # a prep.json count
    # the HTML mirror carries a table and a live link
    assert "<table" in html
    assert 'href="https://drive.google.com/file/d/up-1/view"' in html


def test_a_missing_prep_json_still_makes_an_email(tmp_path):
    job = _job(results_dir=None, cost=None)     # nothing on disk, no cost
    subject, text, html = notify.completion(
        _ws(), job, _file(), _rec(uploaded={}), [], "sf-1")

    assert subject and text and "<table" in html
    assert "unavailable" in text                # cost degrades, not crashes
    assert "—" in text                          # optional fields as em dashes


def test_maybe_complete_sends_multipart_with_the_prep_json_attached(tmp_path):
    opener = _opener()
    job = _job(_with_prep(tmp_path))

    sent = notify.maybe_complete("at-1", _ws(), job, _file(), _rec(),
                                 ["Johnson - book 0.docx"], "sf-1",
                                 opener=opener)

    assert sent is True
    raw = _json.loads(opener.calls[0].data)["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "text/html" in decoded               # the alternative
    assert "prep.json" in decoded               # the attachment filename
    assert "application/json" in decoded


def test_a_completion_send_that_fails_is_swallowed(tmp_path):
    def opener(request, timeout=60):
        raise DriveError("Gmail refused the scope")

    job = _job(_with_prep(tmp_path))
    # No exception escapes, and it reports the miss so the caller retries.
    sent = notify.maybe_complete("at-1", _ws(), job, _file(), _rec(),
                                 ["Johnson - book 0.docx"], "sf-1",
                                 opener=opener)

    assert sent is False
