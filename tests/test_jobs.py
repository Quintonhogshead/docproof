"""JobRunner/JobStore directly, with no worker thread running.

Cancelling a queued job is a race against the worker: the id can already be
sitting in `queue.Queue` when the cancel lands. Driving `run_one` by hand
(its own docstring calls this out as the intended way to test it) reproduces
that race deterministically instead of hoping a live thread loses it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.jobs import Job, JobRunner, JobStore, read_usage
from app.settings import Paths, Settings
from app.spending import LedgerEntry, SpendingLedger, merge_live
from app.usage import _totals_for, build_usage
from docproof.config import load_config
from .conftest import FIXTURES
from .fakes import FakeProvider

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
             "model": "claude-sonnet-5", "mode": "now",
             # These tests exercise the review worker, resume, and checkpoints;
             # the whole-book glossary pass is a separate model call and off here
             # unless a test opts in.
             "glossary_model": "off"}
    fields.update(over)
    return store.save(Job(**fields))


def test_the_jobs_effort_reaches_the_run_config(runner):
    """config_for is where a job's reasoning depth becomes the model's."""
    store, r = runner
    assert r.config_for(_job(store, effort="max")).api.effort == "max"


def test_a_record_from_before_effort_existed_runs_at_low(runner):
    store, r = runner
    job = _job(store)                     # no effort field set
    assert job.effort == "low"
    assert r.config_for(job).api.effort == "low"


def test_per_run_features_reach_the_run_config(runner):
    """A switch flipped on the submission panel becomes an enabled pass."""
    store, r = runner
    cfg = r.config_for(_job(store, features={"storysheet": True, "rewrite": True}))
    assert cfg.storysheet.enabled is True
    assert cfg.rewrite.enabled is True


def test_a_feature_toggle_can_turn_a_pass_or_safety_net_off(runner):
    store, r = runner
    cfg = r.config_for(_job(store, features={"adjudicate": False, "audit": False}))
    assert cfg.adjudicate.enabled is False   # a default-on pass, switched off
    assert cfg.audit == "off"                # the tri-state audit reads as a toggle


def test_a_record_without_features_keeps_the_config_defaults(runner):
    """Old job records (and untouched panels) carry no features and change
    nothing — storysheet stays off, adjudicate stays on."""
    store, r = runner
    job = _job(store)
    assert job.features == {}
    cfg = r.config_for(job)
    assert cfg.storysheet.enabled is False
    assert cfg.adjudicate.enabled is True


def test_a_running_review_names_the_step_it_is_on():
    """plain_state reads the stage while a whole-book pass runs after the loop,
    so the card doesn't say "reviewing" during the rewrite pass."""
    common = dict(id="j", filename="a.docx", source_path="a.docx",
                  model="m", mode="now", state="running")
    assert Job(**common, stage="reviewing", done=3, total=10).plain_state() \
        == "Reviewing (3 of 10 sections)"
    assert "Rewriting" in Job(**common, stage="rewrite").plain_state()
    assert "glossary" in Job(**common, stage="glossary").plain_state()


def test_a_record_without_a_stage_keeps_the_plain_running_message():
    """Older records (and the moment before the first stage lands) fall back to
    the generic running message with its count."""
    job = Job(id="j", filename="a.docx", source_path="a.docx", model="m",
              mode="now", state="running", done=2, total=5)   # stage ""
    assert job.plain_state() == "Reviewing (2 of 5 sections)"


def test_a_per_run_feature_wins_over_the_settings_default(tmp_path):
    """comments is a settings-backed default; a per-run switch overrides it."""
    paths = Paths(tmp_path).ensure()
    store = JobStore(paths)
    r = JobRunner(store, Settings(comments=True), config_path=CONFIG)
    cfg = r.config_for(_job(store, features={"comments": False}))
    assert cfg.comments is False


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


