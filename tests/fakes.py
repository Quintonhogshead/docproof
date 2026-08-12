"""Stand-in providers, and a stand-in Google Drive. No test in this suite may
touch a real API — every vendor call goes through the Provider protocol, and
every Drive call goes through an injected opener, so the fakes here cover both
the synchronous and the batch path, and the folder the watcher watches."""
from __future__ import annotations

import io
import itertools
import json
import re
import urllib.error
import urllib.parse
from typing import Any, Sequence

from app.watch.drive import DOCX_MIME, GOOGLE_DOC_MIME
from docproof.providers import (BatchRequest, BatchStatus, NormalizedUsage,
                                ProviderResult)

USAGE = NormalizedUsage(input_tokens=100, output_tokens=20,
                        cache_read_input_tokens=50)


class FakeProvider:
    """Replays scripted results, one per call, and records what it was asked."""

    name = "fake"

    def __init__(self, results: Sequence[ProviderResult] | None = None):
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> ProviderResult:
        if self.results:
            return self.results.pop(0)
        return ProviderResult(parsed={"findings": []}, usage=USAGE)

    def complete_structured(self, **kwargs) -> ProviderResult:
        self.calls.append(kwargs)
        return self._next()

    # -- batch ----------------------------------------------------------------

    def submit_batch(self, *, requests: Sequence[BatchRequest],
                     **kwargs) -> str:
        self.calls.append({"batch": True, "requests": list(requests), **kwargs})
        self._ids = [r.custom_id for r in requests]
        return "batch-fake-0001"

    def poll_batch(self, batch_id: str) -> BatchStatus:
        n = len(getattr(self, "_ids", []))
        return BatchStatus(state="completed", total=n, succeeded=n)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        return {cid: self._next() for cid in getattr(self, "_ids", [])}


class ScriptedBatchProvider(FakeProvider):
    """Reports `pending_polls` in-progress polls before completing, so restart
    and resume behaviour can be exercised without sleeping."""

    def __init__(self, results=None, pending_polls: int = 1):
        super().__init__(results)
        self.pending_polls = pending_polls
        self.polls = 0

    def poll_batch(self, batch_id: str) -> BatchStatus:
        self.polls += 1
        n = len(getattr(self, "_ids", []))
        if self.polls <= self.pending_polls:
            return BatchStatus(state="in_progress", total=n)
        return BatchStatus(state="completed", total=n, succeeded=n)


class DyingProvider(FakeProvider):
    """Answers `survive` calls, then raises — the app quitting, the network
    vanishing, the SDK throwing something unhandled. Nothing in the real
    provider stack raises mid-run in an orderly way, which is exactly why the
    resumable-progress path needs a fake that does."""

    def __init__(self, results=None, *, survive: int):
        super().__init__(results)
        self.survive = survive

    def complete_structured(self, **kwargs) -> ProviderResult:
        if len(self.calls) >= self.survive:
            raise RuntimeError("the process died here")
        return super().complete_structured(**kwargs)


# Enough labels to make the shared fixture a real manuscript: a title, a
# copyright line, a chapter, a scene break and a back-matter title.
LABELS = {"body-0000": "title page", "body-0002": "copyright",
          "body-0004": "chapter # / title", "body-0008": "scene break",
          "body-0012": "front/backmatter title"}


class TaggingProvider:
    """Answers the one question prep asks, for whatever ids it was sent."""

    name = "fake-tagger"

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, *, user, **kwargs):
        ids = re.findall(r'id="([^"]+)"', user)
        blanks = set(re.findall(r'<blank id="([^"]+)"/>', user))
        self.calls.append({"user": user, **kwargs})
        rows = [{"para_id": pid,
                 "role": LABELS.get(pid, "spacing" if pid in blanks else "body"),
                 "flag": "Byline — the house title page comes from the cover."
                         if pid == "body-0000" else ""}
                for pid in ids]
        return ProviderResult(parsed={"paragraphs": rows}, usage=USAGE)


def finding_result(*, para_id: str, error_type: str, original: str,
                   corrected: str, confidence: str = "high") -> ProviderResult:
    return ProviderResult(parsed={"findings": [{
        "para_id": para_id, "error_type": error_type,
        "original_text": original, "occurrence": 1,
        "corrected_text": corrected, "explanation": "Test finding.",
        "confidence": confidence}]}, usage=USAGE)


def ids() -> itertools.count:
    return itertools.count(1)


# --- Google Drive -------------------------------------------------------------

