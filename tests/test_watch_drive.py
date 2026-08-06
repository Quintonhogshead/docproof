"""Talking to Google Drive, without talking to Google.

Every request this module makes is built here and inspected here: the token
that was sent, the query that was asked, the shape of the body an upload
carries. The other half is the failures — a revoked sign-in, a folder the
account cannot see, no internet — which are the ones a person actually meets,
and each has to arrive as a sentence saying what to do rather than a traceback.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest

from app.watch import drive
from app.watch.drive import (DOCX_MIME, GOOGLE_DOC_MIME, AuthExpired,
                             DriveError, DriveFile)

from .fakes import drive_entry, fake_drive, http_error

FOLDER = "1AbCdEfGhIjKlMnOp"


def query_of(request) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)


# --- signing in ---------------------------------------------------------------

def test_the_refresh_grant_carries_what_google_asks_for():
    opener = fake_drive()

    token = drive.refresh_access_token("id-1", "secret-1", "refresh-1",
                                       opener=opener)

    assert token == "at-1"
    sent = urllib.parse.parse_qs(opener.calls[0].data.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["refresh-1"]
    assert sent["client_id"] == ["id-1"]
    assert opener.calls[0].get_method() == "POST"


def test_a_revoked_sign_in_says_to_sign_in_again():
    """Google answers 400 with `invalid_grant` here, not 401, which would
    otherwise read as a puzzle rather than an instruction."""
    opener = fake_drive(fail={"token": http_error(400, "invalid_grant")})

    with pytest.raises(AuthExpired, match="docproof-watch auth"):
        drive.refresh_access_token("id-1", "secret-1", "gone", opener=opener)


def test_a_sign_in_that_returns_no_token_is_not_a_silent_success():
    opener = fake_drive(access_token="")

    with pytest.raises(AuthExpired):
        drive.refresh_access_token("id-1", "secret-1", "refresh-1",
                                   opener=opener)


# --- reading the folder -------------------------------------------------------

def test_listing_asks_for_the_folder_the_fields_and_the_token():
    opener = fake_drive({"f-1": drive_entry("Book.docx")})

    files = drive.list_folder("at-1", FOLDER, opener=opener)

    assert [f.name for f in files] == ["Book.docx"]
    request = opener.calls[0]
    assert request.get_header("Authorization") == "Bearer at-1"
    query = query_of(request)
    assert query["q"] == [f"'{FOLDER}' in parents and trashed = false"]
    assert "appProperties" in query["fields"][0]


def test_listing_asks_for_shared_drives_too():
    """A folder on a Shared Drive is invisible without these, and they cost
    nothing on an ordinary one — so the watcher never has to be told which
    kind it is looking at."""
    opener = fake_drive()

    drive.list_folder("at-1", FOLDER, opener=opener)

    query = query_of(opener.calls[0])
    assert query["supportsAllDrives"] == ["true"]
    assert query["includeItemsFromAllDrives"] == ["true"]


def test_listing_follows_page_tokens_to_the_end():
    folder = {f"f-{n}": drive_entry(f"Book {n}.docx") for n in range(5)}
    opener = fake_drive(folder, page_size=2)

    files = drive.list_folder("at-1", FOLDER, opener=opener)

    assert len(files) == 5
    assert len(opener.calls) == 3
    assert query_of(opener.calls[1])["pageToken"] == ["2"]


def test_a_listing_reads_the_properties_a_previous_run_wrote():
    opener = fake_drive({"f-1": drive_entry(
        "Book.docx", props={"docproof.state": "formatted"})})

    assert drive.list_folder("at-1", FOLDER, opener=opener)[0].app_properties \
        == {"docproof.state": "formatted"}


def test_a_google_doc_in_the_listing_has_no_size_and_that_is_fine():
    opener = fake_drive({"f-1": drive_entry("Book", mime=GOOGLE_DOC_MIME)})

    listed = drive.list_folder("at-1", FOLDER, opener=opener)[0]

    assert listed.is_google_doc
    assert listed.size == 0


# --- fetching -----------------------------------------------------------------

def test_a_download_writes_the_bytes_it_was_sent(tmp_path):
    opener = fake_drive({"f-1": drive_entry("Book.docx")}, docx=b"PK\x03\x04ish")

    written = drive.download("at-1", "f-1", tmp_path / "in" / "Book.docx",
                             opener=opener)

    assert written.read_bytes() == b"PK\x03\x04ish"
    assert query_of(opener.calls[0])["alt"] == ["media"]


def test_a_google_doc_is_asked_for_as_a_docx(tmp_path):
    opener = fake_drive(docx=b"exported")

    drive.export_docx("at-1", "f-1", tmp_path / "Book.docx", opener=opener)

    assert opener.calls[0].full_url.split("?")[0].endswith("/f-1/export")
    assert query_of(opener.calls[0])["mimeType"] == [DOCX_MIME]


def test_a_doc_too_big_to_export_says_what_google_said(tmp_path):
    """Ten megabytes is Drive's limit, and "the file exceeds the maximum size
    for exporting" is a different afternoon from "insufficient permissions"."""
    opener = fake_drive(fail={"export": http_error(
        403, "This file is too large to be exported.")})

    with pytest.raises(DriveError, match="too large to be exported"):
        drive.export_docx("at-1", "f-1", tmp_path / "Book.docx", opener=opener)


# --- writing back -------------------------------------------------------------