def test_a_cancel_landing_while_the_worker_prepares_still_wins(runner,
                                                               monkeypatch):
    """The id was dequeued and the guard passed; the cancel lands while the
    document is still being ingested. The old plain `state="running"` write
    clobbered the cancellation and ran the whole review anyway, billed."""
    import app.jobs as jobsmod
    from .fakes import FakeProvider

    provider = FakeProvider()
    store, r = _review_setup(runner, monkeypatch, provider)
    _job(store, state="queued")

    real_prepare = jobsmod.prepare

    def prepare_and_lose_the_race(*a, **kw):
        store.update_if("j1", expect="queued", state="cancelled")
        return real_prepare(*a, **kw)

    monkeypatch.setattr("app.jobs.prepare", prepare_and_lose_the_race)

    r.run_one("j1")
    assert store.get("j1").state == "cancelled"
    assert provider.calls == [], "a cancelled job must never reach the provider"


def test_a_cancel_landing_during_batch_submission_still_wins(runner,
                                                             monkeypatch):
    """Submitting to the vendor takes a network round-trip; a cancel in that
    window must not be overwritten by `waiting` — the batch is already gone,
    but the job the user pulled back stays pulled back."""
    from types import SimpleNamespace

    store, r = runner
    _job(store, state="queued", mode="batch")
    monkeypatch.setattr("app.jobs.build_provider", lambda cfg, api_key=None: None)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")

    def submit_and_lose_the_race(*a, **kw):
        store.update_if("j1", expect="queued", state="cancelled")
        return SimpleNamespace(job_id="b-1", request_count=3)

    monkeypatch.setattr("app.jobs.batchlib.submit", submit_and_lose_the_race)

    r.run_one("j1")
    assert store.get("j1").state == "cancelled"


def test_the_batch_id_is_on_disk_before_the_job_says_waiting(runner,
                                                             monkeypatch):
    """`waiting` is the ticker's cue to go looking for the id file. Written in
    the other order, a tick between the two writes failed the job as lost
    while the batch was live — and billed — at the vendor."""
    from types import SimpleNamespace

    store, r = runner
    _job(store, state="queued", mode="batch")
    monkeypatch.setattr("app.jobs.build_provider", lambda cfg, api_key=None: None)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    monkeypatch.setattr(
        "app.jobs.batchlib.submit",
        lambda *a, **kw: SimpleNamespace(job_id="b-1", request_count=3))

    real = store.update_if
    seen = {}

    def spying(job_id, *, expect, **fields):
        if fields.get("state") == "waiting":
            seen["id_on_disk"] = (store.dir(job_id) / "batch_job_id").is_file()
        return real(job_id, expect=expect, **fields)

    monkeypatch.setattr(store, "update_if", spying)

    r.run_one("j1")
    assert store.get("j1").state == "waiting"
    assert seen["id_on_disk"] is True


def test_only_one_tick_pass_runs_at_a_time(runner, monkeypatch):
    """The ticker thread and POST /api/tick both call tick_once. A second
    caller is turned away — two passes over the same ready batch used to
    collect it twice, into two folders."""
    store, r = runner
    _job(store, state="scheduled", mode="batch", schedule_at="00:00")
    monkeypatch.setattr(JobRunner, "_due", lambda self, job: True)

    assert r._tick_mutex.acquire(blocking=False)   # another pass is looking
    try:
        r.tick_once()
        assert store.get("j1").state == "scheduled"    # turned away, no work
    finally:
        r._tick_mutex.release()

    r.tick_once()                                  # the lock is free again
    assert store.get("j1").state == "queued"


def test_update_if_only_wins_when_the_state_still_matches(runner):
    store, _ = runner
    _job(store, state="queued")

    # Stale expectation: refused, nothing changes.
    assert store.update_if("j1", expect="running", state="cancelled") is None
    assert store.get("j1").state == "queued"

    # Correct expectation: applied.
    assert store.update_if("j1", expect="queued", state="cancelled").state \
        == "cancelled"


# --- recovering a batch that landed after the collect failed ------------------
#
# The batch is billed and its results sit at the vendor, so a collect-time
# failure must be finished by re-collecting, not resubmitted (that is `retry`,
# which pays for the same batch twice).