def drive_entry(name: str, *, mime: str = DOCX_MIME, props: dict | None = None,
                modified: str = "2026-01-02T03:04:05.000Z",
                size: int = 4096) -> dict:
    """One file as the Drive API describes it."""
    entry = {"name": name, "mimeType": mime, "appProperties": props or {},
             "modifiedTime": modified}
    if mime != GOOGLE_DOC_MIME:
        entry["size"] = str(size)        # Drive sends this as a string
    return entry


def _matches_q(entry: dict, q: str) -> str | bool:
    """Whether one stored file satisfies a Drive `q` string, the way the real
    API scopes a listing.

    Only the clauses DocProof actually sends are modelled — `'<id>' in parents`,
    `name = '...'` (case-sensitive, as Drive is), `name contains '...'`
    (case-insensitive), `mimeType = '...'`. An entry with no `parents` key
    matches any parent clause, so a flat-folder fixture that never set one keeps
    returning everything, exactly as before this filter existed."""
    def _unescape(s: str) -> str:
        return s.replace("\\'", "'").replace("\\\\", "\\")

    parent = re.search(r"'([^']+)' in parents", q)
    if parent and "parents" in entry and parent.group(1) not in entry["parents"]:
        return False
    name_eq = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
    if name_eq and entry.get("name", "") != _unescape(name_eq.group(1)):
        return False
    name_has = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
    if name_has and (_unescape(name_has.group(1)).casefold()
                     not in entry.get("name", "").casefold()):
        return False
    mime = re.search(r"mimeType = '([^']+)'", q)
    if mime and entry.get("mimeType", "") != mime.group(1):
        return False
    # `appProperties has { key='k' and value='v' }` — how the archive finds a
    # job's folder by the id stamped on it rather than by a name two books share.
    prop = re.search(
        r"appProperties has \{ key='([^']+)' and value='((?:[^'\\]|\\.)*)' \}", q)
    if prop and (entry.get("appProperties", {}).get(prop.group(1))
                 != _unescape(prop.group(2))):
        return False
    return True


