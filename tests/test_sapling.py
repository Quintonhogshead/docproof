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


# -- the Sapling pass folded into a full review ------------------------------

def _one_para_doc(text):
    from docproof.models import DocumentModel, ParagraphRef
    return DocumentModel("x.docx", (ParagraphRef(
        "body-0", "word/document.xml", "body", text, "Normal"),))


def _prepared(text, lexicon=()):
    """A stand-in for the parts of Prepared that _sapling_findings reads: the
    document and the spell scan's protected lexicon."""
    from types import SimpleNamespace
    return SimpleNamespace(doc=_one_para_doc(text),
                           spell=SimpleNamespace(lexicon=tuple(lexicon)))


def _cfg_with_sapling(enabled):
    from docproof.config import load_config
    cfg = load_config("config/default.yaml")
    cfg.sapling.enabled = enabled
    return cfg


def test_pass_off_makes_no_call_and_no_findings(monkeypatch):
    from docproof.pipeline import _sapling_findings

    def boom(*a, **k):
        raise AssertionError("Sapling called while the pass was off")
    monkeypatch.setattr(sapling.httpx, "post", boom)
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    assert _sapling_findings(_cfg_with_sapling(False),
                             _prepared("the teh cat.")) == []


def test_pass_on_without_a_key_skips_gracefully(monkeypatch):
    from docproof.pipeline import _sapling_findings
    monkeypatch.delenv("SAPLING_API_KEY", raising=False)
    # No key → no findings, no exception (a warning is logged).
    assert _sapling_findings(_cfg_with_sapling(True),
                             _prepared("the teh cat.")) == []


def test_pass_failure_degrades_to_no_findings(monkeypatch):
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")

    def fail(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(sapling.httpx, "post", fail)
    assert _sapling_findings(_cfg_with_sapling(True),
                             _prepared("the teh cat.")) == []


def test_blind_pass_builds_quoted_sentence_findings(monkeypatch):
    """With confirm off, Sapling folds edits straight in as quoted-sentence
    findings — the older behaviour, kept for A/B measurement."""
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    cfg = _cfg_with_sapling(True)
    cfg.sapling.confirm = False
    findings = _sapling_findings(cfg, _prepared("The cat sat on teh mat."))
    assert len(findings) == 1
    f = findings[0]
    # A quoted sentence + corrected sentence, like a sweep — not raw offsets.
    assert f.original_text == "The cat sat on teh mat."
    assert f.corrected_text == "The cat sat on the mat."
    assert f.status == "pending" and f.anchor is None
    assert f.error_type == "sapling"


def test_pass_folds_tracked_changes_into_the_review(tmp_path, monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import finish, prepare
    from docproof.utils.xml_helpers import (DEL_TAG, INS_TAG, DocxPackage,
                                            qn, walk_package)
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))

    src = _make_docx(tmp_path / "book.docx", "The cat sat on teh mat.")
    cfg = _cfg_with_sapling(True)
    cfg.sapling.confirm = False        # blind fold-in; no LLM valve in this test
    cfg.audit = "off"
    prepared = prepare(cfg, src, "config/error_types")
    out = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "out",
                 source_path=src)

    # The rebranded output name, and Sapling's edit applied as a tracked change
    # authored to the press.
    assert out.reviewed_path.name == "book - Atmosphere Press Proofreader.docx"
    pkg = DocxPackage(out.reviewed_path)
    ins = sum(len(list(wp.element.iter(INS_TAG))) for wp in walk_package(pkg))
    dele = sum(len(list(wp.element.iter(DEL_TAG))) for wp in walk_package(pkg))
    assert ins >= 1 and dele >= 1
    authors = {el.get(qn("w:author"))
               for wp in walk_package(pkg) for el in wp.element.iter(INS_TAG)}
    assert authors == {"Atmosphere Press Proofreader"}


# -- the feature toggle -------------------------------------------------------