def _failed_batch_job(store, **over):
    """A review that failed with a completed batch still at the vendor: the
    batch_job_id file is what marks "there is a batch to collect"."""
    job = _job(store, mode="batch", state="failed",
               error="LanguageTool not found in /root/.cache/language_tool_python.",
               **over)
    (store.dir(job.id) / "batch_job_id").write_text("batch-1", "utf-8")
    return job


def test_recover_sends_a_failed_batch_back_to_be_collected(runner):
    store, r = runner
    _failed_batch_job(store)

    updated = r.recover("j1")

    assert updated is not None
    # Back on the ticker's collect path (waiting/collecting), not resubmitted
    # (queued), so the paid batch is reused instead of billed again.
    assert updated.state == "waiting"
    assert updated.error is None
    assert store.get("j1").state == "waiting"


def test_recover_refuses_a_failure_with_no_batch_at_the_vendor(runner):
    """A "now" run, or a batch that died before it was ever submitted, has
    nothing at the vendor — recover leaves it failed for Retry to rerun."""
    store, r = runner
    _job(store, mode="batch", state="failed", error="ingest blew up")

    assert r.recover("j1") is None
    assert store.get("j1").state == "failed"


def test_recover_refuses_an_audit_failure(runner):
    """A reject-all audit failure would only fail the same audit again; it has
    its own Download-anyway path, so recover declines it."""
    store, r = runner
    _failed_batch_job(store, audit_failed=True)

    assert r.recover("j1") is None
    assert store.get("j1").state == "failed"


def test_recover_ignores_a_job_that_did_not_fail(runner):
    store, r = runner
    _job(store, mode="batch", state="waiting")
    (store.dir("j1") / "batch_job_id").write_text("batch-1", "utf-8")

    assert r.recover("j1") is None


def test_can_recover_matches_what_recover_will_do(runner):
    store, r = runner
    recoverable = _failed_batch_job(store, id="good")
    no_batch = _job(store, id="nobatch", mode="batch", state="failed")
    audit = _failed_batch_job(store, id="audit", audit_failed=True)

    assert r.can_recover(recoverable) is True
    assert r.can_recover(no_batch) is False
    assert r.can_recover(audit) is False


# --- resuming what was already paid for ---------------------------------------
#
# The afternoon this exists because of: a 192-call review was interrupted and
# restarted four times, and every restart began again at call one, billed.

def _review_setup(runner_fixture, monkeypatch, provider):
    store, r = runner_fixture
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    settings_dir = store.paths.root / "out"
    r.settings.output_dir = str(settings_dir)
    # These tests pin resume/checkpoint semantics, which are defined by document
    # order — a call fails, the run dies, the resume reuses exactly the
    # successful calls. Concurrent fetch would make *which* call the
    # DyingProvider kills non-deterministic; that speed optimisation is proved
    # equivalent to serial separately, in test_concurrency.py. So run these one
    # call at a time.
    base = r.config_for                       # bound to this runner

    def serial_config(self, job):
        cfg = base(job)
        cfg.api.concurrency = 1
        return cfg

    monkeypatch.setattr(type(r), "config_for", serial_config)
    return store, r


def _die_midway(r, job_id):
    with pytest.raises(RuntimeError, match="died here"):
        r.run_one(job_id)          # what _work would catch and mark failed


def test_an_interrupted_review_resumes_instead_of_starting_over(
        runner, monkeypatch):
    from .fakes import DyingProvider, FakeProvider

    store, r = _review_setup(runner, monkeypatch, DyingProvider(survive=1))
    _job(store, state="queued")
    _die_midway(r, "j1")
    assert (store.dir("j1") / "checkpoint.json").is_file()

    # The app restarts: a fresh runner, a healthy provider, the same folder.
    fresh = FakeProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: fresh)
    store.update("j1", state="queued")
    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "done", job.error
    total = job.total
    # The call the first attempt paid for was not made again.
    assert len(fresh.calls) == total - 1
    assert not (store.dir("j1") / "checkpoint.json").exists()


