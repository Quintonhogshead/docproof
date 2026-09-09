from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from app.watch import native_files
from app.watch.hubspot import HubSpotError


class Response:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_form_redirect_url_exchanges_numeric_file_id_then_downloads_without_token(tmp_path):
    calls = []
    source = (
        "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/"
        "signed-url-redirect/221489330197?portalId=1&sign=x&conversionId=c-1&"
        "filename=Corrections%20List.pdf"
    )

    def opener(request, timeout=60):
        calls.append(request)
        if len(calls) == 1:
            return Response(json.dumps({"url": "https://f.hubspotusercontent00.net/signed/x"}).encode())
        return Response(b"attachment", {"Content-Disposition": "attachment; filename=ignored.pdf"})

    target = native_files.download_file("secret-token", source, tmp_path, opener=opener)
    assert target.name == "Corrections List.pdf"
    assert target.read_bytes() == b"attachment"
    assert calls[0].full_url == "https://api.hubapi.com/files/v3/files/221489330197/signed-url"
    assert calls[0].get_header("Authorization") == "Bearer secret-token"
    assert calls[1].get_header("Authorization") is None
    assert calls[1].header_items() == []


def test_public_hubspot_cdn_is_downloaded_without_auth_and_filename_is_safe(tmp_path):
    calls = []

    def opener(request, timeout=60):
        calls.append(request)
        return Response(b"proof")

    url = "https://f.hubspotusercontent00.net/a/../../notes%3A.pdf?filename=..%2Fsafe%3A.pdf"
    target = native_files.download_file("secret-token", url, tmp_path, opener=opener)
    assert target.name == "safe-.pdf"
    assert calls[0].get_header("Authorization") is None


def test_public_hubspot_cdn_subdomain_is_downloaded_without_auth(tmp_path):
    calls = []

    def opener(request, timeout=60):
        calls.append(request)
        return Response(b"proof")

    target = native_files.download_file(
        "secret-token", "https://foo.hubspotusercontent-na1.net/proof.pdf",
        tmp_path, opener=opener)
    assert target.name == "proof.pdf"
    assert calls[0].get_header("Authorization") is None


@pytest.mark.parametrize("url", [
    "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/not-a-number",
    "https://api-na1.hubspot.com.evil.example/form-integrations/v1/uploaded-files/signed-url-redirect/12",
    "https://evilhubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/12",
])
def test_unrecognized_hubspot_file_urls_are_refused_before_network(url, tmp_path):
    with pytest.raises(HubSpotError, match="recognized HubSpot"):
        native_files.download_file("secret-token", url, tmp_path,
                                   opener=lambda *_a, **_k: pytest.fail("network"))


def test_signed_url_permission_error_names_required_scope(tmp_path):
    error = urllib.error.HTTPError("https://api.hubapi.com/files/v3/files/12/signed-url",
                                   403, "forbidden", {}, io.BytesIO())

    with pytest.raises(HubSpotError, match="files.ui_hidden.read"):
        native_files.download_file("secret-token",
                                   "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/12",
                                   tmp_path, opener=lambda *_a, **_k: (_ for _ in ()).throw(error))


def test_permission_error_carries_manual_file_identity(tmp_path):
    error = urllib.error.HTTPError("https://api.hubapi.com/files/v3/files/12/signed-url",
                                   403, "forbidden", {}, io.BytesIO())
    url = ("https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/"
           "signed-url-redirect/12?filename=marked%20proof.pdf")
    with pytest.raises(native_files.ManualAttachmentRequired) as caught:
        native_files.download_file("token", url, tmp_path,
                                   opener=lambda *_a, **_k: (_ for _ in ()).throw(error))
    assert caught.value.file_id == "12"
    assert caught.value.filename == "marked proof.pdf"


def test_manual_cache_requires_matching_manifest_hash_and_id(tmp_path):
    source = tmp_path / "marked.pdf"
    source.write_bytes(b"one")
    cache = tmp_path / "manual-attachments"
    stored = native_files.store_manual_file(source, cache, "221489330197",
                                             "../marked:proof.pdf")
    url = ("https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/"
           "signed-url-redirect/221489330197?filename=marked.pdf")
    assert stored.name == "marked-proof.pdf"
    assert native_files.cached_file(url, cache) == stored

    stored.write_bytes(b"tampered")
    assert native_files.cached_file(url, cache) is None
    with pytest.raises(HubSpotError, match="changed after it was stored"):
        native_files.store_manual_file(source, cache, "221489330197",
                                       "marked:proof.pdf")

    with pytest.raises(HubSpotError, match="numeric"):
        native_files.store_manual_file(source, cache, "not-a-file-id")


def test_redirect_handlers_drop_or_refuse_sensitive_headers():
    request = urllib.request.Request("https://api.hubapi.com/files/v3/files/12/signed-url",
                                     headers={"Authorization": "Bearer secret", "X-Test": "x"})
    stripped = native_files._StripHeaders().redirect_request(
        request, None, 302, "found", {}, "https://f.hubspotusercontent00.net/x")
    assert stripped.get_header("Authorization") is None
    assert stripped.header_items() == []
    assert native_files._NoRedirect().redirect_request(
        request, None, 302, "found", {}, "https://evil.example/x") is None


def test_production_drive_opener_is_replaced_at_both_token_boundaries(monkeypatch,
                                                                       tmp_path):
    from app.watch import drive

    calls = []

    def safe_api(request, timeout=60):
        calls.append(("api", request))
        return Response(json.dumps({"url": "https://f.hubspotusercontent00.net/x"}).encode())

    def safe_download(request, timeout=60):
        calls.append(("download", request))
        return Response(b"proof")

    monkeypatch.setattr(native_files, "_open_no_redirect", safe_api)
    monkeypatch.setattr(native_files, "_open_stripped", safe_download)
    native_files.download_file(
        "secret-token",
        "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/221489330197",
        tmp_path, opener=drive._open_url)
    assert [kind for kind, _request in calls] == ["api", "download"]
    assert calls[0][1].get_header("Authorization") == "Bearer secret-token"
    assert calls[1][1].header_items() == []
