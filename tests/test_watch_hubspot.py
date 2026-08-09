"""Talking to HubSpot, without talking to HubSpot.

The client is two requests — a search and a patch — and both are built here and
inspected here: the token that was sent, the filter that was asked, the body a
completion carries. The other half is the answers a person actually meets: an
empty ready list, a surname shared across ready Projects, a token HubSpot will
not take.

And `keys`, the pure filename→key step the gate leans on, which has to be
readable and testable without any of the above.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from app.watch import hubspot
from app.watch.hubspot import (HubSpotAuthError, HubSpotError,
                               HubSpotGuardError, HubSpotRecord)
from app.watch.keys import key_from_name

from .fakes import fake_drive


def body_of(request) -> dict:
    return json.loads(request.data)


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def scripted(*answers):
    """An opener that returns each answer in turn, raising any that is an
    exception — for the shapes `fake_drive`'s live store cannot pose, like two
    records sharing one key."""
    served = iter(answers)
    calls: list = []

    def opener(request, timeout=60):
        calls.append(request)
        answer = next(served)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(json.dumps(answer).encode())

    opener.calls = calls
    return opener


def hs_error(status: int, message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"message": message}).encode()
    return urllib.error.HTTPError("https://api.hubapi.com/crm/v3/objects/"
                                  "deals/search", status, "nope", {},
                                  io.BytesIO(body))


# --- find_by_value ------------------------------------------------------------

def test_the_search_carries_the_filter_the_properties_and_the_token():
    opener = fake_drive(hubspot={"Grest": {"docproof": "Ready for Formatting",
                                           "author_last_name": "Grest"}})

    records = hubspot.find_by_value(
        "tok-1", "0-970", "docproof", "Ready for Formatting",
        want_properties=["docproof", "author_last_name"], opener=opener)

    assert [r.id for r in records] == ["hs-Grest"]
    request = opener.calls[0]
    assert request.full_url == ("https://api.hubapi.com/crm/v3/objects/0-970/"
                                "search")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok-1"
    sent = body_of(request)
    assert sent["filterGroups"][0]["filters"][0] == {
        "propertyName": "docproof", "operator": "EQ",
        "value": "Ready for Formatting"}
    assert sent["properties"] == ["docproof", "author_last_name"]
    assert sent["limit"] == 100


def test_nothing_ready_is_an_empty_list_not_an_error():
    """No Project flagged ready is a reason to wait, not to fail."""
    opener = fake_drive(hubspot={})

    assert hubspot.find_by_value("tok-1", "0-970", "docproof",
                                 "Ready for Formatting",
                                 want_properties=["docproof"],
                                 opener=opener) == []


def test_find_by_value_returns_every_match():
    """A shared surname is fine here: the gate picks among them by name, so all
    of them come back rather than the search refusing."""
    opener = scripted({"total": 2, "results": [
        {"id": "1", "properties": {"author_last_name": "Smith"}},
        {"id": "2", "properties": {"author_last_name": "Smith"}}]})

    records = hubspot.find_by_value("tok-1", "0-970", "docproof",
                                    "Ready for Formatting",
                                    want_properties=["author_last_name"],
                                    opener=opener)
    assert [r.id for r in records] == ["1", "2"]


def test_find_by_value_pages_until_the_cursor_runs_out():
    """Two pages: the second request carries the `after` the first handed back."""
    opener = scripted(
        {"results": [{"id": "1", "properties": {}}],
         "paging": {"next": {"after": "100"}}},
        {"results": [{"id": "2", "properties": {}}]})

    records = hubspot.find_by_value("tok-1", "0-970", "docproof",
                                    "Ready for Formatting",
                                    want_properties=[], opener=opener)
    assert [r.id for r in records] == ["1", "2"]
    assert body_of(opener.calls[1])["after"] == "100"


def test_null_properties_come_back_as_blank_strings():
    """HubSpot sends an unset property as null; the record reads it as blank."""
    opener = scripted({"total": 1, "results": [
        {"id": "7", "properties": {"docproof": "Ready for Formatting",
                                   "author_last_name": None}}]})

    records = hubspot.find_by_value("tok-1", "0-970", "docproof",
                                    "Ready for Formatting",
                                    want_properties=["author_last_name"],
                                    opener=opener)
    assert records[0].properties["author_last_name"] == ""


def test_a_rejected_token_is_an_auth_error_that_names_the_fix():
    opener = fake_drive(hubspot={"x": {}},
                        fail={"hubspot_search": hs_error(401, "bad token")})

    with pytest.raises(HubSpotAuthError, match="hubspot-token"):
        hubspot.find_by_value("tok-1", "0-970", "docproof",
                              "Ready for Formatting",
                              want_properties=["docproof"], opener=opener)


def test_a_busy_hubspot_is_a_plain_error_that_says_it_will_retry():
    opener = fake_drive(hubspot={"x": {}},
                        fail={"hubspot_search": hs_error(503)})

    with pytest.raises(HubSpotError, match="try again") as caught:
        hubspot.find_by_value("tok-1", "0-970", "docproof",
                              "Ready for Formatting",
                              want_properties=["docproof"], opener=opener)
    assert not isinstance(caught.value, HubSpotAuthError)


# --- set_properties -----------------------------------------------------------

def test_a_completion_patches_only_the_properties_it_is_given():
    opener = fake_drive(hubspot={"9781234567890": {"ready": "true",
                                                   "done": "false"}})

    hubspot.set_properties("tok-1", "deals", "hs-9781234567890",
                           {"done": "true"}, allow={"done"}, opener=opener)

    request = opener.calls[0]
    assert request.get_method() == "PATCH"
    assert request.full_url == ("https://api.hubapi.com/crm/v3/objects/deals/"
                                "hs-9781234567890")
    assert body_of(request) == {"properties": {"done": "true"}}
    # The one boolean changed; what was already there is untouched.
    props = opener.hubspot["hs-9781234567890"]["properties"]
    assert props == {"ready": "true", "done": "true"}


def test_a_property_outside_the_allowlist_is_refused_before_any_request():
    """The guard that makes a write-scoped token safe to grant: DocProof may set
    only the properties it names, and a stray one is stopped here — no request
    leaves, so nothing on the record, and no other property, is ever touched."""
    opener = scripted()          # no answers scripted: any call would raise

    with pytest.raises(HubSpotGuardError, match="author_last_name"):
        hubspot.set_properties(
            "tok-1", "0-970", "hs-Grest",
            {"docproof": "Formatting Complete", "author_last_name": "wiped"},
            allow={"docproof", "output_file"}, opener=opener)

    assert opener.calls == []    # the guard fired before the wire


# --- name_matches -------------------------------------------------------------

@pytest.mark.parametrize("stored,key,want", [
    ("Grest", "Grest", True),
    ("grest", "GREST", True),                       # case-insensitive
    ("  Grest  ", "Grest", True),                   # trimmed
    ("Lichtenstein (and Dolores DelBello)", "Lichtenstein", True),  # co-author
    ("Smith", "Smithson", False),                   # not a loose prefix match
    ("Smithson", "Smith", False),
    ("St Denis", "St Denis", True),                 # a space in the surname
    ("Grest", "", False),
    ("", "Grest", False)])
def test_name_matches_pairs_a_filename_key_with_a_stored_surname(
        stored, key, want):
    assert hubspot.name_matches(stored, key) is want


# --- keys ---------------------------------------------------------------------

def test_with_no_pattern_the_key_is_the_filename_stem():
    assert key_from_name("9781234567890.docx", "") == "9781234567890"


def test_a_pattern_returns_its_first_capture_group():
    assert key_from_name("Wolves [ISBN 9781234567890].docx",
                         r"ISBN (\d{13})") == "9781234567890"


def test_a_pattern_with_no_group_returns_the_whole_match():
    assert key_from_name("order-4471-draft.docx", r"\d{4}") == "4471"


def test_a_pattern_that_does_not_match_is_no_key_not_an_error():
    assert key_from_name("untitled.docx", r"ISBN (\d{13})") is None


def test_an_empty_name_has_no_key():
    assert key_from_name("", "") is None
