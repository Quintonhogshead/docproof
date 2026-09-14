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
                if "reviewed_check_ids" in kwargs["schema"]["properties"]:
                    result.setdefault("reviewed_check_ids", [s["id"] for s in payload["focused_sites"]])
                return result
        if stage == "poetry":
            return {"classification": "poetry" if self.poetry else "prose", "reason": "Fixed samples."}
        if stage == "story_sheet":
            return {"narration": "Third person past tense", "characters": [], "notes": []}
        properties = kwargs["schema"]["properties"]
        if "reading_notes" in properties:
            return {"findings": [], "reading_notes": "Fixture continuity read."}
        if "reviewed_ids" in properties:
            owned = payload.get("sites", payload.get("paragraphs", []))
            return {"reviewed_ids": [row["id"] for row in owned], "findings": [],
                    "comment_decisions": [comment_decision(q) for q in payload.get("comments", [])],
                    **({"reviewed_check_ids": [s["id"] for s in payload["focused_sites"]]}
                       if "reviewed_check_ids" in properties else {}),
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


@pytest.fixture(autouse=True)
def local_scans(monkeypatch):
    """Existing editorial cases isolate the local scanner's transport/generators.

    New local integration cases replace these stubs or restore the real adapter;
    the workflow's anchoring, arbitration, checks and sequencing always run.
    """
    from galley import fixed_local
    originals = {"initial": fixed_local.collect_local_candidates,
                 "completion": fixed_local.collect_completion_candidates}
    monkeypatch.setattr(fixed_local, "collect_local_candidates",
                        lambda *args, **kwargs: ([], {"fixture": "local clean"}))
    monkeypatch.setattr(fixed_local, "collect_completion_candidates",
                        lambda *args, **kwargs: ([], {"fixture": "completion clean"}))
    return originals


def _local_row(pid, before, after, *, source="LanguageTool", category="grammar"):
    return {**finding(pid, before, after, category), "source": source}


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
        if stage == "typed_screen":
            return {"decisions": [ruling(x, replacement="aw" if model == SONNET else "e") for x in payload["sites"]]}
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
        "poetry", "story_sheet", "typed", "numbers", "broken_repair", "checks", "ensemble_sweep", "continuity", "fable", "astra"]
    assert [data["phase"] for event, data in progress if event == "phase_start"] == [
        "poetry", "story_sheet", "typed", "numbers", "broken_repair", "checks", "ensemble_sweep", "continuity", "fable", "astra"]
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


def test_frontier_structure_context_uses_each_readers_current_book(make_book, tmp_path):
    book = make_book("Chapter One: The Harbr", "The boat arrived.")
    doc = Document(book)
    doc.paragraphs[0].style = "Heading 1"
    doc.save(book)

    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                    "findings": [finding(row["id"], "Harbr", "Harbor", "spelling")],
                    "comment_decisions": [], "editorial_verdict": "ready"}

    readers = Readers(handler=handler)
    FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    fable = next(x for x in readers.events if x["stage"] == "fable")
    astra = next(x for x in readers.events if x["stage"] == "astra")
    assert "The Harbr" in fable["payload"]["structure_context"]["excerpt"]
    assert "The Harbor" in astra["payload"]["structure_context"]["excerpt"]
    assert "The Harbr" not in astra["payload"]["structure_context"]["excerpt"]
    assert astra["payload"]["structure_context"]["complete_inventory"] is False
    assert "Do not infer missing entries" in astra["system"]


def test_press_prompts_and_focused_evidence_reach_both_final_readers(make_book, tmp_path):
    from galley.press_prompt import EDITORIAL_RULES
    book = make_book('“Wait.” He said. Red, white and blue.',
                     'He walks and waits and watches.')
    readers = Readers()
    result = FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    final = [r for r in readers.events if r["stage"] in {"fable", "astra"}]
    assert len(final) == 2 and final[0]["system"] == final[1]["system"]
    for request in final:
        assert all(text in request["system"] for text in EDITORIAL_RULES.values())
        assert {s["check"] for s in request["payload"]["focused_sites"]} >= {
            "dialogue_matrix", "serial_comma", "narrative_tense"}
        assert request["payload"]["paragraph_metadata"]
        assert "reviewed_check_ids" in request["schema"]["required"]
    story = next(r for r in readers.events if r["stage"] == "story_sheet")
    assert "first/last\nparagraph IDs" in story["system"]
    assert "Canadian" in story["system"]
    stage = json.loads(Path(result["stages"][-1]["path"]).read_text())
    assert stage["evidence"]["press_audit"]["accepted_sha256"]
    assert stage["evidence"]["press_audit"]["raw_signal_counts"]["sweep_dialogue_tag"] == 1


@pytest.mark.parametrize("stage", ["fable", "astra"])
def test_final_reader_cannot_skip_focused_sites(make_book, tmp_path, stage):
    def handler(name, model, payload, kwargs):
        if name == stage:
            return {"reviewed_ids": [p["id"] for p in payload["paragraphs"]],
                    "reviewed_check_ids": [], "findings": [], "comment_decisions": [],
                    "editorial_verdict": "ready"}
    with pytest.raises(FixedWorkflowError, match="focused-check coverage"):
        FixedWorkflow(make_book('Red, white and blue.'), tmp_path / "run",
                      calls=Readers(handler=handler)).run()
    assert not (tmp_path / "run/result.json").exists()


