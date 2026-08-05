"""Google Drive, over its REST API and nothing else.

No vendor SDK. The four things the watcher needs — list a folder, fetch a
file, put one back, mark one — are four HTTP requests, and the SDK that wraps
them brings a dependency tree, its own auth machinery and its own opinions
about retries. `app/version.py` already talks to GitHub this way, so this
module copies it exactly: one `_open_url` at the bottom of everything, passed
in by every caller, so no test ever reaches Google.

Every failure that a person could fix comes back as a sentence saying how.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("docproof.app.watch.drive")

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")

# Sent on everything. A folder on a Shared Drive is invisible without them, and
# they cost nothing on an ordinary one — so the watcher works on either without
# being told which it is looking at.
SHARED_DRIVE = {"supportsAllDrives": "true"}
SHARED_DRIVE_LIST = {**SHARED_DRIVE, "includeItemsFromAllDrives": "true"}


class DriveError(RuntimeError):
    """Something Drive would not do. The message is written to be read."""


class AuthExpired(DriveError):
    """The saved sign-in no longer works. Needs a person, not a retry."""


@dataclass(frozen=True)
class DriveFile:
    """One entry in the watched folder.

    `app_properties` is where DocProof writes what it has done with a file.
    They are private to the OAuth client that wrote them — invisible to the
    author, and invisible to a different client id, which docs/watch.md warns
    about."""

    id: str
    name: str
    mime_type: str
    app_properties: dict[str, str] = field(default_factory=dict)
    modified_time: str = ""
    size: int = 0                    # 0 for a native Doc: Drive omits it

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type == GOOGLE_DOC_MIME

    @classmethod
    def from_api(cls, raw: dict) -> "DriveFile":
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            mime_type=str(raw.get("mimeType", "")),
            app_properties=dict(raw.get("appProperties") or {}),
            modified_time=str(raw.get("modifiedTime", "")),
            size=int(raw.get("size") or 0),
        )


# --- the wire -----------------------------------------------------------------

def _open_url(request: urllib.request.Request, timeout: int = 60):
    """The one place this module touches the network. Passed in by every
    caller so no test ever reaches Google."""
    return urllib.request.urlopen(request, timeout=timeout)


def _request(url: str, token: str, *, data: bytes | None = None,
             method: str | None = None,
             content_type: str | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if content_type:
        request.add_header("Content-Type", content_type)
    return request


def _reason(error: urllib.error.HTTPError) -> str:
    """Google's own words for what went wrong, when it gave any.

    Worth the few lines: "the file exceeds the maximum size for exporting"
    is a different afternoon from "insufficient permissions", and both arrive
    as a 403."""
    try:
        body = json.loads(error.read())
    except Exception:                        # noqa: BLE001 - best effort only
        return ""
    if isinstance(body, dict):
        detail = body.get("error")
        if isinstance(detail, dict):
            return str(detail.get("message", "") or "")
        if isinstance(detail, str):
            return str(body.get("error_description", "") or detail)
    return ""


def _call(request: urllib.request.Request, *, opener, what: str) -> bytes:
    """One request, with every failure a person could fix turned into a
    sentence saying how to fix it."""
    try:
        with opener(request) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        detail = _reason(e)
        tail = f" Google said: {detail}" if detail else ""
        if e.code == 401:
            raise AuthExpired(
                "Google no longer accepts the saved sign-in. Run "
                "`docproof-watch auth` to sign in again." + tail) from e
        if e.code == 403:
            raise DriveError(
                f"Google Drive refused to {what}. Check that the account you "
                f"signed in with can open the folder, and that it has not run "
                f"out of storage.{tail}") from e
        if e.code == 404:
            raise DriveError(
                f"Google Drive could not find what it needed to {what}. Check "
                f"the folder id with `docproof-watch init`, and that the file "
                f"has not been moved or put in the bin.{tail}") from e
        if e.code == 429 or e.code >= 500:
            raise DriveError(
                f"Google Drive is busy and would not {what} right now "
                f"({e.code}). The next run will try again.{tail}") from e
        raise DriveError(f"Google Drive answered {e.code} ({e.reason}) trying "
                         f"to {what}.{tail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise DriveError(f"Could not reach Google Drive to {what}: "
                         f"{getattr(e, 'reason', e)}. The next run will try "
                         f"again.") from e
    except OSError as e:
        raise DriveError(f"Could not {what}: {e}") from e


def _json_call(request: urllib.request.Request, *, opener, what: str) -> dict:
    body = _call(request, opener=opener, what=what)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise DriveError(f"Google Drive sent something unreadable while "
                         f"trying to {what}: {e}") from e
    if not isinstance(parsed, dict):
        raise DriveError(f"Google Drive sent an unexpected answer while "
                         f"trying to {what}.")
    return parsed


def _url(base: str, params: dict) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


# --- signing in ---------------------------------------------------------------

def refresh_access_token(client_id: str, client_secret: str,
                         refresh_token: str, *, opener=_open_url) -> str:
    """Trade the saved refresh token for an access token good for this run.

    Access tokens are never written down. They last an hour, a tick lasts
    minutes, and the thing worth keeping — the refresh token — is in the
    Keychain."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        answer = _json_call(request, opener=opener, what="sign in to Google")
    except DriveError as e:
        # A revoked, expired or superseded refresh token is a 400 here rather
        # than a 401, which would otherwise read as a puzzle rather than an
        # instruction.
        if "invalid_grant" in str(e) or "400" in str(e):
            raise AuthExpired(
                "Google no longer accepts the saved sign-in — it may have been "
                "revoked, or the OAuth client rebuilt. Run `docproof-watch "
                "auth` to sign in again.") from e
        raise
    token = str(answer.get("access_token", ""))
    if not token:
        raise AuthExpired("Google did not return an access token. Run "
                          "`docproof-watch auth` to sign in again.")
    return token


