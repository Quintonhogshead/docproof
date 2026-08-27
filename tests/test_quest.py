"""The quest skin generator: sampling, the one cheap call, the fallback that
keeps the page rendering, and the upload endpoint. No test reaches a vendor."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from docproof.providers import ProviderResult
from docproof.quest import DEFAULT_SKIN, generate_skin, price_band
from docproof.quest.skin import (MIDDLE_WORDS, OPENING_WORDS, sample_text)

from .fakes import FakeProvider, USAGE


def _character(alias: str) -> dict:
    return {"alias": alias, "job": "Does the thing, stylishly.", "look": "A memorable coat."}


def _skin_payload(**over) -> dict:
    d = {
        "book_title": "The Wet City", "genre": "noir thriller",
        "maturity": "standard", "palette": "rain", "language": "English",
        "is_fiction": True, "themes": ["rain", "lies"],
        "narration": "I read your opening. A dead man, a wet city.",
        "empty_party": "An empty office.",
        "empty_bench": "The whole crew is on the clock.",
        "signoff": "we'll leave word with your service (okay, an email).",
        "pip": _character("Slim"), "bram": _character("Sgt. Brammell"),
        "maple": _character("Records"), "cinder": _character("The Fixer"),
        "sage": _character("The Tail"), "lark": _character("Vel"),
    }
    d.update(over)
    return d


def _write_txt(tmp_path, words=200, name="book.txt", extra=""):
    path = tmp_path / name
    path.write_text("It rained on the city. " * (words // 5) + extra,
                    encoding="utf-8")
    return path


def test_price_band_edges():
    assert price_band(20_000) == 0.7
    assert price_band(60_000) == 1.0
    assert price_band(120_000) == 1.0
    assert price_band(120_001) == 1.5


def test_sample_text_adds_middle_slice_only_for_long_books():
    short = " ".join(f"w{i}" for i in range(100))
    assert "MIDDLE" not in sample_text(short)
    long = " ".join(f"w{i}" for i in range(OPENING_WORDS + MIDDLE_WORDS + 500))
    sampled = sample_text(long)
    assert "OPENING SAMPLE:" in sampled and "MIDDLE OF THE BOOK:" in sampled
    # The middle slice starts at the halfway word, not right after the opening.
    assert f"w{(OPENING_WORDS + MIDDLE_WORDS + 500) // 2}" in sampled


def test_generate_skin_happy_path(tmp_path):
    provider = FakeProvider(
        results=[ProviderResult(parsed=_skin_payload(), usage=USAGE)])
    result = generate_skin(_write_txt(tmp_path), provider)
    assert not result.fallback and result.error is None
    assert result.skin.genre == "noir thriller"
    assert result.skin.pip.alias == "Slim"
    assert result.band == 0.7 and result.word_count > 0
    assert result.cost is not None and result.cost > 0
    # The call went out with the strict schema and the sample, not the raw file.
    call = provider.calls[0]
    assert call["schema_name"] == "quest_skin"
    assert "OPENING SAMPLE:" in call["user"]


def test_generate_skin_falls_back_on_junk(tmp_path):
    provider = FakeProvider(
        results=[ProviderResult(parsed={"nope": 1}, usage=USAGE)])
    result = generate_skin(_write_txt(tmp_path), provider)
    assert result.fallback and "schema" in (result.error or "")
    assert result.skin == DEFAULT_SKIN          # the page still renders


def test_generate_skin_flags_alias_collisions(tmp_path):
    # "Slim" appears in the book itself — likely a real character's name.
    path = _write_txt(tmp_path, extra=" Slim lit a cigarette.")
    provider = FakeProvider(
        results=[ProviderResult(parsed=_skin_payload(), usage=USAGE)])
    result = generate_skin(path, provider)
    assert result.alias_collisions == ("Slim",)


def test_skin_endpoint_round_trip(tmp_path, monkeypatch):
    provider = FakeProvider(
        results=[ProviderResult(parsed=_skin_payload(), usage=USAGE)])
    monkeypatch.setattr("app.routes.quest.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.routes.quest.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.routes.quest._cache", {})
    app = create_app(tmp_path, start_runner=False)
    body = b"It rained on the city. " * 50
    with TestClient(app) as client:
        resp = client.post("/api/quest/skin",
                           files={"file": ("book.txt", body, "text/plain")})
        assert resp.status_code == 200
        data = resp.json()
        assert data["skin"]["pip"]["alias"] == "Slim"
        assert data["band"] == 0.7 and not data["fallback"]
        assert data["cached"] is False

        # Same bytes again: answered from the cache, no second call.
        again = client.post("/api/quest/skin",
                            files={"file": ("book.txt", body, "text/plain")})
        assert again.json()["cached"] is True
        assert len(provider.calls) == 1

        bad = client.post("/api/quest/skin",
                          files={"file": ("book.pdf", b"x", "application/pdf")})
        assert bad.status_code == 400
        assert ".docx" in bad.json()["detail"]


def test_standalone_site_serves_page_and_rate_limits(monkeypatch):
    from app import quest_site

    provider = FakeProvider(
        results=[ProviderResult(parsed=_skin_payload(), usage=USAGE)])
    monkeypatch.setattr("app.routes.quest.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.routes.quest.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.routes.quest._cache", {})
    monkeypatch.setattr(quest_site, "PER_IP_LIMIT", 1)
    app = quest_site.create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").json()["ok"] is True
        page = client.get("/")
        assert page.status_code == 200 and "Galley" in page.text

        ok = client.post("/api/quest/skin",
                         files={"file": ("book.txt", b"words " * 40,
                                         "text/plain")})
        assert ok.status_code == 200

        # Second skin from the same IP inside the window: politely refused.
        limited = client.post("/api/quest/skin",
                              files={"file": ("other.txt", b"more words " * 40,
                                              "text/plain")})
        assert limited.status_code == 429
        assert "rest" in limited.json()["detail"]


def test_waitlist_signup_dedupes_and_validates(tmp_path, monkeypatch):
    from app import quest_site

    monkeypatch.setattr(quest_site, "WAITLIST_PATH",
                        str(tmp_path / "waitlist.jsonl"))
    app = quest_site.create_app()
    with TestClient(app) as client:
        ok = client.post("/api/quest/waitlist",
                         json={"email": "Author@Example.com"})
        assert ok.status_code == 200 and ok.json() == {"ok": True,
                                                       "already": False}
        # Same address, different case: already aboard, not re-written.
        again = client.post("/api/quest/waitlist",
                            json={"email": "author@example.com "})
        assert again.json() == {"ok": True, "already": True}
        assert (tmp_path / "waitlist.jsonl").read_text().count("\n") == 1

        bad = client.post("/api/quest/waitlist", json={"email": "not-an-email"})
        assert bad.status_code == 400

    # A fresh app rereads the file: dedupe survives restarts.
    with TestClient(quest_site.create_app()) as client:
        rejoin = client.post("/api/quest/waitlist",
                             json={"email": "author@example.com"})
        assert rejoin.json()["already"] is True


def test_papery_pages_serve(monkeypatch, tmp_path):
    from app import quest_site

    monkeypatch.setattr(quest_site, "WAITLIST_PATH",
                        str(tmp_path / "waitlist.jsonl"))
    with TestClient(quest_site.create_app()) as client:
        for path, marker in [("/", "Bring me your book"),
                             ("/quote", "Drop your manuscript"),
                             ("/party", "Meet the Party"),
                             ("/pricing", "Five ways"),
                             ("/customize", "The Party Assembles")]:
            resp = client.get(path)
            assert resp.status_code == 200 and marker in resp.text, path
        assert client.get("/assets/sc-shared.css").status_code == 200
        assert client.get("/assets/sc-shared.js").status_code == 200


def test_collision_catches_borrowed_surnames(tmp_path):
    # The book has an Ida Pomeroy; the model names Maple "Maple Pomeroy".
    path = _write_txt(tmp_path, extra=" Ida Pomeroy ran the knitting circle.")
    payload = _skin_payload(maple={"alias": "Maple Pomeroy",
                                   "job": "Keeps the registry.",
                                   "look": "Precise, bespectacled."})
    provider = FakeProvider(
        results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = generate_skin(path, provider)
    assert "Maple Pomeroy" in result.alias_collisions
