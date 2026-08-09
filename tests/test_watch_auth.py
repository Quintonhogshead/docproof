"""Signing in to Google, without signing in to Google.

The flow is three decisions and some plumbing. The decisions — what the
consent page is asked for, whether the answer that came back belongs to this
run, and what is worth keeping out of the reply — are pure or take an injected
opener, so they are all tested here. The plumbing takes an injected listener,
which is what lets the whole flow run end to end without a socket.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.watch import auth
from app.watch.auth import AuthError

from .fakes import fake_drive, http_error

CLIENT = ("client-1.apps.googleusercontent.com", "secret-1")


def query_of(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class FakeLoopback:
    """A listener that answers with whatever the consent page was asked for.

    It reads the state out of the URL the browser was sent to, which is how a
    test gets a matching answer without knowing the random value up front —
    and how a mismatched one is arranged on purpose."""

    redirect_uri = "http://127.0.0.1:54321"

    def __init__(self, *, code: str = "one-time-code", answer: str | None = None,
                 state: str | None = None):
        self.code = code
        self.answer = answer
        self.state = state
        self.opened: list[str] = []
        self.closed = False

    # the flow calls listen() to get one of these; being its own factory keeps
    # the test's wiring to one object
    def __call__(self) -> "FakeLoopback":
        return self

    def open_browser(self, url: str) -> bool:
        self.opened.append(url)
        return True

    def __enter__(self) -> "FakeLoopback":
        return self

    def __exit__(self, *exc) -> bool:
        self.closed = True
        return False

    def wait(self, timeout: float) -> str:
        if self.answer is not None:
            return self.answer
        state = self.state or query_of(self.opened[-1])["state"][0]
        return f"/?state={state}&code={self.code}"


def token_reply(payload: dict):
    """An opener that answers the token endpoint with exactly this."""
    import json

    class Response:
        def read(self): return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout=60):
        opener.calls.append(request)
        return Response()

    opener.calls = []
    return opener


# --- what the consent page is asked for ---------------------------------------

def test_the_consent_page_asks_for_offline_access_and_a_fresh_approval():
    """`access_type=offline` is what asks for a refresh token at all, and
    `prompt=consent` is what makes Google send a new one to somebody who has
    approved this before — without it a second sign-in succeeds and hands back
    nothing worth keeping."""
    query = query_of(auth.consent_url("id-1", "http://127.0.0.1:9/", "st-1"))

    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["id-1"]
    assert query["state"] == ["st-1"]


def test_the_scope_is_the_whole_of_drive_plus_send_only_gmail():
    """`drive.file` grants an application its own files — the ones it created
    or a person explicitly opened with it. Every manuscript this watcher exists
    to find was put there by somebody else. `gmail.send` is added for the alert
    email: send-only, so it cannot read a message."""
    scopes = auth.SCOPE.split()
    assert "https://www.googleapis.com/auth/drive" in scopes
    assert "https://www.googleapis.com/auth/gmail.send" in scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" not in scopes
    assert query_of(auth.consent_url("id-1", "http://x/", "s"))["scope"] == \
        [auth.SCOPE]


# --- whether the answer belongs to this run -----------------------------------

def test_the_code_is_read_out_of_the_address_google_sent():
    assert auth.parse_redirect("/?state=st-1&code=abc123", "st-1") == "abc123"


def test_an_answer_carrying_somebody_elses_state_is_ignored():
    """The listener answers anything that reaches that port for those few
    seconds. A code arriving without this run's state is a code from
    somewhere else."""
    with pytest.raises(AuthError, match="did not match"):
        auth.parse_redirect("/?state=elsewhere&code=abc123", "st-1")


def test_an_answer_with_no_state_at_all_is_ignored():
    with pytest.raises(AuthError, match="did not match"):
        auth.parse_redirect("/?code=abc123", "st-1")


def test_declining_in_the_browser_says_so_plainly():
    with pytest.raises(AuthError, match="declined"):
        auth.parse_redirect("/?state=st-1&error=access_denied", "st-1")


def test_any_other_refusal_from_google_is_repeated_back():
    with pytest.raises(AuthError, match="invalid_scope"):
        auth.parse_redirect("/?state=st-1&error=invalid_scope", "st-1")


def test_an_answer_with_no_code_is_not_a_sign_in():
    with pytest.raises(AuthError, match="no sign-in code"):
        auth.parse_redirect("/?state=st-1", "st-1")


# --- what is worth keeping ----------------------------------------------------

def test_the_code_is_traded_for_a_refresh_token():
    opener = token_reply({"refresh_token": "refresh-1",
                          "access_token": "at-1"})

    token = auth.exchange_code(*CLIENT, "one-time-code",
                               "http://127.0.0.1:54321", opener=opener)

    assert token == "refresh-1"
    sent = urllib.parse.parse_qs(opener.calls[0].data.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["one-time-code"]
    assert sent["redirect_uri"] == ["http://127.0.0.1:54321"]


def test_a_sign_in_that_returns_no_refresh_token_is_a_failure_not_a_save():
    """An access token dies within the hour. Storing one would leave a watcher
    that works this afternoon and not tomorrow."""
    opener = token_reply({"access_token": "at-1"})

    with pytest.raises(AuthError, match="myaccount.google.com/permissions"):
        auth.exchange_code(*CLIENT, "code", "http://127.0.0.1:1", opener=opener)


def test_a_client_google_does_not_recognise_says_so():
    opener = fake_drive(fail={"token": http_error(401, "invalid_client")})

    with pytest.raises(auth.AuthExpired):
        auth.exchange_code(*CLIENT, "code", "http://127.0.0.1:1", opener=opener)


# --- the whole flow -----------------------------------------------------------

def test_a_sign_in_opens_the_page_and_hands_back_the_token():
    loopback = FakeLoopback()
    opener = token_reply({"refresh_token": "refresh-1"})

    token = auth.run_flow(*CLIENT, open_browser=loopback.open_browser,
                          opener=opener, listen=loopback)

    assert token == "refresh-1"
    assert loopback.opened[0].startswith(auth.AUTH_URL)
    assert loopback.closed


def test_the_redirect_google_is_given_is_the_port_that_was_actually_opened():
    """Ephemeral, so nothing has to be reserved and two sign-ins cannot
    collide. Google must be told the one this run got."""
    loopback = FakeLoopback()
    opener = token_reply({"refresh_token": "refresh-1"})

    auth.run_flow(*CLIENT, open_browser=loopback.open_browser, opener=opener,
                  listen=loopback)

    assert query_of(loopback.opened[0])["redirect_uri"] == \
        ["http://127.0.0.1:54321"]
    assert urllib.parse.parse_qs(opener.calls[0].data.decode())[
        "redirect_uri"] == ["http://127.0.0.1:54321"]


def test_every_sign_in_invents_its_own_state():
    opener = token_reply({"refresh_token": "refresh-1"})
    states = []
    for _ in range(2):
        loopback = FakeLoopback()
        auth.run_flow(*CLIENT, open_browser=loopback.open_browser,
                      opener=opener, listen=loopback)
        states.append(query_of(loopback.opened[0])["state"][0])

    assert states[0] != states[1]


def test_an_answer_that_does_not_match_stops_the_flow_before_the_exchange():
    loopback = FakeLoopback(answer="/?state=elsewhere&code=abc")
    opener = token_reply({"refresh_token": "refresh-1"})

    with pytest.raises(AuthError, match="did not match"):
        auth.run_flow(*CLIENT, open_browser=loopback.open_browser,
                      opener=opener, listen=loopback)

    assert opener.calls == []          # nothing was traded


def test_signing_in_with_no_client_says_where_to_get_one():
    with pytest.raises(AuthError, match="docs/watch.md"):
        auth.run_flow("", "", listen=FakeLoopback())


def test_the_listener_is_closed_even_when_the_answer_is_refused():
    loopback = FakeLoopback(answer="/?error=access_denied&state=x")

    with pytest.raises(AuthError):
        auth.run_flow(*CLIENT, open_browser=loopback.open_browser,
                      listen=loopback)

    assert loopback.closed


# --- the listener itself ------------------------------------------------------
#
# The only part a fake cannot stand in for: whether a browser arriving at that
# port is heard. These use a real socket on 127.0.0.1, which is not the network
# — nothing leaves the machine — for the same reason test_lock.py uses a real
# second process for a kernel lock.

def fetch(url: str) -> int:
    """One request from something standing in for a browser."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_listener_hears_the_browser_and_reports_where_it_went():
    with auth.Loopback() as loopback:
        assert loopback.redirect_uri.startswith("http://127.0.0.1:")
        answers = []
        browser = threading.Thread(
            target=lambda: answers.append(
                fetch(loopback.redirect_uri + "/?state=st-1&code=abc123")))
        browser.start()

        path = loopback.wait(timeout=10)

        browser.join(timeout=10)

    assert auth.parse_redirect(path, "st-1") == "abc123"
    assert answers == [200]


