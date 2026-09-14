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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect, so the caller can decide what rides along
    on the next hop. urllib would otherwise carry every header — the bearer
    token included — to wherever HubSpot points, which is a CDN."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_first_hop(request: urllib.request.Request, timeout: int = 60):
    """`_open_url` that stops at the first redirect (raised as an HTTPError
    carrying the Location), for the one request that carries the token."""
    return urllib.request.build_opener(_NoRedirect()).open(request,
                                                           timeout=timeout)


def download_file(token: str, url: str, dest_dir, *, opener=_open_url,
                  fallback_name: str = "submission") -> "Path":
    """A file the author attached to a HubSpot form, onto disk.

    A form's file-upload field stores the uploaded file as a URL on the record —
    a signed `form-integrations/.../signed-url-redirect/...` link, or a
    `hubspotusercontent` address. The bearer token rides along only to HubSpot's
    own hosts, and only for the first hop: a redirect off that host is followed
    with a fresh, header-free request, so the token never reaches the CDN the
    signed link lands on. The name comes from the URL's `filename=` (what the
    author called it), else the Content-Disposition, else the path; a name with
    no suffix cannot be read, and the caller says so."""
    from pathlib import Path
    import re as _re
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HubSpotError(f"{url!r} is not a web address DocProof can fetch.")
    request = urllib.request.Request(url, method="GET")
    host = (parsed.hostname or "").lower()
    with_token = host == "hubapi.com" or host.endswith(".hubapi.com") \
        or host == "hubspot.com" or host.endswith(".hubspot.com")
    if with_token:
        request.add_header("Authorization", f"Bearer {token}")
    # The token-bearing hop must not be followed blindly; an injected opener
    # (a test's) is trusted to answer the request itself, redirect and all.
    first_hop = _open_first_hop if (with_token and opener is _open_url) else opener
    what = "download the form's file"
    try:
        with _answer(request, opener=first_hop, what=what) as r:
            body = r.read()
            headers = getattr(r, "headers", None)
    except HubSpotError as e:
        hop = e.__cause__
        location = ""
        if isinstance(hop, urllib.error.HTTPError) and 300 <= hop.code < 400:
            location = (hop.headers or {}).get("Location", "") or ""
        if not location:
            raise
        onward = urllib.request.Request(urllib.parse.urljoin(url, location),
                                        method="GET")      # no headers, on purpose
        with _answer(onward, opener=opener, what=what) as r:
            body = r.read()
            headers = getattr(r, "headers", None)
    disposition = (headers.get("Content-Disposition", "") or "") \
        if headers is not None else ""
    query = urllib.parse.parse_qs(parsed.query)
    name = (query.get("filename") or [""])[0]
    if not name and disposition:
        m = _re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)", disposition)
        if m:
            name = urllib.parse.unquote(m.group(1))
    if not name:
        name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    name = name.replace("/", "-").replace(":", "-").strip() or fallback_name
    name = Path(name).name or fallback_name
    if Path(name).suffix.lower() in (".pdf", ".docx") and \
            body.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html")):
        raise HubSpotError("HubSpot returned a sign-in page instead of the "
                           "form's file; the token may lack the files scope.")
    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(body)
    return target


def form_submissions(token: str, form_id: str, *, opener=_open_url,
                     limit: int = 10000) -> list[dict]:
    """Read the exact HubSpot form's submission events, oldest-page-first.

    Used by any stage that folds several form submissions into one job rather
    than trusting a single CRM property snapshot. A 403 (a missing `forms`
    read scope) is surfaced as a `HubSpotError`, deliberately: a caller that
    swallowed it could mistake "cannot read the form" for "the form is empty"
    and silently reuse whatever it read last."""
    if not form_id:
        return []
    rows: list[dict] = []
    after = ""
    while True:
        # HubSpot's submissions endpoint accepts at most 50 per page even
        # though other CRM APIs permit 100.
        query = {"limit": str(min(limit, 50))}
        if after:
            query["after"] = after
        params = urllib.parse.urlencode(query)
        request = _request(
            f"{API}/form-integrations/v1/submissions/forms/{form_id}?{params}",
            token)
        answer = _json_call(request, opener=opener,
                            what=f"read submissions for form {form_id}")
        page = answer.get("results") or answer.get("submissions") or []
        rows.extend(row for row in page if isinstance(row, dict))
        after = str((answer.get("paging") or {}).get("next", {}).get("after")
                    or answer.get("after") or "")
        if not after or not page:
            return rows
        if len(rows) >= limit:
            raise HubSpotError(
                f"The corrections form has more than {limit} submissions; "
                f"raise the cap before running.")


def timestamp_value(value) -> float:
    """Normalize HubSpot's ISO or millisecond submission timestamps to a Unix
    timestamp in seconds."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        number = float(text)
        # HubSpot's form API returns submittedAt in epoch milliseconds. Keep
        # accepting seconds for settings and older captured intake manifests.
        return number / 1000.0 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def row_marker(row: dict) -> str:
    """A stable id for one form submission event — its timestamp and event id,
    so the same submission read twice (a re-polled page) folds to one entry
    rather than two."""
    stamp = str(row.get("submittedAt") or "")
    event_id = str(row.get("conversionId") or row.get("id") or "")
    return f"{stamp}|{event_id}" if stamp and event_id else stamp or event_id


def file_urls(value: str) -> list[str]:
    """The URL(s) a file-upload property holds. HubSpot joins several with a
    semicolon; whitespace and blanks are dropped."""
    return [part.strip() for part in (value or "").replace("\n", ";").split(";")
            if part.strip()]


def name_matches(stored: str, key: str) -> bool:
    """Whether a filename's author key names this record.

    Whitespace-, accent- and case-insensitive, and a co-author parenthetical is set aside —
    a file named for "Lichtenstein" matches a record whose author last name is
    stored "Lichtenstein (and Dolores DelBello)" — because the folder carries
    one surname and the record may carry more. An empty key or an empty stored
    value matches nothing, so a blank never sweeps a book in by accident."""
    from .names import name_key

    s = name_key(stored or "")
    k = name_key(key or "")
    if not s or not k:
        return False
    main = s.split(" (")[0].strip()
    return k == s or k == main
