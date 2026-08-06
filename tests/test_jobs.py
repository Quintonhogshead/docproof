"""JobRunner/JobStore directly, with no worker thread running.

Cancelling a queued job is a race against the worker: the id can already be
sitting in `queue.Queue` when the cancel lands. Driving `run_one` by hand
(its own docstring calls this out as the intended way to test it) reproduces
that race deterministically instead of hoping a live thread loses it.
"""
from __future__ import annotations

from pathlib import Path

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
    """Same findings, same f-NNNN ids, same usage totals — a reader of the
    output cannot tell the run was ever interrupted."""
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
    assert a["usage"] == b["usage"]


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


# --- the dictionary scan, per job ---------------------------------------------

def test_a_job_leaves_the_dictionary_scan_to_the_config_by_default(runner):
    """A job that says nothing about the scan gets whatever the press has
    configured — which is what every job recorded before these fields
    existed meant, and what the watcher still submits."""
    store, r = runner
    cfg = r.config_for(_job(store))
    assert cfg.spellcheck.enabled is load_config(CONFIG).spellcheck.enabled


def test_a_job_can_turn_the_dictionary_scan_off_for_itself(runner):
    """Per submission rather than per install: the dictionary belongs to the
    manuscript. A book dense enough in invented vocabulary is better read
    without one than with every second word raised."""
    store, r = runner
    assert r.config_for(_job(store, spellcheck=False)).spellcheck.enabled is False
    assert r.config_for(_job(store, spellcheck=True)).spellcheck.enabled is True


def test_a_job_can_name_its_own_dictionary(runner):
    """spylls ships en_US only, so a British manuscript needs the press to
    point at an .aff/.dic pair of its own — for that book, not for all of
    them."""
    store, r = runner
    cfg = r.config_for(_job(store, dictionary="dicts/en_GB"))
    assert cfg.spellcheck.dictionary == "dicts/en_GB"


def test_the_choice_survives_a_round_trip_through_the_store(runner):
    """Jobs are JSON on disk and a batch can be collected days later, so a
    setting the manifest cannot carry is a setting that quietly reverts."""
    store, _ = runner
    _job(store, spellcheck=False, dictionary="dicts/en_GB")
    reloaded = store.get("j1")
    assert reloaded.spellcheck is False
    assert reloaded.dictionary == "dicts/en_GB"


def test_the_change_log_can_be_turned_off(runner):
    """It costs no API call — it is written from findings already paid for —
    so this buys tidiness rather than money. Off means the file is simply not
    written."""
    store, r = runner
    assert r.config_for(_job(store)).change_log is True
    r.settings.change_log = False
    assert r.config_for(_job(store)).change_log is False


def test_a_job_carries_its_own_answer_about_voice(runner):
    """The most book-specific decision the pipeline makes, so it belongs to
    the book. Silence means whatever the config says."""
    store, r = runner
    assert r.config_for(_job(store)).voice == "preserve"
    assert r.config_for(_job(store, voice="correct")).voice == "correct"
    assert store.get("j1").voice == "correct"


# --- room in the vendor's queue -----------------------------------------------
#
# Batch APIs meter enqueued tokens across every batch in flight at once, and
# the room comes back as batches finish rather than as time passes. Four
# manuscripts dropped on the window used to go out inside a few seconds and the
# later ones came back rejected.

def _ceiling(monkeypatch, tokens: int) -> None:
    base = JobRunner.config_for

    def with_ceiling(self, job):
        cfg = base(self, job)
        cfg.batch.enqueued_token_ceiling = tokens
        return cfg

    monkeypatch.setattr(JobRunner, "config_for", with_ceiling)


def _batch_setup(runner_fixture, monkeypatch, ceiling: int):
    from .fakes import FakeProvider

    store, r = _review_setup(runner_fixture, monkeypatch, FakeProvider())
    _ceiling(monkeypatch, ceiling)
    return store, r


def test_a_batch_holds_when_the_vendors_queue_is_already_full(runner,
                                                              monkeypatch):
    store, r = _batch_setup(runner, monkeypatch, 1_000_000)
    _job(store, id="ahead", state="waiting", mode="batch",
         est_tokens=1_000_000)
    _job(store, id="j2", state="queued", mode="batch")

    def boom(*a, **k):
        raise AssertionError("nothing may be sent while the queue is full")

    monkeypatch.setattr("app.jobs.batchlib.submit", boom)

    r.run_one("j2")
    held = store.get("j2")
    assert held.state == "holding"
    assert held.est_tokens > 0          # measured, so the ticker can add it up
    assert not (store.dir("j2") / "batch_job_id").exists()


