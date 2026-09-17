"""Jev pre-screens the local rule candidates; nothing it did not judge is dropped.

No test here reaches TypeSafe: the ledger is given a fake client, or the lane
is disabled outright and no client is ever built.
"""
from __future__ import annotations

import json

import pytest

from galley import fixed_prescreen as prescreen
from galley.jev import JevLedger, JevUnavailable


@pytest.fixture
def make_book(tmp_path):
    """The same one-paragraph-per-line book the fixed workflow tests use."""
    from docx import Document

    def create(*paragraphs, name="source.docx"):
        path = tmp_path / name
        doc = Document()
        for paragraph in paragraphs:
            doc.add_paragraph(paragraph)
        doc.save(path)
        return path
    return create


@pytest.fixture
def quiet_completion(monkeypatch):
    """The completion sweeps are another lane's business; these cases are about
    the initial local rows and what the pre-screen does to them."""
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates",
                        lambda *args, **kwargs: ([], {"fixture": "completion clean"}))


def row(pid, quote, replacement, *, category="grammar", occurrence=1, source="local:languagetool"):
    return {"para_id": pid, "quote": quote, "replacement": replacement, "occurrence": occurrence,
            "category": category, "action": "edit" if replacement != quote else "query",
            "reason": "Local rule matched.", "missing_knowledge": "", "source": source,
            "local_evidence": {"producer": category}}


class FakeAnswer:
    """Mirrors the SDK's answer objects (see galley.jev._plain_answers)."""
    def __init__(self, *, noul=None, probabilities=None, confidence=None):
        if noul is not None:
            self.noul = noul
        else:
            self.probabilities = probabilities
            self.confidence = confidence
            self.choice = max(probabilities, key=probabilities.get)


class FakeUsage:
    input_tokens = 1200
    output_tokens = 0


class FakeResponse:
    model = "jev-fake"
    usage = FakeUsage()

    def __init__(self, answers):
        self.answers = answers


class FakeClient:
    """Answers from a callable over (state, questions); records every request."""
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def system_one(self, *, state, questions, model):
        self.requests.append({"state": state, "questions": questions, "model": model})
        return FakeResponse({qid: self.answer(state, qid, q) for qid, q in questions.items()})


def ledger_for(tmp_path, answer):
    client = FakeClient(answer)
    return JevLedger(tmp_path / "jev", client_factory=lambda: client), client


def choice(p):
    return FakeAnswer(probabilities={"corrected": p, "original": 1 - p, "either": 0.0}, confidence=0.8)


# ------------------------------------------------------------------ requests

def test_a_concrete_replacement_asks_a_choice_and_a_query_asks_a_noul():
    text = "She said that was fine, and left; the room felt cold."
    rows = [row("p1", "cold", "old", category="spelling"),
            row("p1", "that was fine", "that was fine", category="clarity")]
    jobs, index, unjudged = prescreen.build_requests(rows, {"p1": text}, house_rule="HOUSE")

    assert unjudged == [] and len(jobs) == 1 and len(index[0]) == 2
    state, questions = jobs[0]
    assert state["house_rule"] == "HOUSE" and state["paragraph"] == text
    assert questions["c01"]["type"] == "choice" and questions["c02"]["type"] == "noul"
    assert "`variants.c01.original`" in questions["c01"]["instructions"]
    assert "`variants.c01.corrected`" in questions["c01"]["instructions"]
    assert "spelling error" in questions["c01"]["instructions"]
    assert set(questions["c01"]["criteria"]) == {"corrected", "original", "either"}
    assert "`variants.c02.sentence`" in questions["c02"]["instructions"]
    assert "`variants.c02.site`" in questions["c02"]["instructions"]
    assert "genuine clarity error" in questions["c02"]["instructions"]
    # The compared pair differs in exactly the span the rule touched.
    assert state["variants"]["c01"] == {"original": text, "corrected": text.replace("cold", "old"),
                                        "kind": "spelling"}
    assert state["variants"]["c02"]["site"] == "that was fine"
    assert state["variants"]["c02"]["kind"] == "clarity"


def test_a_whole_paragraph_row_is_narrowed_to_the_changed_words():
    text = "They was tired."
    jobs, _, _ = prescreen.build_requests([row("p1", text, "They were tired.")], {"p1": text},
                                          house_rule="HOUSE")
    variant = jobs[0][0]["variants"]["c01"]
    assert variant["original"] == text and variant["corrected"] == "They were tired."


