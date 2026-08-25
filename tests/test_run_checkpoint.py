"""The whole-run findings checkpoint (docproof/run_checkpoint.py).

The contract: what run_sync paid for survives to disk before finish() runs, and a
resume replays it only when the fingerprint proves it belongs to this run.
"""

from __future__ import annotations

import json

from docproof import run_checkpoint
from docproof.models import (CoverageGap, CoverageLedger, Finding, StageWarning,
                             Usage)
from docproof.windowing import WindowReport


def _finding(fid: str, para: str = "body-0001") -> Finding:
    return Finding(finding_id=fid, chunk_id="chunk-001", para_id=para,
                   error_type="spelling", original_text="teh",
                   occurrence=1, corrected_text="the", explanation="typo",
                   confidence="high")


def _usage() -> Usage:
    u = Usage()
    u.add(type("R", (), {"input_tokens": 100, "output_tokens": 20,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0})(), model="claude-x")
    return u


def _coverage() -> CoverageLedger:
    cov = CoverageLedger()
    cov.total = 7
    cov.gaps = [CoverageGap("spelling", "chunk-003", ("body-0009", "body-0010"))]
    cov.unruled = [WindowReport(label="confirm", asked=10, answered=8,
                                truncated_calls=2, extra_calls=0)]
    cov.degraded = [StageWarning("continuity read", "provider 401", "failed")]
    return cov


FP = {"content_hash": "abc123", "model": "claude-x", "config": "/x/default.yaml"}


def test_round_trip_findings_usage_coverage(tmp_path):
    findings = [_finding("f-0001"), _finding("f-0002", "body-0002")]
    run_checkpoint.save(tmp_path, findings=findings, usage=_usage(),
                        coverage=_coverage(), fingerprint=FP)
    hit = run_checkpoint.load(tmp_path, fingerprint=FP)
    assert hit is not None
    assert [f.finding_id for f in hit.findings] == ["f-0001", "f-0002"]
    assert hit.findings[1].para_id == "body-0002"
    # usage
    assert hit.usage.input_tokens == 100
    assert hit.usage.api_calls == 1
    assert hit.usage.by_model.get("claude-x", {}).get("output_tokens") == 20
    # coverage — the degradation surface must survive a resume
    assert hit.coverage.total == 7
    assert hit.coverage.gaps[0].para_ids == ("body-0009", "body-0010")
    assert hit.coverage.unruled[0].lost == 2
    assert hit.coverage.degraded[0].reason == "provider 401"


def test_missing_checkpoint_is_none(tmp_path):
    assert run_checkpoint.load(tmp_path, fingerprint=FP) is None


def test_fingerprint_mismatch_is_ignored(tmp_path):
    run_checkpoint.save(tmp_path, findings=[_finding("f-0001")], usage=Usage(),
                        coverage=None, fingerprint=FP)
    other = {**FP, "content_hash": "different"}
    assert run_checkpoint.load(tmp_path, fingerprint=other) is None


def test_version_mismatch_is_ignored(tmp_path):
    path = tmp_path / run_checkpoint.FILENAME
    path.write_text(json.dumps({"version": 999, "fingerprint": FP,
                                "usage": {}, "coverage": None, "findings": []}),
                    encoding="utf-8")
    assert run_checkpoint.load(tmp_path, fingerprint=FP) is None


def test_corrupt_checkpoint_is_ignored(tmp_path):
    (tmp_path / run_checkpoint.FILENAME).write_text("{ not json",
                                                    encoding="utf-8")
    assert run_checkpoint.load(tmp_path, fingerprint=FP) is None


def test_clear_removes_the_file(tmp_path):
    run_checkpoint.save(tmp_path, findings=[_finding("f-0001")], usage=Usage(),
                        coverage=None, fingerprint=FP)
    assert (tmp_path / run_checkpoint.FILENAME).is_file()
    run_checkpoint.clear(tmp_path)
    assert not (tmp_path / run_checkpoint.FILENAME).is_file()
    run_checkpoint.clear(tmp_path)  # idempotent — no error on a missing file


def test_none_coverage_round_trips_to_empty_ledger(tmp_path):
    run_checkpoint.save(tmp_path, findings=[], usage=Usage(), coverage=None,
                        fingerprint=FP)
    hit = run_checkpoint.load(tmp_path, fingerprint=FP)
    assert hit is not None
    assert hit.coverage.total == 0 and hit.coverage.gaps == []
