"""HubSpot's CRM, over its REST API and nothing else.

The watcher asks HubSpot one question and gives it one answer: what does this
book's status property say, and — when a manuscript has been prepared — move
that status on. That is a search and a patch, two HTTP requests, so this module
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


class HubSpotGuardError(HubSpotError):
    """A write DocProof refused to make — caught here, before it left, because a
    property was not on the short allowlist DocProof is permitted to touch. Not
    HubSpot's word but ours: the token's write scope bounds which object can be
    written, and this bounds which properties, which no token can."""


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



def find_by_value(token: str, object_type: str, prop: str, value: str, *,
                  want_properties, opener=_open_url, cap: int = 500
                  ) -> list[HubSpotRecord]:
    """Every record whose `prop` equals `value`.

    The gate searches for the short list of books an editor has flagged — the
    status property at its "ready" value — and then picks among them by author
    key, so this hands back all of them rather than refusing when more than one
    comes back. Among thousands of records only a handful are ever ready at once,
    so the list is small; it is paged to `cap` all the same, and a ready list
    longer than that is logged rather than silently trimmed."""
    records: list[HubSpotRecord] = []
    after: str | None = None
    while True:
        payload: dict = {
            "filterGroups": [{"filters": [{
                "propertyName": prop, "operator": "EQ", "value": value}]}],
            "properties": list(want_properties),
            "limit": 100,
        }
        if after:
            payload["after"] = after
        request = _request(f"{API}/crm/v3/objects/{object_type}/search",
                           token, data=json.dumps(payload).encode(),
                           method="POST")
        answer = _json_call(request, opener=opener,
                            what=f"search the {object_type} records")
        for raw in answer.get("results") or []:
            records.append(HubSpotRecord.from_api(raw))
            if len(records) >= cap:
                log.warning("More than %d %s records are '%s'; only the first "
                            "%d were looked at.", cap, object_type, value, cap)
                return records
        after = ((answer.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return records


def set_properties(token: str, object_type: str, record_id: str,
                   props: dict[str, str], *, allow, opener=_open_url) -> None:
    """Write these properties onto the record, leaving the rest as they were.

    A patch, so the one boolean DocProof owns is set without disturbing
    anything an editor put there.

    `allow` is the short set of property names DocProof is permitted to write —
    the status property, and the output property when one is configured. Any key
    outside it is refused here, before the request is built, so a bug or a bad
    config can never make this the wire for touching a property DocProof does
    not own. `allow` has no default on purpose: every caller, now and later, has
    to name what it may write, so the guard cannot be inherited-around."""
    stray = sorted(set(props) - set(allow))
    if stray:
        log.warning("Refusing a HubSpot write: %s not in the allowlist %s.",
                    ", ".join(stray), sorted(allow))
        raise HubSpotGuardError(
            "DocProof will not write " + ", ".join(stray) + f" on a "
            f"{object_type} record: it may only set "
            + (", ".join(sorted(allow)) or "no property") + ". Nothing was sent.")
    body = json.dumps({"properties": props}).encode()
    request = _request(f"{API}/crm/v3/objects/{object_type}/{record_id}",
                       token, data=body, method="PATCH")
    _json_call(request, opener=opener,
               what=f"mark the {object_type} record done")


def name_matches(stored: str, key: str) -> bool:
    """Whether a filename's author key names this record.

    Trimmed and case-insensitive, and a co-author parenthetical is set aside —
    a file named for "Lichtenstein" matches a record whose author last name is
    stored "Lichtenstein (and Dolores DelBello)" — because the folder carries
    one surname and the record may carry more. An empty key or an empty stored
    value matches nothing, so a blank never sweeps a book in by accident."""
    s = (stored or "").strip().lower()
    k = (key or "").strip().lower()
    if not s or not k:
        return False
    main = s.split(" (")[0].strip()
    return k == s or k == main
