"""I3 — cost-accounting audit.

The governor's ledger must equal a fresh cost_of_usage recomputation over every
model call, so a run can never under-report its spend (the historical
under-count bug class). Each detector here bills exactly cost_of_usage of the
tokens it threads, so the ledger (sum of charged costs) must equal
cost_of_usage of the total tokens threaded — by the linearity of per-token
pricing.

The sync path is audited here. The batched wave path is D5, which is gated behind
the P0 experiment (the C human-gate) and not yet built; its audit rides this same
harness once it lands.
"""

import pytest

from docproof.models import Usage
from docproof.providers import cost_of_usage

from galley.adapters import AdapterResult, Scope
from galley.orchestrator import Dispatch, run_galley

from tests.galley.fakes import gfinding, make_manuscript

MODEL = "claude-opus-5"
FIXED = lambda: "2026-08-21T00:00:00Z"  # noqa: E731


class AuditDetector:
    """A detector that bills exactly cost_of_usage of the tokens it threads.

    Records every token delta it wrote (``threaded``) so the test can recompute
    the priced total independently of the ledger.
    """

    def __init__(self, name, tokens_in, tokens_out, findings=()):
        self.name = name
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self._findings = list(findings)
        self.threaded = Usage()  # what this detector added, for the recompute

    def run(self, ms, scope, budget_usd, usage):
        for u in (usage, self.threaded):
            bucket = u.by_model.setdefault(
                MODEL, {"input_tokens": 0, "output_tokens": 0, "api_calls": 0})
            bucket["input_tokens"] += self.tokens_in
            bucket["output_tokens"] += self.tokens_out
            bucket["api_calls"] += 1
            u.input_tokens += self.tokens_in
            u.output_tokens += self.tokens_out
            u.api_calls += 1

        delta = Usage()
        delta.by_model[MODEL] = {
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            "api_calls": 1,
        }
        cost = cost_of_usage(delta, fallback_model=MODEL) or 0.0
        return AdapterResult(findings=list(self._findings), coverage_notes=[], cost_usd=cost)


def _combined_usage(*detectors):
    total = Usage()
    for d in detectors:
        for model, bucket in d.threaded.by_model.items():
            into = total.by_model.setdefault(model, {})
            for key, value in bucket.items():
                into[key] = into.get(key, 0) + value
        total.input_tokens += d.threaded.input_tokens
        total.output_tokens += d.threaded.output_tokens
        total.api_calls += d.threaded.api_calls
    return total


def test_ledger_equals_recomputed_cost_sync(tmp_path):
    ms = make_manuscript("alpha", "beta", "gamma")
    ladder = AuditDetector("docproof_ladder", 2000, 400,
                           findings=[gfinding("g-1", "body-0001", "teh", "the")])
    reread = AuditDetector("reread", 1000, 200,
                           findings=[gfinding("g-2", "body-0002", "x", "y")])

    def plan(hyps, gov, cf):
        return [Dispatch("reread", Scope())] if len(cf.waves) < 2 else []

    cf = run_galley(
        ms, "T2", budget_usd=100.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        plan_wave=plan, clock=FIXED,
    )

    # Every model call landed in Usage.by_model (two adapter runs, one each).
    combined = _combined_usage(ladder, reread)
    assert combined.by_model[MODEL]["api_calls"] == 2

    # The ledger equals a fresh cost_of_usage over every token threaded.
    recomputed = (cost_of_usage(combined, fallback_model=MODEL) or 0.0) \
        + combined.sapling_cost
    assert cf.budget.spent_usd == pytest.approx(recomputed)
    assert cf.budget.spent_usd > 0


def test_no_drift_across_charges(tmp_path):
    # The ledger's own charges must sum to its reported spend exactly.
    ms = make_manuscript("alpha")
    ladder = AuditDetector("docproof_ladder", 1500, 300)
    cf = run_galley(
        ms, "T0", budget_usd=100.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder}, clock=FIXED,
    )
    charge_sum = sum(c.cost_usd for c in cf.budget.charges)
    assert cf.budget.spent_usd == pytest.approx(charge_sum)
    recomputed = cost_of_usage(_combined_usage(ladder), fallback_model=MODEL) or 0.0
    assert charge_sum == pytest.approx(recomputed)
