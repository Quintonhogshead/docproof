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


def test_interrupted_stage_resumes_from_paid_read_receipts(tmp_path, monkeypatch):
    from galley.fixed_workflow import FixedWorkflow

    source = tmp_path / "Writer - Book 1.docx"
    document = Document()
    document.add_paragraph("She recieved two letters.")
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
