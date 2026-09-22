"""iMessage, both ways: sending through Messages.app over `osascript`, and
reading the same Mac's `chat.db` for what came back.

There is no send API here — Apple never shipped one — so sending is
"tell Messages.app to do what a person's finger would do", exactly the way
every other iMessage automation on macOS works. The one rule that matters is
that the *text itself* never becomes part of the AppleScript source: it rides
in as an `argv` item (`osascript -e '<script>' -- handle text`), so a message
containing a quote, a backslash, or a stray line asking the script to do
something else stays inert data to `on run argv`, never code.

Reading is a straight SQLite read of the same file Messages.app already
writes, opened `mode=ro&immutable=1` so Warden never blocks on — or is
blocked by — Messages' own writer, and never risks leaving a stray `-wal`
file behind. `chat.db`'s `message.text` is usually enough; the minority of
messages that arrive as rich text (a pasted edit, certain reactions) carry
`NULL` there and the real string sits inside `attributedBody`, an
NSKeyedArchiver blob this module does not fully parse — it only pulls the
first `NSString` run out of it, which is where chat.db keeps the plain text
of an ordinary message even when it travels this way, and gives up cleanly
otherwise.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # pragma: no cover - typing only
    from app.warden.journal import Journal

log = logging.getLogger("docproof.app.warden.imessage")

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# chat.db's `date` column has stored nanoseconds since the Mac (Cocoa) epoch,
# 2001-01-01T00:00:00Z, since Big Sur. Older macOS stored seconds instead; this
# module targets a current Mac, which is the only place Warden runs.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Inbound:
    """One text message somebody else sent, read back out of chat.db."""

    rowid: int
    handle: str
    text: str
    at: datetime


def _apple_ns(at: datetime) -> int:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return int((at - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def _from_apple_ns(ns: int) -> datetime:
    return APPLE_EPOCH + timedelta(seconds=(ns or 0) / 1_000_000_000)


def normalize_handle(raw: str) -> str:
    """A handle in the form it can be compared in: an email lowercased, a
    phone number reduced to its digits with a leading US country code
    dropped — so `owner_handle: "+1 (555) 123-4567"` in warden.yaml matches
    whatever shape chat.db happens to have stored today."""
    raw = (raw or "").strip()
    if "@" in raw:
        return raw.lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _text_from_attributed_body(blob: bytes | None) -> str:
    """Best-effort text out of a `NULL`-text message's `attributedBody`.

    The real archive is NSKeyedArchiver's undocumented `streamtyped` format —
    not worth a full parser for a monitoring agent that only needs the plain
    string. Every message this module cares about (a command, a "yes 3")
    arrives as ordinary text with `message.text` already set; this only runs
    on the rare rich message, and reads it by finding the `NSString` class
    marker, skipping the fixed run of archiver framing bytes that follows it,
    then reading a length-prefixed payload — one byte for a short string, or
    `0x81` plus a two-byte little-endian length for a longer one. Anything
    that doesn't match that shape comes back as `""` rather than raising:
    a message Warden can't read is a message it ignores, not a crash."""
    if not blob:
        return ""
    marker = b"NSString"
    idx = blob.find(marker)
    if idx == -1:
        return ""
    body = blob[idx + len(marker):]
    body = body[5:]                    # fixed archiver framing before the payload
    if not body:
        return ""
    try:
        if body[0] == 0x81:
            length = int.from_bytes(body[1:3], "little")
            start = 3
        else:
            length = body[0]
            start = 1
        return body[start:start + length].decode("utf-8", errors="replace")
    except (IndexError, UnicodeDecodeError):
        return ""