def test_pass_records_its_char_cost_on_usage(monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    cfg = _cfg_with_sapling(True)                 # cost_per_1k_chars defaults 0.025
    cfg.sapling.confirm = False                   # cost is billed before the valve
    usage = Usage()
    prepared = _prepared("x" * 4000)              # 4,000 chars sent
    _sapling_findings(cfg, prepared, usage)
    assert usage.sapling_chars == 4000
    assert usage.sapling_cost == 4000 * 0.025 / 1000    # $0.10


def test_summary_totals_and_breaks_out_sapling_and_settings(tmp_path):
    from docproof.config import load_config
    from docproof.formats import DOCX
    from docproof.models import DocumentModel, ParagraphRef, Usage
    from docproof.reporting import write_summary_md
    cfg = load_config("config/default.yaml")
    cfg.sapling.enabled = True
    cfg.sapling.variety = "us-variety"
    usage = Usage(sapling_chars=2000, sapling_cost=0.05)
    doc = DocumentModel("Book.docx", (ParagraphRef(
        "body-0", "word/document.xml", "body", "A line.", "Normal"),))
    out = tmp_path / "summary.md"
    write_summary_md(out, doc=doc, findings=[], usage=usage, cfg=cfg,
                     applied_ids=(), fmt=DOCX)
    txt = out.read_text("utf-8")
    # The total folds Sapling in, and the breakdown names its share.
    assert "**$0.0500**" in txt
    assert "Sapling grammar check: $0.0500 (2,000 characters at $0.025/1k)" in txt
    # The settings section says what actually ran, on and off.
    assert "## Settings used" in txt
    assert "Sapling grammar check on (us-variety)" in txt
    assert "Story sheet off" in txt
    assert "Spell scan on" in txt


def test_sapling_feature_toggle_round_trips():
    from app import features as featureslib
    from docproof.config import load_config
    cfg = load_config("config/default.yaml")
    assert cfg.sapling.enabled is False
    featureslib.apply_features(cfg, {"sapling": True})
    assert cfg.sapling.enabled is True
    row = {f["id"]: f for f in featureslib.feature_catalog(cfg)}["sapling"]
    assert row["default"] is True and row["heavy"] is True
    assert row["cost"] == {"kind": "grammar", "rate_per_1k": 0.025}


# -- readable explanations and the comments toggle ----------------------------

def _edit(original, replacement, error_type, general=""):
    return sapling.Edit(start=0, end=len(original), original=original,
                        replacement=replacement, error_type=error_type,
                        general_error_type=general)


def test_describe_reads_out_a_substitution():
    d = sapling.describe(_edit("teh", "the", "R:SPELL", "Spelling"))
    assert d == "Spelling: “teh” → “the”."


def test_describe_names_an_insertion_and_a_deletion():
    assert sapling.describe(_edit("", ",", "M:PUNCT", "Punctuation")) \
        == "Punctuation: add “,”."
    assert sapling.describe(_edit("very", "", "R:ADV", "Word Choice")) \
        == "Adverb: remove “very”."


def test_describe_prefers_a_known_category_over_the_general_bucket():
    # "R:VERB:TENSE" resolves to the specific label even when the general
    # bucket is a vaguer "Grammar".
    d = sapling.describe(_edit("was", "were", "R:VERB:TENSE", "Grammar"))
    assert d == "Verb tense: “was” → “were”."


def test_describe_falls_back_to_the_general_bucket_then_grammar():
    assert sapling.describe(_edit("x", "y", "R:WEIRD", "Style")) \
        == "Style: “x” → “y”."
    assert sapling.describe(_edit("x", "y", "", "")) == "Grammar: “x” → “y”."


def test_pass_findings_carry_explanations_and_are_not_silent_by_default(monkeypatch):
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    cfg = _cfg_with_sapling(True)
    cfg.sapling.confirm = False
    findings = _sapling_findings(cfg, _prepared("The cat sat on teh mat."))
    assert findings[0].explanation == "Spelling: “teh” → “the”."
    assert findings[0].silent is False


def test_comments_off_makes_pass_findings_silent(monkeypatch):
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    cfg = _cfg_with_sapling(True)
    cfg.sapling.confirm = False
    cfg.sapling.comments = False
    findings = _sapling_findings(cfg, _prepared("The cat sat on teh mat."))
    assert findings[0].silent is True
    # The explanation is still built — it just won't be hung in the margin.
    assert findings[0].explanation == "Spelling: “teh” → “the”."


def test_silent_findings_get_no_margin_comment(tmp_path, monkeypatch):
    """End to end: with sapling.comments off, the applied tracked change carries
    no Word comment, while the same run with it on writes one."""
    from docproof.models import Usage
    from docproof.pipeline import finish, prepare
    from docproof.utils.xml_helpers import DocxPackage

    def comment_count(out_path):
        from docproof.utils.xml_helpers import qn
        pkg = DocxPackage(out_path)
        if not pkg.has("word/comments.xml"):
            return 0
        return len(list(pkg.tree("word/comments.xml").iter(qn("w:comment"))))

    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    src = _make_docx(tmp_path / "book.docx", "The cat sat on teh mat.")

    cfg = _cfg_with_sapling(True)
    cfg.sapling.confirm = False        # blind fold-in; no LLM valve in this test
    cfg.audit = "off"
    prepared = prepare(cfg, src, "config/error_types")
    on = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "on",
                source_path=src)
    assert comment_count(on.reviewed_path) >= 1

    cfg.sapling.comments = False
    prepared = prepare(cfg, src, "config/error_types")
    off = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "off",
                 source_path=src)
    assert comment_count(off.reviewed_path) == 0


# -- to_candidates: filtering before the valve --------------------------------

def _para_edit(original, replacement, error_type="R:SPELL", general="Spelling",
               start=0):
    return sapling.ParaEdit(
        para_id="body-0", start=start, end=start + len(original),
        original=original, replacement=replacement, error_type=error_type,
        general_error_type=general)


