import copy
import json
from pathlib import Path

import pytest

from docproof.interior.verify import VerificationError, check_saved, prepare_edits, validate_plan
from docproof.interior.workflow import next_name, run_local


def snapshot(text="Board Songbird now."):
    start = text.find("Songbird")
    return {"stories": [{"id": "1", "text": text, "style_ranges": [
        {"start": 0, "end": start, "font_style": "Regular"},
        {"start": start, "end": start+8, "font_style": "Italic"},
        {"start": start+8, "end": len(text), "font_style": "Regular"}]}],
        "page_count": 1, "fonts": [{"name": "Minion", "status": "INSTALLED"}],
        "links": [], "overset": []}


def edit(**over):
    return {"id": "e1", "story_id": "1", "find": "Songbird", "replacement": "the Songbird",
            "expected_count": 1, "font_style": "", "style_ranges": [
                {"start": 0, "end": 4, "font_style": "Regular"},
                {"start": 4, "end": 12, "font_style": "Italic"}], **over}


def plan():
    return {"instructions": [{"id": "i1", "source_ids": ["s1"], "disposition": "edit",
                              "reason": "Add roman article", "edit_ids": ["e1"]}],
            "edits": [edit()], "questions": [], "designer_reasons": []}


def test_exact_text_and_roman_article_italic_boat():
    a, b = snapshot(), snapshot("Board the Songbird now.")
    assert check_saved(a, b, [edit()])["passed"]
    b["stories"][0]["style_ranges"][0]["font_style"] = "Italic"
    check = check_saved(a, b, [edit()])
    assert not check["passed"] and check["integrity_passed"]
    b["stories"][0]["text"] += "Extra text"
    assert not check_saved(a, b, [edit()])["integrity_passed"]


def test_ambiguous_and_overlapping_anchors_rejected():
    with pytest.raises(VerificationError, match="expected 1 exact"):
        prepare_edits(snapshot("Songbird Songbird"), [edit()])
    with pytest.raises(VerificationError, match="Overlapping"):
        prepare_edits(snapshot(), [edit(), edit(id="e2")])


def test_plan_accounts_for_every_source_and_edit():
    packet = {"sources": [{"id": "s1"}, {"id": "s2"}]}
    with pytest.raises(VerificationError, match="omits"):
        validate_plan(plan(), packet)
    p = plan()
    p["edits"].append(edit(id="e2"))
    with pytest.raises(VerificationError, match="exactly one"):
        validate_plan(p, {"sources": [{"id": "s1"}]})


def test_plan_cannot_omit_one_entry_in_a_multi_entry_attachment():
    packet = {"sources": [{"id": "s1"}], "evidence": [
        {"id": "p1", "kind": "docx_paragraph", "text": "First correction"},
        {"id": "p2", "kind": "docx_paragraph", "text": "Second correction"}]}
    p = plan()
    p["instructions"][0]["covered_evidence_ids"] = ["p1"]
    with pytest.raises(VerificationError, match="evidence entry"):
        validate_plan(p, packet)
    p["instructions"][0]["covered_evidence_ids"] = ["p1", "p2"]
    validate_plan(p, packet)


@pytest.mark.parametrize("property,value", [("point_size", 14), ("tracking", 20),
                                           ("applied_font", "Different font"),
                                           ("paragraph_style", "Heading")])
def test_saved_verifier_detects_unrequested_format_changes(property, value):
    baseline = snapshot()
    for row in baseline["stories"][0]["style_ranges"]:
        row.update(point_size=12, tracking=0, applied_font="Minion", paragraph_style="Body")
    final = copy.deepcopy(baseline)
    final["stories"][0]["style_ranges"][0][property] = value
    result = check_saved(baseline, final, [])
    assert result["integrity_passed"]
    assert not result["passed"]


def test_next_number_not_lexical():
    assert next_name(Path("Hill - Book 9.indd")) == "Hill - Book 10.indd"
    assert next_name(Path("Hill - Book10.INDD")) == "Hill - Book11.indd"


class Native:
    calls = 0
    corrupt = False

    def inspect(self, source, work):
        (work / "baseline.pdf").write_bytes(b"baseline")
        return snapshot()

    def apply(self, source, output, edits, work):
        self.calls += 1
        output.write_bytes(b"corrected")
        return {}

    def verify(self, output, work):
        (work / "final.pdf").write_bytes(b"pdf")
        (work / "final.idml").write_bytes(b"idml")
        return snapshot("Board the Songbird now." + ("X" if self.corrupt else ""))


class Astra:
    def plan(self, *args, **kw):
        return plan()

    def review(self, *args, **kw):
        return {"status": "verified", "instruction_ids": ["i1"], "reviewed_pages": [1], "reasons": []}


def run(tmp_path, native=None, astra=None):
    source = tmp_path / "Hill - Book 4.indd"
    if not source.exists():
        source.write_bytes(b"original")
    return run_local(source, [], "Add the", tmp_path / "job", native=native or Native(),
                     astra=astra or Astra(), packet_builder=lambda *a: {"sources": [{"id": "s1"}]},
                     page_reviewer=lambda *a: {"required_review_pages": [1], "review_images": []})


def test_resume_never_reapplies_and_rejects_changed_artifact(tmp_path):
    n = Native()
    first = run(tmp_path, native=n)
    assert first["status"] == "verified", first
    assert run(tmp_path, native=n) == first
    assert n.calls == 1
    Path(first["output_indd"]).write_bytes(b"tampered")
    assert run(tmp_path, native=n)["status"] == "technical_block"
    assert n.calls == 1


def test_unintended_text_is_not_published_as_partial(tmp_path):
    n = Native()
    n.corrupt = True
    result = run(tmp_path, native=n)
    assert result["status"] == "technical_block"
    assert result["needs_designer"] is None
    assert not result["output_indd"]


def test_incomplete_visual_review_is_operational_block(tmp_path):
    class MissingReview(Astra):
        def review(self, *a, **kw):
            return {"status": "verified", "instruction_ids": ["i1"], "reviewed_pages": [], "reasons": []}
    assert run(tmp_path, astra=MissingReview())["status"] == "technical_block"


def test_interrupted_edit_is_not_repeated(tmp_path):
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "workflow.json").write_text(json.dumps({"stage": "applying"}))
    n = Native()
    result = run(tmp_path, native=n)
    assert result["status"] == "technical_block"
    assert n.calls == 0