def test_a_resumed_review_equals_an_uninterrupted_one(runner, monkeypatch,
                                                      tmp_path):
    """Same findings, same f-NNNN ids — a reader of the output cannot tell
    the run was ever interrupted. The usage totals are the one honest
    difference: the first attempt paid for a call that failed, the resume
    paid to retry it, and the total owns both. (Here the failed call is the
    scripted finding landing in a pass whose schema rejects it.)"""
    import json as jsonlib

    from .fakes import DyingProvider, FakeProvider, finding_result

    splice = "The manuscript was finished, nobody wanted to read it."
    def scripted():
        return [finding_result(
            para_id="body-0000", error_type="comma_splice", original=splice,
            corrected=splice.replace(",", ";", 1))]

    # Control: one clean run.
    store, r = _review_setup(runner, monkeypatch, FakeProvider(scripted()))
    _job(store, state="queued")
    r.run_one("j1")
    control = store.get("j1")
    control_findings = jsonlib.loads(
        (Path(control.results_dir) / "findings.json").read_text("utf-8"))

    # Interrupted run of the same document, dying after 2 calls.
    _job(store, id="j2", state="queued")
    dying = DyingProvider(scripted(), survive=2)
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: dying)
    _die_midway(r, "j2")
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: FakeProvider())
    store.update("j2", state="queued")
    r.run_one("j2")
    resumed = store.get("j2")
    resumed_findings = jsonlib.loads(
        (Path(resumed.results_dir) / "findings.json").read_text("utf-8"))

    strip = ("generated_at",)
    a = {k: v for k, v in control_findings.items() if k not in strip}
    b = {k: v for k, v in resumed_findings.items() if k not in strip}
    assert a["findings"] == b["findings"]      # ids, order, statuses — all of it
    # One extra call, at USAGE's prices: the schema-mismatch call the first
    # attempt burned before dying, which the resume retried rather than
    # forgot. Erasing it was the bug — the control's total is what an
    # uninterrupted run costs, not what this one did.
    au, bu = a["usage"], b["usage"]
    assert bu["api_calls"] == au["api_calls"] + 1
    assert bu["input_tokens"] == au["input_tokens"] + 100
    assert bu["output_tokens"] == au["output_tokens"] + 20
    assert bu["cache_read_input_tokens"] == au["cache_read_input_tokens"] + 50


def test_a_failed_call_is_retried_on_resume_not_trusted(runner, monkeypatch):
    """A provider error mid-run returns no findings without raising. That
    chunk must be re-asked on resume — "the call failed" and "this chunk is
    clean" are different facts that both look like []."""
    from docproof.providers import ProviderResult
    from .fakes import DyingProvider, FakeProvider, USAGE

    # Call 1 errors (recorded ok=False), call 2 succeeds, then death.
    provider = DyingProvider(
        [ProviderResult(stop_reason="error", error="503", usage=USAGE)],
        survive=2)
    store, r = _review_setup(runner, monkeypatch, provider)
    _job(store, state="queued")
    _die_midway(r, "j1")

    fresh = FakeProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: fresh)
    store.update("j1", state="queued")
    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "done"
    # total - 1: only the one *successful* first-attempt call was reused.
    assert len(fresh.calls) == job.total - 1


def test_an_edited_document_invalidates_the_checkpoint(runner, monkeypatch,
                                                       tmp_path):
    from .fakes import DyingProvider, FakeProvider

    source = tmp_path / "manuscript.docx"
    source.write_bytes((FIXTURES / "simple.docx").read_bytes())
    store, r = _review_setup(runner, monkeypatch, DyingProvider(survive=1))
    _job(store, state="queued", source_path=str(source),
         filename="manuscript.docx")
    _die_midway(r, "j1")

    # The author edits the manuscript between the crash and the retry.
    source.write_bytes((FIXTURES / "styled.docx").read_bytes())
    fresh = FakeProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: fresh)
    store.update("j1", state="queued")
    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "done"
    assert len(fresh.calls) == job.total      # every call made afresh


def test_a_different_model_invalidates_the_checkpoint(runner, monkeypatch):
    from .fakes import DyingProvider, FakeProvider

    store, r = _review_setup(runner, monkeypatch, DyingProvider(survive=1))
    _job(store, state="queued")
    _die_midway(r, "j1")

    fresh = FakeProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: fresh)
    store.update("j1", state="queued", model="claude-haiku-4-5")
    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "done"
    assert len(fresh.calls) == job.total      # cached answers were another model's


