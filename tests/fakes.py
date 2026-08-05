"""Stand-in providers, and a stand-in Google Drive. No test in this suite may
touch a real API — every vendor call goes through the Provider protocol, and
every Drive call goes through an injected opener, so the fakes here cover both
the synchronous and the batch path, and the folder the watcher watches."""
from __future__ import annotations

import io
import itertools
import json
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


def fake_drive(files: dict[str, dict] | None = None, *, docx: bytes = b"",
               fail: dict | None = None, page_size: int | None = None,
               access_token: str = "at-1"):
    """Stands in for Google Drive, holding one folder that stays live.

    The folder is a real dict the fake reads and writes, not a scripted list of
    answers: an upload lands in it and the next listing sees it. That is what
    makes "the second tick finds nothing to do" a thing a test can simply ask
    for, rather than a sequence it has to choreograph.

    `fail` maps an endpoint — token, list, download, export, upload, patch — to
    an exception raised the first time that endpoint is called and not after,
    which is how a tick is made to die in a chosen place."""
    calls = []
    store = {fid: {**meta, "id": fid} for fid, meta in (files or {}).items()}
    content: dict[str, bytes] = {}
    uploads = itertools.count(1)
    failures = dict(fail or {})

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
        meta, media = {}, b""
        for part in request.data.split(f"--{boundary}".encode()):
            headers, sep, payload = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]   # exactly the separator, not the bytes
            if b"application/json" in headers:
                meta = json.loads(payload)
            else:
                media = payload
        return meta, media

    def opener(request, timeout=60):
        calls.append(request)
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if "oauth2.googleapis.com" in request.full_url:
            _maybe_fail("token")
            return Response(json.dumps({"access_token": access_token,
                                        "expires_in": 3599}).encode())

        if "/upload/drive/v3/files" in path:
            _maybe_fail("upload")
            meta, media = _multipart(request)
            new_id = f"up-{next(uploads)}"
            store[new_id] = {"id": new_id, "name": meta.get("name", ""),
                             "mimeType": DOCX_MIME,
                             "appProperties": meta.get("appProperties", {}),
                             "parents": meta.get("parents", []),
                             "modifiedTime": "2026-01-02T03:04:05.000Z"}
            content[new_id] = media
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
        items = [{k: v for k, v in entry.items() if k != "parents"}
                 for entry in store.values()]
        start = int(query.get("pageToken", ["0"])[0])
        size = page_size or max(len(items), 1)
        answer: dict = {"files": items[start:start + size]}
        if start + size < len(items):
            answer["nextPageToken"] = str(start + size)
        return Response(json.dumps(answer).encode())

    opener.calls = calls
    opener.files = store
    opener.content = content
    return opener


def http_error(status: int, message: str = "") -> urllib.error.HTTPError:
    """A Google error, with Google's own explanation in the body when the test
    cares that it reaches the person reading the log."""
    body = json.dumps({"error": {"code": status, "message": message}}).encode()
    return urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files",
                                  status, "nope", {}, io.BytesIO(body))
