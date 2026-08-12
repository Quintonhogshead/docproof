"""Multi-round review in the app: config wiring and worker dispatch (stage F).

config_for turns a job's `rounds`/`judge_prompt` into the run config, and run_one
sends a multi-round job to the dedicated worker-thread driver rather than the
single-review or batch-submit paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings
from .conftest import FIXTURES

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


@pytest.fixture
def runner(tmp_path):
    store = JobStore(Paths(tmp_path).ensure())
    return store, JobRunner(store, Settings(), config_path=CONFIG)


def _job(store, **over) -> Job:
    fields = {"id": "j1", "filename": "simple.docx",
              "source_path": str(FIXTURES / "simple.docx"),
              "model": "claude-sonnet-5", "mode": "now", "glossary_model": "off"}
    fields.update(over)
    return store.save(Job(**fields))


def test_rounds_and_judge_prompt_reach_the_run_config(runner):
    store, r = runner
    cfg = r.config_for(_job(store, rounds=3, judge_prompt="Be strict."))
    assert cfg.rounds.count == 3
    assert cfg.rounds.judge_prompt == "Be strict."


def test_a_record_without_rounds_is_a_single_review(runner):
    store, r = runner
    job = _job(store)                          # older record / untouched panel
    assert job.rounds == 1 and job.judge_prompt == ""
    assert r.config_for(job).rounds.count == 1


def test_run_one_sends_a_multiround_job_to_the_rounds_driver(runner, monkeypatch):
    store, r = runner
    seen = []
    monkeypatch.setattr(r, "_run_rounds", lambda jid: seen.append(("rounds", jid)))
    monkeypatch.setattr(r, "_run_now", lambda jid: seen.append(("now", jid)))
    monkeypatch.setattr(r, "_submit_batch", lambda jid: seen.append(("batch", jid)))
    r.run_one(_job(store, id="a", rounds=2, mode="now").id)
    r.run_one(_job(store, id="b", rounds=1, mode="now").id)
    r.run_one(_job(store, id="c", rounds=3, mode="batch").id)   # rounds wins over mode
    assert seen == [("rounds", "a"), ("now", "b"), ("rounds", "c")]
