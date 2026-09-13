"""A rebuild preserves decisions and checks comments beside corrected text."""
import json

import docx
import pytest

from docproof.config import Config, load_config
from docproof.ingest import build_document_model, preflight
from docproof.models import Finding
from docproof.reassembler import apply_tracked_changes, paragraph_view_text
from docproof.replay import rebuild_from_rows
from docproof.utils.xml_helpers import walk_package
from docproof.validator import validate_findings
from galley.comment_reconcile import actual_comments
from galley.verify import paragraph_views
from tests.galley.test_settle import _replay_config


def _source(tmp_path, text, *, comment=None):
    source = docx.Document()
    para = source.add_paragraph(text)
    if comment:
        source.add_comment(para.runs, text=comment, author="Author")
    path = tmp_path / "source.docx"
    source.save(path)
    return path


def _rebuild(tmp_path, source, rows, name="run", *, remap=False):
    cfg = load_config(_replay_config(tmp_path))
    cfg.output_dir = str(tmp_path / name)
    result = rebuild_from_rows(cfg, manuscript=source, rows=rows,
                               error_dir="config/error_types",
                               remap_unchanneled=remap, id_prefix="replay")
    env = json.loads(result.outputs.findings_json.read_text())
    return result, env["findings"]


@pytest.mark.parametrize("status", ["rejected_by_verifier", "rejected_duplicate",
                                    "rejected_policy", "skipped_low_confidence"])
def test_rejected_row_cannot_become_an_edit_on_rebuild(tmp_path, status):
    source = _source(tmp_path, "The blue lamp was lit.")
    result, rows = _rebuild(tmp_path, source, [dict(
        para_id="body-0000", original_text="blue", corrected_text="red",
        confidence="high", error_type="spelling", status=status,
        explanation="This proposal was declined.")])
    assert paragraph_views(tmp_path / "run")[1]["body-0000"] == "The blue lamp was lit."
    assert not actual_comments(result.outputs.reviewed_path)
    assert any(r["status"] == status and not r["applied"] for r in rows)


def test_unplaced_query_remains_a_question_instead_of_applying_proposal(tmp_path):
    source = _source(tmp_path, "Alex met Sam.")
    result, rows = _rebuild(tmp_path, source, [dict(
        para_id="body-0000", original_text="Alex", corrected_text="Pat",
        confidence="high", error_type="spelling", status="query",
        queried=False, applied=False, explanation="Did Alex or Pat meet Sam?")])
    assert paragraph_views(tmp_path / "run")[1]["body-0000"] == "Alex met Sam."
    assert len(actual_comments(result.outputs.reviewed_path)) == 1
    assert next(r for r in rows if r["explanation"].startswith("Did Alex"))["queried"]


def test_corrected_quote_scan_does_not_regenerate_on_rebuild(tmp_path):
    source = _source(tmp_path, "“Not at night.", comment="Author's original note.")
    result, rows = _rebuild(tmp_path, source, [dict(
        para_id="body-0000", original_text="“Not at night.",
        corrected_text="“Not at night.”", confidence="high",
        error_type="imported_edit")])
    assert paragraph_views(tmp_path / "run")[1]["body-0000"] == "“Not at night.”"
    comments = actual_comments(result.outputs.reviewed_path)
    assert [c["explanation"] for c in comments] == ["Author's original note."]
    stale = [r for r in rows if r["error_type"] == "unclosed_quote"]
    assert stale and all(r["status"] == "rejected_noop" and not r["queried"] for r in stale)
    again, again_rows = _rebuild(tmp_path, source, rows, "again")
    assert paragraph_views(tmp_path / "again")[1] == paragraph_views(tmp_path / "run")[1]
    assert [c["explanation"] for c in actual_comments(again.outputs.reviewed_path)] == [
        "Author's original note."]


@pytest.mark.parametrize("remap", [False, True])
def test_declined_source_scan_is_not_regenerated(tmp_path, remap):
    source = _source(tmp_path, "“Not at night.")
    _result, rows = _rebuild(tmp_path, source, [])
    scan = next(r for r in rows if r["error_type"] == "unclosed_quote")
    scan.update(state="dropped", disposition_reason="Deliberate unfinished quotation.")
    result, rebuilt = _rebuild(tmp_path, source, [scan], "again", remap=remap)
    assert not actual_comments(result.outputs.reviewed_path)
    assert any(r["status"] == "rejected_noop" for r in rebuilt)

    # Settlement starts its next rebuild from live rows only. The prior
    # decision must survive that handoff too, without the caller copying it.
    from galley.settle import Settler, SettleOptions
    settler = Settler(tmp_path / "again", cfg=load_config(_replay_config(tmp_path)),
                      manuscript=source, error_dir="config/error_types",
                      options=SettleOptions())
    next_build = settler._rebuild([], snapshot="next-round")
    assert not actual_comments(next_build.outputs.reviewed_path)


def test_question_survives_if_the_correction_did_not_land(tmp_path):
    source = _source(tmp_path, "“Not at night.")
    cfg = Config(comments=False)
    pkg = preflight(source, "abort")
    model = build_document_model(pkg, cfg)
    from docproof.models import Anchor
    edit = Finding("edit", "replay", "body-0000", "imported_edit",
                   "“Not at night.", 1, "“Not at night.”", "Close quote.", "high",
                   status="validated", anchor=Anchor(0, 1, "wrong", "”"))
    query = Finding("query", "scan", "body-0000", "unclosed_quote",
                    "“Not at night.", 1, "“Not at night.", "Missing closing quote?",
                    "high", force_query=True)
    query = validate_findings([query], model, "medium")[0]
    stats = apply_tracked_changes(pkg, model, [edit, query], cfg)
    assert stats.skipped == ("edit",)
    assert stats.queried == ("query",) and not stats.reconciled
    assert paragraph_view_text(next(walk_package(pkg)).element, "accept") == "“Not at night."


def test_corrected_query_is_removed_only_at_its_own_occurrence(tmp_path):
    source = _source(tmp_path, "The blue lamp faced the blue door.")
    result, rows = _rebuild(tmp_path, source, [
        dict(para_id="body-0000", original_text="blue", occurrence=2,
             corrected_text="red", confidence="high", error_type="spelling"),
        dict(para_id="body-0000", original_text="blue", occurrence=1,
             corrected_text="red", confidence="high", error_type="spelling",
             force_query=True, explanation="Should the lamp be red?")])
    assert len(actual_comments(result.outputs.reviewed_path)) == 1
    assert paragraph_views(tmp_path / "run")[1]["body-0000"] == "The blue lamp faced the red door."
