"""Create a native Google Doc in one shared Author teasers folder."""
from __future__ import annotations

import json
from pathlib import Path
import urllib.error
from urllib.parse import urlparse

from docproof.teasers.document import write_document
from docproof.teasers.models import Draft, Review, Storysheet, approval_issues
from .teasers import TeaserError, lock
from .watch import drive
from .watch.settings import GOOGLE_KEY, WatchSettings, google_client


def token_for(home, *, opener=drive._open_url):
    from app.settings import get_api_key
    ws = WatchSettings.load(Path(home))
    refresh, client = get_api_key(GOOGLE_KEY), google_client(ws)
    if not refresh or not all(client):
        raise TeaserError("The formatting workflow's Google sign-in is unavailable.")
    return drive.refresh_access_token(*client, refresh, opener=opener)


def ensure_folder(queue, token, *, opener=drive._open_url):
    # One folder for the connected Google identity, across authors and books.
    with lock(queue.root / "delivery.lock"):
        cfg = queue.settings()
        folder_id = cfg.get("folder_id")
        if folder_id:
            folder = drive.get_file(token, folder_id, opener=opener)
            if not folder.is_folder or folder.name != "Author teasers":
                raise TeaserError("The configured Author teasers destination is not the expected folder.")
            return folder_id
        matches = drive.search_files(token,
            "trashed = false and mimeType = 'application/vnd.google-apps.folder' "
            "and name = 'Author teasers' and 'root' in parents", opener=opener)
        if len(matches) > 1:
            raise TeaserError("There are multiple Author teasers folders; select its folder ID in settings.")
        folder_id = matches[0].id if matches else drive.create_folder(token, "root", "Author teasers",
                            app_properties={"docproof.teasers.folder": "1"}, opener=opener)
        queue.configure(folder_id=folder_id)
        return folder_id


def resume_import(queue, task, token, folder_id, path, *, opener=drive._open_url):
    """Persist the upload session before sending bytes; recover a lost final reply."""
    body = Path(path).read_bytes()
    session = task.get("upload_session")
    offset = 0
    if session:
        request = drive._request(session, token, method="PUT", data=b"")
        request.add_header("Content-Range", f"bytes */{len(body)}")
        try:
            with opener(request) as response:
                result = json.load(response)
                if result.get("id"):
                    return str(result["id"])
                raise TeaserError("Google returned an incomplete upload receipt.")
        except urllib.error.HTTPError as exc:
            if exc.code == 308:
                byte_range = exc.headers.get("Range", "")
                offset = int(byte_range.rsplit("-", 1)[-1]) + 1 if byte_range else 0
            elif exc.code in (404, 410):
                # The caller has already searched for a completed document.
                session = None
            else:
                raise
    if not session:
        metadata = {"name": task["book_label"] + " — Author teasers",
                    "mimeType": drive.GOOGLE_DOC_MIME, "parents": [folder_id],
                    "appProperties": {"docproof.teaser": task["id"], "docproof.output": "teaser"}}
        url = drive._url(drive.UPLOAD_API, {"uploadType": "resumable", "fields": "id", **drive.SHARED_DRIVE})
        request = drive._request(url, token, method="POST", data=json.dumps(metadata).encode(),
                                 content_type="application/json")
        request.add_header("X-Upload-Content-Type", drive.DOCX_MIME)
        request.add_header("X-Upload-Content-Length", str(len(body)))
        with drive._answer(request, opener=opener, what="start the teaser document upload") as response:
            session = response.headers.get("Location", "")
        parsed = urlparse(session)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
            raise TeaserError("Google did not return a valid upload session.")
        task["upload_session"] = session
        queue.save(task)
    if not 0 <= offset < len(body):
        raise TeaserError("Google returned an invalid upload offset.")
    request = drive._request(session, token, method="PUT", data=body[offset:], content_type=drive.DOCX_MIME)
    request.add_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
    result = drive._json_call(request, opener=opener, what="upload the teaser Google Doc")
    if not result.get("id"):
        raise TeaserError("Google did not return the completed teaser document ID.")
    return str(result["id"])


def verify_document(token, file_id, draft, folder_id, *, opener=drive._open_url):
    metadata = drive._json_call(drive._request(drive._url(f"{drive.API}/files/{file_id}",
        {"fields": "id,mimeType,webViewLink,parents,trashed", **drive.SHARED_DRIVE}), token),
        opener=opener, what="verify the teaser Google Doc")
    if (metadata.get("mimeType") != drive.GOOGLE_DOC_MIME or metadata.get("trashed") or
            folder_id not in metadata.get("parents", []) or not metadata.get("webViewLink")):
        raise TeaserError("Google did not confirm the native teaser document and its destination.")
    url = drive._url(f"{drive.API}/files/{file_id}/export", {"mimeType": "text/plain"})
    text = drive._call(drive._request(url, token), opener=opener, what="read back the teaser Google Doc").decode("utf-8-sig")
    normalized = " ".join(text.split())
    required = [p for t in draft.teasers for p in t.paragraphs]
    required += ["Teaser elements & best practices", draft.editorial_note, *draft.opening_hooks,
                 *draft.best_practices, *draft.modification_checklist]
    required += [v for e in draft.elements for v in (e.name, e.purpose, e.book_specific_guidance)]
    if any(" ".join(value.split()) not in normalized for value in required):
        raise TeaserError("The Google Doc readback is missing some approved teaser content.")
    return metadata["webViewLink"]


def deliver(queue, task, home, *, token=None, opener=drive._open_url):
    if task["state"] == "complete":
        return task
    if task["state"] != "approved":
        raise TeaserError("Only an approved teaser package can be uploaded.")
    draft = Draft.model_validate(task["drafts"][-1]["content"])
    review = Review.model_validate(task["reviews"][-1])
    issues = approval_issues(draft, review, [c["id"] for c in task["chunks"]])
    if issues:
        raise TeaserError("Upload withheld: " + "; ".join(issues))
    token = token or token_for(home, opener=opener)
    folder_id = ensure_folder(queue, token, opener=opener)
    path = queue.root / task["id"] / "Author teasers.docx"
    if not path.exists():
        write_document(path, Storysheet.model_validate(task["storysheet"]), draft, review,
                       book_label=task["book_label"])
    file_id = task.get("document_id")
    if not file_id:
        # Reconcile an upload whose acknowledgement was lost before retrying.
        matches = drive.search_files(token,
            f"trashed = false and '{folder_id}' in parents and appProperties has "
            f"{{ key='docproof.teaser' and value='{task['id']}' }}", opener=opener)
        if len(matches) > 1:
            raise TeaserError("Multiple documents claim this teaser run; review the destination folder.")
        if matches:
            file_id = matches[0].id
        else:
            file_id = resume_import(queue, task, token, folder_id, path, opener=opener)
        task["document_id"] = file_id
        queue.save(task)
    task["document_url"] = verify_document(token, file_id, draft, folder_id, opener=opener)
    task["progress"] = "Five teasers and the editing guide are ready"
    task.pop("upload_session", None)
    task.pop("error", None)
    queue.save(task, "complete")
    return queue.get(task["id"])
