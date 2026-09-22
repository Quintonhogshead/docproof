"""Warden's Gmail calls, against a fake opener that never leaves this
process — canned answers for the OAuth token refresh, `messages.list`,
`messages.get` and `messages.send`, with every request it received kept so
a test can inspect the query it built, which refresh token it used, and
what actually went out."""
from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

from app.warden.messaging import gmail

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _headers_for(msg: dict) -> list[dict]:
    out = []
    for name, key in (("From", "from"), ("Subject", "subject"),
                      ("Date", "date"), ("In-Reply-To", "in_reply_to")):
        if msg.get(key):
            out.append({"name": name, "value": msg[key]})
    return out


def fake_gmail(*, messages: dict[str, dict] | None = None,
              access_token: str = "at-1", token_key: str = ""):
    """`messages` maps a message id to its headers/snippet. `token_key`, when
    set, is stamped into the access token so a test can tell which refresh
    token (notify vs. inbox) a call actually used."""
    messages = messages or {}
    calls: list = []
    sent: list[dict] = []

    def opener(request, timeout=60):
        calls.append(request)
        url = request.full_url
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)

        if "oauth2.googleapis.com" in url:
            body = urllib.parse.parse_qs(request.data.decode())
            refresh = body.get("refresh_token", [""])[0]
            token = f"{access_token}:{refresh}" if token_key else access_token
            return _Response(json.dumps(
                {"access_token": token, "expires_in": 3599}).encode())

        assert "gmail.googleapis.com" in url
        if request.get_method() == "POST" and parsed.path.endswith("/send"):
            payload = json.loads(request.data)
            raw = base64.urlsafe_b64decode(payload["raw"])
            sent.append({"msg": message_from_bytes(raw)})
            return _Response(json.dumps({"id": "sent-1"}).encode())

        if parsed.path == "/gmail/v1/users/me/messages":
            ids = list(messages.keys())
            return _Response(json.dumps(
                {"messages": [{"id": mid} for mid in ids]}).encode())

        # messages/<id> — a metadata read
        mid = parsed.path.rsplit("/", 1)[-1]
        msg = messages.get(mid, {})
        return _Response(json.dumps({
            "snippet": msg.get("snippet", ""),
            "payload": {"headers": _headers_for(msg)},
        }).encode())

    opener.calls = calls
    opener.sent = sent
    return opener


class FakeSecrets:
    def __init__(self, **values):
        self.values = {
            "google_client_id": "client-1",
            "google_client_secret": "secret-1",
            "google_notify_refresh": "notify-refresh",
            "google_inbox_refresh": "inbox-refresh",
            **values,
        }

    def get(self, name):
        return self.values.get(name)


class FakeConfig:
    def __init__(self, **kw):
        self.team_emails = kw.get("team_emails", [])
        self.inbox_label = kw.get("inbox_label", "DocProof")
        self.inbox_senders = kw.get("inbox_senders", [])
        self.notify_email = kw.get("notify_email", "docwatch@atmospherepress.com")
        self.extra = kw.get("extra", {})


def _list_query(opener) -> str:
    for request in opener.calls:
        parsed = urllib.parse.urlparse(request.full_url)
        if parsed.path == "/gmail/v1/users/me/messages":
            return urllib.parse.parse_qs(parsed.query)["q"][0]
    raise AssertionError("no list call was made")


# --- recent -----------------------------------------------------------------

def test_recent_lists_then_reads_metadata_for_each_message():
    opener = fake_gmail(messages={
        "m1": {"from": "a@example.com", "subject": "Hi", "date": "Mon",
              "snippet": "hello there"},
        "m2": {"from": "b@example.com", "subject": "Re: Hi", "date": "Tue",
              "snippet": "reply", "in_reply_to": "<m1@example.com>"},
    })

    found = gmail.recent("at-1", query="label:DocProof", max_results=10,
                         opener=opener)

    assert [m["id"] for m in found] == ["m1", "m2"]
    assert found[0]["from"] == "a@example.com"
    assert found[0]["snippet"] == "hello there"
    assert found[1]["in_reply_to"] == "<m1@example.com>"
    # the list call carried the query and cap
    list_calls = [r for r in opener.calls
                 if urllib.parse.urlparse(r.full_url).path ==
                 "/gmail/v1/users/me/messages"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(list_calls[0].full_url).query)
    assert query["q"] == ["label:DocProof"]
    assert query["maxResults"] == ["10"]


def test_recent_asks_for_metadata_format_and_the_four_headers():
    opener = fake_gmail(messages={"m1": {"from": "a@example.com"}})

    gmail.recent("at-1", query="x", opener=opener)

    meta_calls = [r for r in opener.calls if "/messages/m1" in r.full_url]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(meta_calls[0].full_url).query)
    assert query["format"] == ["metadata"]
    assert set(query["metadataHeaders"]) == {"From", "Subject", "Date",
                                              "In-Reply-To"}


# --- inbox --------------------------------------------------------------------