def test_final_reader_can_propose_tracked_title_italics_and_astra_sees_it(make_book, tmp_path):
    from galley.fixed_documents import write_manuscripts, paragraph_views
    from docproof.utils.xml_helpers import DocxPackage, qn
    book = make_book("She read The Great Gatsby yesterday.")
    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            p = payload["paragraphs"][0]
            return {"reviewed_ids": [p["id"]],
                    "findings": [finding(p["id"], "The Great Gatsby", "The Great Gatsby", "format")],
                    "comment_decisions": [], "editorial_verdict": "ready"}
    readers = Readers(handler=handler)
    result = FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    assert len(result["formats"]) == 1
    astra = next(r for r in readers.events if r["stage"] == "astra")
    meta = next(iter(astra["payload"]["paragraph_metadata"].values()))
    assert any(r["italic"] is True for r in meta["formatting"])
    check = next(r for r in readers.events if r["stage"] == "fable_checks_correction")
    assert check["payload"]["changes"][0]["format_proposals"]
    assert "Long-work titles" in check["system"]
    tracked, clean, _ = write_manuscripts(book, tmp_path / "out", result["accepted"], formats=result["formats"])
    assert paragraph_views(tracked, "reject") == result["original"]
    assert paragraph_views(clean) == result["accepted"]
    assert DocxPackage(tracked).tree("word/document.xml").find('.//' + qn('w:rPrChange')) is not None


def test_astra_profiles_fables_checked_text_not_the_source(make_book, tmp_path):
    book = make_book("He walks and waits and watches.")
    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            p = payload["paragraphs"][0]
            return {"reviewed_ids": [p["id"]], "findings": [finding(p["id"], p["text"],
                    "He walked and waited and watched.")], "comment_decisions": [], "editorial_verdict": "ready"}
    readers = Readers(handler=handler)
    FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    values = {r["stage"]: r["payload"]["narrative_profile"]["paragraphs"][0]
              for r in readers.events if r["stage"] in {"fable", "astra"}}
    assert values["fable"]["present"] == 3 and values["astra"]["present"] == 0
    assert values["astra"]["past"] == 3


def test_real_footnote_and_endnote_parts_reach_final_readers_and_reject_audit(make_book, tmp_path):
    from lxml import etree
    from docproof.utils.xml_helpers import DocxPackage, qn
    from galley.fixed_documents import write_manuscripts, paragraph_views, _report
    book = make_book("Body text with notes.")
    pkg = DocxPackage(book)
    body = pkg.tree("word/document.xml").find(qn("w:body")).find(qn("w:p"))
    for kind in ("footnote", "endnote"):
        part = f"word/{kind}s.xml"
        notes = etree.Element(qn(f"w:{kind}s"), nsmap={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})
        note = etree.SubElement(notes, qn(f"w:{kind}"), {qn("w:id"): "1"})
        p = etree.SubElement(note, qn("w:p"))
        marker = etree.SubElement(p, qn("w:r"))
        etree.SubElement(marker, qn(f"w:{kind}Ref"))
        run = etree.SubElement(p, qn("w:r"))
        etree.SubElement(run, qn("w:t")).text = f"The {kind} has teh error."
        pkg.add_part(part, notes)
        reference = etree.SubElement(body, qn("w:r"))
        etree.SubElement(reference, qn(f"w:{kind}Reference"), {qn("w:id"): "1"})
        rels = pkg.tree("word/_rels/document.xml.rels")
        etree.SubElement(rels, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
                         Id=f"rIdTest{kind}", Type=f"http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}s", Target=f"{kind}s.xml")
        types = pkg.tree("[Content_Types].xml")
        etree.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
                         PartName="/" + part, ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}s+xml")
    for name in ("word/document.xml", "word/_rels/document.xml.rels", "[Content_Types].xml"):
        pkg.mark_modified(name)
    pkg.save(book)
    before = book.read_bytes()
    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            return {"reviewed_ids": [p["id"] for p in payload["paragraphs"]],
                    "findings": [finding(p["id"], "teh", "the", "spelling") for p in payload["paragraphs"] if "teh" in p["text"]],
                    "comment_decisions": [], "editorial_verdict": "ready"}
    readers = Readers(handler=handler)
    result = FixedWorkflow(book, tmp_path / "run", calls=readers).run()
    assert book.read_bytes() == before
    for request in (r for r in readers.events if r["stage"] in {"fable", "astra"}):
        assert {m["part"] for m in request["payload"]["paragraph_metadata"].values()} >= {
            "word/footnotes.xml", "word/endnotes.xml"}
    tracked, clean, details = write_manuscripts(book, tmp_path / "out", result["accepted"])
    assert paragraph_views(tracked, "reject") == result["original"]
    assert paragraph_views(clean) == result["accepted"]
    assert len([v for v in result["accepted"].values() if "has the error" in v]) == 2
    report = _report(result, details)
    assert "word/footnotes.xml: 1" in report and "word/endnotes.xml: 1" in report


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
    from types import SimpleNamespace
    from docproof.models import DocumentModel
    flow = FixedWorkflow(make_book(text), tmp_path / "unit", calls=readers or Readers())
    flow.original = {"p": text}
    flow.current = dict(flow.original)
    flow.prose_prepared = SimpleNamespace(doc=DocumentModel(str(flow.source), ()))
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
    proposals, comments, coverage = flow._read("broken_repair", OPUS, ids=["p"])
    assert proposals == comments == []
    assert coverage[0]["paragraph_ids"] == ["p"]
    assert "scope" in flow.history[-1]["rejected_proposal"]["reason"]
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
    assert [x["model"] for x in flow.calls.events] == [LUNA, SONNET, OPUS, LUNA]


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
    assert [x["stage"] for x in flow.calls.events] == ["check_correction", "check_correction_sonnet", "check_correction_disputes"]


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
    flow._comments([comment_decision(flow.questions[0], quote="her")], "astra", model=ASTRA)
    assert flow.questions == []
    assert "unambiguous contextual quote" in flow.history[-1]["rejected_proposal"]["reason"]


def test_failed_parallel_batch_cancels_queued_reads(monkeypatch):
    from concurrent.futures import Future
    from galley.fixed_parallel import ReadScheduler
    from galley.fixed_policy import configuration
    scheduled = []
    class ControlledExecutor:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def submit(self, callback, *args):
            future = Future()
            if not scheduled:
                future.set_exception(FixedWorkflowError("failed read"))
            scheduled.append(future)
            return future
    monkeypatch.setattr("galley.fixed_parallel.ThreadPoolExecutor", ControlledExecutor)
    with pytest.raises(FixedWorkflowError, match="failed read"):
        ReadScheduler(configuration()).map([(SONNET, lambda: None)] * 4)
    assert all(future.cancelled() for future in scheduled[1:])


