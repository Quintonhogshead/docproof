"""The quest skin generator: sampling, the one cheap call, the fallback that
keeps the page rendering, and the upload endpoint. No test reaches a vendor."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from docproof.providers import ProviderResult
from docproof.quest import DEFAULT_SKIN, generate_skin, price_band
from docproof.quest.skin import (MIDDLE_WORDS, OPENING_WORDS, sample_text)
from docproof.quest.sweep import (LANES, MAX_CATCHES, SWEEP_WORDS, iter_sweep,
                                  run_lane, sweep, sweep_sample)

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
    # Names are permanent: whatever the model wrote, the party keeps its own.
    assert result.skin.pip.alias == "Pip"
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


def test_generate_skin_names_are_permanent(tmp_path):
    # The model dressed the whole party in invented names; every one is
    # overwritten with the true first name. Job and look survive untouched.
    provider = FakeProvider(
        results=[ProviderResult(parsed=_skin_payload(), usage=USAGE)])
    result = generate_skin(_write_txt(tmp_path), provider)
    for key, name in [("pip", "Pip"), ("bram", "Bram"), ("maple", "Maple"),
                      ("cinder", "Cinder"), ("sage", "Sage"), ("lark", "Lark")]:
        character = getattr(result.skin, key)
        assert character.alias == name
        assert character.job == "Does the thing, stylishly."
    assert result.alias_collisions == ()


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
        assert data["skin"]["pip"]["alias"] == "Pip"
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


class _SweepProvider:
    """Answers all three questions the sweep asks, by schema: the skin call
    gets a costume, every lane call gets one scrap quoting the member's own
    name (so the verbatim gate passes when the sample contains the names), and
    every judge call keeps everything unless told to drop that member. Thread-
    safe enough for the parallel sweep — it answers by what it was asked."""

    name = "fake-sweep"

    def __init__(self, *, empty_lanes=(), fail_lanes=(), judge_drops=()):
        self.empty_lanes = set(empty_lanes)   # keyed by member name in the prompt
        self.fail_lanes = set(fail_lanes)
        self.judge_drops = set(judge_drops)
        self.lane_calls = 0
        self.judge_calls = 0

    def complete_structured(self, *, schema_name, system, user="", **kwargs):
        if schema_name == "quest_skin":
            return ProviderResult(parsed=_skin_payload(), usage=USAGE)
        if schema_name == "quest_sweep_judge":
            self.judge_calls += 1
            who = next((n for _, n, *_ in LANES
                        if f"proposed by {n}," in system), "?")
            n = user.count("before:")
            keep = [who not in self.judge_drops] * n
            return ProviderResult(parsed={"keep": keep}, usage=USAGE)
        self.lane_calls += 1
        who = next((n for _, n, *_ in LANES if f"You are {n}," in system), "?")
        if who in self.fail_lanes:
            return ProviderResult(stop_reason="error", error="lane boom",
                                  usage=USAGE)
        if who in self.empty_lanes:
            return ProviderResult(parsed={"catches": []}, usage=USAGE)
        catches = [{"before": who, "after": f"{who}-after",
                    "why": "test catch"}]
        if who == "Pip":
            # Pip promises two — hand her a second real quote from the sample
            # so the generic tests never trip her keep-looking retries.
            catches.append({"before": "rode out", "after": "rode home",
                            "why": "second catch"})
        return ProviderResult(parsed={"catches": catches}, usage=USAGE)


# A sample every fake lane's catch quotes honestly: it contains each member's
# name, so the verbatim gate passes and the cross-lane dedupe sees six
# distinct snags.
_SWEEP_SAMPLE = "Pip and Bram and Maple and Cinder and Sage and Lark rode out."


def test_sweep_sample_is_the_first_pages_only():
    text = " ".join(f"w{i}" for i in range(SWEEP_WORDS + 500))
    words = sweep_sample(text).split()
    assert len(words) == SWEEP_WORDS
    assert words[0] == "w0" and words[-1] == f"w{SWEEP_WORDS - 1}"


def test_run_lane_swallows_a_failed_call():
    result = run_lane("some prose", LANES[0],
                      FakeProvider(results=[ProviderResult(
                          stop_reason="error", error="down", usage=USAGE)]))
    assert result.key == "pip" and result.catches == [] and result.error


def test_run_lane_caps_catches_at_three():
    over = {"catches": [{"before": "wet prose", "after": str(i), "why": "x"}
                        for i in range(5)]}
    keep = {"keep": [True, True, True]}
    result = run_lane("very wet prose indeed", LANES[0],
                      FakeProvider(results=[
                          ProviderResult(parsed=over, usage=USAGE),
                          ProviderResult(parsed=keep, usage=USAGE)]))
    assert len(result.catches) == MAX_CATCHES


def test_run_lane_verbatim_gate_drops_invented_quotes():
    """A catch whose "before" is not the author's own words never reaches the
    page — and with nothing left, no judge call is spent."""
    fake = FakeProvider(results=[ProviderResult(parsed={"catches": [
        {"before": "teh watchtower", "after": "the watchtower",
         "why": "invented"}]}, usage=USAGE)])
    result = run_lane("A clean page about a lighthouse.", LANES[0], fake)
    assert result.catches == [] and result.error is None
    assert len(fake.calls) == 1   # the lane read; no judge for zero catches


def test_run_lane_judge_drops_bad_fixes_and_failure_keeps():
    """The judge's drop verdicts land; a judge that dies keeps the
    verbatim-checked catches instead of sinking the lane."""
    catches = {"catches": [
        {"before": "wet prose", "after": "dry prose", "why": "fine"},
        {"before": "very wet", "after": "worse", "why": "bad fix"}]}
    judged = run_lane("very wet prose indeed", LANES[0], FakeProvider(results=[
        ProviderResult(parsed=catches, usage=USAGE),
        ProviderResult(parsed={"keep": [True, False]}, usage=USAGE)]))
    assert [c["before"] for c in judged.catches] == ["wet prose"]
    unjudged = run_lane("very wet prose indeed", LANES[0], FakeProvider(results=[
        ProviderResult(parsed=catches, usage=USAGE),
        ProviderResult(stop_reason="error", error="judge down", usage=USAGE)]))
    assert len(unjudged.catches) == 2


def test_sweep_runs_every_lane_once_and_keys_each():
    provider = _SweepProvider(empty_lanes={"Sage"}, fail_lanes={"Lark"})
    results = sweep(_SWEEP_SAMPLE, provider)
    assert [r.key for r in results] == [key for key, *_ in LANES]
    assert provider.lane_calls == len(LANES)
    by_key = {r.key: r for r in results}
    assert by_key["pip"].catches[0]["before"] == "Pip"
    assert by_key["sage"].catches == []           # empty is honest, not an error
    assert by_key["lark"].catches == [] and by_key["lark"].error


def test_sweep_judge_drop_reaches_the_result():
    provider = _SweepProvider(judge_drops={"Cinder"})
    by_key = {r.key: r for r in sweep(_SWEEP_SAMPLE, provider)}
    assert by_key["cinder"].catches == []
    assert by_key["pip"].catches           # everyone else keeps theirs
    assert provider.judge_calls == len(LANES)


def test_iter_sweep_yields_every_lane():
    keys = {r.key for r in iter_sweep(_SWEEP_SAMPLE, _SweepProvider())}
    assert keys == {key for key, *_ in LANES}


def _sse_events(text):
    """Parse a Server-Sent Event body into (event, json) pairs."""
    import json as _json
    out = []
    for frame in text.strip().split("\n\n"):
        if not frame.strip():
            continue
        ev, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = _json.loads(line[6:])
        out.append((ev, data))
    return out


def test_sweep_endpoint_streams_skin_then_lanes(tmp_path, monkeypatch):
    provider = _SweepProvider(empty_lanes={"Maple"})
    monkeypatch.setattr("app.routes.quest.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.routes.quest.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.routes.quest._sweep_cache", {})
    app = create_app(tmp_path, start_runner=False)
    body = (_SWEEP_SAMPLE + " It rained on the city. ").encode() * 30
    with TestClient(app) as client:
        resp = client.post("/api/quest/sweep",
                           files={"file": ("book.txt", body, "text/plain")})
        assert resp.status_code == 200
        events = _sse_events(resp.text)
        kinds = [e for e, _ in events]
        assert kinds[0] == "skin" and kinds[-1] == "done"
        assert kinds.count("lane") == len(LANES)
        skin = next(d for e, d in events if e == "skin")
        # Names are permanent: the model's "Slim" never reaches the wire.
        assert skin["skin"]["pip"]["alias"] == "Pip"
        lanes = {d["key"]: d for e, d in events if e == "lane"}
        assert set(lanes) == {key for key, *_ in LANES}
        assert lanes["maple"]["catches"] == []
        assert lanes["bram"]["catches"][0]["after"] == "Bram-after"
        done = events[-1][1]
        assert done["sweep_cost"] > 0

        # Same bytes again: replayed from the cache, no new lane calls.
        before = provider.lane_calls
        again = client.post("/api/quest/sweep",
                            files={"file": ("book.txt", body, "text/plain")})
        replay = _sse_events(again.text)
        assert next(d for e, d in replay if e == "skin")["cached"] is True
        assert provider.lane_calls == before


def test_sweep_endpoint_reports_unreadable_as_error_event(tmp_path, monkeypatch):
    provider = _SweepProvider()
    monkeypatch.setattr("app.routes.quest.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.routes.quest.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.routes.quest._sweep_cache", {})
    app = create_app(tmp_path, start_runner=False)
    with TestClient(app) as client:
        # Whitespace-only text: reads clean, but there is nothing to sweep.
        resp = client.post("/api/quest/sweep",
                           files={"file": ("empty.txt", b"   \n  \n",
                                           "text/plain")})
        # An empty upload is refused before the stream (plain 400); a file that
        # is only whitespace slips past the size gate and errors in-stream.
        if resp.status_code == 200:
            events = _sse_events(resp.text)
            assert events and events[0][0] == "error"
        else:
            assert resp.status_code == 400


def test_borrowed_names_cannot_reach_the_page(tmp_path):
    # The book has an Ida Pomeroy and the model names Maple after her; the
    # permanent-names rule flattens it before anything renders.
    path = _write_txt(tmp_path, extra=" Ida Pomeroy ran the knitting circle.")
    payload = _skin_payload(maple={"alias": "Maple Pomeroy",
                                   "job": "Keeps the registry.",
                                   "look": "Precise, bespectacled."})
    provider = FakeProvider(
        results=[ProviderResult(parsed=payload, usage=USAGE)])
    result = generate_skin(path, provider)
    assert result.skin.maple.alias == "Maple"
    assert result.alias_collisions == ()


def test_first_look_dedupes_repeat_snags_across_lanes():
    """Two members quoting the same snag show it once — keyed on the author's
    words, tolerant of quote marks, case, and spacing."""
    from app.routes.quest import _dedupe_catches

    seen: set[str] = set()
    first = _dedupe_catches([
        {"before": "teh door", "after": "the door", "why": "typo"},
        {"before": '"Teh  door"', "after": "fix it", "why": "typo again"},
    ], seen)
    assert len(first) == 1
    later = _dedupe_catches([
        {"before": "TEH DOOR.", "after": "the door", "why": "typo"},
        {"before": "a fresh snag", "after": "fixed", "why": "new"},
        {"before": "", "after": "x", "why": "empty never shown"},
    ], seen)
    assert [c["before"] for c in later] == ["a fresh snag"]


def test_first_look_dedupes_containment_between_lanes():
    """Pip pins up the doubled word; Bram quoting the whole sentence around it
    is the same snag to the reader and stays down. Short keys ("a") only ever
    match exactly."""
    from app.routes.quest import _dedupe_catches

    seen: set[str] = set()
    _dedupe_catches([{"before": "and and", "after": "and", "why": "doubled"}],
                    seen)
    later = _dedupe_catches([
        {"before": "The rain stopped, and and the river rose.",
         "after": "…and the river rose.", "why": "doubled word"},
        {"before": "a", "after": "an", "why": "article"},
    ], seen)
    assert [c["before"] for c in later] == ["a"]


def test_pip_keeps_looking_until_she_has_two():
    """A one-catch first pass triggers a nudged re-read; the passes merge
    (deduped) until Pip has her promised two."""
    from docproof.quest.sweep import run_pip

    text = "very wet prose indeed, and the rain kept on falling all night"
    first = {"catches": [{"before": "wet prose", "after": "dry prose",
                          "why": "typo"}]}
    second = {"catches": [
        {"before": "wet prose", "after": "dry prose", "why": "typo"},
        {"before": "kept on falling", "after": "kept falling", "why": "filler"}]}
    fake = FakeProvider(results=[
        ProviderResult(parsed=first, usage=USAGE),
        ProviderResult(parsed={"keep": [True]}, usage=USAGE),      # judge 1
        ProviderResult(parsed=second, usage=USAGE),
        ProviderResult(parsed={"keep": [True, True]}, usage=USAGE)])  # judge 2
    result = run_pip(text, fake)
    assert [c["before"] for c in result.catches] == ["wet prose",
                                                     "kept on falling"]
    assert result.error is None
    # The second lane read carried the keep-looking nudge; the first did not.
    lane_calls = [c for c in fake.calls
                  if c["schema_name"] == "quest_sweep_lane"]
    assert "found almost nothing" not in lane_calls[0]["system"]
    assert "found almost nothing" in lane_calls[1]["system"]


def test_pip_stops_rereading_when_the_text_runs_out():
    """On a short manuscript the sample cannot widen, so Pip gets exactly one
    nudged re-read — never an endless loop — and keeps what she found."""
    from docproof.quest.sweep import run_pip

    text = "very wet prose indeed"
    one = {"catches": [{"before": "wet prose", "after": "dry prose",
                        "why": "typo"}]}
    keep = {"keep": [True]}
    fake = FakeProvider(results=[
        ProviderResult(parsed=one, usage=USAGE), ProviderResult(parsed=keep, usage=USAGE),
        ProviderResult(parsed=one, usage=USAGE), ProviderResult(parsed=keep, usage=USAGE)])
    result = run_pip(text, fake)
    assert [c["before"] for c in result.catches] == ["wet prose"]
    lane_calls = [c for c in fake.calls
                  if c["schema_name"] == "quest_sweep_lane"]
    assert len(lane_calls) == 2