def fake_drive(files: dict[str, dict] | None = None, *, docx: bytes = b"",
               fail: dict | None = None, page_size: int | None = None,
               access_token: str = "at-1",
               hubspot: dict[str, dict] | None = None):
    """Stands in for Google Drive, holding one folder that stays live.

    The folder is a real dict the fake reads and writes, not a scripted list of
    answers: an upload lands in it and the next listing sees it. That is what
    makes "the second tick finds nothing to do" a thing a test can simply ask
    for, rather than a sequence it has to choreograph.

    `fail` maps an endpoint — token, list, download, export, upload, patch, and
    the HubSpot pair hubspot_search / hubspot_patch — to an exception raised the
    first time that endpoint is called and not after, which is how a tick is
    made to die in a chosen place.

    `hubspot` maps a key value — what a filename resolves to — onto the record's
    properties, e.g. `{"9781234567890": {"ready": "true", "done": "false"}}`.
    The same opener answers HubSpot as well as Drive, because a tick threads one
    opener to both. `opener.hubspot` is the live record store to assert against.
    """
    calls = []
    emails: list[dict] = []
    store = {fid: {**meta, "id": fid} for fid, meta in (files or {}).items()}
    content: dict[str, bytes] = {}
    uploads = itertools.count(1)
    failures = dict(fail or {})

    # Keyed by a stable, predictable id so a test can name the record it expects
    # to be patched. `_key` is what a filename must resolve to to find it.
    hs_records = {f"hs-{key}": {"id": f"hs-{key}", "_key": str(key),
                                "properties": dict(props)}
                  for key, props in (hubspot or {}).items()}

    class Response:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _maybe_fail(kind: str) -> None:
        error = failures.pop(kind, None)
        if error is not None:
            raise error

    def _multipart(request) -> tuple[dict, bytes]:
        boundary = request.get_header("Content-type", "").split(
            "boundary=", 1)[1]
        parts = []
        for part in request.data.split(f"--{boundary}".encode()):
            headers, sep, payload = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]   # exactly the separator, not the bytes
            parts.append(payload)
        # multipart/related order is fixed: the first part is the JSON metadata,
        # the second is the media — whatever its own content type. Sniffing for
        # "application/json" instead would mistake a JSON artifact (the archive
        # manifest) for the metadata and lose its bytes.
        meta = json.loads(parts[0]) if parts else {}
        media = parts[1] if len(parts) > 1 else b""
        return meta, media

    def opener(request, timeout=60):
        calls.append(request)
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if "api.hubapi.com" in request.full_url:
            if path.endswith("/search"):
                _maybe_fail("hubspot_search")
                body = json.loads(request.data)
                filt = body["filterGroups"][0]["filters"][0]
                prop, op = filt.get("propertyName"), filt.get("operator")
                target = filt.get("value")
                results = []
                for r in hs_records.values():
                    pv = r["properties"].get(prop, "")
                    hit = (pv not in ("", None) if op == "HAS_PROPERTY"
                           else pv == target)
                    if hit:
                        results.append({"id": r["id"],
                                        "properties": r["properties"]})
                return Response(json.dumps({"total": len(results),
                                            "results": results}).encode())
            _maybe_fail("hubspot_patch")     # the only other verb is the PATCH
            rid = path.rsplit("/", 1)[-1]
            rec = hs_records.setdefault(
                rid, {"id": rid, "_key": "", "properties": {}})
            rec["properties"].update(
                json.loads(request.data).get("properties") or {})
            return Response(json.dumps({"id": rid}).encode())

        if "gmail.googleapis.com" in request.full_url:
            _maybe_fail("gmail_send")
            emails.append(json.loads(request.data))
            return Response(json.dumps({"id": "msg-1"}).encode())

        if "oauth2.googleapis.com" in request.full_url:
            _maybe_fail("token")
            return Response(json.dumps({"access_token": access_token,
                                        "expires_in": 3599}).encode())

        if "/upload/drive/v3/files" in path:
            _maybe_fail("upload")
            # A media update (PATCH ?uploadType=media) replaces one file's bytes
            # in place, keeping its id — how the archive rewrites its manifest.
            if request.get_method() == "PATCH" or query.get("uploadType") == ["media"]:
                file_id = path.rsplit("/", 1)[-1]
                entry = store.setdefault(
                    file_id, {"id": file_id, "name": "", "mimeType": DOCX_MIME,
                              "appProperties": {}})
                content[file_id] = request.data
                return Response(json.dumps({"id": entry["id"]}).encode())
            meta, media = _multipart(request)
            new_id = f"up-{next(uploads)}"
            store[new_id] = {"id": new_id, "name": meta.get("name", ""),
                             "mimeType": DOCX_MIME,
                             "appProperties": meta.get("appProperties", {}),
                             "parents": meta.get("parents", []),
                             "modifiedTime": "2026-01-02T03:04:05.000Z"}
            content[new_id] = media
            return Response(json.dumps({"id": new_id}).encode())

        # `files.create` with a JSON body and no media — a folder. A plain POST
        # to the API (not the upload endpoint), which is how the archive lays
        # down its Reviews/YYYY-MM/<book> tree.
        if (request.get_method() == "POST" and path.endswith("/drive/v3/files")
                and request.data):
            _maybe_fail("create")
            meta = json.loads(request.data)
            new_id = f"fold-{next(uploads)}"
            store[new_id] = {"id": new_id, "name": meta.get("name", ""),
                             "mimeType": meta.get("mimeType", DOCX_MIME),
                             "appProperties": meta.get("appProperties", {}),
                             "parents": meta.get("parents", []),
                             "modifiedTime": "2026-01-02T03:04:05.000Z"}
            return Response(json.dumps({"id": new_id}).encode())

        if request.get_method() == "PATCH":
            _maybe_fail("patch")
            file_id = path.rsplit("/", 1)[-1]
            entry = store.setdefault(file_id, {"id": file_id})
            props = entry.setdefault("appProperties", {})
            props.update(json.loads(request.data).get("appProperties") or {})
            return Response(json.dumps({"id": file_id}).encode())

        if path.endswith("/export"):
            _maybe_fail("export")
            return Response(content.get(path.split("/")[-2], docx))

        if query.get("alt") == ["media"]:
            _maybe_fail("download")
            return Response(content.get(path.rsplit("/", 1)[-1], docx))

        _maybe_fail("list")
        kept = [e for e in store.values()
                if _matches_q(e, query.get("q", [""])[0])]
        items = [{k: v for k, v in entry.items() if k != "parents"}
                 for entry in kept]
        start = int(query.get("pageToken", ["0"])[0])
        size = page_size or max(len(items), 1)
        answer: dict = {"files": items[start:start + size]}
        if start + size < len(items):
            answer["nextPageToken"] = str(start + size)
        return Response(json.dumps(answer).encode())

    opener.calls = calls
    opener.files = store
    opener.content = content
    opener.hubspot = hs_records
    opener.emails = emails
    return opener


def http_error(status: int, message: str = "") -> urllib.error.HTTPError:
    """A Google error, with Google's own explanation in the body when the test
    cares that it reaches the person reading the log."""
    body = json.dumps({"error": {"code": status, "message": message}}).encode()
    return urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files",
                                  status, "nope", {}, io.BytesIO(body))
