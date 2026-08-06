"""Telling "nothing wrong here" apart from "never looked".

Two runs of one manuscript, four minutes apart, same config: one applied 155
changes, the other 110. The gap was a single pass — the basic spelling and
homophone group — that lost one chunk to a truncated response. `_unwrap`
logged it and returned nothing, the run carried on, and both reports read as
clean runs that happened to find different amounts. Nothing in findings.json
or summary.md recorded that a call had failed.

So the run now records the call, not just its findings, and these tests pin
the three things that made the original invisible: the record reaches the
JSON, the summary says which error types lost which section, and a chunk that
genuinely had nothing wrong is never described as unreviewed.
"""
from __future__ import annotations

import json

import pytest

from docproof.config import load_config
from docproof.models import Usage
from docproof.pipeline import finish, prepare, run_sync
from docproof.providers import ProviderResult
from .conftest import FIXTURES
from .fakes import USAGE, FakeProvider

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"
ERROR_DIR = FIXTURES.parent.parent / "config" / "error_types"


def _run(tmp_path, provider):
    """One whole review of simple.docx against a scripted provider."""
    cfg = load_config(CONFIG)
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    findings, usage, passes = run_sync(cfg, prepared, provider)
    out = finish(prepared, findings, usage, cfg, out_dir=tmp_path / "out",
                 source_path=FIXTURES / "simple.docx", passes=passes)
    return (json.loads(out.findings_json.read_text("utf-8")),
            out.summary_md.read_text("utf-8"))


def test_a_clean_run_reports_complete_coverage(tmp_path):
    """Every call answered with an empty list: the manuscript really is clean
    for these types, and the report is entitled to say so."""
    data, summary = _run(tmp_path, FakeProvider())

    coverage = data["passes"]
    assert coverage["requested"] == coverage["answered"] > 0
    assert coverage["complete"] is True
    assert all(c["answered"] for c in coverage["calls"])
    assert "Coverage: complete" in summary
    assert "were not reviewed" not in summary


def test_a_truncated_call_is_recorded_rather_than_absorbed(tmp_path):
    """The failure that started this. One call comes back truncated; the run
    still ships, but both reports name the section and the error types that
    never got read."""
    truncated = ProviderResult(stop_reason="max_tokens", error="output truncated",
                               usage=USAGE)
    # Fails the very first call — pass 0 over chunk-000 — and answers the rest.
    data, summary = _run(tmp_path, FakeProvider([truncated]))

    coverage = data["passes"]
    assert coverage["complete"] is False
    assert coverage["answered"] == coverage["requested"] - 1

    lost = [c for c in coverage["calls"] if not c["answered"]]
    assert len(lost) == 1
    assert lost[0]["reason"] == "max_tokens"
    assert lost[0]["chunk_id"] == "chunk-000" and lost[0]["pass"] == 0
    # The error types that lost the section, not just the pass number: a whole
    # group goes down together, and the report has to say which types those are.
    assert "spelling" in lost[0]["error_types"]

    assert "Sections that were not reviewed" in summary
    assert "chunk-000" in summary and "`spelling`" in summary
    assert "output truncated at the token limit" in summary
    # Truncation is the fixable kind, so the summary says how.
    assert "api.max_output_tokens" in summary


@pytest.mark.parametrize("result,reason", [
    (ProviderResult(stop_reason="refusal", error="declined", usage=USAGE),
     "the model declined this section"),
    (ProviderResult(stop_reason="error", error="503", usage=USAGE),
     "the request failed"),
    (ProviderResult(parsed={"findings": [{"para_id": "nope"}]}, usage=USAGE),
     "the response did not match the finding schema"),
])
def test_every_way_a_call_can_fail_reaches_the_report(tmp_path, result, reason):
    """Four failure modes, four different fixes. Collapsing them into "no
    findings" is what made the original run unreadable."""
    data, summary = _run(tmp_path, FakeProvider([result]))

    assert data["passes"]["complete"] is False
    assert reason in summary


def test_findings_that_fail_the_content_checks_are_not_a_missing_call(tmp_path):
    """A response that arrived and was then filtered down to nothing is a
    different fault from one that never arrived, and must not be reported as
    missing coverage — otherwise the warning cries wolf on every run."""
    # Quotes a paragraph that isn't in the document: dropped by _to_findings.
    answered = ProviderResult(parsed={"findings": [{
        "para_id": "body-9999", "error_type": "spelling",
        "original_text": "Nowhere in this document.", "occurrence": 1,
        "corrected_text": "Nowhere in this document!",
        "confidence": "high"}]}, usage=USAGE)
    data, summary = _run(tmp_path, FakeProvider([answered]))

    coverage = data["passes"]
    assert coverage["complete"] is True
    first = coverage["calls"][0]
    assert first["answered"] is True
    # But the discrepancy is still on the record for anyone looking.
    assert first["returned"] == 1 and first["kept"] == 0
    assert "Coverage: complete" in summary


def test_a_caller_that_tracks_nothing_claims_nothing(tmp_path):
    """`finish` without `passes` — the mock path, older callers — writes null
    rather than an empty coverage block that would read as "0 calls, all
    answered, complete"."""
    cfg = load_config(CONFIG)
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    out = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "out",
                 source_path=FIXTURES / "simple.docx")

    data = json.loads(out.findings_json.read_text("utf-8"))
    assert data["passes"] is None
    assert "Coverage: complete" not in out.summary_md.read_text("utf-8")