def test_new_astra_question_gets_explicit_final_astra_comment_review(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "astra":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [row["id"]],
                    "findings": [finding(row["id"], "someone", "", "author_question", action="query", missing="The intended identity")],
                    "comment_decisions": [], "editorial_verdict": "ready"}
        if stage == "astra_screen":
            return {"decisions": [{**ruling(x, "query"), "missing_knowledge": "The intended identity", "question": "Who was he waiting for?"}
                                  for x in payload["sites"]]}
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("He waited for someone."), tmp_path / "run", calls=readers).run()
    assert len(result["questions"]) == 1
    assert readers.events[-1]["stage"] == "astra_comment_review"
    assert readers.events[-1]["model"] == ASTRA


def test_stale_source_fails_before_any_model_call(make_book, tmp_path):
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
    assert readers.events == []


@pytest.mark.parametrize("actual", [["p1", "p2"], ["c1", "p2", "p1"], ["p1", "c2", "p2", "c1"]])
def test_typed_context_coverage_is_normalized_without_changing_response(actual):
    from docproof.models import Chunk, ParagraphRef
    from galley.fixed_workflow import _typed_response
    def p(pid):
        return ParagraphRef(pid, "word/document.xml", "body", "A quiet room.", "Normal")
    chunk = Chunk("chunk-2", (p("p1"), p("p2")), 20, (p("c1"), p("c2")))
    parsed = {"findings": [], "reviewed_paragraph_ids": actual[:]}
    response = ProviderResult(parsed=parsed)
    normalized = _typed_response(response, chunk)
    assert set(normalized.parsed["reviewed_paragraph_ids"]) == {"p1", "p2"}
    assert response.parsed == parsed and response.parsed["reviewed_paragraph_ids"] == actual
    assert normalized is not response and normalized.parsed is not response.parsed


@pytest.mark.parametrize("actual", [["p1", "c1"], ["p1", "p2", "unknown"],
                                   ["p1", "p2", "p2"], ["p1", "p2", "c1", "c1"], [], None])
def test_typed_context_tolerance_never_invents_or_duplicates_owned_coverage(actual):
    from docproof.models import Chunk, ParagraphRef
    from galley.fixed_workflow import _typed_response
    def p(pid):
        return ParagraphRef(pid, "word/document.xml", "body", "A quiet room.", "Normal")
    chunk = Chunk("chunk-2", (p("p1"), p("p2")), 20, (p("c1"),))
    with pytest.raises(FixedWorkflowError, match="Typed paragraph coverage"):
        _typed_response(ProviderResult(parsed={"findings": [], "reviewed_paragraph_ids": actual}), chunk)


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


@pytest.mark.parametrize("decision", ["apply", "drop"])
def test_local_grammar_missed_by_both_readers_requires_pair_screen_then_luna(
        make_book, tmp_path, monkeypatch, local_scans, decision):
    from types import SimpleNamespace
    scanned = []

    class LocalTool:
        picky = False

        def check(self, text):
            scanned.append(text)
            return [SimpleNamespace(offset=text.index("is"), error_length=2,
                replacements=["are"], rule_id="TEST_SUBJECT_AGREEMENT",
                rule_issue_type="grammar", message="The plural subject takes are.")]

        def close(self):
            pass

    def handler(stage, model, payload, kwargs):
        if stage == "typed_screen":
            assert model in {SONNET, LUNA}
            assert len(payload["sites"]) == 1
            proposal = payload["sites"][0]["proposals"][0]
            assert proposal["models"] == ["local:languagetool"]
            return {"decisions": [ruling(site, decision, "are") for site in payload["sites"]]}

    # All local generators and the scanner's real filtering/coverage adapter
    # run; only the external Java transport is replaced.
    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", local_scans["initial"])
    monkeypatch.setattr("galley.fixed_local.default_lt_factory", lambda dictionary: LocalTool())
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("They is here."), tmp_path / "local", calls=readers).run()

    assert list(result["accepted"].values()) == ["They are here." if decision == "apply" else "They is here."]
    assert result["questions"] == []
    assert scanned == ["They is here."]
    events = readers.events
    typed = [row for row in events if row["stage"] == "typed"]
    assert {row["model"] for row in typed} == {SONNET, LUNA}
    assert {row["model"] for row in events if row["stage"] == "typed_screen"} == {SONNET, LUNA}
    assert not any(row["stage"] == "typed_disputes" for row in events)
    checks = [row for row in events if row["stage"] in {"checks_meaning", "checks_correction"}]
    assert len(checks) == (2 if decision == "apply" else 0)
    assert all(row["model"] == LUNA for row in checks)
    if checks:
        assert all(row["payload"]["changes"][0]["after"] == "They are here." for row in checks)
    saved = json.loads((tmp_path / "local/stages/typed.json").read_text())
    local = saved["evidence"]["local"]
    assert local["proposal_count"] == 1
    assert next(check for check in local["checks"] if check["check"] == "languagetool")["proposal_count"] == 1