def test_a_held_job_goes_out_when_the_batch_ahead_of_it_lands(runner,
                                                              monkeypatch):
    """The whole point of holding: it is a queue, not a refusal."""
    store, r = _batch_setup(runner, monkeypatch, 1_000_000)
    _job(store, id="ahead", state="waiting", mode="batch",
         est_tokens=1_000_000)
    _job(store, id="j2", state="queued", mode="batch")
    r.run_one("j2")
    assert store.get("j2").state == "holding"

    store.update("ahead", state="done")       # the batch ahead of it lands
    r.tick_once()
    assert store.get("j2").state == "queued"
    assert not r.queue.empty()

    r.run_one("j2")
    out = store.get("j2")
    assert out.state == "waiting", out.error
    assert (store.dir("j2") / "batch_job_id").is_file()


def test_a_manuscript_bigger_than_the_whole_ceiling_still_goes_out(runner,
                                                                   monkeypatch):
    """A book too large for the ceiling has nothing to wait for — no batch
    ahead of it will ever make it fit. Holding it would be a job that never
    moves; sending it gets the vendor's answer, which the user can read."""
    store, r = _batch_setup(runner, monkeypatch, 1)
    _job(store, id="j1", state="queued", mode="batch")

    r.run_one("j1")
    job = store.get("j1")
    assert job.state == "waiting", job.error


def test_a_held_job_that_loses_the_race_is_not_read_again(runner, monkeypatch):
    """Two jobs woken by one tick both come back to the worker; the second
    finds the room gone. It must re-hold on the estimate it already has rather
    than ingesting and chunking the manuscript to learn the same number."""
    store, r = _batch_setup(runner, monkeypatch, 1_000_000)
    _job(store, id="ahead", state="waiting", mode="batch",
         est_tokens=1_000_000)
    _job(store, id="j2", state="queued", mode="batch", est_tokens=500_000)

    def boom(*a, **k):
        raise AssertionError("a job already measured must not be read again")

    monkeypatch.setattr("app.jobs.prepare", boom)

    r.run_one("j2")
    held = store.get("j2")
    assert held.state == "holding" and held.est_tokens == 500_000


def test_the_ceiling_is_counted_per_vendor(runner, monkeypatch):
    """One vendor's full queue says nothing about another's. A house running
    OpenAI overnight and Anthropic by day would otherwise throttle itself
    against a limit that does not apply."""
    store, r = _batch_setup(runner, monkeypatch, 1_000_000)
    _job(store, id="ahead", state="waiting", mode="batch",
         model="gpt-5.6-luna", est_tokens=5_000_000)
    _job(store, id="j2", state="queued", mode="batch",
         model="claude-sonnet-5")

    r.run_one("j2")
    assert store.get("j2").state == "waiting", store.get("j2").error


def test_the_queue_can_be_turned_off(runner, monkeypatch):
    """0 is what DocProof did before the queue existed: submit on sight."""
    store, r = _batch_setup(runner, monkeypatch, 0)
    _job(store, id="ahead", state="waiting", mode="batch",
         est_tokens=9_000_000)
    _job(store, id="j2", state="queued", mode="batch")

    r.run_one("j2")
    assert store.get("j2").state == "waiting", store.get("j2").error


def test_a_cancelled_held_job_is_not_woken_by_the_ticker(runner, monkeypatch):
    """The same race the scheduled promotion guards: a cancel landing between
    the ticker reading the job and writing its new state must win."""
    store, r = _batch_setup(runner, monkeypatch, 1_000_000)
    _job(store, id="j1", state="holding", mode="batch", est_tokens=10)
    store.update_if("j1", expect="holding", state="cancelled")

    r.tick_once()
    assert store.get("j1").state == "cancelled"
    assert r.queue.empty()


def test_the_estimate_counts_the_prompts_not_only_the_manuscript(runner,
                                                                 monkeypatch):
    """Every request carries its pass's system prompt — the error-type
    definitions, the book's vocabulary, the variant's conventions — and on a
    manuscript's worth of requests that is not a rounding error. Counting the
    text alone would let a book into the queue at half its real weight."""
    from docproof.pipeline import prepare

    store, r = _batch_setup(runner, monkeypatch, 0)
    job = _job(store, id="j1", state="queued", mode="batch")
    cfg = r.config_for(job)
    prepared = prepare(cfg, job.source_path, r.error_dir)

    r.run_one("j1")
    measured = store.get("j1").est_tokens
    assert measured > prepared.est_document_tokens
