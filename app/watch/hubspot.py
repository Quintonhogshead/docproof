"""HubSpot's CRM, over its REST API and nothing else.

The watcher asks HubSpot two questions and gives it one answer: is this book
marked ready, is it already done, and — when a manuscript has been prepared —
mark it done. That is a search and a patch, two HTTP requests, so this module
copies `drive.py` exactly: one `_open_url` at the bottom, passed in by every
caller, so no test ever reaches HubSpot.

Simpler than Drive in one way that matters: a private-app token does not expire,
so there is no refresh dance. The token goes on every request as a bearer
header and that is the whole of the auth story.

Every failure a person could fix comes back as a sentence saying how.
"""
from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("docproof.app.watch.hubspot")

API = "https://api.hubapi.com"


class HubSpotError(RuntimeError):
    """Something HubSpot would not do. The message is written to be read.

    Deliberately not a `DriveError`: the runner tells the two apart so a folder
    that reads fine but a CRM that will not answer is not blamed on Google."""


class HubSpotAuthError(HubSpotError):
    """The token is wrong or was revoked. Needs a person, not a retry."""


@dataclass(frozen=True)
class HubSpotRecord:
    """One CRM object, as far as the watcher cares about it: an id and the
    handful of properties it asked for."""

    id: str
    properties: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict) -> "HubSpotRecord":
        props = raw.get("properties")
        return cls(
            id=str(raw.get("id", "")),
            properties={k: "" if v is None else str(v)
                        for k, v in (props or {}).items()},
        )


# --- the wire -----------------------------------------------------------------

def _open_url(request: urllib.request.Request, timeout: int = 60):
    """The one place this module touches the network. Passed in by every caller
    so no test ever reaches HubSpot."""
    return urllib.request.urlopen(request, timeout=timeout)


def _request(url: str, token: str, *, data: bytes | None = None,
             method: str | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    return request


def _reason(error: urllib.error.HTTPError) -> str:
    """HubSpot's own words for what went wrong, when it gave any.

    Its errors carry a `message`, and often a `category` — "OBJECT_NOT_FOUND"
    reads differently from "RATE_LIMIT", and both can arrive as the same code."""
    try:
        body = json.loads(error.read())
    except Exception:                        # noqa: BLE001 - best effort only
        return ""
    if isinstance(body, dict):
        return str(body.get("message", "") or "")
    return ""


@contextlib.contextmanager
def _answer(request: urllib.request.Request, *, opener, what: str):
    """The open response, with every failure a person could fix turned into a
    sentence saying how to fix it."""
    try:
        with opener(request) as response:
            yield response
    except urllib.error.HTTPError as e:
        detail = _reason(e)
        tail = f" HubSpot said: {detail}" if detail else ""
        if e.code in (401, 403):
            raise HubSpotAuthError(
                "HubSpot would not accept the token. Check that the private-app "
                "token is right and still has CRM read and write scopes. Run "
                "`docproof-watch hubspot-token` to paste a new one." + tail
            ) from e
        if e.code == 429 or e.code >= 500:
            raise HubSpotError(
                f"HubSpot is busy and would not {what} right now ({e.code}). "
                f"The next run will try again.{tail}") from e
        raise HubSpotError(f"HubSpot answered {e.code} ({e.reason}) trying to "
                           f"{what}.{tail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise HubSpotError(f"Could not reach HubSpot to {what}: "
                           f"{getattr(e, 'reason', e)}. The next run will try "
                           f"again.") from e
    except OSError as e:
        raise HubSpotError(f"Could not {what}: {e}") from e


def _json_call(request: urllib.request.Request, *, opener, what: str) -> dict:
    with _answer(request, opener=opener, what=what) as response:
        body = response.read()
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise HubSpotError(f"HubSpot sent something unreadable while trying to "
                           f"{what}: {e}") from e
    if not isinstance(parsed, dict):
        raise HubSpotError(f"HubSpot sent an unexpected answer while trying to "
                           f"{what}.")
    return parsed


# --- asking --------------------------------------------------------------------

def find_record(token: str, object_type: str, key_property: str,
                key_value: str, *, want_properties, opener=_open_url
                ) -> HubSpotRecord | None:
    """The one record whose `key_property` equals `key_value`.

    `None` when nothing matches — a manuscript whose key names no book yet, a
    reason to wait rather than a reason to stop. More than one match is a human
    data problem, not something to guess at: two books cannot share an ISBN, so
    it is raised the way prep raises "gave up", loud and needing a person."""
    body = json.dumps({
        "filterGroups": [{"filters": [{
            "propertyName": key_property,
            "operator": "EQ",
            "value": key_value,
        }]}],
        "properties": list(want_properties),
        # Two is enough to tell "one" from "more than one" without asking for a
        # page of books that happen to share a broken key.
        "limit": 2,
    }).encode()
    request = _request(f"{API}/crm/v3/objects/{object_type}/search",
                       token, data=body, method="POST")
    answer = _json_call(request, opener=opener,
                        what=f"look up the {object_type} record")
    results = answer.get("results")
    if not isinstance(results, list) or not results:
        return None
    if len(results) > 1:
        raise HubSpotError(
            f"More than one {object_type} has {key_property} = {key_value}. "
            f"HubSpot cannot say which book this file is, so nothing was "
            f"touched — a person needs to fix the duplicate.")
    return HubSpotRecord.from_api(results[0])


def set_properties(token: str, object_type: str, record_id: str,
                   props: dict[str, str], *, opener=_open_url) -> None:
    """Write these properties onto the record, leaving the rest as they were.

    A patch, so the one boolean DocProof owns is set without disturbing
    anything an editor put there."""
    body = json.dumps({"properties": props}).encode()
    request = _request(f"{API}/crm/v3/objects/{object_type}/{record_id}",
                       token, data=body, method="PATCH")
    _json_call(request, opener=opener,
               what=f"mark the {object_type} record done")


def is_on(record: HubSpotRecord, prop: str) -> bool:
    """Whether a HubSpot boolean is set.

    HubSpot keeps booleans as the strings "true"/"false". Anything that is not
    "true" — a "false", a blank, a property that was never set — is off, so a
    misspelled property name reads as "not ready" and waits rather than
    surprising anyone by running."""
    if not prop:
        return False
    return record.properties.get(prop, "").strip().lower() == "true"
