"""Jev's brute-force sites: deterministic generation, thresholds, and a screen
that still decides. No test here may reach TypeSafe; every client is injected."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from galley.fixed_jev import (build_requests, collect_jev_candidates, comma_delete_sites,
                              comma_insert_sites, confusion_map, rows_from_answers,
                              sentence_around, spelling_sites)
from galley.fixed_policy import JEV_THRESHOLDS
from galley.jev import JevLedger, JevUnavailable
from test_galley_fixed_workflow import Readers, make_book  # noqa: F401  (fixture)


# ---------------------------------------------------------------- fake transport

class FakeAnswer:
    """A Choice answer in the shape galley.jev reads (never a Noul)."""
    def __init__(self, probabilities, confidence=0.8):
        self.probabilities = dict(probabilities)
        self.choice = max(self.probabilities, key=lambda k: self.probabilities[k])
        self.confidence = confidence


class FakeClient:
    """Answers with `decide(state, key, question)` -> {option: probability}."""
    def __init__(self, decide, *, fail_on=None):
        self.decide = decide
        self.fail_on = fail_on
        self.requests = []

    def system_one(self, *, state, questions, model):
        self.requests.append({"state": state, "questions": sorted(questions), "model": model})
        if self.fail_on is not None and self.fail_on(state):
            raise JevUnavailable("Jev refused the request (402): out of credits")
        answers = {key: FakeAnswer(self.decide(state, key, question))
                   for key, question in questions.items()}
        return SimpleNamespace(answers=answers, model=model,
                               usage=SimpleNamespace(input_tokens=100_000, output_tokens=0))


def ledger_for(tmp_path, decide, *, fail_on=None, name="jev"):
    client = FakeClient(decide, fail_on=fail_on)
    return JevLedger(tmp_path / name, model="jev-test", workers=2,
                     client_factory=lambda: client), client


def comma_decider(*, wanted, probability=0.9, otherwise=0.01):
    """`wanted` is a substring of the with_comma variant that should score high."""
    def decide(state, key, question):
        variant = state["variants"][key]
        name = "with_comma" if "with_comma" in variant else "without_comma"
        high = wanted in variant[name]
        p = probability if high else otherwise
        return {name: p, "original": 1 - p - 0.005, "either": 0.005}
    return decide


# ---------------------------------------------------------------- site generators

def test_comma_insert_sites_take_word_boundaries_only():
    text = 'He ran — fast, and “quiet” then stopped.'
    offsets = [s["pos"] for s in comma_insert_sites(text)]
    # A boundary after a letter or a closing quote, never one after a comma or
    # a dash, and never one whose next character opens a dash.
    assert [text[o - 1] for o in offsets] == ["e", "d", "”", "n"]
    assert all(text[o] == " " for o in offsets)
    assert comma_insert_sites("one") == [] and comma_insert_sites("") == []
    assert comma_insert_sites("cat ") == []         # a trailing space has no successor
    assert [s["variants"] for s in comma_insert_sites("cat sat down")] == [
        {"with_comma": ","}, {"with_comma": ","}]


def test_comma_insert_site_carries_an_empty_span_at_the_boundary():
    site = comma_insert_sites("the cat sat")[0]
    assert site["pos"] == site["end"] == 3 and site["kind"] == "comma_insert"


def test_comma_delete_sites_skip_numbers_and_edges():
    text = "We paid 1,200 dollars, then left, quietly,"
    sites = comma_delete_sites(text)
    # The thousands comma and the paragraph's last character are not sites.
    assert [s["pos"] for s in sites] == [text.index(" then") - 1, text.index(" quietly") - 1]
    assert all(s["variants"] == {"without_comma": ""} and s["end"] == s["pos"] + 1 for s in sites)
    assert comma_delete_sites("1,000,000") == []


def test_confusion_map_and_spelling_sites(tmp_path):
    path = tmp_path / "confusion_sets.txt"
    # LanguageTool's own format: <word1>; <word2>; <factor>   # optional comment
    path.write_text("their; there; they're; 4\nthen; than; 10  # common\n# comment only\n", "utf-8")
    confusion = confusion_map(path)
    assert confusion["their"] == {"there", "they're"} and confusion["than"] == {"then"}
    assert confusion_map(tmp_path / "absent.txt") == {}

    sites = spelling_sites("Then they walked over their bridge", confusion)
    assert [(s["pos"], sorted(s["variants"])) for s in sites] == [
        (0, ["Than"]), (len("Then they walked over "), ["there", "they're"])]
    # The manuscript's own capitalization travels with the alternate.
    assert sites[0]["variants"]["Than"] == "Than"


def test_sentence_around_windows_only_long_paragraphs():
    short = "He left. She stayed."
    assert sentence_around(short, 12) == (0, len(short))
    long_text = ("The harbor was quiet that morning. " * 20) + "Mr. Poole waited there. He left."
    start, end = sentence_around(long_text, len(long_text) - 5)
    assert long_text[start:end] == "He left."
    # An abbreviation does not end a sentence.
    inside = long_text.index("Poole")
    start, end = sentence_around(long_text, inside)
    assert long_text[start:end].startswith("Mr. Poole")


# ---------------------------------------------------------------- requests

def test_build_requests_batches_thirty_variants_of_one_paragraph():
    text = " ".join(f"word{n}" for n in range(40))
    sites = comma_insert_sites(text)
    jobs, index = build_requests("comma_insert", [("body-0000", text, sites)])
    assert len(sites) == 39 and [len(entries) for entries in index] == [30, 9]
    state, questions = jobs[0]
    assert state["paragraph"] == text and sorted(questions) == sorted(state["variants"])
    assert list(state["variants"]) == [f"c{n + 1:02d}" for n in range(30)]
    assert state["house_rule"].startswith("Chicago Manual of Style comma rules")
    assert state["variants"]["c01"]["with_comma"].startswith("word0, word1")
    assert questions["c01"]["type"] == "choice"
    assert "`variants.c01.original`" in questions["c01"]["instructions"]
    assert "`variants.c01.with_comma`" in questions["c01"]["instructions"]
    assert set(questions["c01"]["criteria"]) == {"with_comma", "original", "either"}


def test_build_requests_spelling_question_names_every_alternate():
    text = "They walked over their bridge"
    sites = spelling_sites(text, {"their": {"there", "they're"}})
    jobs, _ = build_requests("spelling", [("body-0000", text, sites)])
    state, questions = jobs[0]
    assert state["variants"]["c01"]["there"] == "They walked over there bridge"
    assert "`variants.c01.there`" in questions["c01"]["instructions"]
    assert questions["c01"]["criteria"]["there"] == "The writer meant 'there'"
    assert state["house_rule"].startswith("Standard American English word choice")


# ---------------------------------------------------------------- answers to rows

def _answers(kind, text, probabilities_by_site):
    """Run the pure pipeline over one paragraph with given probabilities."""
    from galley.fixed_jev import generate_sites
    sites = generate_sites(kind, text, confusion={"their": {"there"}, "then": {"than"}})
    jobs, index = build_requests(kind, [("body-0000", text, sites)])
    answers = []
    for entries in index:
        answers.append({key: {"type": "choice", "confidence": 0.7, "choice": "original",
                              "probabilities": probabilities_by_site(site)}
                        for key, _pid, site in entries})
    return rows_from_answers(kind, answers, index, {"body-0000": text})


def test_comma_insert_rows_keep_sites_at_or_above_the_threshold():
    text = "He left and she stayed and he waited"
    assert JEV_THRESHOLDS["comma_insert"] == 0.30

    def probabilities(site):
        p = 0.31 if site["pos"] == text.index(" and she") else 0.29
        return {"with_comma": p, "original": 1 - p, "either": 0.0}

    rows = _answers("comma_insert", text, probabilities)
    assert len(rows) == 1
    row = rows[0]
    assert row["quote"] == "left and" and row["replacement"] == "left, and"
    assert row["category"] == "punctuation" and row["source"] == "jev" and row["occurrence"] == 1
    assert row["jev"]["kind"] == "comma_insert" and row["jev"]["probability"] == 0.31
    assert row["jev"]["threshold"] == 0.30 and row["jev"]["variant"] == "with_comma"
    assert "Jev p=0.31" in row["reason"]


def test_comma_delete_rows_need_a_higher_threshold_than_insertions():
    text = "He left, and she stayed, quietly there"
    assert JEV_THRESHOLDS["comma_delete"] == 0.50
    rows = _answers("comma_delete", text,
                    lambda site: {"without_comma": 0.49, "original": 0.51, "either": 0.0})
    assert rows == []
    rows = _answers("comma_delete", text,
                    lambda site: {"without_comma": 0.5, "original": 0.5, "either": 0.0})
    assert [r["quote"] for r in rows] == ["left, and", "stayed, quietly"]
    assert [r["replacement"] for r in rows] == ["left and", "stayed quietly"]
    assert all(r["category"] == "punctuation" for r in rows)


def test_spelling_rows_take_the_best_alternate_above_nine_tenths():
    text = "They walked over their bridge then stopped"
    assert JEV_THRESHOLDS["spelling"] == 0.90
    rows = _answers("spelling", text,
                    lambda site: {"original": 0.11, "there": 0.89, "than": 0.89})
    assert rows == []
    rows = _answers("spelling", text,
                    lambda site: {"original": 0.05, "there": 0.95, "than": 0.95})
    assert [(r["quote"], r["replacement"]) for r in rows] == [
        ("their bridge", "there bridge"), ("then stopped", "than stopped")]
    assert [r["category"] for r in rows] == ["spelling", "spelling"]
    assert rows[0]["jev"]["variant"] == "there" and rows[0]["jev"]["probability"] == 0.95


def test_rows_anchor_repeated_text_by_occurrence():
    text = "He left and she left and he waited"
    rows = _answers("comma_insert", text,
                    lambda site: {"with_comma": 0.9 if site["pos"] == text.rindex("left") + 4 else 0.0,
                                  "original": 0.1, "either": 0.0})
    assert len(rows) == 1 and rows[0]["quote"] == "left and" and rows[0]["occurrence"] == 2
    # The quote/occurrence pair locates the site the way the workflow does.
    from galley.fixed_workflow import _locate
    low, _high = _locate(text, rows[0]["quote"], rows[0]["occurrence"])
    assert low == text.rindex("left")


def test_rows_without_an_answer_are_not_proposed():
    text = "He left and she stayed"
    sites = comma_insert_sites(text)
    _jobs, index = build_requests("comma_insert", [("body-0000", text, sites)])
    assert rows_from_answers("comma_insert", [{}], index, {"body-0000": text}) == []
    assert rows_from_answers("comma_insert", [None], index, {"body-0000": text}) == []


# ---------------------------------------------------------------- the lane

@pytest.fixture
def no_confusion_sets(monkeypatch, tmp_path):
    """A machine without the pinned LanguageTool distribution."""
    monkeypatch.setenv("GALLEY_LANGUAGETOOL_HOME", str(tmp_path / "absent"))
    return tmp_path


def test_collect_asks_only_about_prose_and_receipts_every_answer(tmp_path, no_confusion_sets):
    texts = {"body-0000": "He left and she stayed", "body-0001": "a poem, unpunctuated",
             "body-0002": "   "}
    ledger, client = ledger_for(tmp_path, comma_decider(wanted="left, and"))
    rows, evidence = collect_jev_candidates(
        None, texts, tmp_path / "jev", identity={"source_sha256": "abc"},
        poetry_ids={"body-0001"}, ledger=ledger)

    assert [r["para_id"] for r in rows] == ["body-0000"]
    assert rows[0]["replacement"] == "left, and"
    assert all("body-0001" not in json.dumps(request["state"]) for request in client.requests)
    assert evidence["generated"]["comma_insert"] == 4 and evidence["kept"]["comma_insert"] == 1
    assert evidence["generated"]["spelling"] == 0 and evidence["kept"]["comma_delete"] == 0
    assert any("confusion sets" in note for note in evidence["notes"])
    assert evidence["paragraphs"] == 1 and evidence["thresholds"] == JEV_THRESHOLDS
    assert evidence["usage"]["calls"] == 1 and evidence["usage"]["input_tokens"] == 100_000
    assert evidence["usage"]["usd"] == pytest.approx(0.0042)
    receipts = sorted((tmp_path / "jev").glob("*/*.json"))
    assert [p.parent.name for p in receipts] == ["typed_comma_insert"]

    # A resumed lane replays its receipts: same rows, same recorded usage, no
    # second request to the service.
    again, evidence_again = collect_jev_candidates(
        None, texts, tmp_path / "jev", identity={"source_sha256": "abc"},
        poetry_ids={"body-0001"}, ledger=ledger)
    assert again == rows and evidence_again == evidence
    assert len(client.requests) == 1


def test_collect_keeps_what_it_judged_before_the_lane_refused(tmp_path, no_confusion_sets):
    texts = {"body-0000": "He left and she stayed, quietly there"}
    ledger, client = ledger_for(
        tmp_path, comma_decider(wanted="left, and"),
        fail_on=lambda state: any("without_comma" in v for v in state["variants"].values()))
    rows, evidence = collect_jev_candidates(
        None, texts, tmp_path / "jev", identity={}, poetry_ids=set(), ledger=ledger)
    assert [r["replacement"] for r in rows] == ["left, and"]
    assert "402" in evidence["unavailable"] and evidence["kept"]["comma_delete"] == 0
    assert evidence["kept"]["comma_insert"] == 1


def test_workflow_skips_the_lane_without_a_key(make_book, tmp_path, monkeypatch):
    from galley import jev
    from galley.fixed_workflow import FixedWorkflow
    monkeypatch.setattr("galley.fixed_local.collect_local_candidates",
                        lambda *a, **k: ([], {"fixture": "local clean"}))
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates",
                        lambda *a, **k: ([], {"fixture": "completion clean"}))
    monkeypatch.setattr("galley.fixed_jev.collect_jev_candidates",
                        lambda *a, **k: pytest.fail("the lane asked Jev without a key"))
    assert not jev.enabled()                    # GALLEY_JEV=off from the suite's guard

    book = make_book("He left and she stayed. The boat arrived.")
    flow = FixedWorkflow(book, tmp_path / "run", calls=Readers())
    flow.run()
    skipped = [h for h in flow.history if h["stage"] == "typed_jev"]
    assert len(skipped) == 1 and "TYPESAFE_API_KEY" in skipped[0]["skipped"]
    evidence = json.loads((tmp_path / "run/stages/typed.json").read_text())["evidence"]
    assert "TYPESAFE_API_KEY" in evidence["jev"]["skipped"]
    assert flow.jev is None


def test_workflow_jev_row_is_screened_and_never_applied(make_book, tmp_path, monkeypatch):
    from galley.fixed_workflow import FixedWorkflow
    monkeypatch.setattr("galley.fixed_local.collect_local_candidates",
                        lambda *a, **k: ([], {"fixture": "local clean"}))
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates",
                        lambda *a, **k: ([], {"fixture": "completion clean"}))
    monkeypatch.setattr("galley.jev.enabled", lambda: True)
    monkeypatch.setattr("galley.fixed_jev.confusion_map", lambda *a, **k: {})

    screened = []

    def handler(stage, model, payload, kwargs):
        if stage == "typed_screen":
            screened.extend(payload["sites"])
            return {"decisions": [{"id": site["id"], "action": "drop", "replacement": "",
                                   "reason": "The comma is a preference here.",
                                   "question": "", "missing_knowledge": ""}
                                  for site in payload["sites"]]}

    readers = Readers(handler=handler)
    book = make_book("He left and she stayed. The boat arrived.")
    ledger, client = ledger_for(tmp_path / "run", comma_decider(wanted="left, and"))
    flow = FixedWorkflow(book, tmp_path / "run", calls=readers)
    flow.jev = ledger
    result = flow.run()

    assert client.requests, "the injected client answered the typed stage"
    # Sonnet and Luna screen the same packet independently; it is one site.
    sites = [s for s in screened if any(p["models"] == ["jev"] for p in s["proposals"])]
    assert len(sites) == 2 and sites[0] == sites[1]
    proposal = sites[0]["proposals"][0]
    assert proposal["category"] == "punctuation" and proposal["action"] == "edit"
    assert sites[0]["before"] == "" and proposal["replacement"] == ","
    # Screened, not applied: one model's proposal never auto-accepts, and this
    # screen dropped it.
    assert list(result["accepted"].values()) == ["He left and she stayed. The boat arrived."]
    evidence = json.loads((tmp_path / "run/stages/typed.json").read_text())["evidence"]
    assert evidence["jev"]["kept"]["comma_insert"] == 1
    assert evidence["jev"]["usage"]["calls"] == 1
    candidate = next(c for c in evidence["candidates"] if c["models"] == ["jev"])
    assert candidate["jev"]["kind"] == "comma_insert" and candidate["jev"]["probability"] == 0.9
    assert candidate["replacement"] == "," and candidate["category"] == "punctuation"
    screened_history = [h for h in flow.history if h["stage"] == "typed_screened"
                        and h["site"]["proposals"][0]["models"] == ["jev"]]
    assert len(screened_history) == 1 and screened_history[0]["decision"]["action"] == "drop"


def test_timeline_prices_the_jev_lane(tmp_path, no_confusion_sets):
    from galley import fixed_timeline
    run = tmp_path / "run"
    (run / "calls" / "calls").mkdir(parents=True)
    ledger, _client = ledger_for(run, comma_decider(wanted="left, and"), name="jev")
    collect_jev_candidates(None, {"body-0000": "He left and she stayed"}, run / "jev",
                           identity={}, poetry_ids=set(), ledger=ledger)
    report = fixed_timeline.summarize(run)
    row = next(s for s in report["stages"] if s["stage"] == "jev")
    assert row["attempts"] == 1 and row["tokens"]["input_tokens"] == 100_000
    assert row["api_usd"] == pytest.approx(0.0042)
    assert report["run"]["api_usd"] == pytest.approx(0.0042)
    assert report["jev"]["stages"]["typed_comma_insert"]["calls"] == 1
    assert "jev" in fixed_timeline.render(report)