def test_a_browser_asking_for_a_favicon_is_not_the_answer():
    """Given half a chance a browser asks for /favicon.ico. Taking that as the
    reply would end the sign-in before it started."""
    with auth.Loopback() as loopback:
        answers = []

        def browser():
            answers.append(fetch(loopback.redirect_uri + "/favicon.ico"))
            answers.append(fetch(loopback.redirect_uri + "/?state=s&code=c"))

        thread = threading.Thread(target=browser)
        thread.start()

        path = loopback.wait(timeout=10)

        thread.join(timeout=10)

    assert answers == [404, 200]
    assert auth.parse_redirect(path, "s") == "c"


def test_a_sign_in_nobody_finishes_is_given_up_on():
    with auth.Loopback() as loopback:
        with pytest.raises(AuthError, match="given up on"):
            loopback.wait(timeout=0)


# --- reporting it -------------------------------------------------------------

def test_the_status_says_where_a_sign_in_came_from_and_never_what_it_is(
        monkeypatch):
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)

    stored = auth.token_source(lambda name: "refresh-1", has_client=True)
    assert stored == {"configured": True, "source": "keychain", "client": True}

    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "refresh-2")
    from_env = auth.token_source(lambda name: "refresh-2", has_client=True)
    assert from_env["source"] == "environment"


def test_no_sign_in_is_reported_as_no_sign_in(monkeypatch):
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)

    assert auth.token_source(lambda name: None, has_client=False) == {
        "configured": False, "source": None, "client": False}
