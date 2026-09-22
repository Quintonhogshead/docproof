"""Warden's mail, over the same Gmail REST calls DocWatch already uses.

No new HTTP plumbing: `app.watch.drive.refresh_access_token` trades a saved
refresh token for an access token, and `app.watch.notify.send` builds and
posts the MIME message, exactly as they do for a completion email. What is
new here is *which* refresh token gets used for what, because Warden reads
with a different Google identity than it sends with:

* `google_notify_refresh` is DocWatch's own mailbox — it already carries
  `gmail.send`, so team alerts and (`replies`) its own sent thread's answers
  go out and come back through it.
* `google_inbox_refresh` is Quinton's personal inbox, signed in read-only —
  it can never send, only list and read, which is exactly the scope a
  monitoring agent should hold for somebody else's mail.

`cfg` and `secrets` are duck-typed on purpose (see `app.warden.config` and
`app.warden.secrets`, built alongside this module): this file only ever reads
attributes off them, so it neither has to import those modules at run time
nor wait on them to exist to be tested.
"""
from __future__ import annotations

import math
import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.watch import drive, notify

if TYPE_CHECKING:                      # pragma: no cover - typing only
    from app.warden.config import WardenConfig
    from app.warden.secrets import Secrets

MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

# The headers a `metadata`-format read asks for — everything the snapshot
# and the reply parser need, and nothing of the body, which Warden never
# reads: a monitoring agent has no business seeing the content of Quinton's
# mail beyond what a subject line and a snippet already show.
METADATA_HEADERS = ("From", "Subject", "Date", "In-Reply-To")

TAG = "[Warden]"


def _get(secrets: "Secrets", name: str) -> str:
    return secrets.get(name) or ""


def _client_creds(secrets: "Secrets") -> tuple[str, str]:
    return _get(secrets, "google_client_id"), _get(secrets, "google_client_secret")


def _notify_token(cfg: "WardenConfig", secrets: "Secrets", *, opener) -> str:
    client_id, client_secret = _client_creds(secrets)
    refresh = _get(secrets, "google_notify_refresh")
    return drive.refresh_access_token(client_id, client_secret, refresh,
                                      opener=opener)


def _inbox_token(cfg: "WardenConfig", secrets: "Secrets", *, opener) -> str:
    client_id, client_secret = _client_creds(secrets)
    refresh = _get(secrets, "google_inbox_refresh")
    return drive.refresh_access_token(client_id, client_secret, refresh,
                                      opener=opener)


def _notify_email(cfg: "WardenConfig") -> str:
    """The DocWatch notify address — read straight off the config when it is
    there, else out of the free-form `extra` bag config.py round-trips
    unknown keys through (the docwatch snapshot's own `notify_email`, kept
    alongside Warden's other settings rather than duplicated into the
    schema)."""
    direct = getattr(cfg, "notify_email", "") or ""
    if direct:
        return direct
    extra = getattr(cfg, "extra", None) or {}
    return str(extra.get("notify_email", "") or "")


def _newer_than(since: datetime, *, now: datetime | None = None) -> str:
    """Gmail's `newer_than` only counts whole days, so `since` is rounded up
    to the nearest one that still covers it — a `since` twenty minutes ago
    becomes `newer_than:1d`, never `newer_than:0d`, which Gmail treats as
    "everything"."""
    now = now or datetime.now(timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = (now - since).total_seconds()
    days = max(1, math.ceil(elapsed / 86400)) if elapsed > 0 else 1
    return f"newer_than:{days}d"


def recent(token: str, *, query: str, max_results: int = 50,
          opener) -> list[dict]:
    """Every message matching `query`, newest-listed-first as Gmail returns
    them, each expanded to the handful of headers Warden ever looks at.

    Two calls per message is how Gmail's API works — `messages.list` returns
    only ids, `messages.get` is what carries headers — so this is the one
    place that cost is paid; `inbox` and `replies` are both just a query
    string in front of it."""
    list_url = f"{MESSAGES_URL}?{urllib.parse.urlencode({'q': query, 'maxResults': str(max_results)})}"
    listing = drive._json_call(
        drive._request(list_url, token), opener=opener, what="list mail")
    out: list[dict] = []
    for item in listing.get("messages") or []:
        mid = item.get("id") if isinstance(item, dict) else None
        if not mid:
            continue
        params = [("format", "metadata")] + [
            ("metadataHeaders", h) for h in METADATA_HEADERS]
        meta_url = f"{MESSAGES_URL}/{mid}?{urllib.parse.urlencode(params)}"
        meta = drive._json_call(
            drive._request(meta_url, token), opener=opener,
            what="read a message")
        payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
        headers: dict[str, str] = {}
        for h in payload.get("headers") or []:
            if isinstance(h, dict) and h.get("name"):
                headers[str(h["name"])] = str(h.get("value", ""))
        out.append({
            "id": mid,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": str(meta.get("snippet", "")),
            "in_reply_to": headers.get("In-Reply-To", ""),
        })
    return out


def _inbox_query(cfg: "WardenConfig", since: datetime) -> str:
    label = getattr(cfg, "inbox_label", "") or ""
    senders = [s for s in (getattr(cfg, "inbox_senders", None) or []) if s]
    clauses = []
    if label:
        clauses.append(f"label:{label}")
    if senders:
        clauses.append("from:(" + " OR ".join(senders) + ")")
    scope = f"({' OR '.join(clauses)}) " if clauses else ""
    return f"{scope}{_newer_than(since)}"


def inbox(cfg: "WardenConfig", secrets: "Secrets", *, since: datetime,
          opener) -> list[dict]:
    """Quinton's own inbox, scoped to `inbox_label` or a message from one of
    `inbox_senders` — the two ways a HubSpot notice or an author reply can
    reach him — read since `since`. Read-only: `google_inbox_refresh` cannot
    send, so a bug here can never turn into mail going out under his name."""
    token = _inbox_token(cfg, secrets, opener=opener)
    results = recent(token, query=_inbox_query(cfg, since), max_results=50,
                     opener=opener)
    return [{"id": r["id"], "from": r["from"], "subject": r["subject"],
             "date": r["date"], "snippet": r["snippet"]} for r in results]


def replies(cfg: "WardenConfig", secrets: "Secrets", *, since: datetime,
           opener) -> list[dict]:
    """Answers to Warden's own `[Warden]`-tagged mail, read back out of the
    notify mailbox that sent them — `-from:me` so an alert never shows up
    counted as its own reply."""
    token = _notify_token(cfg, secrets, opener=opener)
    query = f"subject:{TAG} {_newer_than(since)} -from:me"
    return recent(token, query=query, max_results=50, opener=opener)


def send_team(cfg: "WardenConfig", secrets: "Secrets", subject: str,
             body: str, *, opener) -> list[str]:
    """One email, tagged `[Warden]`, to every address in `team_emails` —
    sent from the notify mailbox (the only Google identity Warden holds that
    can send at all) with its own address set as Reply-To, so a reply lands
    back on the mailbox `replies` reads, whatever "send as" alias Gmail
    happened to use. Returns the addresses it actually sent to."""
    token = _notify_token(cfg, secrets, opener=opener)
    reply_to = _notify_email(cfg) or None
    tagged = f"{TAG} {subject}" if not subject.startswith(TAG) else subject
    sent: list[str] = []
    for addr in (getattr(cfg, "team_emails", None) or []):
        if not addr:
            continue
        notify.send(token, addr, tagged, body, reply_to=reply_to,
                    opener=opener)
        sent.append(addr)
    return sent


__all__ = ["MESSAGES_URL", "recent", "inbox", "replies", "send_team"]