def test_local_completion_runs_after_ensemble_fable_and_astra(make_book, tmp_path, monkeypatch):
    completion_calls = []

    def complete(prepared, original, texts, *args, **kwargs):
        completion_calls.append((kwargs["stage"], dict(texts)))
        pid = next(iter(texts))
        rows = [_local_row(pid, "teh", "the", source="recurrence", category="spelling")] if "teh" in texts[pid] else []
        return rows, {"recurrence_candidates": len(rows)}

    def handler(stage, model, payload, kwargs):
        if stage == "local_completion_screen":
            assert model in {SONNET, LUNA}
            return {"decisions": [ruling(site, replacement=site["proposals"][0]["replacement"])
                                  for site in payload["sites"]]}

    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates", complete)
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("She found teh letter."), tmp_path / "completion", calls=readers).run()

    assert list(result["accepted"].values()) == ["She found the letter."]
    assert [stage for stage, _ in completion_calls] == ["completion", "completion_fable", "completion_astra"]
    assert all(texts == result["accepted"] for stage, texts in completion_calls[1:])
    events = readers.events
    stages = [row["stage"] for row in events]
    assert max(stages.index("ensemble_sweep_opus"), stages.index("ensemble_sweep_sol")) < stages.index("local_completion_screen")
    assert stages.index("local_completion_screen") < stages.index("local_completion_checks_meaning")
    assert stages.index("local_completion_checks_meaning") < stages.index("local_completion_checks_correction") < stages.index("fable")
    assert [row for row in events if row["stage"] == "fable"][0]["payload"]["paragraphs"][0]["text"] == "She found the letter."
    assert result["questions"] == []
    for stage in ("fable", "astra"):
        saved = json.loads((tmp_path / f"completion/stages/{stage}.json").read_text())
        assert saved["evidence"]["local"] == {"recurrence_candidates": 0}


def test_fable_edit_is_propagated_by_the_post_fable_completion_pass(make_book, tmp_path, monkeypatch):
    def complete(prepared, original, texts, *args, **kwargs):
        if kwargs["stage"] == "completion_fable" and texts["body-0000"] != original["body-0000"]:
            return [_local_row("body-0001", "Beckham", "Brooks", source="recurrence", category="spelling")], {"seeded": 1}
        return [], {"seeded": 0}

    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                    "findings": [finding(row["id"], "Beckham", "Brooks", "spelling")] if row["id"] == "body-0000" else [],
                    "comment_decisions": [], "editorial_verdict": "ready"}
        if stage == "local_completion_fable_screen":
            return {"decisions": [ruling(site, replacement=site["proposals"][0]["replacement"])
                                  for site in payload["sites"]]}

    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates", complete)
    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("Beckham smiled.", "Then Beckham left."), tmp_path / "propagate", calls=readers).run()
    assert list(result["accepted"].values()) == ["Brooks smiled.", "Then Brooks left."]
    astra = [row for row in readers.events if row["stage"] == "astra"]
    assert astra and [p["text"] for p in astra[0]["payload"]["paragraphs"]] == ["Brooks smiled.", "Then Brooks left."]
    assert json.loads((tmp_path / "propagate/stages/fable.json").read_text())["evidence"]["local"] == {"seeded": 1}


def test_poetry_never_invokes_local_collectors(make_book, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Poetry must not enter a local proofreading collector")

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", forbidden)
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates", forbidden)
    readers = Readers(poetry=True)
    result = FixedWorkflow(make_book("The Moon\n  waits, 20 times\n—quiet"),
                           tmp_path / "poetry-local", calls=readers).run()
    assert result["accepted"] == result["original"]
    assert {row["stage"] for row in readers.events} == {"poetry", "spelling"}


def test_local_collector_cannot_emit_a_candidate_in_embedded_poetry(
        make_book, tmp_path, monkeypatch):
    def handler(stage, model, payload, kwargs):
        if stage == "poetry":
            return {"classification": "mixed", "reason": "Verse and prose."}
        if stage == "poetry_sections":
            return {"paragraphs": [{"id": row["id"], "poetry": "\n" in row["text"]} for row in payload]}

    def leak(prepared, texts, *args, **kwargs):
        pid = next(iter(kwargs["poetry_ids"]))
        return [_local_row(pid, "Moon", "moon", category="spelling")], {}

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", leak)
    readers = Readers(handler=handler)
    with pytest.raises(FixedWorkflowError, match="protected poetry"):
        FixedWorkflow(make_book("The Moon\n  waits", "She waited by the door."),
                      tmp_path / "mixed-local", calls=readers).run()
    assert not any(row["stage"] in {"typed_disputes", "fable", "astra"} for row in readers.events)


def test_real_local_generators_exclude_embedded_poetry_at_both_checkpoints(
        make_book, tmp_path, monkeypatch, local_scans):
    scanned = []

    class LocalTool:
        def check(self, text):
            scanned.append(text)
            return []

        def close(self):
            pass

    def handler(stage, model, payload, kwargs):
        if stage == "poetry":
            return {"classification": "mixed", "reason": "Verse and prose."}
        if stage == "poetry_sections":
            return {"paragraphs": [{"id": row["id"], "poetry": "\n" in row["text"]} for row in payload]}

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", local_scans["initial"])
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates", local_scans["completion"])
    monkeypatch.setattr("galley.fixed_local.default_lt_factory", lambda dictionary: LocalTool())
    source = make_book("teh Moon\n  waits, 20 times\n—quiet", "A quiet paragraph.")
    readers = Readers(handler=handler)
    result = FixedWorkflow(source, tmp_path / "real-mixed-local", calls=readers).run()

    assert scanned == ["A quiet paragraph."]
    assert result["accepted"] == result["original"]
    assert result["questions"] == []
    for stage, expected in (("typed", "initial"), ("ensemble_sweep", "completion"),
                            ("fable", "completion_fable"), ("astra", "completion_astra")):
        saved = json.loads((tmp_path / f"real-mixed-local/stages/{stage}.json").read_text())
        local = saved["evidence"]["local"]
        assert local["stage"] == expected
        assert local["paragraph_ids"] == ["body-0001"]
        assert local["excluded_poetry_ids"] == ["body-0000"]
        assert all(check["paragraph_ids"] == ["body-0001"] for check in local["checks"])


def test_missing_languagetool_prevents_later_stages_after_parallel_reads(
        make_book, tmp_path, monkeypatch, local_scans):
    from galley.fixed_local import FixedLocalError

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", local_scans["initial"])
    monkeypatch.setattr("docproof.languagetool.AVAILABLE", False)
    readers = Readers()
    with pytest.raises((FixedLocalError, FixedWorkflowError), match="LanguageTool"):
        FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "missing-local", calls=readers).run()
    assert not any(row["stage"] in {"fable", "astra"} for row in readers.events)
    assert not (tmp_path / "missing-local/result.json").exists()


