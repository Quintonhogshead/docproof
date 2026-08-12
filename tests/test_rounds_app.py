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


def test_judge_model_pick_overrides_the_config_default(runner):
    store, r = runner
    cfg = r.config_for(_job(store, rounds=2, judge_model="claude-opus-5"))
    assert cfg.rounds.judge_model == "claude-opus-5"


def test_empty_judge_model_keeps_the_config_default(runner):
    store, r = runner
    cfg = r.config_for(_job(store, rounds=2))          # untouched picker
    assert cfg.rounds.judge_model == "gpt-5.6-sol"


def test_a_record_without_rounds_is_a_single_review(runner):
    store, r = runner
    job = _job(store)                          # older record / untouched panel
    assert job.rounds == 1 and job.judge_prompt == ""
    assert r.config_for(job).rounds.count == 1


def test_continuity_prompt_reaches_the_run_config(runner):
    store, r = runner
    cfg = r.config_for(_job(store, continuity_prompt="Only eye-colour drift."))
    assert cfg.continuity.prompt == "Only eye-colour drift."


def test_continuity_only_strips_the_run_to_that_one_read(runner):
    store, r = runner
    cfg = r.config_for(_job(store, continuity_only=True))
    assert cfg.continuity.enabled is True              # forced on, it IS the run
    assert cfg.error_types == [] and cfg.sweeps == []
    for p in ("glossary", "adjudicate", "rewrite", "languagetool",
              "consistency", "spellcheck"):
        assert getattr(cfg, p).enabled is False


def test_a_record_without_continuity_flags_is_a_normal_review(runner):
    store, r = runner
    job = _job(store)                          # older record / untouched panel
    assert job.continuity_prompt == "" and job.continuity_only is False
    cfg = r.config_for(job)
    assert cfg.continuity.prompt == "" and cfg.error_types != []


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


# --- per-round progress on the results card ----------------------------------

def test_round_aware_state_names_round_and_sections(runner):
    store, _ = runner
    job = _job(store, state="running", stage="reviewing",
               rounds=3, total_rounds=3, review_round=2, done=5, total=40)
    assert job.plain_state() == "Round 2 of 3 — reviewing (5 of 40 sections)"


def test_round_aware_state_without_a_count_names_only_the_round(runner):
    # Ingesting the working document, or waiting on a vendor batch: no section
    # count yet, and "0 of 0 sections" is the very display this replaced.
    store, _ = runner
    job = _job(store, state="running", stage="reviewing",
               rounds=2, total_rounds=2, review_round=1, done=0, total=0)
    assert job.plain_state() == "Round 1 of 2 — reviewing"


def test_batch_multiround_state_reads_as_overnight(runner):
    # A batch round has no section count for hours; "reviewing" would read as a
    # stuck synchronous pass, so it says what is actually happening.
    store, _ = runner
    job = _job(store, mode="batch", state="running", stage="reviewing",
               rounds=3, total_rounds=3, review_round=1, done=0, total=0)
    assert job.plain_state() == "Round 1 of 3 — processing overnight"


def test_single_review_state_is_unchanged(runner):
    store, _ = runner
    job = _job(store, state="running", stage="reviewing", done=5, total=40)
    assert job.plain_state() == "Reviewing (5 of 40 sections)"


def test_a_record_without_round_fields_reads_as_a_single_review(runner):
    # An app.json written before these fields existed: the loader fills the
    # defaults and the card renders the plain single-review message.
    import json

    from app.jobs import APP_MANIFEST

    store, _ = runner
    job = _job(store, id="old", state="running", stage="reviewing",
               done=3, total=9)
    path = store.dir("old") / APP_MANIFEST
    data = json.loads(path.read_text("utf-8"))
    del data["review_round"], data["total_rounds"]
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.get("old")
    assert loaded.review_round == 0 and loaded.total_rounds == 0
    assert loaded.plain_state() == "Reviewing (3 of 9 sections)"


