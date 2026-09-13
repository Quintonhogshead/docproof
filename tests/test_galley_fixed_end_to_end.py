"""Real orchestration, receipts and Word delivery with offline model endpoints."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

import pytest
from docx import Document

from docproof.providers import NormalizedUsage, ProviderResult
from galley import codex_runner, driver as gd, fixed_calls as fc
from galley.fixed_documents import paragraph_views, validate_delivery_package
from galley.fixed_workflow import ASTRA, FABLE, LUNA, OPUS, SOL, SONNET


@pytest.fixture(autouse=True)
def local_transport(monkeypatch):
    """Keep the local rules/cache real; replace only the external Java service."""
    from galley import fixed_local
    checked = []

    class LocalTool:
        picky = False

        def check(self, text):
            checked.append(text)
            return []

        def close(self):
            pass

    monkeypatch.setattr(fixed_local, "default_lt_factory", lambda dictionary: LocalTool())
    return checked


class ScriptedReaders:
    def __init__(self, poetry):
        self.poetry = poetry
        self.requests = []

    def answer(self, model, user, schema):
        self.requests.append((model, user))
        fields = schema["properties"]
        if "classification" in fields:
            return {"classification": "poetry" if self.poetry else "prose", "reason": "Fixture classification."}
        if "narration" in fields:
            return {"narration": "Third person, past tense.", "characters": [], "notes": []}
        if "reviewed_paragraph_ids" in fields:
            paragraphs = re.findall(r'<paragraph id="([^"]+)">\n(.*?)\n</paragraph>', user, re.S)
            item = fields["findings"]["items"]
            if "$ref" in item:
                item = schema["$defs"][item["$ref"].split("/")[-1]]
            props = item["properties"]
            kinds = props["error_type"].get("enum", [props["error_type"].get("const")])
            findings = []
            if "spelling" in kinds:
                for pid, text in paragraphs:
                    if "recieved" in text:
                        row = dict(para_id=pid, error_type="spelling", original_text="recieved",
                                   occurrence=1, corrected_text="received", confidence="high")
                        if "explanation" in props:
                            row["explanation"] = "Correct the misspelling."
                        findings.append(row)
            return {"findings": findings, "reviewed_paragraph_ids": [p[0] for p in paragraphs]}
        payload = json.loads(user)
        if "reviewed_ids" in fields:
            owned = payload.get("paragraphs", [])
            ids = ([x["id"] for x in payload["sites"]] if "sites" in payload else [x["id"] for x in owned])
            return {"reviewed_ids": ids, "findings": [], "comment_decisions": [], "editorial_verdict": "ready",
                    **({"reviewed_check_ids": [s["id"] for s in payload["focused_sites"]]}
                       if "reviewed_check_ids" in fields else {})}
        if "decisions" in fields:
            if "changes" in payload:
                return {"decisions": [{"id": x["id"], "verdict": "approve", "reason": "The spelling correction preserves meaning."}
                                       for x in payload["changes"]]}
            return {"decisions": [{"id": x["id"], "action": "drop", "replacement": "", "reason": "No clear error.",
                                    "missing_knowledge": "", "question": ""} for x in payload["sites"]]}
        raise AssertionError(f"Unexpected reader contract: {list(fields)}")

    def complete_structured(self, **request):
        body = self.answer(request["model"], request["user"], request["schema"])
        return ProviderResult(parsed=body, actual_model=request["model"],
                              usage=NormalizedUsage(input_tokens=100, output_tokens=30),
                              resource_usage={"input_tokens": 100, "output_tokens": 30,
                                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})

    def subscription(self, prompt, schema, directory, **options):
        assert options["no_tools"] is True
        assert options["model"] in {SOL, ASTRA}
        return self.answer(options["model"], prompt.rsplit("\n\n", 1)[-1], schema)


@pytest.mark.parametrize("poetry", [False, True])
def test_real_fixed_driver_delivers_and_resumes_without_new_generations(tmp_path, monkeypatch, poetry, local_transport):
    source = tmp_path / "Writer - Book 1.docx"
    document = Document()
    document.add_paragraph("She recieved two letters.")
    document.save(source)
    readers = ScriptedReaders(poetry)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    monkeypatch.setattr(gd, "spawn_claude", lambda *a, **k: pytest.fail("A supervising Brain started"))
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    documents = [p for p in result.handoff if p.suffix == ".docx"]
    assert len(documents) == 2
    for path in documents:
        assert list(paragraph_views(path).values()) == ["She received two letters."]
    corrected = next(p for p in documents if "Clean" not in p.name)
    assert paragraph_views(corrected, "reject") == paragraph_views(source)
    models = {model for model, _ in readers.requests}
    assert models == ({SONNET} if poetry else {SONNET, LUNA, OPUS, SOL, FABLE, ASTRA})
    count = len(readers.requests)
    assert local_transport == ([] if poetry else ["She recieved two letters."])
    hashes = {x["name"]: x["sha256"] for x in package["artifacts"]}
    again = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode=None).run()
    assert again.outcome == "done", again.reason
    assert len(readers.requests) == count
    assert local_transport == ([] if poetry else ["She recieved two letters."])
    reused = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert {x["name"]: x["sha256"] for x in reused["artifacts"]} == hashes


@pytest.mark.parametrize("text", ["She recieved two letters.",
    "She walked down the quiet lane and went home before the rain arrived"])
def test_interrupted_stage_resumes_from_paid_read_receipts(tmp_path, monkeypatch, text):
    from galley.fixed_workflow import FixedWorkflow

    source = tmp_path / "Writer - Book 1.docx"
    document = Document()
    document.add_paragraph(text)
    document.save(source)
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    stage_start = FixedWorkflow._stage

    def interrupt(self, stage):
        if stage == "fable":
            raise OSError("Simulated local interruption between completed reads")
        stage_start(self, stage)

    monkeypatch.setattr(FixedWorkflow, "_stage", interrupt)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    stopped = worker.run()
    assert stopped.outcome == "blocked" and "Simulated local interruption" in stopped.reason
    count = len(readers.requests)
    assert count > 0 and FABLE not in {model for model, _ in readers.requests}
    monkeypatch.setattr(FixedWorkflow, "_stage", stage_start)
    resumed = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode=None).run()
    assert resumed.outcome == "done", resumed.reason
    assert [model for model, _ in readers.requests[count:]] == [FABLE, ASTRA]
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True


def test_failed_local_code_repair_resumes_without_repeating_paid_intake(tmp_path, monkeypatch):
    from galley import fixed_local
    source = tmp_path / "Writer.docx"
    document = Document()
    document.add_paragraph("She recieved two letters.")
    document.save(source)
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    language_tool = fixed_local._language_tool
    def fail(*a, **k):
        raise fixed_local.FixedLocalError("Simulated local checker failure")
    monkeypatch.setattr(fixed_local, "_versions", lambda: {"checker.py": "before"})
    monkeypatch.setattr(fixed_local, "_language_tool", fail)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "blocked" and "Simulated" in result.reason
    assert [model for model, _ in readers.requests] == [SONNET, LUNA]
    monkeypatch.setattr(fixed_local, "_versions", lambda: {"checker.py": "repaired"})
    monkeypatch.setattr(fixed_local, "_language_tool", language_tool)
    result = worker.run()
    assert result.outcome == "done", result.reason
    assert [user for _, user in readers.requests].count(readers.requests[0][1]) == 1
    assert [user for _, user in readers.requests].count(readers.requests[1][1]) == 1
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True


def test_fixed_checkpoint_never_reports_done_before_certification(tmp_path, monkeypatch):
    from galley.fixed_workflow import FixedWorkflow
    source = tmp_path / "Writer.docx"
    document = Document()
    document.add_paragraph("A quiet room.")
    document.save(source)
    def stop(self):
        checkpoint = json.loads((self.directory.parent / "driver/driver.json").read_text())
        assert checkpoint["outcome"] == "running"
        raise OSError("Still waiting for required reads")
    monkeypatch.setattr(FixedWorkflow, "run", stop)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "blocked" and "Still waiting" in result.reason
    checkpoint = json.loads((worker.workspace / "runs/driver/driver.json").read_text())
    assert checkpoint["outcome"] == "blocked"


def test_revised_input_delivers_and_resumes_with_certified_original(tmp_path, monkeypatch):
    from test_galley_fixed_intake import revised_book
    from galley.fixed_intake import FixedIntakeError

    source = revised_book(tmp_path)
    incoming = source.read_bytes()
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    assert source.read_bytes() == incoming
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    final = json.loads((worker.workspace / "runs/fixed/result.json").read_text())
    assert final["identity"]["intake"]["original_sha256"] == hashlib.sha256(incoming).hexdigest()
    tracked = next(Path(x["path"]) for x in package["artifacts"] if x["role"] == "tracked")
    assert paragraph_views(tracked, "reject") == paragraph_views(source)
    assert "She received two letters." in paragraph_views(tracked).values()
    count = len(readers.requests)
    assert worker.run().outcome == "done"
    assert len(readers.requests) == count
    original = worker.workspace / "runs/fixed/intake/original" / source.name
    original.write_bytes(original.read_bytes() + b"changed")
    with pytest.raises(FixedIntakeError, match="original or accepted baseline changed"):
        validate_delivery_package(package)
    assert worker.run().outcome == "blocked"
    assert len(readers.requests) == count


def test_context_ids_in_raw_typed_receipts_survive_certified_resume(tmp_path, monkeypatch):
    from galley import fixed_policy
    configuration = fixed_policy.configuration
    def small_chunks(poetry=False):
        cfg = configuration(poetry)
        cfg.chunking.token_budget = 12
        return cfg
    monkeypatch.setattr(fixed_policy, "configuration", small_chunks)
    source = tmp_path / "Writer.docx"
    document = Document()
    for text in ("She recieved two letters while she was waiting in the quiet room.",
                 "The morning sunlight came through the window and fell across the wooden table.",
                 "Outside the door, her sister waited patiently for the morning post to arrive."):
        document.add_paragraph(text)
    document.save(source)
    # This reader deliberately lists every paragraph, including read-only context.
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    responses = {}
    for path in (worker.workspace / "runs/fixed/calls/calls").glob("*/request.json"):
        request = json.loads(path.read_text())
        if request["stage"] == "typed" and request["user"].startswith("<context>"):
            receipt = json.loads(path.with_name("receipt.json").read_text())
            response = path.parent / "attempts" / str(receipt["attempt"]) / "response.json"
            raw = json.loads(response.read_text())["result"]["parsed"]
            owned = re.findall(r'<paragraph id="([^"]+)">', request["user"].split("</context>")[-1])
            assert len(raw["reviewed_paragraph_ids"]) > len(owned)
            responses[response] = response.read_bytes()
    assert responses
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    count = len(readers.requests)
    assert worker.run().outcome == "done"
    assert len(readers.requests) == count
    assert all(path.read_bytes() == raw for path, raw in responses.items())


def test_certified_local_checks_cannot_be_removed_after_delivery(tmp_path, monkeypatch):
    from galley.fixed_local import FixedLocalError

    source = tmp_path / "Writer - Book 1.docx"
    document = Document()
    document.add_paragraph("She recieved two letters.")
    document.save(source)
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    stage = json.loads((worker.workspace / "runs/fixed/stages/typed.json").read_text())
    local = stage["evidence"]["local"]
    from pathlib import Path
    path = Path(local["path"])
    path.write_bytes(path.read_bytes() + b"changed")
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    with pytest.raises(FixedLocalError, match="evidence has changed"):
        validate_delivery_package(package)
    count = len(readers.requests)
    resumed = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode=None).run()
    assert resumed.outcome == "blocked"
    assert len(readers.requests) == count


def test_distinct_local_questions_at_one_anchor_reach_adjudication_together():
    from galley.fixed_workflow import _candidate, _groups

    texts = {"p": "The visitor arrived Monday, May 1, 2024."}
    base = {"para_id": "p", "quote": texts["p"], "replacement": texts["p"],
            "occurrence": 1, "action": "query", "missing_knowledge": ""}
    calendar = _candidate({**base, "category": "continuity", "reason": "The date and weekday disagree."},
                          texts, "local:calendar")
    identity = _candidate({**base, "category": "consistency", "reason": "The visitor has two name spellings."},
                          texts, "local:consistency")
    assert calendar["id"] != identity["id"]
    groups = _groups([calendar, identity])
    assert len(groups) == 1 and len(groups[0]) == 2
    assert {p["reason"] for p in groups[0]} == {
        "The date and weekday disagree.", "The visitor has two name spellings."}


@pytest.fixture
def completed_prose_review(tmp_path, monkeypatch):
    """A real certified two-paragraph run, with only paid/Java endpoints faked."""
    from galley.fixed_documents import _verify_result

    source = tmp_path / "Writer - Book 1.docx"
    document = Document()
    document.add_paragraph("She recieved two letters.")
    document.add_paragraph("The door was open.")
    document.save(source)
    readers = ScriptedReaders(False)
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    outcome = worker.run()
    assert outcome.outcome == "done", outcome.reason
    directory = worker.workspace / "runs/fixed"
    result = json.loads((directory / "result.json").read_text())
    assert _verify_result(result, directory) == source
    return directory, result


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2))


def _rehash_stage_and_result(directory, result, name, payload):
    """Model a writer wiring the wrong receipt, rather than simple file damage."""
    stage = next(row for row in result["stages"] if row["stage"] == name)
    _write_json(stage["path"], payload)
    stage["sha256"] = fc._hash(payload)
    result["result_sha256"] = fc._hash({key: value for key, value in result.items()
                                       if key not in {"usage", "result_sha256"}})
    _write_json(directory / "result.json", result)
    marker = json.loads((directory / "workflow.json").read_text())
    marker["result_sha256"] = result["result_sha256"]
    _write_json(directory / "workflow.json", marker)


def test_valid_initial_local_receipt_cannot_certify_the_completion_stage(completed_prose_review):
    from galley.fixed_documents import FixedDocumentError, _verify_result
    from galley.fixed_local import validate_local_evidence

    directory, result = completed_prose_review
    typed = json.loads((directory / "stages/typed.json").read_text())
    ensemble = json.loads((directory / "stages/ensemble_sweep.json").read_text())
    initial = copy.deepcopy(typed["evidence"]["local"])
    assert validate_local_evidence(initial, directory / "local", result["identity"])["request"]["stage"] == "initial"
    ensemble["evidence"]["local"] = initial
    _rehash_stage_and_result(directory, result, "ensemble_sweep", ensemble)
    with pytest.raises(FixedDocumentError, match="(?i)(local|deterministic|completion)"):
        _verify_result(result, directory)


@pytest.mark.parametrize("stage_name,mutation", [
    ("typed", "paragraph_subset"),
    ("typed", "different_initial_text"),
    ("ensemble_sweep", "paragraph_subset"),
    ("ensemble_sweep", "different_original"),
])
def test_self_consistent_local_packet_must_match_the_reviewed_source(
        completed_prose_review, stage_name, mutation):
    from galley.fixed_documents import FixedDocumentError, _verify_result
    from galley.fixed_local import validate_local_evidence

    directory, result = completed_prose_review
    stage = json.loads((directory / "stages" / f"{stage_name}.json").read_text())
    evidence = stage["evidence"]["local"]
    packet = json.loads(Path(evidence["path"]).read_text())
    request = packet["request"]
    assert len(request["paragraphs"]) == 2
    if mutation == "paragraph_subset":
        request["paragraphs"] = request["paragraphs"][:1]
    elif mutation == "different_initial_text":
        request["paragraphs"][0]["text"] = "A different source paragraph."
    else:
        first = next(iter(request["original"]))
        request["original"][first] = "A different original manuscript."
    # Build an internally valid completed detector receipt for those wrong
    # inputs. The certification boundary must compare it with the actual book,
    # even when every local hash and local coverage claim agrees with itself.
    ids = [p["para_id"] for p in request["paragraphs"]]
    packet["findings"], packet["diagnostics"] = [], []
    for check in packet["checks"]:
        check["paragraph_ids"] = ids
        check["proposal_count"] = 0
    packet["request_sha256"] = fc._hash(request)
    packet["result_sha256"] = fc._hash({key: value for key, value in packet.items()
                                        if key != "result_sha256"})
    _write_json(evidence["path"], packet)
    marker = json.loads(Path(evidence["marker_path"]).read_text())
    marker["request_sha256"] = packet["request_sha256"]
    _write_json(evidence["marker_path"], marker)
    evidence.update(sha256=hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest(),
        marker_sha256=hashlib.sha256(Path(evidence["marker_path"]).read_bytes()).hexdigest(),
        request_sha256=packet["request_sha256"], paragraph_ids=ids,
        checks=copy.deepcopy(packet["checks"]), proposal_count=0, diagnostic_count=0)
    assert validate_local_evidence(evidence, directory / "local", result["identity"]) == packet
    _rehash_stage_and_result(directory, result, stage_name, stage)
    with pytest.raises(FixedDocumentError, match="(?i)(local|deterministic|paragraph|original)"):
        _verify_result(result, directory)


def test_press_method_final_scan_cannot_be_omitted_from_delivery(completed_prose_review):
    from galley.fixed_documents import FixedDocumentError, _verify_result
    directory, result = completed_prose_review
    stage = json.loads((directory / "stages/astra.json").read_text())
    del stage["evidence"]["press_audit"]
    _rehash_stage_and_result(directory, result, "astra", stage)
    with pytest.raises(FixedDocumentError, match="press-method final scan"):
        _verify_result(result, directory)


def test_all_six_reader_models_recover_incomplete_coverage_before_stage_completion(tmp_path, monkeypatch):
    source = tmp_path / "Writer.docx"
    document = Document()
    document.add_paragraph("She recieved 20 letters while waiting in the quiet room.")
    document.save(source)
    readers = ScriptedReaders(False)
    answer = readers.answer
    failed_models = set()
    def incomplete_once(model, user, schema):
        result = answer(model, user, schema)
        if model not in failed_models:
            for field in ("reviewed_paragraph_ids", "reviewed_ids", "decisions"):
                if result.get(field):
                    failed_models.add(model)
                    return {**result, field: result[field][1:]}
        return result
    readers.answer = incomplete_once
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    assert failed_models == {SONNET, LUNA, OPUS, SOL, FABLE, ASTRA}
    budget = json.loads((worker.workspace / "runs/fixed/calls/budget.json").read_text())
    assert sum(row["status"] == "failed" for row in budget["entries"].values()) == 6
    assert all(row["status"] in {"completed", "failed"} for row in budget["entries"].values())
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    count = len(readers.requests)
    assert worker.run().outcome == "done"
    assert len(readers.requests) == count


@pytest.mark.parametrize("defect", ["anchor", "content"])
def test_invalid_suggestions_across_all_reader_stages_preserve_valid_edits_and_resume(tmp_path, monkeypatch, defect):
    source = tmp_path / "Writer.docx"
    document = Document()
    document.add_paragraph("She recieved 20 letters while waiting in the quiet room.")
    document.save(source)
    original_bytes = source.read_bytes()
    readers = ScriptedReaders(False)
    answer = readers.answer
    def suggestions(model, user, schema):
        result = answer(model, user, schema)
        if "reviewed_paragraph_ids" in result and result["findings"]:
            bad = {**result["findings"][0], "original_text": "Invented quotation that never appears in this book.",
                   "corrected_text": "An equally unsupported replacement."}
            if defect == "content":
                bad.update(original_text=result["findings"][0]["original_text"], corrected_text="Unsafe\x00text")
            return {**result, "findings": result["findings"] + [bad]}
        if "reviewed_ids" in result:
            payload = json.loads(user)
            number = "sites" in payload
            pid = payload["sites"][0]["para_id"] if number else payload["paragraphs"][0]["id"]
            category = "number_style" if number else "broken_sentence" if model == OPUS else "format" if model == FABLE else "author_question" if model == ASTRA else "grammar"
            bad = {"para_id": pid, "quote": "A nonexistent quotation spanning several imaginary paragraphs.",
                   "occurrence": 1, "replacement": "Unsupported correction.", "category": category,
                   "action": "query" if model == ASTRA else "edit", "reason": "Synthetic unanchored proposal.",
                   "missing_knowledge": "Unsupported synthetic question." if model == ASTRA else ""}
            if defect == "content":
                bad["quote"] = payload["paragraphs"][pid] if number else payload["paragraphs"][0]["text"]
                if number:
                    bad["category"] = "grammar"
                elif model != FABLE:
                    bad["replacement"] = "Unsafe\x00text"
            return {**result, "findings": [bad]}
        return result
    readers.answer = suggestions
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    final = json.loads((worker.workspace / "runs/fixed/result.json").read_text())
    rejected = [h for h in final["history"] if h.get("rejected_proposal")]
    assert {h["stage"] for h in rejected} == {"typed", "numbers", "ensemble_sweep_opus", "ensemble_sweep_sol", "fable", "astra"}
    assert {h["rejected_proposal"]["model"] for h in rejected} == {SONNET, LUNA, OPUS, SOL, FABLE, ASTRA}
    assert final["questions"] == [] and source.read_bytes() == original_bytes
    assert list(final["accepted"].values()) == ["She received 20 letters while waiting in the quiet room."]
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    report = next(Path(row["path"]) for row in package["artifacts"] if row["role"] == "report")
    assert "model suggestions were rejected" in report.read_text()
    count = len(readers.requests)
    response_bytes = {p: p.read_bytes() for p in (worker.workspace / "runs/fixed/calls/calls").glob("*/attempts/*/response.json")}
    assert worker.run().outcome == "done"
    assert len(readers.requests) == count
    assert all(p.read_bytes() == value for p, value in response_bytes.items())


def test_nested_dispute_ids_reach_certified_book_and_resume_without_new_calls(tmp_path, monkeypatch):
    source = tmp_path / "Writer.docx"
    doc = Document()
    doc.add_paragraph("She recieved two letters.")
    doc.save(source)
    original = source.read_bytes()
    readers = ScriptedReaders(False)
    answer = readers.answer
    def nested(model, user, schema):
        result = answer(model, user, schema)
        if "reviewed_paragraph_ids" in result and model == LUNA:
            result["findings"] = []  # Force a genuine ensemble disagreement.
        if "decisions" in result:
            payload = json.loads(user)
            sites = payload.get("sites", [])
            if sites and all(s.get("proposals") for s in sites):
                return {"decisions": [{"id": p["id"], "action": "apply" if p["category"] == "spelling" else "drop",
                    "replacement": p["replacement"] if p["category"] == "spelling" else "",
                    "reason": "Clear spelling correction." if p["category"] == "spelling" else "No clear error.",
                    "missing_knowledge": "", "question": ""} for s in sites for p in s["proposals"]]}
        return result
    readers.answer = nested
    monkeypatch.setattr(fc, "_default_provider", lambda *a, **k: readers)
    monkeypatch.setattr(codex_runner, "run_structured", readers.subscription)
    worker = gd.Driver(source, "writer", workspace_root=tmp_path / "work", execution_mode="fixed")
    result = worker.run()
    assert result.outcome == "done", result.reason
    final = json.loads((worker.workspace / "runs/fixed/result.json").read_text())
    assert list(final["accepted"].values()) == ["She received two letters."]
    assert final["questions"] == [] and source.read_bytes() == original
    receipts = [json.loads(p.read_text()) for p in (worker.workspace / "runs/fixed/calls/calls").glob("*/receipt.json")]
    assert any(r.get("decision_normalization") for r in receipts)
    package = json.loads((worker.workspace / "runs/driver/package.json").read_text())
    assert validate_delivery_package(package)["delivery_ready"] is True
    count = len(readers.requests)
    raw = {p: p.read_bytes() for p in (worker.workspace / "runs/fixed/calls/calls").glob("*/attempts/*/response.json")}
    assert worker.run().outcome == "done"
    assert len(readers.requests) == count and all(p.read_bytes() == data for p, data in raw.items())
