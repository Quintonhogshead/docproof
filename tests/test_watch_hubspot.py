"""Talking to HubSpot, without talking to HubSpot.

The client is two requests — a search and a patch — and both are built here and
inspected here: the token that was sent, the filter that was asked, the body a
completion carries. The other half is the answers a person actually meets: no
record, two records for one key, a token HubSpot will not take.

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
from app.watch.hubspot import HubSpotAuthError, HubSpotError, HubSpotRecord
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


# --- find_record --------------------------------------------------------------

def test_the_search_carries_the_filter_the_properties_and_the_token():
    opener = fake_drive(hubspot={"9781234567890": {"ready": "true"}})

    record = hubspot.find_record("tok-1", "deals", "isbn", "9781234567890",
                                 want_properties=["ready", "done"],
                                 opener=opener)

    assert record is not None and record.id == "hs-9781234567890"
    request = opener.calls[0]
    assert request.full_url == ("https://api.hubapi.com/crm/v3/objects/deals/"
                                "search")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok-1"
    sent = body_of(request)
    assert sent["filterGroups"][0]["filters"][0] == {
        "propertyName": "isbn", "operator": "EQ", "value": "9781234567890"}
    assert sent["properties"] == ["ready", "done"]
    assert sent["limit"] == 2


def test_no_record_is_none_not_an_error():
    """A key that names no book yet is a reason to wait, not to fail."""
    opener = fake_drive(hubspot={})

    assert hubspot.find_record("tok-1", "deals", "isbn", "missing",
                               want_properties=["ready"], opener=opener) is None


def test_two_records_for_one_key_is_refused():
    """Two books cannot share an ISBN; guessing which is worse than stopping."""
    opener = scripted({"total": 2, "results": [
        {"id": "1", "properties": {}}, {"id": "2", "properties": {}}]})

    with pytest.raises(HubSpotError, match="More than one"):
        hubspot.find_record("tok-1", "deals", "isbn", "dup",
                            want_properties=["ready"], opener=opener)


def test_null_properties_come_back_as_blank_strings():
    """HubSpot sends an unset property as null; the record reads it as off."""
    opener = scripted({"total": 1, "results": [
        {"id": "7", "properties": {"ready": "true", "done": None}}]})

    record = hubspot.find_record("tok-1", "deals", "isbn", "x",
                                 want_properties=["ready", "done"],
                                 opener=opener)

    assert record.properties["done"] == ""
    assert hubspot.is_on(record, "done") is False


def test_a_rejected_token_is_an_auth_error_that_names_the_fix():
    opener = fake_drive(hubspot={"x": {}},
                        fail={"hubspot_search": hs_error(401, "bad token")})

    with pytest.raises(HubSpotAuthError, match="hubspot-token"):
        hubspot.find_record("tok-1", "deals", "isbn", "x",
                            want_properties=["ready"], opener=opener)


def test_a_busy_hubspot_is_a_plain_error_that_says_it_will_retry():
    opener = fake_drive(hubspot={"x": {}},
                        fail={"hubspot_search": hs_error(503)})

    with pytest.raises(HubSpotError, match="try again") as caught:
        hubspot.find_record("tok-1", "deals", "isbn", "x",
                            want_properties=["ready"], opener=opener)
    assert not isinstance(caught.value, HubSpotAuthError)


# --- set_properties -----------------------------------------------------------

def test_a_completion_patches_only_the_properties_it_is_given():
    opener = fake_drive(hubspot={"9781234567890": {"ready": "true",
                                                   "done": "false"}})

    hubspot.set_properties("tok-1", "deals", "hs-9781234567890",
                           {"done": "true"}, opener=opener)

    request = opener.calls[0]
    assert request.get_method() == "PATCH"
    assert request.full_url == ("https://api.hubapi.com/crm/v3/objects/deals/"
                                "hs-9781234567890")
    assert body_of(request) == {"properties": {"done": "true"}}
    # The one boolean changed; what was already there is untouched.
    props = opener.hubspot["hs-9781234567890"]["properties"]
    assert props == {"ready": "true", "done": "true"}


# --- is_on --------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("  true  ", True),
    ("false", False), ("", False), ("1", False), ("yes", False)])
def test_is_on_treats_only_true_as_on(value, expected):
    record = HubSpotRecord(id="1", properties={"flag": value})
    assert hubspot.is_on(record, "flag") is expected


def test_is_on_of_a_property_that_was_never_set_is_off():
    record = HubSpotRecord(id="1", properties={})
    assert hubspot.is_on(record, "flag") is False
    assert hubspot.is_on(record, "") is False


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
