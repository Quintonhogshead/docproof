"""Reading and sending iMessages, without touching Messages.app or a real
chat.db. `read_since` gets a temporary SQLite file built to the same shape
Apple's, with a NULL-text row to prove the `attributedBody` fallback, a
message from a handle not on the allow-list, and an outgoing one — all three
must be filtered out, leaving only what a stranger actually said, oldest
first. `send` gets a fake `run` that records exactly what `osascript` would
have been handed, to prove the message text always arrives as an argument,
never spliced into the script."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.warden.messaging import imessage
from app.warden.messaging.imessage import Inbound

OWNER = "+1 (555) 123-4567"           # what a person types into warden.yaml
OWNER_STORED = "+15551234567"         # what chat.db happens to have stored
STRANGER = "+19995550000"


def _apple_ns(at: datetime) -> int:
    return imessage._apple_ns(at)


def _attributed_body(text: str) -> bytes:
    """A NULL-text message's `attributedBody`, built the same way
    `_text_from_attributed_body` reads one back: the `NSString` class
    marker, five bytes of archiver framing, a one-byte length, then the
    UTF-8 payload."""
    payload = text.encode("utf-8")
    assert len(payload) < 0x81               # keeps this fixture on the short path
    return b"\x04\x0bstreamtyped" + b"NSString" + b"\x00" * 5 + \
        bytes([len(payload)]) + payload


def _build_chat_db(path, since: datetime) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            attributedBody BLOB,
            handle_id INTEGER,
            date INTEGER,
            is_from_me INTEGER,
            cache_roomnames TEXT
        );
        """
    )
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, ?, 'iMessage')",
                (OWNER_STORED,))
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (2, ?, 'iMessage')",
                (STRANGER,))

    before = since - timedelta(hours=1)
    plus1 = since + timedelta(minutes=1)
    plus2 = since + timedelta(minutes=2)
    plus3 = since + timedelta(minutes=3)
    plus4 = since + timedelta(minutes=4)

    rows = [
        # (rowid, text, attributedBody, handle_id, date, is_from_me) — too old, excluded
        (1, "status", None, 1, _apple_ns(before), 0),
        # in order, kept: plain text from the owner
        (2, "status", None, 1, _apple_ns(plus2), 0),
        # NULL text, kept via the attributedBody fallback, sorts after #2
        (3, None, _attributed_body("yes 3"), 1, _apple_ns(plus3), 0),
        # a stranger's text — excluded, not on the allow-list
        (4, "hello", None, 2, _apple_ns(plus1), 0),
        # Quinton's own outgoing reply — excluded, is_from_me
        (5, "on it", None, 1, _apple_ns(plus4), 1),
    ]
    conn.executemany(
        "INSERT INTO message (ROWID, text, attributedBody, handle_id, date, "
        "is_from_me) VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


# --- normalization --------------------------------------------------------------

def test_normalize_handle_reduces_a_phone_to_its_digits():
    assert imessage.normalize_handle(OWNER) == "5551234567"
    assert imessage.normalize_handle(OWNER_STORED) == "5551234567"


def test_normalize_handle_lowercases_an_email():
    assert imessage.normalize_handle("Quinton@Atmospherepress.com") == \
        "quinton@atmospherepress.com"


# --- reading ----------------------------------------------------------------

def test_read_since_keeps_only_allowed_incoming_text_after_the_cutoff(tmp_path):
    since = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "chat.db"
    _build_chat_db(db_path, since)

    found = imessage.read_since(since, db_path=db_path, allowed_handles=[OWNER])

    assert [f.rowid for f in found] == [2, 3]
    assert all(isinstance(f, Inbound) for f in found)


def test_read_since_converts_the_apple_epoch_and_keeps_chronological_order(tmp_path):
    since = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "chat.db"
    _build_chat_db(db_path, since)

    found = imessage.read_since(since, db_path=db_path, allowed_handles=[OWNER])

    assert found[0].at < found[1].at
    assert found[0].at - since == timedelta(minutes=2)
    assert found[0].handle == OWNER_STORED


def test_read_since_falls_back_to_attributedbody_when_text_is_null(tmp_path):
    since = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "chat.db"
    _build_chat_db(db_path, since)

    found = imessage.read_since(since, db_path=db_path, allowed_handles=[OWNER])

    assert found[0].text == "status"
    assert found[1].text == "yes 3"


def test_read_since_a_broken_attributedbody_comes_back_empty_not_raising(tmp_path):
    since = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,
            handle_id INTEGER, date INTEGER, is_from_me INTEGER,
            cache_roomnames TEXT
        );
        """)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, ?, 'iMessage')",
                (OWNER_STORED,))
    conn.execute(
        "INSERT INTO message (ROWID, text, attributedBody, handle_id, date, "
        "is_from_me) VALUES (1, NULL, ?, 1, ?, 0)",
        (b"\x01\x02\x03 no class marker in here \xff\xfe",
         _apple_ns(since + timedelta(minutes=1))))
    conn.commit()
    conn.close()

    found = imessage.read_since(since, db_path=db_path, allowed_handles=[OWNER])

    assert found[0].text == ""


def test_read_since_a_missing_db_is_a_quiet_empty_list(tmp_path):
    assert imessage.read_since(
        datetime.now(timezone.utc), db_path=tmp_path / "nope.db",
        allowed_handles=[OWNER]) == []


# --- sending ------------------------------------------------------------------

class _Result:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def test_send_passes_the_text_as_an_argv_item_never_interpolated():
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _Result(0)

    ok = imessage.send(OWNER, "hi \"friend\" -- it's 'me'", run=fake_run)

    assert ok is True
    (args, kwargs) = calls[0]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    script = args[2]
    assert args[3] == "--"
    assert args[4] == OWNER
    assert args[5] == "hi \"friend\" -- it's 'me'"
    # the script itself never contains the message text
    assert "hi \"friend\"" not in script
    assert "on run argv" in script
    assert kwargs.get("timeout") == 30


def test_send_returns_false_when_osascript_refuses():
    def fake_run(args, **kwargs):
        return _Result(1, stderr="Messages got an error: not signed in")

    assert imessage.send(OWNER, "hello", run=fake_run) is False


def test_send_returns_false_when_run_raises():
    def fake_run(args, **kwargs):
        raise OSError("osascript not found")

    assert imessage.send(OWNER, "hello", run=fake_run) is False


# --- rate limiting --------------------------------------------------------------

class _FakeJournal:
    def __init__(self, sent_this_hour: int):
        self._n = sent_this_hour

    def texts_sent_last_hour(self) -> int:
        return self._n


def test_rate_ok_is_true_under_the_cap():
    assert imessage.rate_ok(_FakeJournal(3), 4) is True


def test_rate_ok_is_false_at_the_cap():
    assert imessage.rate_ok(_FakeJournal(4), 4) is False


def test_send_skips_osascript_when_the_journal_says_the_cap_is_spent():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _Result(0)

    ok = imessage.send(OWNER, "hello", run=fake_run,
                       journal=_FakeJournal(4), max_per_hour=4)

    assert ok is False
    assert calls == []


def test_send_goes_through_when_the_journal_has_room():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _Result(0)

    ok = imessage.send(OWNER, "hello", run=fake_run,
                       journal=_FakeJournal(1), max_per_hour=4)

    assert ok is True
    assert len(calls) == 1
