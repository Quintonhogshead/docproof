"""JobRunner/JobStore directly, with no worker thread running.

Cancelling a queued job is a race against the worker: the id can already be
sitting in `queue.Queue` when the cancel lands. Driving `run_one` by hand
(its own docstring calls this out as the intended way to test it) reproduces
that race deterministically instead of hoping a live thread loses it.
"""
from __future__ import annotations

import pytest

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings
from docproof.config import load_config
from .conftest import FIXTURES

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


@pytest.fixture
def runner(tmp_path):
    paths = Paths(tmp_path).ensure()
    store = JobStore(paths)
    r = JobRunner(store, Settings(), config_path=CONFIG)
    return store, r


def _job(store, **over) -> Job:
    fields = {"id": "j1", "filename": "simple.docx",
             "source_path": str(FIXTURES / "simple.docx"),
             "model": "claude-sonnet-5", "mode": "now"}
    fields.update(over)
    return store.save(Job(**fields))


def test_a_queued_job_still_in_the_worker_queue_does_not_run(runner,
                                                             monkeypatch):
    """The id can still be in `queue.Queue` after cancelling — nothing removes
    it. What matters is that dequeuing it does nothing."""
    store, r = runner
    _job(store, state="queued")
    r.queue.put("j1")

    called = []
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: called.append(1))

    # What POST /api/jobs/{id}/cancel does under the hood.
    cancelled = store.update_if("j1", expect="queued", state="cancelled")
    assert cancelled is not None and cancelled.state == "cancelled"

    r.run_one("j1")                       # what the worker thread would do
    assert not called, "a cancelled job must never reach the provider"
    assert store.get("j1").state == "cancelled"


def test_a_cancelled_queued_batch_job_is_never_submitted(runner, monkeypatch):
    """The bug this guards against: _submit_batch had no state check at all,
    so a cancelled batch job sitting in the queue would still go to the
    vendor the moment the worker reached it."""
    store, r = runner
    _job(store, state="queued", mode="batch")
    r.queue.put("j1")

    def boom(*a, **k):
        raise AssertionError("a cancelled job must never be submitted")

    monkeypatch.setattr("app.jobs.batchlib.submit", boom)
    store.update_if("j1", expect="queued", state="cancelled")

    r.run_one("j1")
    job = store.get("j1")
    assert job.state == "cancelled"
    assert not (store.dir("j1") / "batch_job_id").exists()


def test_a_cancelled_prep_job_is_never_tagged(runner, monkeypatch):
    store, r = runner
    _job(store, state="queued", kind="prep",
         source_path=str(FIXTURES / "googledoc.docx"))
    r.queue.put("j1")

    def boom(*a, **k):
        raise AssertionError("a cancelled prep job must not run")

    monkeypatch.setattr("app.jobs.preplib.prepare", boom)
    store.update_if("j1", expect="queued", state="cancelled")

    r.run_one("j1")
    assert store.get("j1").state == "cancelled"


def test_update_if_only_wins_when_the_state_still_matches(runner):
    store, _ = runner
    _job(store, state="queued")

    # Stale expectation: refused, nothing changes.
    assert store.update_if("j1", expect="running", state="cancelled") is None
    assert store.get("j1").state == "queued"

    # Correct expectation: applied.
    assert store.update_if("j1", expect="queued", state="cancelled").state \
        == "cancelled"


def test_a_scheduled_job_promoted_by_the_ticker_stays_promoted(runner,
                                                                monkeypatch):
    """The mirror image of the cancel race: an ordinary tick, with nothing
    cancelling underneath it, must still promote a due job."""
    store, r = runner
    _job(store, state="scheduled", mode="batch", schedule_at="00:00")
    monkeypatch.setattr(JobRunner, "_due", lambda self, job: True)

    r.tick_once()
    assert store.get("j1").state == "queued"
    assert not r.queue.empty()