def test_to_candidates_maps_edit_and_carries_describe_note():
    cands = sapling.to_candidates([_para_edit("teh", "the")],
                                  {"body-0": "teh cat."})
    assert len(cands) == 1
    c = cands[0]
    assert (c.para_id, c.start, c.end, c.original, c.replacement) \
        == ("body-0", 0, 3, "teh", "the")
    assert c.note == "Spelling: “teh” → “the”."   # rides into the margin


def test_to_candidates_lexicon_suppresses_a_name_misspelling():
    # A spelling flag on one of the author's own protected words is a name, not
    # an error — dropped before the model, exactly as in the LanguageTool pass.
    cands = sapling.to_candidates(
        [_para_edit("Aeryn", "Aaron")], {"body-0": "Aeryn ran."},
        lexicon=["Aeryn"])
    assert cands == []


def test_to_candidates_disabled_error_type_drops_a_class():
    e = _para_edit("  ", " ", error_type="R:ORTH", general="Orthography", start=1)
    cands = sapling.to_candidates([e], {"body-0": "a  b"},
                                  disabled_error_types=["ORTH"])
    assert cands == []


def test_to_candidates_drops_offset_drift_and_no_ops():
    drift = _para_edit("teh", "the")            # text says "the", not "teh"
    noop = _para_edit("the", "the")             # nothing to change
    cands = sapling.to_candidates([drift, noop], {"body-0": "the cat."})
    assert cands == []


# -- the confirm valve: an LLM accepts/rejects each Sapling edit ---------------

def _fake_provider(*batches):
    """A FakeProvider that returns one verdicts payload per confirm batch."""
    from docproof.providers import ProviderResult
    from .fakes import FakeProvider
    return FakeProvider([ProviderResult(parsed={"verdicts": list(b)})
                         for b in batches])


def test_confirm_valve_accepts_a_real_error(monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    prov = _fake_provider([{"index": 1, "is_error": True, "confidence": "high"}])
    monkeypatch.setattr("docproof.providers.build_provider", lambda *a, **k: prov)
    findings = _sapling_findings(_cfg_with_sapling(True),
                                 _prepared("The cat sat on teh mat."), Usage())
    assert len(findings) == 1
    f = findings[0]
    assert f.error_type == "sapling"
    assert f.corrected_text == "The cat sat on the mat."
    assert f.explanation == "Spelling: “teh” → “the”."   # Sapling's own line
    assert f.silent is False and f.force_query is False


def test_confirm_valve_keeps_voice_and_logs_the_rejection(tmp_path, monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    # The LLM rules the edit would touch something deliberate → KEEP the original.
    prov = _fake_provider([{"index": 1, "is_error": False, "confidence": "high"}])
    monkeypatch.setattr("docproof.providers.build_provider", lambda *a, **k: prov)
    findings = _sapling_findings(_cfg_with_sapling(True),
                                 _prepared("The cat sat on teh mat."), Usage(),
                                 out_dir=tmp_path)
    assert findings == []
    import json
    logged = json.loads((tmp_path / "sapling_rejected.json").read_text("utf-8"))
    assert logged and logged[0]["original"] == "teh"


def test_confirm_softer_confidence_becomes_a_query_not_a_silent_edit(monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    prov = _fake_provider([{"index": 1, "is_error": True, "confidence": "medium"}])
    monkeypatch.setattr("docproof.providers.build_provider", lambda *a, **k: prov)
    cfg = _cfg_with_sapling(True)                 # edit_confidence defaults high
    findings = _sapling_findings(cfg, _prepared("The cat sat on teh mat."),
                                 Usage())
    assert len(findings) == 1 and findings[0].force_query is True


def test_confirm_path_respects_comments_toggle(monkeypatch):
    from docproof.models import Usage
    from docproof.pipeline import _sapling_findings
    monkeypatch.setenv("SAPLING_API_KEY", "k")
    monkeypatch.setattr(sapling.httpx, "post", _spellfix_post("teh", "the"))
    prov = _fake_provider([{"index": 1, "is_error": True, "confidence": "high"}])
    monkeypatch.setattr("docproof.providers.build_provider", lambda *a, **k: prov)
    cfg = _cfg_with_sapling(True)
    cfg.sapling.comments = False
    findings = _sapling_findings(cfg, _prepared("The cat sat on teh mat."),
                                 Usage())
    assert len(findings) == 1 and findings[0].silent is True


def test_sapling_comments_feature_toggle_round_trips():
    from app import features as featureslib
    from docproof.config import load_config
    cfg = load_config("config/default.yaml")
    assert cfg.sapling.comments is True
    featureslib.apply_features(cfg, {"sapling_comments": False})
    assert cfg.sapling.comments is False
    row = {f["id"]: f
           for f in featureslib.feature_catalog(cfg)}["sapling_comments"]
    assert row["default"] is False and row["group"] == "output"