def test_stylistic_diagnostic_cannot_become_a_local_proofreading_candidate(
        make_book, tmp_path, monkeypatch):
    def inappropriate(prepared, texts, *args, **kwargs):
        pid = next(iter(texts))
        return [_local_row(pid, texts[pid], texts[pid], source="reading_level", category="reading_level")], {}

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", inappropriate)
    readers = Readers()
    with pytest.raises(FixedWorkflowError, match="stylistic diagnostic"):
        FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "style-local", calls=readers).run()
    assert not any(row["stage"] == "typed_disputes" for row in readers.events)


def test_unchanged_rejected_local_site_is_not_paid_for_again_at_completion(
        make_book, tmp_path, monkeypatch):
    def initial(prepared, texts, *args, **kwargs):
        return [_local_row(next(iter(texts)), "quiet", "calm")], {}

    def completion(prepared, original, texts, *args, **kwargs):
        return [_local_row(next(iter(texts)), "quiet", "calm", source="recurrence")], {}

    monkeypatch.setattr("galley.fixed_local.collect_local_candidates", initial)
    monkeypatch.setattr("galley.fixed_local.collect_completion_candidates", completion)
    readers = Readers()  # Both screeners reject the stylistic suggestion.
    result = FixedWorkflow(make_book("A quiet paragraph."), tmp_path / "dedup-local", calls=readers).run()
    assert result["accepted"] == result["original"] and result["questions"] == []
    assert sum(row["stage"] == "typed_screen" for row in readers.events) == 2
    assert not any(row["stage"] in {"typed_disputes", "local_completion_screen", "local_completion_disputes",
                                    "local_completion_fable_screen", "local_completion_astra_screen"}
                   for row in readers.events)


def test_call_coverage_freezes_all_assigned_inventories_and_only_known_read_context():
    from galley.fixed_workflow import _call_coverage, FRONTIER_SCHEMA, READ_SCHEMA, CHECK_SCHEMA, DECISIONS
    payload = {"paragraphs": [{"id": "p1"}], "context": {"c1": "Read-only context."},
               "focused_sites": [{"id": "f1"}], "comments": [{"id": "q1"}]}
    contract = _call_coverage(payload, FRONTIER_SCHEMA)
    assert contract == {
        "reviewed_ids": {"ids": ["p1"], "id_key": None, "context_ids": ["c1"]},
        "reviewed_check_ids": {"ids": ["f1"], "id_key": None, "context_ids": []},
        "comment_decisions": {"ids": ["q1"], "id_key": "id", "context_ids": []}}
    assert _call_coverage({"sites": [{"id": "n1"}], "paragraphs": {"p1": "Text."}}, READ_SCHEMA)["reviewed_ids"]["ids"] == ["n1"]
    assert _call_coverage({"changes": [{"id": "p1"}]}, CHECK_SCHEMA)["decisions"]["ids"] == ["p1"]
    assert _call_coverage({"sites": [{"id": "d1"}]}, DECISIONS)["decisions"]["ids"] == ["d1"]


@pytest.mark.parametrize("defect", ["missing_quote", "unknown_paragraph", "bad_occurrence", "empty_quote"])
def test_unanchored_reader_proposal_is_audited_without_edit_or_comment(make_book, tmp_path, defect):
    from galley.fixed_workflow import _hash
    flow = FixedWorkflow(make_book("A quiet room."), tmp_path / "run", calls=Readers())
    texts = {"p1": "A quiet room."}
    row = finding("p1", "quiet", "silent")
    if defect == "missing_quote":
        row["quote"] = "A quote longer than the entire paragraph that the reader invented."
    elif defect == "unknown_paragraph":
        row["para_id"] = "another-paragraph"
    elif defect == "bad_occurrence":
        row["occurrence"] = 2
    else:
        row["quote"] = ""
    before = json.loads(json.dumps(row))
    assert flow._reader_candidate("fable", row, texts, FABLE) is None
    rejected = flow.history[0]["rejected_proposal"]
    assert rejected["status"] == "rejected_no_anchor"
    assert rejected["finding"] == row == before
    assert rejected["reviewed_sha256"] == _hash(texts)
    assert flow.questions == [] and texts == {"p1": "A quiet room."}
    valid = flow._reader_candidate("fable", finding("p1", "quiet", "silent"), texts, FABLE)
    assert valid is not None
    text = texts["p1"]
    assert text[:valid["start"]] + valid["replacement"] + text[valid["end"]:] == "A silent room."


def test_rejected_model_proposal_handling_does_not_hide_local_or_internal_failures(make_book, tmp_path, monkeypatch):
    from types import SimpleNamespace
    flow = FixedWorkflow(make_book("A quiet room."), tmp_path / "run", calls=Readers())
    texts = {"p1": "A quiet room."}
    row = {**finding("p1", "Invented quote.", "Replacement."), "source": "local:sweeps"}
    with pytest.raises(FixedWorkflowError, match="quote does not occur"):
        flow._local_candidates([row], texts=texts, prepared=SimpleNamespace(query_types=(), format_types={}))
    assert flow.history == []
    def unexpected(*args, **kwargs):
        raise RuntimeError("Unrelated integrity failure")
    monkeypatch.setattr("galley.fixed_workflow._candidate", unexpected)
    with pytest.raises(RuntimeError, match="Unrelated integrity failure"):
        flow._reader_candidate("typed", row, texts, SONNET)
    assert flow.history == []


