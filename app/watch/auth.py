"""Signing in to Google, once, from a browser on this Mac.

The installed-application flow: DocProof opens a consent page, Google sends
the answer back to a web server that exists for those few seconds on
127.0.0.1, and what comes out is a refresh token that goes straight to the
Keychain. Nothing is typed into DocProof, which is the point — a password
manager's page is the only place a Google password belongs.

The decisions live in `consent_url`, `parse_redirect` and `exchange_code`, all
of which are pure or take an injected opener. `run_flow` is the plumbing that
holds them together, and the listener it needs is injected too, so the whole
flow can be exercised without a socket.
"""
from __future__ import annotations

import http.server
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from app.settings import ENV_VARS, set_api_key

from . import drive
from .drive import AuthExpired, DriveError
from .settings import GOOGLE_KEY, WatchSettings

log = logging.getLogger("docproof.app.watch.auth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# The whole of Drive, not `drive.file`. `drive.file` grants an application its
# own files only — the ones it created or a person explicitly opened with it —
# and every manuscript this watcher exists to find was put there by somebody
# else. There is no narrower scope that can see an author's upload.
SCOPE = "https://www.googleapis.com/auth/drive"

DEFAULT_TIMEOUT = 300

DONE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>DocProof</title></head>
<body style="font: 16px/1.5 -apple-system, sans-serif; margin: 4rem auto;
             max-width: 30rem; color: #222">
<h1 style="font-size: 1.3rem">DocProof is signed in.</h1>
<p>You can close this tab and go back to the terminal.</p>
</body></html>"""


class AuthError(DriveError):
    """The sign-in did not finish. Says what to do about it."""


# --- the pure parts -----------------------------------------------------------

def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The page a person approves.

    `access_type=offline` is what asks for a refresh token at all, and
    `prompt=consent` is what makes Google send a new one even to somebody who
    has approved this before — without it, signing in a second time succeeds
    and hands back nothing worth keeping."""
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def parse_redirect(path: str, expected_state: str) -> str:
    """The one-time code out of the address Google sent the browser to.

    The state check is not ceremony: the listener answers anything that
    reaches that port for those few seconds, and a code arriving without the
    state this run invented is a code from somewhere else."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    error = (query.get("error") or [""])[0]
    if error == "access_denied":
        raise AuthError("The sign-in was declined in the browser. Run "
                        "`docproof-watch auth` again to try once more.")
    if error:
        raise AuthError(f"Google refused the sign-in: {error}.")
    if (query.get("state") or [""])[0] != expected_state:
        raise AuthError("The answer from Google did not match the request "
                        "this run made, so it was ignored. Run "
                        "`docproof-watch auth` again.")
    code = (query.get("code") or [""])[0]
    if not code:
        raise AuthError("Google's answer carried no sign-in code. Run "
                        "`docproof-watch auth` again.")
    return code


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, *, opener=drive._open_url) -> str:
    """The one-time code for the refresh token worth keeping."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    request = urllib.request.Request(
        drive.TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    answer = drive._json_call(request, opener=opener,
                              what="finish signing in")
    token = str(answer.get("refresh_token", ""))
    if not token:
        # Google withholds it when an earlier approval is still on record and
        # prompt=consent was somehow not honoured. Nothing to store, so say so
        # rather than saving an access token that dies within the hour.
        raise AuthError(
            "Google completed the sign-in but sent no refresh token, so there "
            "is nothing to save. Remove DocProof at "
            "https://myaccount.google.com/permissions and run "
            "`docproof-watch auth` again.")
    return token


# --- the listener -------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:                            # noqa: N802 - stdlib
        query = urllib.parse.urlparse(self.path).query
        if "code=" not in query and "error=" not in query:
            # A browser asks for /favicon.ico given half a chance. Not the
            # answer this is waiting for.
            self.send_error(404)
            return
        self.server.result = self.path                   # type: ignore[attr-defined]
        body = DONE_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """The stdlib's default is to print every request to stderr, which in
        a terminal running `auth` looks like something went wrong."""


class Loopback:
    """A web server that exists for one answer, on a port nobody chose.

    127.0.0.1 with an ephemeral port is the redirect Google documents for
    installed applications, and it is why no client secret here has to be
    kept: the answer never leaves the machine."""

    def __init__(self):
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.result = None                       # type: ignore[attr-defined]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def wait(self, timeout: float = DEFAULT_TIMEOUT) -> str:
        self._server.timeout = 1
        deadline = time.monotonic() + timeout
        while self._server.result is None:               # type: ignore[attr-defined]
            if time.monotonic() >= deadline:
                raise AuthError(
                    "Nothing came back from the browser, so the sign-in was "
                    "given up on. Run `docproof-watch auth` to try again.")
            self._server.handle_request()
        return self._server.result                       # type: ignore[attr-defined]

    def __enter__(self) -> "Loopback":
        return self

    def __exit__(self, *exc) -> bool:
        self._server.server_close()
        return False


# --- the flow -----------------------------------------------------------------

def run_flow(client_id: str, client_secret: str, *,
             open_browser=webbrowser.open, opener=drive._open_url,
             listen=Loopback, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Sign in, and hand back the refresh token to store.

    `open_browser` is one seam rather than two: the CLI passes something that
    prints the address before opening it, because a browser that refuses to
    open on a Mac somebody is ssh'd into should not be the end of the road."""
    if not client_id or not client_secret:
        raise AuthError("There is no Google OAuth client to sign in with. Run "
                        "`docproof-watch auth` and paste the client id and "
                        "secret from the Google Cloud console — docs/watch.md "
                        "walks through making one.")
    state = secrets.token_urlsafe(24)
    with listen() as loopback:
        redirect_uri = loopback.redirect_uri
        open_browser(consent_url(client_id, redirect_uri, state))
        answer = loopback.wait(timeout)
    code = parse_redirect(answer, state)
    return exchange_code(client_id, client_secret, code, redirect_uri,
                         opener=opener)


def sign_in(home: str | Path, client_id: str, client_secret: str, *,
            open_browser=webbrowser.open, opener=drive._open_url,
            listen=Loopback, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Sign in, and keep it: the token to the Keychain, the client beside the
    rest of the watcher's settings.

    The order is the point, and is why this is one function rather than three
    lines repeated at each front door. Nothing is written until Google has
    actually answered — a half-saved sign-in, with a client recorded and no
    token, is a watcher that looks configured and fails every night."""
    token = run_flow(client_id, client_secret, open_browser=open_browser,
                     opener=opener, listen=listen, timeout=timeout)
    set_api_key(GOOGLE_KEY, token)
    ws = WatchSettings.load(home)
    ws.client_id, ws.client_secret = client_id, client_secret
    ws.save(home)


def web_consent(client_id: str, redirect_uri: str) -> tuple[str, str]:
    """Start a sign-in that finishes in the browser, not on a loopback.

    The hosted build cannot open a browser on the server or run a listener a
    person can reach, so there is no `run_flow` here: Google sends its answer
    back to a route on this same server (`redirect_uri`), and this only has to
    hand back the freshly-minted state to remember and the consent page to send
    the browser to. The state is what the callback checks the answer against."""
    state = secrets.token_urlsafe(24)
    return state, consent_url(client_id, redirect_uri, state)


def token_source(get_key, has_client: bool) -> dict:
    """What `auth --status` reports: whether there is a sign-in and where it
    came from, never the token itself."""
    env = os.environ.get(ENV_VARS["google"])
    stored = get_key("google")
    return {
        "configured": bool(stored),
        "source": "environment" if env else ("keychain" if stored else None),
        "client": has_client,
    }


__all__ = ["AUTH_URL", "SCOPE", "AuthError", "AuthExpired", "Loopback",
           "consent_url", "exchange_code", "parse_redirect", "run_flow",
           "sign_in", "token_source", "web_consent"]
