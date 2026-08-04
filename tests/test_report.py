"""findings.json, read back for a human: grouped by kind, before/after with
the changed words marked, and no scores or jargon on screen."""
from __future__ import annotations

import json

from app.report import build_report, diff_segments

SPLICE = "The manuscript was finished, nobody wanted to read it."
FIXED = "The manuscript was finished; nobody wanted to read it."


def _finding(fid, error_type="comma_splice", *, applied=True,
             status="validated", confidence="high", explanation="A splice."):
    return {
        "finding_id": fid, "chunk_id": "chunk-000", "para_id": f"body-{fid}",
        "error_type": error_type, "original_text": SPLICE, "occurrence": 1,
        "corrected_text": FIXED, "explanation": explanation,
        "confidence": confidence, "status": status, "anchor": None,
        "applied": applied,
    }


def _write(tmp_path, findings, **over):
    payload = {
        "schema_version": 1, "generated_at": "2026-08-04T10:00:00+00:00",
        "source": "/tmp/Novel.docx", "batch": True,
        "config": {"model": "claude-sonnet-5", "error_types": ["comma_splice"]},
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "api_calls": 3},
        "findings": findings,
    }
    payload.update(over)
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- the diff -----------------------------------------------------------------

def test_only_the_changed_words_are_marked():
    before, after = diff_segments(SPLICE, FIXED)
    # A one-character fix inside a long sentence: everything else must read as
    # unchanged, or the reader has to hunt for what moved.
    assert [s["text"] for s in before if s["changed"]] == ["finished,"]
    assert [s["text"] for s in after if s["changed"]] == ["finished;"]
    assert "".join(s["text"] for s in before if not s["changed"]) \
        == "".join(s["text"] for s in after if not s["changed"])


def test_an_identical_sentence_marks_nothing():
    before, after = diff_segments(SPLICE, SPLICE)
    assert not any(s["changed"] for s in before + after)


def test_a_pure_insertion_marks_only_the_new_words():
    before, after = diff_segments("She went the shop.", "She went to the shop.")
    assert not any(s["changed"] for s in before)
    assert [s["text"] for s in after if s["changed"]] == ["to"]


# --- the report ---------------------------------------------------------------

def test_findings_are_grouped_by_kind_commonest_first(tmp_path):
    path = _write(tmp_path, [
        _finding("0001", "spelling"),
        _finding("0002", "comma_splice"),
        _finding("0003", "comma_splice"),
    ])
    report = build_report(path, {"comma_splice": "Comma splices",
                                 "spelling": "Spelling"})

    assert [g["error_name"] for g in report["groups"]] == ["Comma splices",
                                                           "Spelling"]
    assert [g["count"] for g in report["groups"]] == [2, 1]
    assert report["headline"]["applied"] == 3
    assert report["headline"]["paragraphs"] == 3
    assert report["headline"]["top_name"] == "Comma splices"


def test_the_headline_counts_paragraphs_not_findings(tmp_path):
    two_in_one = [_finding("0001"), _finding("0002")]
    two_in_one[1]["para_id"] = two_in_one[0]["para_id"]
    report = build_report(_write(tmp_path, two_in_one))
    assert report["headline"]["applied"] == 2
    assert report["headline"]["paragraphs"] == 1


def test_unapplied_findings_are_separated_not_dropped(tmp_path):
    path = _write(tmp_path, [
        _finding("0001"),
        _finding("0002", applied=False, status="skipped_low_confidence",
                 confidence="low"),
        _finding("0003", applied=False, status="rejected_no_anchor"),
    ])
    report = build_report(path)

    assert len(report["groups"]) == 1                  # only what was applied
    assert [f["finding_id"] for f in report["low_confidence"]] == ["0002"]
    assert [f["finding_id"] for f in report["not_applied"]] == ["0003"]
    assert report["headline"]["low_confidence"] == 1


def test_scores_and_statuses_are_rendered_as_words(tmp_path):
    path = _write(tmp_path, [
        _finding("0001", confidence="low", applied=False,
                 status="skipped_low_confidence")])
    report = build_report(path)

    f = report["low_confidence"][0]
    assert f["confidence_word"] == "Unsure — worth a look"
    assert f["status_word"] == "Left alone — below your care setting"
    # Nothing a reader sees should name the machinery.
    for word in ("chunk", "token", "anchor", "batch", "API"):
        assert word.lower() not in json.dumps(
            [report["headline"], report["groups"]]).lower()


def test_cost_is_priced_at_the_rate_that_was_actually_paid(tmp_path):
    batched = build_report(_write(tmp_path, [_finding("0001")], batch=True))
    full = build_report(_write(tmp_path, [_finding("0001")], batch=False))
    # $3 in + $15 out per MTok on Sonnet, halved for a batch.
    assert full["cost"] == 18.0
    assert batched["cost"] == 9.0


def test_a_review_that_changed_nothing_still_reports(tmp_path):
    report = build_report(_write(tmp_path, []))
    assert report["headline"]["applied"] == 0
    assert report["groups"] == []
    assert report["source"] == "Novel.docx"
