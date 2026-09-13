"""Real Word intake and mocked readers exercise the fixed editorial sequence."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from docx import Document

from docproof.providers import ProviderResult
from galley.fixed_workflow import (ASTRA, FABLE, LUNA, OPUS, SOL, SONNET,
                                   FixedWorkflow, FixedWorkflowError, _candidate)


def finding(pid, quote, replacement, category="grammar", *, action="edit", missing=""):
    return {"para_id": pid, "quote": quote, "occurrence": 1,
            "replacement": replacement, "category": category, "action": action,
            "reason": "A clear local proofreading issue.", "missing_knowledge": missing}


def ruling(site, action="apply", replacement=None):
    return {"id": site["id"], "action": action,
            "replacement": site.get("after", site.get("before", "")) if replacement is None else replacement,
            "reason": "Checked the context.", "missing_knowledge": "",
            "question": ""}


def comment_decision(q, action="retain", quote=None):
    return {"id": q["id"], "action": action, "quote": quote or q["quote"],
            "question": q["question"], "reason": "The author must supply the missing identity.",
            "missing_knowledge": q["missing_knowledge"]}


class Readers:
    """No model transport; schemas/coverage still pass through real analyzers."""
    def __init__(self, *, poetry=False, handler=None, typed=None):
        self.poetry, self.handler, self.typed = poetry, handler, typed
        self.events = []

    def ask(self, stage, **kwargs):
        payload = json.loads(kwargs["user"])
        self.events.append({"stage": stage, "payload": payload, **kwargs})
        if self.handler:
            result = self.handler(stage, kwargs["model"], payload, kwargs)
            if result is not None:
                return result
        if stage == "poetry":
            return {"classification": "poetry" if self.poetry else "prose", "reason": "Fixed samples."}
        if stage == "story_sheet":
            return {"narration": "Third person past tense", "characters": [], "notes": []}
        properties = kwargs["schema"]["properties"]
        if "reviewed_ids" in properties:
            owned = payload.get("sites", payload.get("paragraphs", []))
            return {"reviewed_ids": [row["id"] for row in owned], "findings": [],
                    "comment_decisions": [comment_decision(q) for q in payload.get("comments", [])],
                    "editorial_verdict": "ready"}
        if "changes" in payload:
            return {"decisions": [{"id": x["id"], "verdict": "approve", "reason": "Correct."}
                                  for x in payload["changes"]]}
        if "comments" in payload:
            return {"decisions": [comment_decision(q) for q in payload["comments"]]}
        return {"decisions": [ruling(site, "drop") for site in payload["sites"]]}

    def provider(self, stage, cfg):
        parent = self

        class TypedProvider:
            name = "fixed-test"

            def complete_structured(self, **kwargs):
                parent.events.append({"stage": stage, "claude_lane": cfg.api.claude_lane, **kwargs})
                paragraphs = dict(re.findall(r'<paragraph id="([^"]+)">\n(.*?)\n</paragraph>',
                                             kwargs["user"].split("</context>")[-1], re.S))
                types = kwargs["schema"]["$defs"]["RawFinding"]["properties"]["error_type"].get("enum")
                if types is None:
                    types = [kwargs["schema"]["$defs"]["RawFinding"]["properties"]["error_type"]["const"]]
                answer = parent.typed(stage, kwargs["model"], paragraphs, types) if parent.typed else []
                if isinstance(answer, ProviderResult):
                    return answer
                return ProviderResult(parsed={"findings": answer,
                                              "reviewed_paragraph_ids": list(paragraphs)})

        return TypedProvider()

    def assert_complete(self):
        pass

    def usage_summary(self):
        return {"mocked_calls": len(self.events)}


@pytest.fixture
def make_book(tmp_path):
    def create(*paragraphs, name="source.docx"):
        path = tmp_path / name
        doc = Document()
        for paragraph in paragraphs:
            doc.add_paragraph(paragraph)
        doc.save(path)
        return path
    return create


def _typed_row(pid, source, before, after, key):
    return {"para_id": pid, "error_type": key, "original_text": source,
            "corrected_text": source.replace(before, after), "occurrence": 1,
            "confidence": "high", "explanation": "A clear proofreading error."}


def test_real_docx_full_fixed_sequence_and_successive_corrected_versions(make_book, tmp_path, monkeypatch):
    book = make_book("We seen teh 20 birds beside a apple. They was bright. It are warm. He walk home.")

    def typed(stage, model, paragraphs, keys):
        result = []
        for pid, text in paragraphs.items():
            if "spelling" in keys:
                result.append(_typed_row(pid, text, "teh", "the", "spelling"))
            if "subject_verb_agreement" in keys:
                result.append(_typed_row(pid, text, "seen", "saw" if model == SONNET else "see", "subject_verb_agreement"))
        return result

    def handler(stage, model, payload, kwargs):
        if stage == "typed_disputes":
            return {"decisions": [ruling(x, replacement="aw") for x in payload["sites"]]}
        if stage == "numbers":
            pid, text = next(iter(payload["paragraphs"].items()))
            return {"reviewed_ids": [x["id"] for x in payload["sites"]],
                    "findings": [finding(pid, "20", "twenty", "number_style")],
                    "comment_decisions": [], "editorial_verdict": "ready"}
        edits = {"broken_repair": ("They was", "They were", "broken_sentence"),
                 "ensemble_sweep_opus": ("a apple", "an apple", "grammar"),
                 "ensemble_sweep_sol": ("a apple", "an apple", "grammar"),
                 "fable": ("It are", "It is", "grammar"),
                 "astra": ("He walk", "He walks", "grammar")}
        if stage in edits:
            before, after, category = edits[stage]
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                    "findings": [finding(row["id"], before, after, category)],
                    "comment_decisions": [], "editorial_verdict": "ready"}

    readers = Readers(handler=handler, typed=typed)
    # A model unexpectedly launched from prepare/finish is a test failure.
    monkeypatch.setattr("docproof.providers.build_provider", lambda *_: pytest.fail("Unscheduled model call"))
    progress = []
    flow = FixedWorkflow(book, tmp_path / "run", calls=readers,
                         progress=lambda event, **data: progress.append((event, data)))
    result = flow.run()
    assert list(result["accepted"].values()) == [
        "We saw the twenty birds beside an apple. They were bright. It is warm. He walks home."]
    assert result["questions"] == []
    assert [row["stage"] for row in result["stages"]] == [
        "poetry", "story_sheet", "typed", "numbers", "broken_repair", "checks", "ensemble_sweep", "fable", "astra"]
    assert [data["phase"] for event, data in progress if event == "phase_start"] == [
        "poetry", "story_sheet", "typed", "numbers", "broken_repair", "checks", "ensemble_sweep", "fable", "astra"]
    assert all(data["ok"] for event, data in progress if event == "phase_end")
    events = readers.events
    assert len([x for x in events if x["stage"] == "typed"]) == 18
    assert {x["model"] for x in events if x["stage"] == "typed"} == {SONNET, LUNA}
    assert all("number_style" not in x["schema"]["$defs"]["RawFinding"]["properties"]["error_type"].get("enum", [])
               for x in events if x["stage"] == "typed")
    assert all(x["claude_lane"] == "subagent" for x in events if x["stage"] == "typed")
    by_stage = {x["stage"]: x for x in events}
    assert by_stage["story_sheet"]["model"] == LUNA
    assert "type 2 diabetes" not in by_stage["story_sheet"]["system"]
    assert "type 2 diabetes" not in by_stage["poetry"]["system"]
    assert "type 2 diabetes" in by_stage["fable"]["system"]
    assert by_stage["typed_disputes"]["model"] == OPUS
    assert by_stage["ensemble_sweep_opus"]["payload"]["paragraphs"] == by_stage["ensemble_sweep_sol"]["payload"]["paragraphs"]
    assert by_stage["ensemble_sweep_sol"]["model"] == SOL
    assert "an apple" in by_stage["fable"]["payload"]["paragraphs"][0]["text"]
    assert "It is warm" in by_stage["astra"]["payload"]["paragraphs"][0]["text"]
    assert by_stage["astra"]["model"] == ASTRA
    assert any(x["stage"] == "astra_checks_correction" for x in events)


def test_poetry_runs_only_classification_and_sonnet_spelling(make_book, tmp_path):
    source = "teh Moon\n  waits, 20 times\n—quiet"
    book = make_book(source)

    def typed(stage, model, paragraphs, keys):
        return [_typed_row(pid, text, "teh", "the", "spelling") for pid, text in paragraphs.items()]

    readers = Readers(poetry=True, typed=typed)
    result = FixedWorkflow(book, tmp_path / "poetry", calls=readers).run()
    assert list(result["accepted"].values()) == [source.replace("teh", "the")]
    assert result["poetry_only"]
    assert {x["stage"] for x in readers.events} == {"poetry", "spelling"}
    assert {x["model"] for x in readers.events} == {SONNET}
    assert all("type 2 diabetes" not in x["system"] for x in readers.events)


def test_embedded_poetry_spelling_never_enters_luna_or_opus_change_checks(make_book, tmp_path):
    verse = "teh Moon\n  waits"

    def typed(stage, model, paragraphs, keys):
        rows = []
        for pid, text in paragraphs.items():
            if stage == "spelling":
                rows.append(_typed_row(pid, text, "teh", "the", "spelling"))
            elif "subject_verb_agreement" in keys:
                rows.append(_typed_row(pid, text, "They was", "They were", "subject_verb_agreement"))
        return rows

    def handler(stage, model, payload, kwargs):
        if stage == "poetry":
            return {"classification": "mixed", "reason": "Verse and prose."}
        if stage == "poetry_sections":
            return {"paragraphs": [{"id": row["id"], "poetry": "\n" in row["text"]} for row in payload]}
        if "changes" in payload:
            assert all("Moon" not in row["before"] for row in payload["changes"])
        if stage == "typed_disputes":
            pytest.fail("The poem must not trigger an Opus dispute")

    readers = Readers(typed=typed, handler=handler)
    result = FixedWorkflow(make_book(verse, "They was here."), tmp_path / "mixed", calls=readers).run()
    assert list(result["accepted"].values()) == [verse.replace("teh", "the"), "They were here."]
    assert not result["poetry_only"]
    assert len([row for row in readers.events if row["stage"] == "spelling"]) == 1


def _flow(make_book, tmp_path, readers=None, text="He waited for someone."):
    flow = FixedWorkflow(make_book(text), tmp_path / "unit", calls=readers or Readers())
    flow.original = {"p": text}
    flow.current = dict(flow.original)
    return flow


def test_story_sheet_preserves_interleaved_manuscript_order(make_book, tmp_path):
    flow = _flow(make_book, tmp_path)
    flow.original = {"body-0000": "Opening text.", "table-0-r0-c0-p0": "Table in the middle.",
                     "body-0001": "Following text."}
    flow.current = dict(flow.original)
    flow._story()
    assert flow.calls.events[0]["payload"]["manuscript"] == [
        {"id": "body-0000", "text": "Opening text."},
        {"id": "table-0-r0-c0-p0", "text": "Table in the middle."},
        {"id": "body-0001", "text": "Following text."}]


def test_conflicting_poetry_findings_do_not_call_opus_or_create_comments(make_book, tmp_path):
    flow = _flow(make_book, tmp_path, text="teh Moon")
    flow.poetry_ids = {"p"}
    candidates = [_candidate(finding("p", "teh", fix, "spelling"), flow.current, SONNET)
                  for fix in ("the", "ten")]
    assert flow._adjudicate("typed", candidates, (SONNET,)) == []
    assert flow.calls.events == [] and flow.questions == []
    case = _candidate(finding("p", "Moon", "moon", "spelling"), flow.current, SONNET)
    flow._apply("typed", [case])
    assert flow.current["p"] == "teh Moon"


@pytest.mark.parametrize("answer", [ProviderResult(stop_reason="refusal"),
    ProviderResult(parsed={"findings": []})])
def test_incomplete_typed_read_is_never_a_clean_book(make_book, tmp_path, answer):
    readers = Readers(typed=lambda *args: answer)
    with pytest.raises(FixedWorkflowError, match="complete|coverage"):
        FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "broken", calls=readers).run()
    assert not any(x["stage"] == "fable" for x in readers.events)
    assert not (tmp_path / "broken/result.json").exists()


def test_number_read_requires_every_extracted_site(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "numbers":
            return {"reviewed_ids": [], "findings": [], "comment_decisions": [], "editorial_verdict": "ready"}
    readers = Readers(handler=handler)
    with pytest.raises(FixedWorkflowError, match="Number coverage"):
        FixedWorkflow(make_book("There were 20 birds."), tmp_path / "broken", calls=readers).run()


def test_read_rejects_missing_paragraphs_and_broken_repair_scope(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        return {"reviewed_ids": ["p"], "findings": [finding("p", "waited", "paused", "spelling")],
                "comment_decisions": [], "editorial_verdict": "ready"}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    with pytest.raises(FixedWorkflowError, match="Broken-sentence repair exceeded"):
        flow._read("broken_repair", OPUS, ids=["p"])
    assert "ONLY genuinely broken sentences" in flow.calls.events[0]["system"]
    flow.calls.handler = lambda *args: {"reviewed_ids": [], "findings": [], "comment_decisions": [], "editorial_verdict": "ready"}
    with pytest.raises(FixedWorkflowError, match="paragraph coverage"):
        flow._read("astra", ASTRA)


def test_luna_rejection_is_settled_by_opus_instead_of_becoming_comment(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "check_meaning":
            return {"decisions": [{"id": "p", "verdict": "reject", "reason": "Unsure."}]}
        if stage == "check_meaning_disputes":
            return {"decisions": [ruling(payload["sites"][0], replacement="He waited for somebody.")]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    before = dict(flow.current)
    flow.current["p"] = "He waited for somebody."
    flow._checks("check", before)
    assert flow.current["p"] == "He waited for somebody."
    assert flow.questions == []
    assert [x["model"] for x in flow.calls.events] == [LUNA, OPUS, LUNA]


def test_formatting_enters_correction_check_and_rejected_format_is_removed(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "check_correction":
            assert payload["changes"][0]["format_proposals"][0]["format"] == "italic"
            return {"decisions": [{"id": "p", "verdict": "reject", "reason": "No house basis."}]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    row = _candidate(finding("p", "someone", "someone", "format"), flow.current, OPUS,
                     format_types={"format": "italic"})
    before = flow._apply("fable", [row])
    flow._checks("check", before)
    assert flow.formats == []
    assert [x["stage"] for x in flow.calls.events] == ["check_correction", "check_correction_disputes"]


def test_comment_resolved_by_rejected_edit_is_reviewed_on_restored_text(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "fable_checks_meaning":
            return {"decisions": [{"id": "p", "verdict": "reject", "reason": "Invented identity."}]}
        if stage == "fable_comment_review":
            assert payload["paragraphs"]["p"] == "He waited for someone."
            return {"decisions": [comment_decision(q) for q in payload["comments"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    flow._question("p", flow.current["p"], "Who was he waiting for?", "The intended identity", "An unresolved reference.", "typed")
    decisions = [comment_decision(flow.questions[0], "drop")]
    before = flow._apply("fable", [_candidate(finding("p", "someone", "Mary"), flow.current, FABLE)])
    flow._checks("fable_checks", before)
    flow._comments(decisions, "fable", before=before, model=FABLE)
    assert flow.current["p"] == before["p"]
    assert len(flow.questions) == 1
    assert flow.calls.events[-1]["stage"] == "fable_comment_review"


def test_late_correction_elsewhere_refreshes_retained_comment(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        assert stage == "astra_comment_review"
        assert payload["changed_passages"] == [{"para_id": "other", "before": "She was the visitor.", "after": "Jo was the visitor."}]
        return {"decisions": [comment_decision(q, "drop") for q in payload["comments"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler), text="The visitor waited.")
    flow.original["other"] = flow.current["other"] = "She was the visitor."
    flow._question("p", flow.current["p"], "Who is the visitor?", "The visitor's identity", "An unresolved identity.", "fable")
    before = dict(flow.current)
    flow.current["other"] = "Jo was the visitor."
    flow._comments([comment_decision(flow.questions[0])], "astra", before=before, model=ASTRA)
    assert flow.questions == []
    assert len(flow.calls.events) == 1


def test_final_comment_cannot_silently_anchor_to_wrong_repeated_word(make_book, tmp_path):
    flow = _flow(make_book, tmp_path, text="He saw her, and her visitor left.")
    flow._question("p", flow.current["p"], "Who does the latter her mean?", "The intended identity", "An unclear reference.", "fable")
    with pytest.raises(FixedWorkflowError, match="unambiguous contextual quote"):
        flow._comments([comment_decision(flow.questions[0], quote="her")], "astra")


def test_failed_typed_stage_cancels_queued_reads(make_book, tmp_path, monkeypatch):
    from concurrent.futures import Future
    scheduled = []

    class ControlledExecutor:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, callback, chunk):
            future = Future()
            if not scheduled:
                future.set_result(ProviderResult(stop_reason="refusal"))
            scheduled.append(future)
            return future

    monkeypatch.setattr("galley.fixed_workflow.ThreadPoolExecutor", ControlledExecutor)
    with pytest.raises(FixedWorkflowError, match="typed detector did not complete"):
        FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "blocked", calls=Readers()).run()
    assert len(scheduled) > 1
    assert all(future.cancelled() for future in scheduled[1:])


def test_new_astra_question_gets_explicit_final_astra_comment_review(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "astra":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [row["id"]],
                    "findings": [finding(row["id"], "someone", "", "author_question", action="query", missing="The intended identity")],
                    "comment_decisions": [], "editorial_verdict": "ready"}
        if stage == "astra_disputes":
            return {"decisions": [{**ruling(x, "query"), "missing_knowledge": "The intended identity", "question": "Who was he waiting for?"}
                                  for x in payload["sites"]]}
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("He waited for someone."), tmp_path / "run", calls=readers).run()
    assert len(result["questions"]) == 1
    assert readers.events[-1]["stage"] == "astra_comment_review"
    assert readers.events[-1]["model"] == ASTRA


def test_stale_source_and_tracked_input_fail_before_any_model_call(make_book, tmp_path):
    source = make_book("A quiet paragraph.")
    readers = Readers()
    flow = FixedWorkflow(source, tmp_path / "run", calls=readers)
    doc = Document(source)
    doc.add_paragraph("Changed after intake.")
    doc.save(source)
    with pytest.raises(FixedWorkflowError, match="source changed"):
        flow.run()
    assert readers.events == []
    with pytest.raises(FixedWorkflowError, match="source or fixed recipe changed"):
        FixedWorkflow(source, tmp_path / "run", calls=readers)
    tracked = Path(__file__).parent / "fixtures/tracked.docx"
    from docproof.ingest import IngestError
    with pytest.raises(IngestError):
        FixedWorkflow(tracked, tmp_path / "tracked", calls=readers).run()
    assert readers.events == []


def test_replay_rejects_tampered_stage_evidence(make_book, tmp_path):
    source = make_book("A quiet paragraph.")
    directory = tmp_path / "run"
    FixedWorkflow(source, directory, calls=Readers()).run()
    path = directory / "stages/poetry.json"
    data = json.loads(path.read_text())
    data["accepted_sha256"] = "wrong"
    path.write_text(json.dumps(data))
    with pytest.raises(FixedWorkflowError, match="Saved poetry evidence changed"):
        FixedWorkflow(source, directory, calls=Readers()).run()