def test_broken_sentence_reader_rejects_unanchored_repairs_but_keeps_coverage(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        return {"reviewed_ids": ["p"], "findings": [finding("p", "An invented longer sentence.", "A rewrite.", "broken_sentence")],
                "comment_decisions": [], "editorial_verdict": "ready"}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    proposals, comments, coverage = flow._read("broken_repair", OPUS, ids=["p"])
    assert proposals == comments == []
    assert coverage[0]["paragraph_ids"] == ["p"]
    assert flow.history[0]["rejected_proposal"]["status"] == "rejected_no_anchor"


def test_rejected_proposal_stage_evidence_is_stable_across_reader_completion_order(make_book, tmp_path):
    flow = FixedWorkflow(make_book("A quiet room."), tmp_path / "run", calls=Readers())
    texts = {"p1": "A quiet room."}
    for label, model in (("opus", OPUS), ("sol", SOL)):
        flow._reader_candidate("ensemble_sweep_" + label, finding("p1", "Missing text.", "Replacement."), texts, model)
    flow._record("ensemble_sweep", readings=[])
    path = flow.directory / "stages/ensemble_sweep.json"
    before = path.read_bytes()
    assert len(json.loads(before)["evidence"]["rejected_proposals"]) == 2
    flow.history.reverse()
    flow._record("ensemble_sweep", readings=[])
    assert path.read_bytes() == before


@pytest.mark.parametrize("defect", ["scope", "control", "format_text", "format_unknown", "format_italic"])
def test_invalid_model_proposal_drops_only_bad_suggestion(make_book, tmp_path, defect):
    flow = _flow(make_book, tmp_path)
    bad = finding("p", "someone", "someone", "format")
    options = {"format_types": {"format": "italic"},
               "formatting": {"p": [{"start": 0, "end": len(flow.current["p"]), "italic": False}]}}
    if defect == "scope":
        options["allowed_categories"] = {"broken_sentence"}
    elif defect == "control":
        bad["replacement"] = "some\x00one"
    elif defect == "format_text":
        bad["replacement"] = "Mary"
    elif defect == "format_unknown":
        options["formatting"]["p"][0]["italic"] = None
    else:
        options["formatting"]["p"][0]["italic"] = True
    assert flow._reader_candidate("fable", bad, flow.current, FABLE, **options) is None
    good = flow._reader_candidate("fable", finding("p", "waited", "waits"), flow.current, FABLE)
    flow._apply("fable", [good])
    assert flow.current["p"] == "He waits for someone."
    assert flow.formats == flow.questions == []
    assert flow.history[0]["rejected_proposal"]["finding"] == bad


@pytest.mark.parametrize("defect", ["blank_question", "blank_knowledge", "control", "missing_quote"])
def test_invalid_comment_drops_while_valid_comment_survives(make_book, tmp_path, defect):
    flow = _flow(make_book, tmp_path)
    for missing in ("Identity", "Location"):
        flow._question("p", flow.current["p"], "What is the " + missing + "?", missing, "Unresolved.", "typed")
    decisions = [comment_decision(q) for q in flow.questions]
    bad = decisions[0]
    bad[{"blank_question": "question", "blank_knowledge": "missing_knowledge",
         "control": "question", "missing_quote": "quote"}[defect]] = {
             "blank_question": " ", "blank_knowledge": " ", "control": "Who\x00?", "missing_quote": "Invented quote."}[defect]
    flow._comments(decisions, "astra", model=ASTRA)
    assert len(flow.questions) == 1
    assert flow.questions[0]["missing_knowledge"] == "Location"
    assert flow.history[-2]["rejected_proposal"]["model"] == ASTRA


@pytest.mark.parametrize("action", ["apply", "query"])
def test_invalid_opus_disposition_drops_site_without_losing_good_edit(make_book, tmp_path, action):
    def handler(stage, model, payload, kwargs):
        if stage == "typed_screen":
            return {"decisions": [ruling(site, "apply" if model == SONNET else "drop", site["proposals"][0]["replacement"])
                                  for site in payload["sites"]]}
        assert stage == "typed_disputes"
        return {"decisions": [ruling(site, action, "bad\x00text") if site["before"] == "someone"
                              else ruling(site, "apply", site["proposals"][0]["replacement"]) for site in payload["sites"]]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    rows = [_candidate(finding("p", before, after), flow.current, SONNET)
            for before, after in (("waited", "waits"), ("someone", "Mary"))]
    flow._apply("typed", flow._adjudicate("typed", rows, force=True))
    assert flow.current["p"] == "He waits for someone."
    assert flow.questions == []
    assert len([h for h in flow.history if h.get("rejected_proposal")]) == 1


def test_invalid_check_adjudication_restores_text_and_removes_disputed_format(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "check_meaning":
            return {"decisions": [{"id": "p", "verdict": "reject", "reason": "Unsafe edit."}]}
        if stage == "check_meaning_disputes":
            return {"decisions": [ruling({"id": "p"}, "apply", "Bad\x00paragraph.")]}
    flow = _flow(make_book, tmp_path, Readers(handler=handler))
    before = flow._apply("fable", [_candidate(finding("p", "waited", "waits"), flow.current, FABLE),
                                  _candidate(finding("p", "someone", "someone", "format"), flow.current, FABLE,
                                             format_types={"format": "italic"})])
    flow._checks("check", before)
    assert flow.current == before
    assert flow.formats == flow.questions == []
    assert len([h for h in flow.history if h.get("rejected_proposal")]) == 1


def test_number_scope_noise_does_not_prevent_valid_number_correction(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "numbers":
            return {"reviewed_ids": [s["id"] for s in payload["sites"]],
                    "findings": [finding("p", "birds", "bees", "grammar"),
                                 finding("p", "20", "twenty", "number_style")],
                    "comment_decisions": [], "editorial_verdict": "ready"}
    flow = _flow(make_book, tmp_path, Readers(handler=handler), text="There were 20 birds.")
    flow._numbers()
    assert flow.current["p"] == "There were twenty birds."
    assert len([h for h in flow.history if h.get("rejected_proposal")]) == 2


def test_comment_resolution_cannot_depend_on_discarded_reader_proposal(make_book, tmp_path):
    flow = _flow(make_book, tmp_path)
    flow._question("p", flow.current["p"], "Who is the visitor?", "Identity", "Unresolved.", "typed")
    before = dict(flow.current)
    assert flow._reader_candidate("fable", finding("p", "someone", "Mary\x00"), flow.current, FABLE) is None
    flow._comments([comment_decision(flow.questions[0], "drop")], "fable", before=before, model=FABLE)
    assert len(flow.questions) == 1
    assert flow.calls.events[-1]["stage"] == "fable_comment_review"
    assert flow.current == before


# --- final walk-through scope, book map, reader guards ------------------------

def test_walkthrough_prompts_reach_only_the_final_readers_and_their_checks(make_book, tmp_path):
    from galley.press_prompt import CONTINUITY_TASK, FINAL_WALKTHROUGH, FINAL_WALKTHROUGH_CHECK

    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                    "findings": [{**finding(row["id"], "brand new", "brand-new", "usage"), "evidence": []}],
                    "comment_decisions": [], "editorial_verdict": "ready"}

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("A brand new board waited."), tmp_path / "scope", calls=readers).run()
    assert list(result["accepted"].values()) == ["A brand-new board waited."]
    systems = {}
    for row in readers.events:
        systems.setdefault(row["stage"], row["system"])
    assert all(FINAL_WALKTHROUGH in systems[stage] for stage in ("fable", "astra"))
    assert all(FINAL_WALKTHROUGH not in systems[stage] for stage in systems if stage not in {"fable", "astra"})
    assert CONTINUITY_TASK in systems["continuity"]
    assert all(CONTINUITY_TASK not in systems[stage] for stage in systems if stage != "continuity")
    assert FINAL_WALKTHROUGH_CHECK in systems["fable_checks_meaning"]
    assert FINAL_WALKTHROUGH_CHECK not in systems.get("typed_screen", "")
    fable = [row for row in readers.events if row["stage"] == "fable"]
    assert all(row["payload"]["book_map"]["complete_inventory"] is True for row in fable)
    assert "usage" in fable[0]["schema"]["properties"]["findings"]["items"]["properties"]["category"]["enum"]
    from galley.fixed_workflow import READ_SCHEMA
    assert "usage" not in READ_SCHEMA["properties"]["findings"]["items"]["properties"]["category"]["enum"]


def test_book_map_lists_headings_and_running_heads_and_header_edits_round_trip(tmp_path):
    from galley.fixed_documents import write_manuscripts
    document = Document()
    document.add_paragraph("CHAPTER 2", style="Heading 1")
    document.add_paragraph("The first body paragraph of the chapter.")
    document.add_paragraph("The second body paragraph of the chapter.")
    document.add_paragraph("TOP TEN")
    document.add_paragraph("Only one item.")
    document.sections[0].header.paragraphs[0].text = "CHAPTER ONE"
    source = tmp_path / "book.docx"
    document.save(source)
    header_id = None

    def handler(stage, model, payload, kwargs):
        nonlocal header_id
        if stage == "fable":
            heads = [x for x in payload["paragraphs"] if x["id"].startswith("header")]
            if heads:
                header_id = heads[0]["id"]
                return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                        "findings": [{**finding(header_id, "CHAPTER ONE", "CHAPTER 1", "structure"), "evidence": []}],
                        "comment_decisions": [], "editorial_verdict": "ready"}

    readers = Readers(handler=handler)
    result = FixedWorkflow(source, tmp_path / "map", calls=readers).run()
    assert result["accepted"][header_id] == "CHAPTER 1"
    book_map = next(row for row in readers.events if row["stage"] == "fable")["payload"]["book_map"]
    assert [(h["text"], h["signal"], h["body_paragraphs"]) for h in book_map["headings"]] == [
        ("CHAPTER 2", "style", 2), ("TOP TEN", "caps_line", 1)]
    assert [(h["location"], h["text"]) for h in book_map["headers_footers"]] == [("header", "CHAPTER ONE")]
    tracked, clean, _ = write_manuscripts(source, tmp_path / "final", result["accepted"])
    assert Document(clean).sections[0].header.paragraphs[0].text == "CHAPTER 1"


@pytest.mark.parametrize("replacement, problem", [
    ("*Huckleberry Finn*", "markup"),
    ("Huckleberry\nFinn", "line break"),
])
def test_reader_replacements_that_are_not_manuscript_text_are_rejected(replacement, problem):
    from galley.fixed_workflow import RejectedModelProposal
    texts = {"p1": "She read Huckleberry Finn twice."}
    with pytest.raises(RejectedModelProposal, match=problem) as info:
        _candidate(finding("p1", "Huckleberry Finn", replacement), texts, FABLE)
    assert info.value.status == "rejected_invalid_proposal"
    with pytest.raises(RejectedModelProposal, match="paragraph start"):
        _candidate(finding("p1", "She", " She"), texts, FABLE)
    assert _candidate(finding("p1", "She read", "She had read"), texts, FABLE)["replacement"] == "had "


def test_markup_from_a_final_reader_lands_in_the_rejected_diagnostics(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "fable":
            row = payload["paragraphs"][0]
            return {"reviewed_ids": [x["id"] for x in payload["paragraphs"]],
                    "findings": [{**finding(row["id"], "Huckleberry Finn", "*Huckleberry Finn*", "typesetting"), "evidence": []}],
                    "comment_decisions": [], "editorial_verdict": "ready"}

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("She read Huckleberry Finn twice."), tmp_path / "markup", calls=readers).run()
    assert result["accepted"] == result["original"]
    rejected = [h for h in result["history"] if h.get("rejected_proposal") and h["stage"] == "fable"]
    assert rejected and rejected[0]["rejected_proposal"]["status"] == "rejected_invalid_proposal"
    assert "markup" in rejected[0]["rejected_proposal"]["reason"]


# --- continuity lane ----------------------------------------------------------

def continuity_finding(pid, quote, replacement, evidence, *, action="edit", question="", missing=""):
    return {"para_id": pid, "quote": quote, "occurrence": 1, "replacement": replacement,
            "action": action, "category": "continuity", "reason": "The surname is established elsewhere.",
            "question": question, "missing_knowledge": missing, "evidence": evidence}


def test_continuity_reads_the_whole_book_once_and_opus_rules_with_the_cited_evidence(make_book, tmp_path):
    seen = {}

    def handler(stage, model, payload, kwargs):
        if stage == "continuity":
            seen["payload"] = payload
            assert model == FABLE
            return {"findings": [continuity_finding("body-0001", "Beckham", "Brooks",
                                                    [{"para_id": "body-0000", "quote": "Kai Brooks smiled."}])],
                    "reading_notes": "One surname split."}
        if stage == "continuity_adjudication":
            assert model == OPUS
            seen["sites"] = payload["sites"]
            return {"decisions": [ruling(site, replacement=site["proposals"][0]["replacement"]) for site in payload["sites"]]}
        if stage == "continuity_checks_meaning":
            seen["checks"] = payload["changes"]

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("Kai Brooks smiled.", "Then Kai Beckham left."), tmp_path / "continuity", calls=readers).run()
    assert list(result["accepted"].values()) == ["Kai Brooks smiled.", "Then Kai Brooks left."]
    assert [row["id"] for row in seen["payload"]["book"]] == ["body-0000", "body-0001"]
    assert seen["payload"]["complete_book"] is True and seen["payload"]["part"] == [1, 1]
    assert all(row["location"] == "body" for row in seen["payload"]["book"])
    [site] = seen["sites"]
    assert site["evidence_paragraphs"] == {"body-0000": "Kai Brooks smiled."}
    assert site["proposals"][0]["evidence"][0]["quote"] == "Kai Brooks smiled."
    assert seen["checks"][0]["evidence"][0]["para_id"] == "body-0000"
    assert not any(row["stage"] == "continuity_screen" for row in readers.events)
    applied = [h for h in result["history"] if h.get("applied") and h["stage"] == "continuity"]
    assert applied[0]["applied"]["models"] == [FABLE, OPUS]
    assert [s["stage"] for s in result["stages"]].index("continuity") < [s["stage"] for s in result["stages"]].index("fable")


