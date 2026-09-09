from __future__ import annotations

import hashlib
import json
import urllib.error
from contextlib import contextmanager

import pytest

from docproof.website.wordpress import (
    WordPressBridge,
    WordPressConfigurationError,
    WordPressRemoteError,
    WordPressSecurityError,
    make_release_payload,
    release_digest,
    validate_destination,
)


PAGES = {page: f"<!doctype html><title>{page}</title>__DOCPROOF_BASE__ __DOCPROOF_ASSETS__" for page in ("home", "about", "books", "contact")}
ASSET = {"id": "cover-1", "filename": "cover.jpg", "media_type": "image/jpeg", "sha256": hashlib.sha256(b"cover").hexdigest(), "alt": "Cover", "bytes": b"cover"}


def public_resolver(host, port, **kwargs):
    assert host == "author.example"
    return [(2, 1, 6, "", ("8.8.8.8", port))]


class Response:
    def __init__(self, value, status=200):
        self.value = json.dumps(value).encode() if isinstance(value, dict) else value
        self.status = status

    def getcode(self):
        return self.status

    def read(self):
        return self.value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer)


def receipt(release_id="rev_1"):
    return {"release_id": release_id, "state": "staged", "digest": release_digest(PAGES, [ASSET]), "idempotency_key": "stage_1", "preview_url": "https://author.example/preview", "active_release_id": "none"}


def test_destination_is_https_origin_on_a_public_configured_host():
    assert validate_destination("https://Author.Example/", allowed_hosts=["author.example"], resolver=public_resolver) == "https://author.example"
    for value in ("http://author.example", "https://author.example/path", "https://user:pass@author.example", "https://127.0.0.1"):
        with pytest.raises((WordPressConfigurationError, WordPressSecurityError)):
            validate_destination(value, resolver=public_resolver)


def test_destination_rejects_private_dns_answer():
    def private(host, port, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.1", port))]
    with pytest.raises(WordPressSecurityError, match="non-public"):
        validate_destination("https://author.example", resolver=private)


def test_stage_payload_is_deterministic_and_never_contains_local_paths():
    payload = make_release_payload("rev_1", PAGES, [ASSET], "stage_1", {"author": {"name": "A"}})
    assert payload["digest"] == release_digest(PAGES, [ASSET])
    assert payload["assets"][0]["bytes_b64"] == "Y292ZXI="
    assert "bytes" not in payload["assets"][0]
    assert set(payload["pages"]) == {"home", "about", "books", "contact"}


def test_stage_refuses_hash_mismatch_and_missing_page():
    bad = dict(ASSET, sha256="0" * 64)
    with pytest.raises(WordPressSecurityError, match="hash"):
        make_release_payload("rev_1", PAGES, [bad], "stage_1")
    with pytest.raises(WordPressSecurityError, match="missing"):
        release_digest({"home": "x"}, [])


def test_client_stages_exact_bundle_with_basic_auth_and_injected_opener():
    client = WordPressBridge("https://author.example", "site_1", "svc", "app pass", opener=Opener([receipt()]), resolver=public_resolver)
    result = client.stage_release("rev_1", PAGES, [ASSET], idempotency_key="stage_1")
    assert result.digest == release_digest(PAGES, [ASSET])
    request = client._opener.calls[0]
    assert request.full_url.endswith("/wp-json/docproof/v1/sites/site_1/releases")
    assert request.get_header("Authorization").startswith("Basic ")
    sent = json.loads(request.data)
    assert sent["pages"] == PAGES


def test_client_rejects_a_receipt_for_another_bundle():
    wrong = receipt(); wrong["digest"] = "f" * 64
    client = WordPressBridge("https://author.example", "site_1", "svc", "pass", opener=Opener([wrong]), resolver=public_resolver)
    with pytest.raises(WordPressRemoteError, match="different"):
        client.stage_release("rev_1", PAGES, [ASSET], idempotency_key="stage_1")


def test_client_does_not_follow_authenticated_redirects():
    error = urllib.error.HTTPError("https://author.example", 302, "Found", {}, None)
    client = WordPressBridge("https://author.example", "site_1", "svc", "pass", opener=Opener([error]), resolver=public_resolver)
    with pytest.raises(WordPressSecurityError, match="redirected"):
        client.status()


def test_activation_and_health_method_contracts():
    active = receipt(); active.update({"state": "active", "active_release_id": "rev_1"})
    health = {"status": "inconclusive", "findings": ["outside cache check"], "pages": {"home": {"status": "passed"}}}
    opener = Opener([active, health])
    client = WordPressBridge("https://author.example", "site_1", "svc", "pass", opener=opener, resolver=public_resolver)
    assert client.activate("rev_1", expected_active_release_id="none", idempotency_key="activate_1").state == "active"
    assert client.health("rev_1")["status"] == "inconclusive"
    assert json.loads(opener.calls[0].data)["expected_active_release_id"] == "none"
    assert opener.calls[1].full_url.endswith("health?expected_release_id=rev_1")


def test_reconciliation_never_retries_and_checks_remote_ownership():
    opener = Opener([{"site_id": "site_1", "ownership_ok": True, "active_release_id": "rev_1"}])
    client = WordPressBridge("https://author.example", "site_1", "svc", "pass", opener=opener, resolver=public_resolver)
    assert client.reconcile(expected_active_release_id="rev_1")["site_id"] == "site_1"