# --- reading the folder -------------------------------------------------------

FILE_FIELDS = "id,name,mimeType,appProperties,modifiedTime,size"


def list_folder(token: str, folder_id: str, *, opener=_open_url,
                page_size: int = 200) -> list[DriveFile]:
    """Everything in the folder, one page at a time until there are no more.

    Not recursive on purpose: subfolders are how a person organises their own
    work, and the day one of them means something to DocProof — a copy-edit
    inbox — it will be because `stages` says so, not because the listing
    quietly swept it up."""
    files: list[DriveFile] = []
    page_token = ""
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": str(page_size),
            **SHARED_DRIVE_LIST,
        }
        if page_token:
            params["pageToken"] = page_token
        answer = _json_call(_request(_url(f"{API}/files", params), token),
                            opener=opener, what="read the folder")
        for raw in answer.get("files") or []:
            if isinstance(raw, dict):
                files.append(DriveFile.from_api(raw))
        page_token = str(answer.get("nextPageToken", "") or "")
        if not page_token:
            return files


def download(token: str, file_id: str, dest: str | Path, *,
             opener=_open_url) -> Path:
    """An uploaded file, byte for byte."""
    params = {"alt": "media", **SHARED_DRIVE}
    body = _call(_request(_url(f"{API}/files/{file_id}", params), token),
                 opener=opener, what="download a manuscript")
    return _write(dest, body)


def export_docx(token: str, file_id: str, dest: str | Path, *,
                opener=_open_url) -> Path:
    """A native Google Doc, as the .docx prep can read.

    Drive draws the line at ten megabytes here. A manuscript long enough to
    cross it comes back as a 403 carrying Google's own explanation, which
    `_reason` puts in front of the person reading the log."""
    params = {"mimeType": DOCX_MIME, **SHARED_DRIVE}
    body = _call(_request(_url(f"{API}/files/{file_id}/export", params), token),
                 opener=opener, what="export a Google Doc")
    return _write(dest, body)


def _write(dest: str | Path, body: bytes) -> Path:
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# --- writing back -------------------------------------------------------------

def upload(token: str, folder_id: str, path: str | Path, *,
           name: str | None = None,
           app_properties: dict[str, str] | None = None,
           mime_type: str = DOCX_MIME, opener=_open_url) -> str:
    """Put a finished file in the folder. Returns its new Drive id.

    One multipart request rather than a resumable session: these are
    manuscripts, not video, and a resumable upload's virtue — surviving a
    dropped connection halfway — is one a watcher already has, because the
    next tick re-uploads what the state file says never landed."""
    source = Path(path)
    metadata = {"name": name or source.name, "parents": [folder_id]}
    if app_properties:
        metadata["appProperties"] = app_properties

    boundary = f"docproof-{uuid.uuid4().hex}"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
        json.dumps(metadata).encode(),
        f"\r\n--{boundary}\r\n".encode(),
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        source.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    url = _url(UPLOAD_API, {"uploadType": "multipart", "fields": "id",
                            **SHARED_DRIVE})
    request = _request(url, token, data=body, method="POST",
                       content_type=f"multipart/related; boundary={boundary}")
    answer = _json_call(request, opener=opener,
                        what=f"upload {metadata['name']}")
    new_id = str(answer.get("id", ""))
    if not new_id:
        raise DriveError(f"Google Drive accepted {metadata['name']} but did "
                         f"not say where it put it.")
    return new_id


def set_app_properties(token: str, file_id: str, props: dict[str, str], *,
                       opener=_open_url) -> None:
    """Mark a file with what DocProof has done to it.

    A patch, so the properties already there survive, and a property set to
    None would be the way to remove one. Nothing else about the file is
    touched — least of all its content, which belongs to the author."""
    body = json.dumps({"appProperties": props}).encode()
    url = _url(f"{API}/files/{file_id}", {"fields": "id", **SHARED_DRIVE})
    request = _request(url, token, data=body, method="PATCH",
                       content_type="application/json")
    _json_call(request, opener=opener, what="mark a file as done")