@pytest.mark.parametrize("evidence, reason", [
    ([], "lacks cited evidence"),
    ([{"para_id": "body-0001", "quote": "Beckham"}], "another paragraph"),
    ([{"para_id": "body-0000", "quote": "Kai Brooks frowned."}], "does not occur verbatim"),
])
def test_continuity_edits_without_verified_evidence_are_rejected(make_book, tmp_path, evidence, reason):
    def handler(stage, model, payload, kwargs):
        if stage == "continuity":
            return {"findings": [continuity_finding("body-0001", "Beckham", "Brooks", evidence)],
                    "reading_notes": ""}

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("Kai Brooks smiled.", "Then Kai Beckham left."), tmp_path / "evidence", calls=readers).run()
    assert result["accepted"] == result["original"]
    assert not any(row["stage"] == "continuity_adjudication" for row in readers.events)
    rejected = [h for h in result["history"] if h.get("rejected_proposal") and h["stage"] == "continuity"]
    assert rejected and reason in rejected[0]["rejected_proposal"]["reason"]


@pytest.mark.parametrize("path", ["opus_drop", "opus_query", "reader_query"])
def test_continuity_drop_and_query_paths(make_book, tmp_path, path):
    evidence = [{"para_id": "body-0000", "quote": "the Rusty Hook Tavern"}]

    def handler(stage, model, payload, kwargs):
        if stage == "continuity":
            if path == "reader_query":
                return {"findings": [continuity_finding("body-0001", "the Mad Crabber", "the Mad Crabber", evidence,
                                                        action="query", question="Which name is the restaurant's?",
                                                        missing="The intended restaurant name")], "reading_notes": ""}
            return {"findings": [continuity_finding("body-0001", "the Mad Crabber", "the Rusty Hook Tavern", evidence)],
                    "reading_notes": ""}
        if stage == "continuity_adjudication":
            if path == "opus_drop":
                return {"decisions": [ruling(site, "drop") for site in payload["sites"]]}
            return {"decisions": [{**ruling(site, "query"), "question": "Which name is the restaurant's?",
                                   "missing_knowledge": "The intended restaurant name"} for site in payload["sites"]]}

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("They ate at the Rusty Hook Tavern.", "Later they left the Mad Crabber."),
                           tmp_path / path, calls=readers).run()
    assert result["accepted"] == result["original"]
    if path == "opus_drop":
        assert result["questions"] == []
    else:
        assert [q["question"] for q in result["questions"]] == ["Which name is the restaurant's?"]
        assert result["questions"][0]["stage"] == "continuity"
    assert any(row["stage"] == "continuity_adjudication" for row in readers.events) == (path != "reader_query")


