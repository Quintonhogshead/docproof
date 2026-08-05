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

import logging
from dataclasses import dataclass, field

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
