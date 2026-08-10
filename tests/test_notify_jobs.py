"""Completion emails for every job — built, gated, and sent without a network.

The email schema is the DocWatch completion log; this covers the job-only form
of it (review, prep, promo), the one switch and address it reuses, and the
runner seam that fires it — all through the injected opener, so no mail leaves.
"""
from __future__ import annotations

import base64

from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings
from app.watch import notify
from app.watch.settings import WatchSettings

from .conftest import FIXTURES
from .fakes import fake_drive, http_error

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


def _job(**over) -> Job:
    fields = dict(
        id="j1", filename="Rowan - book 1.docx", source_path="x",
        model="claude-sonnet-5", mode="now", state="done", kind="promo",
        source="app", cost=1.23, words=95000, unverified=2, api_calls=1,
        input_tokens=1000, output_tokens=200,
        created_at="2026-08-10T10:00:00+00:00",
        updated_at="2026-08-10T10:02:30+00:00")
    fields.update(over)
    return Job(**fields)


def _configured(home) -> None:
    WatchSettings(client_id="c", client_secret="s",
                  notify_email="quinton@atmospherepress.com",
                  notify_on_complete=True).save(home)


def _raw(opener) -> str:
    return base64.urlsafe_b64decode(opener.emails[0]["raw"]).decode()


# --- the job-only builder -----------------------------------------------------

def test_completion_for_job_speaks_each_pipeline():
    subj, promo, _ = notify.completion_for_job(_job(kind="promo"))
    assert subj.startswith(notify.DONE_TAGS)
    assert "Rowan - book 1.docx" in subj
    assert "Promo copy" in promo and "Grounding flags to check: 2" in promo

    _, prep, _ = notify.completion_for_job(
        _job(kind="prep", tagged=1200, flags=3, verified=True))
    assert "Format" in prep and "Paragraphs styled: 1,200" in prep

    _, review, _ = notify.completion_for_job(_job(kind="review", applied=42))
    assert "Proofread" in review and "Changes applied: 42" in review


def test_completion_for_job_reports_cost_and_elapsed():
    _, text, _ = notify.completion_for_job(_job())
    assert "$1.23" in text and "Elapsed: 2m 30s" in text


# --- the gate and the send ----------------------------------------------------

def test_send_when_switched_on(tmp_path):
    _configured(tmp_path)
    opener = fake_drive()
    sent = notify.send_job_completion(tmp_path, _job(),
                                      get_key=lambda k: "refresh-1", opener=opener)
    assert sent is True and len(opener.emails) == 1
    assert "quinton@atmospherepress.com" in _raw(opener)


def test_silent_when_the_toggle_is_off(tmp_path):
    WatchSettings(client_id="c", client_secret="s",
                  notify_email="q@a.com", notify_on_complete=False).save(tmp_path)
    opener = fake_drive()
    assert notify.send_job_completion(tmp_path, _job(),
                                      get_key=lambda k: "r", opener=opener) is False
    assert opener.emails == []


def test_silent_without_a_google_sign_in(tmp_path):
    _configured(tmp_path)
    opener = fake_drive()
    assert notify.send_job_completion(tmp_path, _job(),
                                      get_key=lambda k: "", opener=opener) is False
    assert opener.emails == []


def test_a_send_that_fails_never_raises(tmp_path):
    _configured(tmp_path)
    opener = fake_drive(fail={"gmail_send": http_error(403)})   # scope missing
    assert notify.send_job_completion(tmp_path, _job(),
                                      get_key=lambda k: "r", opener=opener) is False


# --- the runner seam ----------------------------------------------------------

def _runner(tmp_path, **over):
    paths = Paths(tmp_path / "app").ensure()
    store = JobStore(paths)
    r = JobRunner(store, Settings(), config_path=CONFIG,
                  notify_home=over.pop("notify_home", tmp_path))
    return store, r


def test_a_finished_app_job_is_emailed(tmp_path, monkeypatch):
    store, r = _runner(tmp_path)
    store.save(_job())
    seen = []
    monkeypatch.setattr("app.watch.notify.send_job_completion",
                        lambda home, job, **k: seen.append(job.id))
    r._notify_done("j1")
    assert seen == ["j1"]


def test_a_watched_format_job_is_left_to_the_tick(tmp_path, monkeypatch):
    """The tick emails watched format jobs with routing this record lacks, so
    the runner must not email them a second time."""
    store, r = _runner(tmp_path)
    store.save(_job(kind="prep", source="watch"))
    seen = []
    monkeypatch.setattr("app.watch.notify.send_job_completion",
                        lambda home, job, **k: seen.append(job.id))
    r._notify_done("j1")
    assert seen == []


def test_watched_promo_is_emailed_by_the_runner(tmp_path, monkeypatch):
    store, r = _runner(tmp_path)
    store.save(_job(kind="promo", source="watch"))
    seen = []
    monkeypatch.setattr("app.watch.notify.send_job_completion",
                        lambda home, job, **k: seen.append(job.id))
    r._notify_done("j1")
    assert seen == ["j1"]


def test_no_notify_home_means_no_email(tmp_path, monkeypatch):
    store, r = _runner(tmp_path, notify_home=None)
    store.save(_job())
    seen = []
    monkeypatch.setattr("app.watch.notify.send_job_completion",
                        lambda home, job, **k: seen.append(job.id))
    r._notify_done("j1")
    assert seen == []
