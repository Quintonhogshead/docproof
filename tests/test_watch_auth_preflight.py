"""`preflight_consent`: reading Google's refusal off where the consent page
lands, without a browser. Google answers an unauthenticated GET of the consent
URL the same way it answers a person: a client whose settings do not allow the
redirect lands on `/signin/oauth/error?authError=<base64 protobuf>` whose first
field is the error code in plain text. Verified against the live page on
2026-09-21 with a Desktop client and an https redirect."""
from __future__ import annotations

import urllib.error

from app.watch import auth as authlib

MISMATCH = "ChVyZWRpcmVjdF91cmlfbWlzbWF0Y2ggkAMyAggB"   # a real one
CONSENT = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"


class Landed:
    def __init__(self, url):
        self.url = url

    def geturl(self):
        return self.url


def opener_landing(url):
    def opener(request, timeout=0):
        return Landed(url)
    return opener


def test_a_redirect_google_will_not_take_is_named():
    landed = ("https://accounts.google.com/signin/oauth/error?authError="
              + MISMATCH + "&flowName=GeneralOAuthFlow")
    assert authlib.preflight_consent(
        CONSENT, opener=opener_landing(landed)) == "redirect_uri_mismatch"


def test_a_request_google_takes_is_not_refused():
    landed = "https://accounts.google.com/v3/signin/identifier?continue=x"
    assert authlib.preflight_consent(CONSENT, opener=opener_landing(landed)) is None


def test_an_error_page_with_no_readable_reason_still_counts():
    landed = "https://accounts.google.com/signin/oauth/error?authError=!!"
    assert authlib.preflight_consent(
        CONSENT, opener=opener_landing(landed)) == "unknown"


def test_a_network_that_cannot_reach_google_does_not_refuse():
    def opener(request, timeout=0):
        raise OSError("no route")
    assert authlib.preflight_consent(CONSENT, opener=opener) is None


def test_an_http_error_is_read_the_same_way():
    def opener(request, timeout=0):
        raise urllib.error.HTTPError(
            "https://accounts.google.com/signin/oauth/error?authError=" + MISMATCH,
            400, "Bad Request", {}, None)
    assert authlib.preflight_consent(CONSENT, opener=opener) == "redirect_uri_mismatch"