def test_a_skipped_continuity_read_still_delivers(make_book, tmp_path):
    def handler(stage, model, payload, kwargs):
        if stage == "continuity":
            return {"_skipped_read": {"stage": stage, "model": model, "reason": "Simulated exhausted read"}}

    readers = Readers(handler=handler)
    result = FixedWorkflow(make_book("A quiet page."), tmp_path / "skipped", calls=readers).run()
    assert result["review_complete"] is False and result["skipped_reads"]
    continuity = json.loads((tmp_path / "skipped/stages/continuity.json").read_text())["evidence"]
    assert continuity["coverage"][0]["status"] == "skipped" and continuity["skipped_reads"]
    assert [row["stage"] for row in readers.events if row["stage"] in {"fable", "astra"}] == ["fable", "astra"]


def test_a_version_2_workspace_requires_a_fresh_run(make_book, tmp_path):
    source = make_book("A quiet page.")
    flow = FixedWorkflow(source, tmp_path / "v2", calls=object())
    marker = json.loads(flow.manifest.read_text())
    marker["identity"]["version"] = "fixed-proofreading-v2"
    flow.manifest.write_text(json.dumps(marker))
    with pytest.raises(FixedWorkflowError, match="fresh workspace"):
        FixedWorkflow(source, tmp_path / "v2", calls=object())
