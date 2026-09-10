from pathlib import Path

import pytest

import galley.codex_runner
from docproof.interior.astra import AstraReviewer, InteriorAstraError


def _inputs():
    packet = {"sources": [{"id": "source-a", "kind": "text", "text": "typo"}],
              "text_source_id": "source-a", "evidence": [{"id": "evidence-a", "source_id": "source-a", "kind": "text", "text": "typo"}]}
    snapshot = {"stories": [{"id": "story-a", "text": "A teh sentence.", "style_ranges": [], "pages": [1]}]}
    return packet, snapshot


def test_plan_requires_exact_native_anchor_and_source_coverage(monkeypatch, tmp_path):
    packet, snapshot = _inputs()

    def fake(prompt, schema, work_dir, *, request_id):
        return {"instructions": [{"id": "i-a", "source_ids": ["source-a"], "disposition": "edit",
                "reason": "typo", "edit_ids": ["e-a"], "covered_evidence_ids": ["evidence-a"]}],
                "edits": [{"id": "e-a", "story_id": "story-a", "find": "teh", "replacement": "the",
                            "expected_count": 1, "font_style": "", "style_ranges": []}],
                "questions": [], "designer_reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().plan(packet, snapshot, tmp_path)
    assert result["edits"][0]["find"] == "teh"

    def bad(prompt, schema, work_dir, *, request_id):
        result = fake(prompt, schema, work_dir, request_id=request_id)
        result["edits"][0]["find"] = "missing"
        return result

    monkeypatch.setattr(galley.codex_runner, "run_structured", bad)
    with pytest.raises(InteriorAstraError, match="anchor count"):
        AstraReviewer().plan(packet, snapshot, tmp_path)


def test_low_confidence_never_returns_edits(monkeypatch, tmp_path):
    packet, snapshot = _inputs()
    packet["sources"][0]["confidence"] = "low"

    def fake(prompt, schema, work_dir, *, request_id):
        return {"instructions": [{"id": "i-a", "source_ids": ["source-a"], "disposition": "edit",
                                   "reason": "guess", "edit_ids": ["e-a"], "covered_evidence_ids": ["evidence-a"]}],
                "edits": [{"id": "e-a", "story_id": "story-a", "find": "teh", "replacement": "the",
                            "expected_count": 1, "font_style": "", "style_ranges": []}],
                "questions": [], "designer_reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().plan(packet, snapshot, tmp_path)
    assert result["edits"] == []
    assert result["instructions"][0]["disposition"] == "clarification"


def test_low_confidence_only_suppresses_its_instruction(monkeypatch, tmp_path):
    packet, snapshot = _inputs()
    packet["sources"].append({"id": "source-b", "kind": "text", "text": "clear"})
    packet["evidence"].append({"id": "evidence-b", "source_id": "source-b", "kind": "text", "text": "clear"})
    snapshot["stories"].append({"id": "story-b", "text": "A teh sentence.", "style_ranges": [], "pages": [1]})
    packet["sources"][0]["confidence"] = "low"

    def fake(prompt, schema, work_dir, *, request_id):
        return {"instructions": [
                    {"id": "i-a", "source_ids": ["source-a"], "disposition": "edit", "reason": "uncertain",
                     "edit_ids": ["e-a"], "covered_evidence_ids": ["evidence-a"]},
                    {"id": "i-b", "source_ids": ["source-b"], "disposition": "edit", "reason": "clear",
                     "edit_ids": ["e-b"], "covered_evidence_ids": ["evidence-b"]}],
                "edits": [
                    {"id": "e-a", "story_id": "story-a", "find": "teh", "replacement": "the", "expected_count": 1,
                     "font_style": "", "style_ranges": []},
                    {"id": "e-b", "story_id": "story-b", "find": "teh", "replacement": "the", "expected_count": 1,
                     "font_style": "", "style_ranges": []}],
                "questions": [], "designer_reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().plan(packet, snapshot, tmp_path)
    assert [edit["id"] for edit in result["edits"]] == ["e-b"]
    assert result["instructions"][0]["disposition"] == "clarification"
    assert result["instructions"][1]["disposition"] == "edit"


def test_one_source_can_have_multiple_independent_instructions(monkeypatch, tmp_path):
    packet, snapshot = _inputs()
    packet["evidence"].append({"id": "evidence-a2", "source_id": "source-a",
                               "kind": "text", "text": "second correction"})

    def fake(prompt, schema, work_dir, *, request_id):
        return {"instructions": [
                    {"id": "i-a1", "source_ids": ["source-a"], "disposition": "edit",
                     "reason": "first", "edit_ids": ["e-a1"], "covered_evidence_ids": ["evidence-a"]},
                    {"id": "i-a2", "source_ids": ["source-a"], "disposition": "clarification",
                     "reason": "second is ambiguous", "edit_ids": [], "covered_evidence_ids": ["evidence-a2"]}],
                "edits": [{"id": "e-a1", "story_id": "story-a", "find": "teh", "replacement": "the",
                            "expected_count": 1, "font_style": "", "style_ranges": []}],
                "questions": [], "designer_reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().plan(packet, snapshot, tmp_path)
    assert len(result["instructions"]) == 2
    assert [row["id"] for row in result["edits"]] == ["e-a1"]


def test_verified_review_requires_all_instructions_and_pages(monkeypatch, tmp_path):
    packet, _snapshot = _inputs()
    prompts = []

    def fake(prompt, schema, work_dir, *, request_id):
        prompts.append(prompt)
        return {"status": "verified", "instruction_ids": ["i-a"], "reviewed_pages": [1, 2],
                "reasons": ["inspected original evidence and all required pages"]}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().review(packet, {"baseline_pdf": "/tmp/baseline.pdf"},
                                    {"output_pdf": "/tmp/final.pdf", "required_review_pages": [1, 2]},
                                    [{"id": "e-a", "instruction_id": "i-a"}], tmp_path)
    assert result["status"] == "verified"
    assert "astra-interior-baseline.json" in prompts[0]
    assert "astra-interior-final.json" in prompts[0]
    assert str(Path("/tmp/baseline.pdf").resolve()) in prompts[0]
    assert str(Path("/tmp/final.pdf").resolve()) in prompts[0]


def test_large_inputs_are_file_backed_instead_of_duplicated_in_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr('docproof.interior.astra.sys.platform', 'darwin')
    packet, snapshot = _inputs()
    packet["evidence"][0]["text"] = "correction " * 20000
    snapshot["stories"][0]["text"] = "A teh sentence."
    prompts = []

    def fake(prompt, schema, work_dir, *, request_id):
        prompts.append(prompt)
        return {"instructions": [{"id": "i-a", "source_ids": ["source-a"],
                                   "disposition": "clarification", "reason": "review",
                                   "edit_ids": [], "covered_evidence_ids": ["evidence-a"]}],
                "edits": [], "questions": [], "designer_reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    AstraReviewer().plan(packet, snapshot, tmp_path)
    assert len(prompts[0]) < 10000
    assert "astra-interior-packet.json" in prompts[0]
    assert "correction correction" not in prompts[0]


def test_recorded_packet_errors_block_verified_status(monkeypatch, tmp_path):
    packet, _snapshot = _inputs()
    packet["errors"] = [{"source_id": "source-a", "kind": "render", "message": "renderer missing"}]

    def fake(prompt, schema, work_dir, *, request_id):
        return {"status": "verified", "instruction_ids": ["i-a"], "reviewed_pages": [1], "reasons": []}

    monkeypatch.setattr(galley.codex_runner, "run_structured", fake)
    result = AstraReviewer().review(packet, {}, {"required_review_pages": [1]},
                                    [{"id": "e-a", "instruction_id": "i-a"}], tmp_path)
    assert result["status"] == "clarification_needed"