def test_inbox_query_combines_label_and_senders_with_newer_than():
    cfg = FakeConfig(inbox_label="DocProof",
                     inbox_senders=["author@example.com", "hubspot.com"])
    opener = fake_gmail(messages={})
    since = datetime.now(timezone.utc) - timedelta(hours=20)

    gmail.inbox(cfg, FakeSecrets(), since=since, opener=opener)

    query = _list_query(opener)
    assert query.startswith("(label:DocProof OR from:(author@example.com OR "
                            "hubspot.com))")
    assert "newer_than:1d" in query


def test_newer_than_rounds_elapsed_time_up_to_whole_days():
    """`_newer_than` alone, against a fixed `now`, so this isn't at the mercy
    of how long the test happens to take to run."""
    assert gmail._newer_than(NOW - timedelta(hours=1), now=NOW) == "newer_than:1d"
    assert gmail._newer_than(NOW - timedelta(hours=50), now=NOW) == "newer_than:3d"
    assert gmail._newer_than(NOW - timedelta(hours=48), now=NOW) == "newer_than:2d"


def test_inbox_uses_the_inbox_refresh_token_not_the_notify_one():
    cfg = FakeConfig()
    opener = fake_gmail(messages={}, token_key="x")

    gmail.inbox(cfg, FakeSecrets(), since=NOW - timedelta(hours=1), opener=opener)

    token_call = next(r for r in opener.calls
                      if "oauth2.googleapis.com" in r.full_url)
    body = urllib.parse.parse_qs(token_call.data.decode())
    assert body["refresh_token"] == ["inbox-refresh"]


def test_inbox_returns_plain_dicts_without_in_reply_to():
    cfg = FakeConfig()
    opener = fake_gmail(messages={
        "m1": {"from": "a@example.com", "subject": "Hi", "date": "Mon",
              "snippet": "hello"},
    })

    found = gmail.inbox(cfg, FakeSecrets(), since=NOW - timedelta(hours=1),
                        opener=opener)

    assert found == [{"id": "m1", "from": "a@example.com", "subject": "Hi",
                      "date": "Mon", "snippet": "hello"}]


# --- replies --------------------------------------------------------------------

def test_replies_query_tags_the_subject_and_excludes_self():
    cfg = FakeConfig()
    opener = fake_gmail(messages={})

    gmail.replies(cfg, FakeSecrets(), since=NOW - timedelta(hours=1), opener=opener)

    query = _list_query(opener)
    assert query.startswith("subject:[Warden]")
    assert "-from:me" in query
    assert "newer_than:1d" in query


def test_replies_uses_the_notify_refresh_token():
    cfg = FakeConfig()
    opener = fake_gmail(messages={}, token_key="x")

    gmail.replies(cfg, FakeSecrets(), since=NOW - timedelta(hours=1), opener=opener)

    token_call = next(r for r in opener.calls
                      if "oauth2.googleapis.com" in r.full_url)
    body = urllib.parse.parse_qs(token_call.data.decode())
    assert body["refresh_token"] == ["notify-refresh"]


# --- send_team --------------------------------------------------------------

def test_send_team_tags_the_subject_and_sets_reply_to():
    cfg = FakeConfig(team_emails=["a@example.com", "b@example.com"],
                     notify_email="docwatch@atmospherepress.com")
    opener = fake_gmail()

    sent_to = gmail.send_team(cfg, FakeSecrets(), "3 findings", "body text",
                              opener=opener)

    assert sent_to == ["a@example.com", "b@example.com"]
    assert len(opener.sent) == 2
    for entry in opener.sent:
        msg = entry["msg"]
        assert msg["Subject"] == "[Warden] 3 findings"
        assert msg["Reply-To"] == "docwatch@atmospherepress.com"
    assert {e["msg"]["To"] for e in opener.sent} == {"a@example.com",
                                                      "b@example.com"}


def test_send_team_reads_notify_email_from_extra_when_not_set_directly():
    cfg = FakeConfig(team_emails=["a@example.com"], notify_email="")
    cfg.extra = {"notify_email": "fallback@atmospherepress.com"}
    opener = fake_gmail()

    gmail.send_team(cfg, FakeSecrets(), "hi", "body", opener=opener)

    assert opener.sent[0]["msg"]["Reply-To"] == "fallback@atmospherepress.com"


def test_send_team_does_not_double_tag_an_already_tagged_subject():
    cfg = FakeConfig(team_emails=["a@example.com"])
    opener = fake_gmail()

    gmail.send_team(cfg, FakeSecrets(), "[Warden] already tagged", "body",
                    opener=opener)

    assert opener.sent[0]["msg"]["Subject"] == "[Warden] already tagged"


def test_send_team_uses_the_notify_refresh_token_not_inbox():
    cfg = FakeConfig(team_emails=["a@example.com"])
    opener = fake_gmail(token_key="x")

    gmail.send_team(cfg, FakeSecrets(), "hi", "body", opener=opener)

    token_call = next(r for r in opener.calls
                      if "oauth2.googleapis.com" in r.full_url)
    body = urllib.parse.parse_qs(token_call.data.decode())
    assert body["refresh_token"] == ["notify-refresh"]


def test_send_team_sends_nobody_when_the_team_list_is_empty():
    cfg = FakeConfig(team_emails=[])
    opener = fake_gmail()

    sent_to = gmail.send_team(cfg, FakeSecrets(), "hi", "body", opener=opener)

    assert sent_to == []
    assert opener.sent == []
