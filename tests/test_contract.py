"""The uniform --json envelope every galley-facing CLI verb shares
(docproof/contract.py): findings/cost/ledger/checkpoint, always present, never
omitted, priced per model, and never shadowed by a verb's own extra fields.
"""
from __future__ import annotations

from docproof.contract import build_envelope
from docproof.models import (CoverageGap, CoverageLedger, Finding, StageWarning,
                             Usage)
from docproof.windowing import WindowReport


def _finding(fid: str = "f-0001") -> Finding:
    return Finding(finding_id=fid, chunk_id="chunk-001", para_id="body-0001",
                   error_type="spelling", original_text="teh", occurrence=1,
                   corrected_text="the", explanation="typo", confidence="high")


def _usage_for(model: str, input_tokens: int, output_tokens: int) -> Usage:
    u = Usage()
    u.add(type("R", (), {"input_tokens": input_tokens,
                         "output_tokens": output_tokens,
                         "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": 0})(), model=model)
    return u


def test_empty_envelope_has_every_key_zeroed_not_omitted():
    env = build_envelope()
    assert set(env) == {"findings", "cost", "ledger", "checkpoint"}
    assert env["findings"] == []
    assert env["cost"] == {"total_usd": 0.0, "by_model": {}}
    assert env["ledger"] == {"total": 0, "gaps": [], "unruled": [], "degraded": []}
    assert env["checkpoint"] is None


def test_finding_objects_are_converted_to_finding_to_dict_shape():
    env = build_envelope(findings=[_finding()])
    assert len(env["findings"]) == 1
    row = env["findings"][0]
    assert row["finding_id"] == "f-0001"
    assert row["para_id"] == "body-0001"
    assert row["anchor"] is None                 # unvalidated, per finding_to_dict


def test_dict_findings_pass_through_unchanged():
    row = {"finding_id": "f-9999", "para_id": "body-0009", "made_up_key": True}
    env = build_envelope(findings=[row])
    assert env["findings"] == [row]


def test_cost_is_priced_per_model_and_summed():
    usage = _usage_for("claude-sonnet-5", 1000, 500)
    env = build_envelope(usage=usage, fallback_model="claude-sonnet-5")
    assert env["cost"]["total_usd"] > 0
    assert "claude-sonnet-5" in env["cost"]["by_model"]
    assert env["cost"]["by_model"]["claude-sonnet-5"] == round(
        env["cost"]["total_usd"], 4)


def test_a_mixed_run_prices_each_model_at_its_own_rate_not_one_flat_rate():
    """The historical bug: pricing a mixed run's whole total at one model's
    rate under/over-counts the other model's share by up to ~40x."""
    usage = Usage()
    usage.add(type("R", (), {"input_tokens": 100000, "output_tokens": 20000,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0})(),
              model="claude-opus-5")               # $5/$25 per Mtok
    usage.add(type("R", (), {"input_tokens": 100000, "output_tokens": 20000,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0})(),
              model="gpt-5.6-luna")                 # $0.20/$1.20 per Mtok
    env = build_envelope(usage=usage, fallback_model="claude-opus-5")
    by_model = env["cost"]["by_model"]
    assert by_model["claude-opus-5"] > by_model["gpt-5.6-luna"]
    # Pricing everything at the cheap model's rate would undercount total.
    flat_at_cheap = round(by_model["gpt-5.6-luna"] * 2, 4)
    assert env["cost"]["total_usd"] > flat_at_cheap


def test_sapling_cost_rides_its_own_by_model_bucket():
    usage = Usage()
    usage.sapling_cost = 1.23
    env = build_envelope(usage=usage, fallback_model="claude-sonnet-5")
    assert env["cost"]["by_model"]["sapling"] == 1.23
    assert env["cost"]["total_usd"] == 1.23


def test_ledger_mirrors_coverage_ledger_fields():
    cov = CoverageLedger()
    cov.total = 7
    cov.gaps = [CoverageGap("spelling", "chunk-003", ("body-0009",))]
    cov.unruled = [WindowReport(label="confirm", asked=10, answered=8,
                                truncated_calls=2, extra_calls=0)]
    cov.degraded = [StageWarning("continuity read", "provider 401", "failed")]
    env = build_envelope(coverage=cov)
    assert env["ledger"]["total"] == 7
    assert env["ledger"]["gaps"][0]["chunk_id"] == "chunk-003"
    assert env["ledger"]["unruled"][0]["label"] == "confirm"
    assert env["ledger"]["degraded"][0]["reason"] == "provider 401"


def test_checkpoint_path_is_stringified_or_null():
    assert build_envelope(checkpoint=None)["checkpoint"] is None
    assert build_envelope(checkpoint="/tmp/x/checkpoint.json")["checkpoint"] == \
        "/tmp/x/checkpoint.json"


def test_extra_fields_ride_alongside_but_never_shadow_the_canonical_keys():
    env = build_envelope(findings=[_finding()],
                         extra={"hypotheses": ["h1"], "findings": "SHADOWED"})
    assert env["hypotheses"] == ["h1"]
    assert env["findings"] != "SHADOWED"
    assert env["findings"][0]["finding_id"] == "f-0001"