def test_run_rounds_reports_progress_onto_the_job(runner, monkeypatch):
    """_run_rounds threads on_progress into the driver, and the callback lands
    round/done/total on the record — the whole path the card reads."""
    from docproof.pipeline import Outputs

    store, r = runner
    states = []

    def fake_sync_rounds(cfg, source, error_dir, *, out_dir, review_provider,
                         judge_provider, on_progress=None, **kw):
        assert on_progress is not None
        for call in ((1, 2, 0, 0), (1, 2, 4, 4), (2, 2, 0, 0), (2, 2, 4, 4)):
            on_progress(*call)
            j = store.get("j1")
            states.append((j.review_round, j.total_rounds, j.done, j.total,
                           j.plain_state()))
        return Outputs(reviewed_path=out_dir / "x.docx",
                       summary_md=out_dir / "s.md",
                       findings_json=out_dir / "f.json",
                       applied=0, findings=0)

    monkeypatch.setattr(r, "_provider", lambda cfg: object())
    monkeypatch.setattr("docproof.rounds.run_sync_rounds", fake_sync_rounds)
    r._run_rounds(_job(store, rounds=2, model="claude-sonnet-5").id)

    assert states == [
        (1, 2, 0, 0, "Round 1 of 2 — reviewing"),
        (1, 2, 4, 4, "Round 1 of 2 — reviewing (4 of 4 sections)"),
        (2, 2, 0, 0, "Round 2 of 2 — reviewing"),
        (2, 2, 4, 4, "Round 2 of 2 — reviewing (4 of 4 sections)"),
    ]
    assert store.get("j1").state == "done"


def test_download_anyway_rebuilds_an_audit_failed_rounds_review(runner, cfg,
                                                               tmp_path):
    """The full recovery path: a paid two-round run whose deliverable is gone
    (as after an audit failure) is rebuilt from rounds/composed.json — no
    provider is touched, and the job lands done with the override recorded."""
    from docproof.rounds import run_sync_rounds
    from tests.fakes import FakeProvider, finding_result

    from .test_rounds_sync import _ApproveJudge, _docx, _minimal

    store, r = runner
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    out = tmp_path / "results"
    out.mkdir()
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    outputs = run_sync_rounds(cfg, str(src), CONFIG.parent / "error_types",
                              out_dir=out, review_provider=review,
                              judge_provider=_ApproveJudge())
    outputs.reviewed_path.unlink()               # the audit withheld the file

    job = _job(store, id="ra", filename=Path(src).name, source_path=str(src),
               rounds=2, state="failed", audit_failed=True,
               results_dir=str(out), error="audit failed")
    updated = r.download_anyway("ra")
    assert updated is not None
    assert updated.state == "done" and updated.audit_overridden
    assert outputs.reviewed_path.is_file()       # rebuilt, no model calls
    assert len(review.calls) == 2                # still just the original rounds


def test_download_anyway_refuses_a_rounds_run_without_a_snapshot(runner,
                                                                 tmp_path):
    # A run from before the snapshot existed (or with its working files cleaned
    # away) returns None — the route's 409 — instead of erroring or spending.
    store, r = runner
    out = tmp_path / "results"
    (out / "rounds").mkdir(parents=True)
    job = _job(store, id="old", rounds=3, state="failed", audit_failed=True,
               results_dir=str(out), error="audit failed")
    assert r.download_anyway("old") is None
    assert store.get("old").state == "failed"    # untouched


def test_abort_marks_a_multiround_run_cancelled_not_failed(runner, monkeypatch):
    """Abort on a multi-round run must stop it cleanly. The driver gets a
    should_cancel that reflects the request, and a JobCancelled out of it lands
    the job in 'cancelled' — not 'failed', and not a silent no-op."""
    from docproof.pipeline import JobCancelled

    store, r = runner

    def fake_sync_rounds(cfg, source, error_dir, *, out_dir, review_provider,
                         judge_provider, on_progress=None, should_cancel=None,
                         **kw):
        assert should_cancel is not None and should_cancel()   # abort requested
        raise JobCancelled()

    monkeypatch.setattr(r, "_provider", lambda cfg: object())
    monkeypatch.setattr("docproof.rounds.run_sync_rounds", fake_sync_rounds)
    job = _job(store, rounds=2, model="claude-sonnet-5")
    r.request_cancel(job.id)                                    # user hit Abort
    r._run_rounds(job.id)
    assert store.get(job.id).state == "cancelled"
