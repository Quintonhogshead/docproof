"""Sapling.ai: the offset-resolving client, the /api/sapling/check route, and
Sapling's key sitting in God Mode alongside the review providers.

No real network is touched — httpx.post is stubbed with a canned response, so
these test the shapes we send and the shapes we make of what comes back, not
Sapling itself. The `clean_env` fixture wipes the key env vars around each test
so the app's os.environ writes never leak."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.main import create_app
from app.settings import ENV_VARS, Paths
from docproof import sapling

SECRET = "test-session-secret"


class FakeResponse:
    """The slice of httpx.Response the client reads: a status, a JSON body (or a
    ValueError from .json() when there isn't one), and .text."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)


def _stub_post(monkeypatch, response, captured=None):
    def fake_post(url, json=None, timeout=None):
        if captured is not None:
            captured["url"] = url
            captured["json"] = json
        return response
    monkeypatch.setattr(sapling.httpx, "post", fake_post)


# -- the client --------------------------------------------------------------

def test_offsets_resolve_to_absolute_positions(monkeypatch):
    text = "One sentence. A secont sentence here."
    # sentence_start 14 puts the second sentence's offsets into the whole text.
    payload = {"edits": [{"sentence": "A secont sentence here.",
                          "sentence_start": 14, "start": 2, "end": 8,
                          "replacement": "second", "error_type": "R:SPELL",
                          "general_error_type": "Spelling"}]}
    _stub_post(monkeypatch, FakeResponse(payload=payload))
    edits = sapling.check(text, "key-123")
    assert len(edits) == 1
    e = edits[0]
    assert (e.start, e.end) == (16, 22)
    assert e.original == "secont"           # sliced from the submitted text
    assert e.replacement == "second"
    assert e.general_error_type == "Spelling"


def test_edits_come_back_sorted_and_malformed_ones_dropped(monkeypatch):
    payload = {"edits": [
        {"sentence_start": 20, "start": 0, "end": 3, "replacement": "the"},
        {"sentence_start": 0, "start": 5, "end": 9, "replacement": "quick"},
        {"sentence_start": 0, "replacement": "no offsets"},   # malformed
    ]}
    _stub_post(monkeypatch, FakeResponse(payload=payload))
    edits = sapling.check("x" * 40, "key")
    assert [e.start for e in edits] == [5, 20]   # sorted, malformed one gone


def test_variety_and_session_reach_the_request(monkeypatch):
    captured = {}
    _stub_post(monkeypatch, FakeResponse(payload={"edits": []}), captured)
    sapling.check("hello", "key", variety="us-variety", session_id="s1")
    assert captured["url"] == sapling.EDITS_URL
    assert captured["json"] == {"key": "key", "text": "hello",
                                "session_id": "s1", "variety": "us-variety"}


def test_blank_text_makes_no_request(monkeypatch):
    def boom(*a, **k):                        # must not be called
        raise AssertionError("posted for blank text")
    monkeypatch.setattr(sapling.httpx, "post", boom)
    assert sapling.check("   ", "key") == []


def test_missing_key_raises():
    with pytest.raises(sapling.SaplingError):
        sapling.check("some text", "")


def test_rejected_key_is_a_readable_error(monkeypatch):
    _stub_post(monkeypatch, FakeResponse(401, payload={"msg": "bad key"}))
    with pytest.raises(sapling.SaplingError, match="rejected the API key"):
        sapling.check("text", "nope")


def test_network_failure_is_wrapped(monkeypatch):
    def fail(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(sapling.httpx, "post", fail)
    with pytest.raises(sapling.SaplingError, match="Could not reach Sapling"):
        sapling.check("text", "key")


def test_unreadable_body_is_an_error(monkeypatch):
    _stub_post(monkeypatch, FakeResponse(200, payload=None, text="not json"))
    with pytest.raises(sapling.SaplingError, match="couldn't read"):
        sapling.check("text", "key")


# -- the route ---------------------------------------------------------------

def make_app(tmp_path):
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    accounts.create_user("ed@press.com", "password1")
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret=SECRET, https_only=False)


def _as(app, email):
    c = TestClient(app)
    assert c.post("/api/login",
                  json={"email": email, "password": "password1"}).status_code == 200
    return c


def test_check_route_returns_edits(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/sapling", json={"key": "sap-key"})
    payload = {"edits": [{"sentence_start": 0, "start": 0, "end": 3,
                          "replacement": "The", "error_type": "R:CASE",
                          "general_error_type": "Capitalization"}]}
    _stub_post(monkeypatch, FakeResponse(payload=payload))
    r = boss.post("/api/sapling/check", json={"text": "the cat sat."})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["edits"][0]["replacement"] == "The"
    assert body["edits"][0]["original"] == "the"


def test_check_route_without_a_key_is_400(tmp_path):
    app = make_app(tmp_path)
    ed = _as(app, "ed@press.com")
    r = ed.post("/api/sapling/check", json={"text": "hello"})
    assert r.status_code == 400
    assert "Sapling" in r.json()["detail"]


def test_check_route_rejects_unknown_variety(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/sapling", json={"key": "sap-key"})
    r = boss.post("/api/sapling/check",
                  json={"text": "hello", "variety": "martian"})
    assert r.status_code == 400


def test_check_route_surfaces_sapling_failure_as_502(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/sapling", json={"key": "sap-key"})
    _stub_post(monkeypatch, FakeResponse(500, payload={"msg": "boom"}))
    r = boss.post("/api/sapling/check", json={"text": "hello"})
    assert r.status_code == 502


# -- Sapling's key in God Mode ------------------------------------------------

def test_sapling_key_manageable_alongside_providers(tmp_path):
    from app.settings import get_api_key
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    rows = {k["provider"]: k for k in boss.get("/api/admin/keys").json()["keys"]}
    assert "sapling" in rows and rows["sapling"]["display"] == "Sapling"
    assert rows["sapling"]["configured"] is False
    boss.put("/api/admin/keys/sapling", json={"key": "sap-portal"})
    assert get_api_key("sapling") == "sap-portal"
    boss.delete("/api/admin/keys/sapling")
    assert get_api_key("sapling") is None


def test_non_admin_cannot_set_the_sapling_key(tmp_path):
    app = make_app(tmp_path)
    ed = _as(app, "ed@press.com")
    assert ed.put("/api/admin/keys/sapling", json={"key": "x"}).status_code == 403


# -- the whole-document pass --------------------------------------------------

def _make_docx(path, *paragraphs):
    import docx
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(path)
    return path


def _spellfix_post(word, replacement, error_type="R:SPELL",
                   general="Spelling"):
    """A fake httpx.post that flags every occurrence of `word` in the sent text.
    Offsets come back sentence-relative with sentence_start 0 (one 'sentence'
    per request is enough for these fixtures)."""
    import re

    def fake_post(url, json=None, timeout=None):
        text = json["text"]
        edits = [{"sentence_start": 0, "start": m.start(), "end": m.end(),
                  "replacement": replacement, "error_type": error_type,
                  "general_error_type": general}
                 for m in re.finditer(re.escape(word), text)]
        return FakeResponse(payload={"edits": edits})
    return fake_post


def test_docx_pass_returns_tracked_changes(tmp_path, monkeypatch):
    import base64

    from docproof.utils.xml_helpers import (DEL_TAG, INS_TAG, DocxPackage,
                                            walk_package)
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/sapling", json={"key": "sap-key"})
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))

    src = _make_docx(tmp_path / "m.docx", "the cat sat on teh mat.",
                     "teh dog too.")
    with src.open("rb") as fh:
        r = boss.post("/api/sapling/docx",
                      files={"file": ("m.docx", fh, "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 2 and body["found"] == 2
    assert all(e["applied"] for e in body["edits"])

    out = tmp_path / "out.docx"
    out.write_bytes(base64.b64decode(body["docx_base64"]))
    pkg = DocxPackage(out)
    ins = sum(len(list(wp.element.iter(INS_TAG))) for wp in walk_package(pkg))
    dele = sum(len(list(wp.element.iter(DEL_TAG))) for wp in walk_package(pkg))
    assert ins == 2 and dele == 2
    # Attributed to Sapling, not DocProof.
    xml = pkg.tree("word/document.xml")
    from docproof.utils.xml_helpers import qn
    authors = {el.get(qn("w:author")) for el in xml.iter(INS_TAG)}
    assert authors == {"Sapling"}


def test_docx_pass_rejects_non_docx(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/sapling", json={"key": "sap-key"})
    r = boss.post("/api/sapling/docx",
                  files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_docx_pass_without_a_key_is_400(tmp_path):
    app = make_app(tmp_path)
    ed = _as(app, "ed@press.com")
    src = _make_docx(tmp_path / "m.docx", "the cat sat on teh mat.")
    with src.open("rb") as fh:
        r = ed.post("/api/sapling/docx",
                    files={"file": ("m.docx", fh, "application/octet-stream")})
    assert r.status_code == 400


# -- the paragraph-mapping helper --------------------------------------------

def test_check_paragraphs_maps_edits_to_the_right_paragraph(monkeypatch):
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    edits = sapling.check_paragraphs(
        [("body-0", "the teh one."), ("body-1", "and teh two.")], "key")
    assert len(edits) == 2
    assert {e.para_id for e in edits} == {"body-0", "body-1"}
    for e in edits:
        assert e.original == "teh" and e.replacement == "the"


def test_check_paragraphs_skips_blank_paragraphs(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["text"] = json["text"]
        return FakeResponse(payload={"edits": []})
    monkeypatch.setattr(sapling.httpx, "post", fake_post)
    sapling.check_paragraphs([("a", "real text"), ("b", "   ")], "key")
    assert "real text" in seen["text"]