def test_an_upload_carries_its_metadata_and_its_bytes(tmp_path):
    source = tmp_path / "tagged_Book.docx"
    source.write_bytes(b"PK\x03\x04tagged")
    opener = fake_drive()

    new_id = drive.upload("at-1", FOLDER, source,
                          app_properties={"docproof.output": "1"},
                          opener=opener)

    assert new_id == "up-1"
    landed = opener.files[new_id]
    assert landed["name"] == "tagged_Book.docx"
    assert landed["parents"] == [FOLDER]
    assert landed["appProperties"] == {"docproof.output": "1"}
    assert opener.content[new_id] == b"PK\x03\x04tagged"


def test_an_upload_is_one_multipart_request(tmp_path):
    source = tmp_path / "Book.docx"
    source.write_bytes(b"bytes")
    opener = fake_drive()

    drive.upload("at-1", FOLDER, source, opener=opener)

    request = opener.calls[0]
    assert request.get_method() == "POST"
    assert request.get_header("Content-type").startswith("multipart/related")
    assert query_of(request)["uploadType"] == ["multipart"]


def test_a_file_too_big_for_multipart_goes_by_session(tmp_path):
    """Google refuses multipart bodies over 5 MB, and a tagged manuscript
    with photographs in it crosses that line. Those used to fail on every
    tick until the give-up marked the book failed."""
    big = tmp_path / "tagged_Big.docx"
    big.write_bytes(b"x" * (drive.MULTIPART_LIMIT + 1))
    seen = []

    class Answer:
        def __init__(self, headers, body):
            self.headers, self.body = headers, body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout=60):
        seen.append(request)
        if "uploadType=resumable" in request.full_url:
            return Answer({"Location": "https://upload.example/session-1"},
                          b"{}")
        return Answer({}, json.dumps({"id": "up-9"}).encode())

    new_id = drive.upload("at-1", FOLDER, big, opener=opener)

    assert new_id == "up-9"
    start, put = seen
    assert query_of(start)["uploadType"] == ["resumable"]
    assert json.loads(start.data)["parents"] == [FOLDER]
    assert put.full_url == "https://upload.example/session-1"
    assert put.get_method() == "PUT"
    assert len(put.data) == drive.MULTIPART_LIMIT + 1


def test_an_upload_can_be_given_a_different_name(tmp_path):
    """Prep writes one `prep_notes.md` per run; a folder holds many books."""
    source = tmp_path / "prep_notes.md"
    source.write_text("notes", encoding="utf-8")
    opener = fake_drive()

    new_id = drive.upload("at-1", FOLDER, source, name="prep_notes_Book.md",
                          mime_type="text/markdown", opener=opener)

    assert opener.files[new_id]["name"] == "prep_notes_Book.md"


def test_an_upload_google_will_not_place_is_not_reported_as_placed(tmp_path):
    source = tmp_path / "Book.docx"
    source.write_bytes(b"bytes")
    opener = fake_drive(fail={"upload": http_error(403, "Quota exceeded.")})

    with pytest.raises(DriveError, match="Quota exceeded"):
        drive.upload("at-1", FOLDER, source, opener=opener)


def test_marking_a_file_patches_it_and_leaves_everything_else_alone():
    opener = fake_drive({"f-1": drive_entry(
        "Book.docx", props={"kept": "yes"})})

    drive.set_app_properties("at-1", "f-1", {"docproof.state": "formatted"},
                             opener=opener)

    assert opener.calls[0].get_method() == "PATCH"
    assert opener.files["f-1"]["appProperties"] == {
        "kept": "yes", "docproof.state": "formatted"}
    assert json.loads(opener.calls[0].data)["appProperties"] == {
        "docproof.state": "formatted"}


# --- when it goes wrong -------------------------------------------------------

def test_a_revoked_token_mid_run_says_to_sign_in_again():
    opener = fake_drive(fail={"list": http_error(401, "Invalid Credentials")})

    with pytest.raises(AuthExpired, match="docproof-watch auth"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_a_folder_the_account_cannot_see_names_the_thing_to_check():
    opener = fake_drive(fail={"list": http_error(404, "File not found")})

    with pytest.raises(DriveError, match="docproof-watch init"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_being_offline_is_a_sentence_not_a_traceback():
    opener = fake_drive(fail={"list": urllib.error.URLError("no route")})

    with pytest.raises(DriveError, match="next run will try again"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_google_being_busy_says_the_next_run_will_try():
    """A 429 or a 503 is worth coming back for; it is not worth a person's
    afternoon."""
    opener = fake_drive(fail={"list": http_error(503, "Backend error")})

    with pytest.raises(DriveError, match="next run will try again"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_an_unreadable_answer_says_so():
    def opener(request, timeout=60):
        class Response:
            def read(self): return b"<html>not json</html>"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Response()

    with pytest.raises(DriveError, match="unreadable"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_an_error_with_no_body_still_produces_a_sentence():
    """`HTTPError` without a readable body is the common shape in the wild.
    Reading Google's explanation is a bonus, never a requirement."""
    bare = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
    opener = fake_drive(fail={"list": bare})

    with pytest.raises(DriveError, match="refused"):
        drive.list_folder("at-1", FOLDER, opener=opener)


def test_nothing_here_reaches_the_network_by_accident(monkeypatch):
    """The default opener is the only door out, and every entry point takes
    one. If a new function forgets to, this is what notices."""
    def forbidden(*a, **kw):
        raise AssertionError("a test reached the network")

    monkeypatch.setattr(drive.urllib.request, "urlopen", forbidden)
    opener = fake_drive({"f-1": drive_entry("Book.docx")})

    assert drive.list_folder("at-1", FOLDER, opener=opener)
    assert isinstance(DriveFile.from_api({"id": "f-1"}), DriveFile)