def test_a_long_paragraph_is_cut_to_the_sentence_around_the_site():
    filler = "The lamps burned low along the quiet road. " * 20
    text = filler + "She seen the door. " + filler
    assert len(text) > prescreen.WHOLE_PARAGRAPH_CHARS
    site = text.index("She seen")
    jobs, _, _ = prescreen.build_requests([row("p1", "seen", "saw")], {"p1": text}, house_rule="HOUSE")
    variant = jobs[0][0]["variants"]["c01"]
    assert variant["original"] == "She seen the door. "
    assert variant["corrected"] == "She saw the door. "
    assert prescreen.sentence_window(text, site) == (site, site + len("She seen the door. "))


def test_one_paragraph_is_batched_thirty_sites_to_a_request():
    text = " ".join(f"word{i} teh" for i in range(65))
    rows = [row("p1", "teh", "the", occurrence=i + 1, category="spelling") for i in range(65)]
    jobs, index, unjudged = prescreen.build_requests(rows, {"p1": text}, house_rule="HOUSE")
    assert unjudged == []
    assert [len(entries) for entries in index] == [30, 30, 5]
    assert all(len(state["variants"]) == len(questions) for state, questions in jobs)
    assert [qid for qid, _, _ in index[1]][:2] == ["c01", "c02"]
    assert [position for _, position, _ in index[1]][:2] == [30, 31]


def test_each_paragraph_gets_its_own_request():
    rows = [row("p1", "teh", "the"), row("p2", "teh", "the")]
    jobs, index, _ = prescreen.build_requests(rows, {"p1": "teh cat", "p2": "teh dog"},
                                              house_rule="HOUSE")
    assert len(jobs) == 2 and [job[0]["paragraph"] for job in jobs] == ["teh cat", "teh dog"]
    assert [entries[0][1] for entries in index] == [0, 1]


# ------------------------------------------------------------------ answers

@pytest.mark.parametrize("p, kept", [(0.0, False), (0.29, False), (0.30, True), (0.9, True)])
def test_the_threshold_decides_at_its_own_value(tmp_path, p, kept):
    ledger, _ = ledger_for(tmp_path, lambda state, qid, q: choice(p))
    rows = [row("p1", "teh", "the", category="spelling")]
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        rows, {"p1": "teh cat"}, ledger=ledger, house_rule="HOUSE", threshold=0.30)
    assert bool(survivors) is kept and bool(dropped) is not kept
    assert evidence["judged"] == 1 and evidence["kept"] == int(kept)
    if kept:
        assert survivors[0]["local_evidence"]["jev"] == {"p": p, "confidence": 0.8, "question": "choice"}
        assert survivors[0]["local_evidence"]["producer"] == "spelling"
    else:
        assert dropped[0]["category"] == "spelling" and dropped[0]["p"] == p
        assert dropped[0]["before"] == "teh" and dropped[0]["replacement"] == "the"
        assert dropped[0]["para_id"] == "p1"


def test_a_noul_answers_a_query_only_row(tmp_path):
    ledger, client = ledger_for(tmp_path, lambda state, qid, q: FakeAnswer(noul=0.8))
    rows = [row("p1", "the door", "the door", category="continuity")]
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        rows, {"p1": "She opened the door."}, ledger=ledger, house_rule="HOUSE", threshold=0.30)
    assert dropped == [] and len(survivors) == 1
    assert survivors[0]["local_evidence"]["jev"] == {"p": 0.8, "confidence": None, "question": "noul"}
    # The ledger built a real SDK Noul object from the plain question.
    assert type(client.requests[0]["questions"]["c01"]).__name__ == "Noul"


def test_read_answer_reports_no_judgment_when_the_answer_is_not_one():
    assert prescreen.read_answer({"type": "noul", "noul": 0.42})["p"] == 0.42
    assert prescreen.read_answer({"type": "choice", "confidence": 0.5,
                                  "probabilities": {"corrected": 0.7}})["p"] == 0.7
    for junk in ({}, {"type": "choice", "probabilities": {}}, "no answer", None):
        assert prescreen.read_answer(junk)["p"] is None