def test_an_interrupted_prep_resumes_and_still_verifies(runner, monkeypatch):
    """Prep windows feed each other, so the resume also has to rebuild the
    running context. The word-for-word check at the end is the proof the
    stitched-together run is a real one."""
    from .test_prep_app import TaggingProvider

    dying = TaggingProvider()
    original = dying.complete_structured

    def die_after_first(**kwargs):
        if len(dying.calls) >= 1:
            raise RuntimeError("the process died here")
        return original(**kwargs)

    dying.complete_structured = die_after_first
    store, r = _review_setup(runner, monkeypatch, dying)
    # Small windows so the fixture spans several calls.
    base = r.config_for

    def tiny_windows(job):
        cfg = base(job)
        cfg.prep.max_paragraphs_per_window = 4
        return cfg

    monkeypatch.setattr(type(r), "config_for",
                        lambda self, job: tiny_windows(job))
    _job(store, state="queued", kind="prep",
         source_path=str(FIXTURES / "googledoc.docx"),
         filename="googledoc.docx", mode="now")

    _die_midway(r, "j1")
    assert (store.dir("j1") / "checkpoint.json").is_file()

    healthy = TaggingProvider()
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: healthy)
    store.update("j1", state="queued")
    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "done", job.error
    assert job.verified is True                    # word-for-word check passed
    assert len(healthy.calls) == job.total - 1     # window 1 came from the cache
    assert not (store.dir("j1") / "checkpoint.json").exists()


def test_cancelling_discards_the_checkpoint(runner, monkeypatch):
    from .fakes import DyingProvider

    store, r = _review_setup(runner, monkeypatch, DyingProvider(survive=2))
    _job(store, state="queued")
    _die_midway(r, "j1")
    assert (store.dir("j1") / "checkpoint.json").is_file()

    store.update_if("j1", expect="running", state="cancelled")
    r.discard_checkpoint("j1")
    assert not (store.dir("j1") / "checkpoint.json").exists()


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


# -- aborting a running job -------------------------------------------------

class _CancelOnFirstCall(FakeProvider):
    """Requests its own job's abort from inside the first model call, so the
    run is mid-flight — some calls already paid for — when the cancel lands."""

    def __init__(self, runner, job_id):
        super().__init__()
        self._runner = runner
        self._job_id = job_id

    def complete_structured(self, **kwargs):
        self._runner.request_cancel(self._job_id)
        return super().complete_structured(**kwargs)


def test_aborting_a_running_review_stops_the_tail(runner, monkeypatch):
    """A review already spending is told to stop; it must halt before working
    the whole document, and end cancelled rather than done — with nothing
    written, since it never finished."""
    store, r = runner
    provider = _CancelOnFirstCall(r, "j1")
    _review_setup(runner, monkeypatch, provider)   # concurrency 1, provider wired
    _job(store, state="queued")

    r.run_one("j1")

    job = store.get("j1")
    assert job.state == "cancelled"
    assert 0 < len(provider.calls) < 6, "stopped mid-document, not at the end"
    assert not job.results_dir, "an aborted run writes nothing"
    assert not (store.dir("j1") / "checkpoint.json").exists()


def test_a_stale_abort_flag_cannot_touch_a_later_run(runner):
    """The worker clears a job's abort request when it finishes with the id, so
    an abort that arrived too late can't quietly kill the next thing."""
    store, r = runner
    r.request_cancel("j1")
    assert r._cancel_pending("j1")
    r._clear_cancel("j1")
    assert not r._cancel_pending("j1")


# -- deleting a finished job ------------------------------------------------

def test_delete_job_removes_the_documents_and_the_record(runner):
    store, r = runner
    out = store.paths.root / "out"
    r.settings.output_dir = str(out)
    results = out / "MyBook"
    results.mkdir(parents=True)
    (results / "MyBook - Pre-Proofread.docx").write_text("reviewed")
    _job(store, state="done", results_dir=str(results))

    assert r.delete_job("j1") is True
    assert not results.exists(), "produced documents are removed"
    assert store.get("j1") is None, "the record leaves the results list"


