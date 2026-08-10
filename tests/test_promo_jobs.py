"""The promo pipeline through the job runner — no HTTP, no worker thread.

Driving `run_one` by hand, the way test_jobs.py does, so the whole
prepare -> run -> finish path runs against a fake provider. Phase 5 adds the
routes and a TestClient; this is the layer beneath them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings
from docproof.promo import PromoError, PromoResult, SocialPost
from docproof.providers import ProviderResult
from .conftest import FIXTURES
from .fakes import USAGE

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


def _canned() -> PromoResult:
    # "manuscript" is a word in simple.docx; "Zephyr" is not, so the grounding
    # check should flag exactly one term.
    return PromoResult(
        teaser="A manuscript nobody wanted, and the Zephyr that changed it.",
        posts=[SocialPost(text=f"Would you read it? ({i + 1})")
               for i in range(12)])


@pytest.fixture
def runner(tmp_path):
    paths = Paths(tmp_path).ensure()
    store = JobStore(paths)
    r = JobRunner(store, Settings(output_dir=str(tmp_path / "out")),
                  config_path=CONFIG)
    return store, r


def _job(store, **over) -> Job:
    fields = {"id": "j1", "filename": "simple.docx",
              "source_path": str(FIXTURES / "simple.docx"),
              "model": "claude-sonnet-5", "mode": "now", "kind": "promo",
              "state": "queued"}
    fields.update(over)
    return store.save(Job(**fields))


def _wire(monkeypatch, r, result: ProviderResult):
    from .fakes import FakeProvider
    provider = FakeProvider(results=[result])
    monkeypatch.setattr(r, "_provider", lambda cfg: provider)
    return provider


def test_promo_writes_two_docx_and_the_editable_json(runner, monkeypatch):
    store, r = runner
    job = _job(store, approval="pending")
    _wire(monkeypatch, r, ProviderResult(parsed=_canned().model_dump(),
                                         usage=USAGE))

    r.run_one("j1")

    done = store.get("j1")
    assert done.state == "done"
    assert done.is_promo and done.approval == "pending"   # gate persists
    out = Path(done.results_dir)
    assert (out / "simple - teaser.docx").exists()
    assert (out / "simple - social posts.docx").exists()
    assert (out / "promo.json").exists()


def test_promo_grounding_count_and_light(runner, monkeypatch):
    store, r = runner
    _job(store)
    _wire(monkeypatch, r, ProviderResult(parsed=_canned().model_dump(),
                                         usage=USAGE))

    r.run_one("j1")

    done = store.get("j1")
    assert done.unverified == 1        # only "Zephyr" is absent from the book
    assert done.verified is False      # a flag means the card invites a look
    assert done.words and done.words > 0


def test_promo_records_what_it_cost(runner, monkeypatch):
    store, r = runner
    _job(store)
    _wire(monkeypatch, r, ProviderResult(parsed=_canned().model_dump(),
                                         usage=USAGE))

    r.run_one("j1")

    done = store.get("j1")
    assert done.api_calls == 1
    assert done.input_tokens == USAGE.input_tokens
    assert done.output_tokens == USAGE.output_tokens
    assert done.cost is not None       # estimate_cost ran on a known model


def test_promo_dispatch_and_plain_state(runner, monkeypatch):
    """run_one routes a promo job to _run_promo, and the state reads in promo's
    own words, not the review vocabulary."""
    store, r = runner
    job = _job(store, state="running")
    assert job.plain_state() == "Reading the book and writing your copy"

    seen = []
    monkeypatch.setattr(r, "_run_promo", lambda job_id: seen.append(job_id))
    r.run_one("j1")
    assert seen == ["j1"]


def test_a_cancelled_promo_job_is_never_generated(runner, monkeypatch):
    store, r = runner
    _job(store)

    def boom(*a, **k):
        raise AssertionError("a cancelled promo job must not run")

    monkeypatch.setattr("app.jobs.promolib.prepare", boom)
    store.update_if("j1", expect="queued", state="cancelled")

    r.run_one("j1")
    assert store.get("j1").state == "cancelled"


def test_a_model_refusal_fails_with_its_sentence(runner, monkeypatch):
    store, r = runner
    _job(store)
    _wire(monkeypatch, r, ProviderResult(parsed=None, stop_reason="refusal",
                                         error="declined", usage=USAGE))

    r.run_one("j1")

    done = store.get("j1")
    assert done.state == "failed"
    assert done.error and "declined" in done.error


def test_verify_claims_on_makes_a_second_call_and_folds_the_flags(runner,
                                                                  monkeypatch):
    """With the entailment pass on, the runner makes the generation call then
    the check call, and the card's count sums both grounding signals."""
    store, r = runner
    _job(store)

    import app.jobs as jobsmod
    real = jobsmod.load_config
    monkeypatch.setattr(jobsmod, "load_config", lambda p: _with_claims(real(p)))

    from .fakes import FakeProvider
    report = {"claims": [{"claim": "Invented", "supported": False, "note": ""}]}
    provider = FakeProvider(results=[
        ProviderResult(parsed=_canned().model_dump(), usage=USAGE),   # generate
        ProviderResult(parsed=report, usage=USAGE)])                  # check
    monkeypatch.setattr(r, "_provider", lambda cfg: provider)

    r.run_one("j1")

    done = store.get("j1")
    assert done.state == "done"
    # One flagged term ("Zephyr") plus one unsupported claim.
    assert done.unverified == 2
    assert done.verified is False
    assert done.api_calls == 2            # generation + the check


def _with_claims(cfg):
    cfg.promo.verify_claims = True
    return cfg