def test_an_unanswered_site_is_kept_not_dropped(tmp_path):
    # A response that omits the question is not a judgment against the row.
    class Empty(FakeClient):
        def system_one(self, *, state, questions, model):
            self.requests.append({"state": state, "questions": questions, "model": model})
            return FakeResponse({})

    client = Empty(lambda *a: None)
    ledger = JevLedger(tmp_path / "jev", client_factory=lambda: client)
    rows = [row("p1", "teh", "the")]
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        rows, {"p1": "teh cat"}, ledger=ledger, house_rule="HOUSE", threshold=0.30)
    assert survivors == rows and dropped == []
    assert evidence["judged"] == 0 and evidence["passed_through"] == 1


# ------------------------------------------------------------------ pass-through

def test_diagnostic_and_unsited_rows_are_never_judged_or_dropped(tmp_path):
    ledger, client = ledger_for(tmp_path, lambda state, qid, q: choice(0.0))
    rows = [row("p1", "quiet", "loud", category="word_echo"),          # diagnostic only
            row("p1", "absent", "present"),                            # quote is not in the text
            row("ghost", "teh", "the"),                                # paragraph is not under review
            row("p1", "teh", "the", occurrence=3),                     # third occurrence does not exist
            row("p1", "teh", "the")]                                   # the only judgeable row
    texts = {"p1": "teh quiet cat"}
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        rows, texts, ledger=ledger, house_rule="HOUSE", threshold=0.30)

    assert [r["quote"] for r in survivors] == ["quiet", "absent", "teh", "teh"]
    assert [d["before"] for d in dropped] == ["teh"]
    assert evidence["judged"] == 1 and evidence["passed_through"] == 4
    assert evidence["unsited_or_diagnostic"] == 4
    assert all("jev" not in (r.get("local_evidence") or {}) for r in survivors)
    # Only the one sited, non-diagnostic row was ever asked about.
    assert sum(len(r["questions"]) for r in client.requests) == 1


def test_an_outage_mid_stage_passes_the_unjudged_rows_through(tmp_path, monkeypatch):
    monkeypatch.setattr(prescreen, "CHUNK", 1)
    seen = []

    class Failing(FakeClient):
        def system_one(self, *, state, questions, model):
            seen.append(state["paragraph"])
            if len(seen) > 1:
                raise JevUnavailable("Jev refused the request (402): out of credits")
            return FakeResponse({qid: choice(0.0) for qid in questions})

    client = Failing(lambda *a: None)
    ledger = JevLedger(tmp_path / "jev", client_factory=lambda: client)
    rows = [row("p1", "teh", "the"), row("p2", "teh", "the"), row("p3", "teh", "the")]
    texts = {"p1": "teh one", "p2": "teh two", "p3": "teh three"}
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        rows, texts, ledger=ledger, house_rule="HOUSE", threshold=0.30)

    assert [r["para_id"] for r in survivors] == ["p2", "p3"]
    assert [d["para_id"] for d in dropped] == ["p1"]
    assert evidence["judged"] == 1 and evidence["passed_through"] == 2
    assert "402" in evidence["unavailable"]


def test_no_rows_means_no_request(tmp_path):
    ledger, client = ledger_for(tmp_path, lambda state, qid, q: choice(1.0))
    survivors, dropped, evidence = prescreen.prescreen_local_candidates(
        [], {"p1": "teh cat"}, ledger=ledger, house_rule="HOUSE", threshold=0.30)
    assert (survivors, dropped) == ([], [])
    assert evidence["requests"] == 0 and client.requests == []


def test_a_receipt_answers_the_same_question_again_for_free(tmp_path):
    ledger, client = ledger_for(tmp_path, lambda state, qid, q: choice(0.9))
    rows = [row("p1", "teh", "the")]
    first = prescreen.prescreen_local_candidates(rows, {"p1": "teh cat"}, ledger=ledger,
                                                 house_rule="HOUSE", threshold=0.30)
    second = prescreen.prescreen_local_candidates(rows, {"p1": "teh cat"}, ledger=ledger,
                                                  house_rule="HOUSE", threshold=0.30)
    assert first == second and len(client.requests) == 1
    assert ledger.usage_summary()["reused"] == 1


# ------------------------------------------------------------------ workflow