def read_since(since: datetime, *, db_path: str | Path | None = None,
               allowed_handles: list[str]) -> list[Inbound]:
    """Every inbound text since `since`, oldest first, from handles on the
    allow-list — normally just `owner_handle`.

    Opened read-only and `immutable=1`: Messages.app is writing this file
    while Warden reads it, and both flags together tell SQLite it may skip
    the locking dance it would otherwise do against a writer, which is what
    makes this safe to run every couple of minutes without ever blocking on,
    or corrupting, Messages' own database. A file that can't be opened at all
    — Full Disk Access not yet granted for this Python binary — is logged
    with the fix and treated as "nothing new", not raised: a listen tick that
    can't read chat.db should try again next time, not crash the loop."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    allowed = {normalize_handle(h) for h in (allowed_handles or [])}
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as e:
        log.warning("Could not open Messages' chat.db at %s (%s). Grant Full "
                    "Disk Access to the Python binary running Warden, and "
                    "make sure Messages.app has signed in at least once.",
                    path, e)
        return []
    try:
        rows = conn.execute(
            "SELECT message.ROWID, handle.id, message.text, "
            "message.attributedBody, message.date "
            "FROM message JOIN handle ON message.handle_id = handle.ROWID "
            "WHERE message.is_from_me = 0 AND message.date > ? "
            "ORDER BY message.date ASC",
            (_apple_ns(since),)).fetchall()
    except sqlite3.DatabaseError as e:
        log.warning("Could not read Messages' chat.db at %s (%s).", path, e)
        return []
    finally:
        conn.close()

    out: list[Inbound] = []
    for rowid, handle_id, text, attributed_body, date in rows:
        if normalize_handle(handle_id or "") not in allowed:
            continue
        body = text if text is not None else _text_from_attributed_body(
            attributed_body)
        out.append(Inbound(rowid=rowid, handle=handle_id or "",
                            text=body or "", at=_from_apple_ns(date)))
    return out


# `on run argv` — the text and the handle both arrive as arguments, never as
# characters spliced into the script source, so nothing in either one is ever
# interpreted as AppleScript.
_SEND_SCRIPT = """
on run argv
    set targetBuddy to item 1 of argv
    set targetText to item 2 of argv
    tell application "Messages"
        set targetService to id of 1st service whose service type = iMessage
        set targetPerson to buddy targetBuddy of service id targetService
        send targetText to targetPerson
    end tell
end run
"""


def rate_ok(journal: "Journal", max_per_hour: int) -> bool:
    """Whether another text may go out this hour, per the journal's own
    count of what has already gone out — the same ledger `send` below
    consults, so a caller checking ahead of time (before composing a
    message, say) sees the same answer `send` would give."""
    return journal.texts_sent_last_hour() < max_per_hour


def send(handle: str, text: str, *, run=subprocess.run,
         journal: "Journal | None" = None,
         max_per_hour: int | None = None) -> bool:
    """Send one iMessage to `handle` through Messages.app. One text per
    call — there is no `send_many`; a caller wanting to notify several
    people calls this once per handle.

    When `journal` is given along with `max_per_hour`, the send is skipped
    (returning `False` without touching `osascript`) once the journal says
    the hour's cap is already spent — `rate_ok` is consulted only then, so a
    caller that doesn't care about rate limits (or handles it itself) can
    leave both out and this behaves exactly like a bare `osascript` wrapper."""
    if journal is not None and max_per_hour is not None:
        if not rate_ok(journal, max_per_hour):
            log.warning("Not texting %s: %d already sent this hour (cap %d).",
                        handle, journal.texts_sent_last_hour(), max_per_hour)
            return False
    try:
        result = run(["osascript", "-e", _SEND_SCRIPT, "--", handle, text],
                     capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Could not run osascript to text %s: %s", handle, e)
        return False
    if getattr(result, "returncode", 1) != 0:
        log.warning("osascript refused to text %s: %s", handle,
                    (getattr(result, "stderr", "") or "").strip())
        return False
    return True


__all__ = ["Inbound", "normalize_handle", "rate_ok", "read_since", "send",
           "DEFAULT_DB_PATH"]