def test_delete_job_spares_a_results_dir_outside_the_output_base(runner,
                                                                 tmp_path):
    """The rmtree only ever reaches inside the configured output directory; a
    results_dir pointing anywhere else has its record removed but its files
    left untouched."""
    store, r = runner
    r.settings.output_dir = str(store.paths.root / "out")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    _job(store, state="done", results_dir=str(outside))

    assert r.delete_job("j1") is True
    assert store.get("j1") is None
    assert (outside / "precious.txt").exists(), "files outside the base survive"


# --- the spending ledger: clearing jobs must not erase the bill --------------

def _spent_job(store, **over) -> Job:
    """A finished job that actually cost something."""
    from datetime import datetime, timezone
    fields = {"state": "done", "input_tokens": 1000, "output_tokens": 500,
              "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 3,
              "cost": 0.42, "words": 1200,
              "created_at": datetime.now(timezone.utc).isoformat()}
    fields.update(over)
    return _job(store, **fields)


def test_clearing_a_job_keeps_its_spending(runner):
    """Deleting a job reclaims its disk but not its cost: the ledger holds the
    totals so the spending screen reads the same before and after a clear."""
    store, r = runner
    _spent_job(store)
    before = build_usage(store.all(), read_usage)["totals"]

    assert r.delete_job("j1") is True
    assert store.get("j1") is None, "the job leaves the results list"

    ledger = SpendingLedger(store.paths.spending_db)
    after = build_usage(merge_live(store.all(), ledger.entries()),
                        read_usage)["totals"]
    assert after["cost"] == before["cost"]
    assert after["input_tokens"] == before["input_tokens"]
    assert after["jobs"] == before["jobs"]


def test_recording_the_same_job_twice_does_not_double_count(runner):
    """Idempotent by id: a job cleared, its spend read, and (somehow) cleared
    again is one row, not two."""
    store, r = runner
    job = _spent_job(store)
    entry = LedgerEntry.from_job(job, _totals_for(job, read_usage))
    ledger = SpendingLedger(store.paths.spending_db)
    ledger.record(entry)
    ledger.record(entry)
    assert len(ledger.entries()) == 1


def test_a_job_that_never_spent_leaves_no_ledger_row(runner):
    """A cancelled job that never reached the API has no cost worth keeping, so
    it does not litter the ledger."""
    store, r = runner
    _job(store, state="cancelled")
    assert r.delete_job("j1") is True
    assert SpendingLedger(store.paths.spending_db).entries() == []


def test_a_live_job_wins_over_its_ledger_row(runner):
    """If a job is somehow both live and in the ledger, the live record is
    authoritative and the stale row is dropped — no double counting."""
    store, r = runner
    job = _spent_job(store)
    entry = LedgerEntry.from_job(job, _totals_for(job, read_usage))
    SpendingLedger(store.paths.spending_db).record(entry)
    merged = merge_live(store.all(),
                        SpendingLedger(store.paths.spending_db).entries())
    assert [j.id for j in merged] == ["j1"]


def test_clearing_does_not_free_monthly_cap_headroom(runner):
    """The cap gate reads month_spend; a cleared job's cost still counts, so a
    user cannot reset their limit by clearing the results list."""
    from app.routes import common
    store, r = runner
    _spent_job(store)
    before = common.month_spend(store, "")
    assert before == pytest.approx(0.42)

    assert r.delete_job("j1") is True
    assert common.month_spend(store, "") == pytest.approx(before)


def test_the_jobs_glossary_model_reaches_the_run_config(runner):
    """The submission panel's glossary pick becomes the pass's model, and "off"
    turns the pass off — without touching the page-by-page reviewer model."""
    store, r = runner
    cfg = r.config_for(_job(store, glossary_model="claude-opus-5"))
    assert cfg.glossary.enabled is True and cfg.glossary.model == "claude-opus-5"

    off = r.config_for(_job(store, id="j2", glossary_model="off"))
    assert off.glossary.enabled is False