def test_the_disabled_lane_builds_no_ledger_and_screens_every_row(make_book, tmp_path, monkeypatch, quiet_completion):
    from galley import jev as jev_lane
    from galley.fixed_workflow import FixedWorkflow
    from test_galley_fixed_workflow import Readers, _local_row

    def initial(prepared, texts, *args, **kwargs):
        return [_local_row(next(iter(texts)), "quiet", "calm")], {"fixture": "local"}

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", initial)
    monkeypatch.setattr(jev_lane, "enabled", lambda: False)
    monkeypatch.setattr(jev_lane, "JevLedger", lambda *a, **k: pytest.fail("the disabled lane built a ledger"))
    readers = Readers()
    result = FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "jev-off", calls=readers).run()

    assert not (tmp_path / "jev-off" / "jev").exists()
    skipped = [h for h in result["history"] if h["stage"] == "typed_jev_prescreen"]
    assert len(skipped) == 1 and "not enabled" in skipped[0]["skipped"]
    # The pair screen sends the same sites to Sonnet and to Luna.
    sites = [s for row in readers.events if row["stage"] == "typed_screen"
             for s in row["payload"]["sites"]]
    assert sorted({s["before"] for s in sites}) == ["quiet"]
    typed = json.loads((tmp_path / "jev-off/stages/typed.json").read_text())
    assert typed["evidence"]["local"] == {"fixture": "local"}


def test_a_low_probability_local_row_never_reaches_the_paid_screen(make_book, tmp_path, monkeypatch, quiet_completion):
    from galley import jev as jev_lane
    from galley.fixed_workflow import FixedWorkflow
    from test_galley_fixed_workflow import Readers, _local_row

    def initial(prepared, texts, *args, **kwargs):
        pid = next(iter(texts))
        return ([_local_row(pid, "teh", "the", category="spelling"),
                 _local_row(pid, "quiet", "calm")], {"fixture": "local"})

    def answer(state, qid, question):
        variant = state["variants"][qid]
        if "corrected" not in variant:
            # The candidate-source lane's own brute-force sites share the
            # ledger; none of them is an error in this fixture.
            return FakeAnswer(probabilities={k: (1.0 if k == "original" else 0.0)
                                             for k in question.criteria}, confidence=1.0)
        # The typo is a real error; the synonym swap is a rule misfire.
        return choice(0.95 if "the typo" in variant["corrected"] else 0.02)

    client = FakeClient(answer)
    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", initial)
    monkeypatch.setattr(jev_lane, "enabled", lambda: True)
    readers = Readers()
    flow = FixedWorkflow(make_book("A quiet paragraph with teh typo."), tmp_path / "jev-on", calls=readers)
    flow.jev = JevLedger(tmp_path / "jev-on" / "jev", client_factory=lambda: client)
    result = flow.run()

    sites = [s for row in readers.events if row["stage"] == "typed_screen"
             for s in row["payload"]["sites"]]
    # The screen sites the surviving typo alone (under the screen's own minimal
    # span, teh -> the); the rejected synonym swap never reaches the request.
    assert sorted({(s["before"], proposal["replacement"])
                   for s in sites for proposal in s["proposals"]}) == [("eh", "he")]
    assert "calm" not in json.dumps(sites)
    typed_evidence = json.loads((tmp_path / "jev-on/stages/typed.json").read_text())["evidence"]
    assert [c["local_evidence"]["jev"] for c in typed_evidence["candidates"]] == [
        {"p": 0.95, "confidence": 0.8, "question": "choice"}]
    entry = next(h for h in result["history"] if h["stage"] == "typed_jev_prescreen")
    assert entry["generated"] == 2 and entry["kept"] == 1
    assert [(d["before"], d["replacement"], d["p"]) for d in entry["dropped"]] == [("quiet", "calm", 0.02)]
    assert entry["usage"]["calls"] == 1
    saved = typed_evidence["local"]
    assert saved["jev_prescreen"]["kept"] == 1 and saved["jev_prescreen"]["dropped"] == 1
    assert saved["jev_prescreen"]["threshold"] == 0.30
    assert "usd" not in json.dumps(saved["jev_prescreen"])  # replayable: no costs or timings
    assert (tmp_path / "jev-on" / "jev" / "typed_prescreen").is_dir()


def test_the_local_certificate_still_validates_beside_the_prescreen_block(tmp_path):
    """Certify reads the local evidence key by key, so the sibling block the
    pre-screen adds to it does not invalidate the packet."""
    from galley import fixed_local
    from test_galley_fixed_local import IDENTITY, collect, para, prepared

    _, evidence = collect(tmp_path, prepared(para("p1", "She walked down the quiet lane")))
    extended = {**evidence, "jev_prescreen": {"kind": "jev_prescreen", "threshold": 0.30,
                                              "judged": 1, "kept": 1, "dropped": 0}}
    assert fixed_local.validate_local_evidence(
        extended, tmp_path / "local", IDENTITY)["status"] == "completed"
