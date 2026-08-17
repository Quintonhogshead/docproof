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

    _, corr, _ = notify.completion_for_job(_job(
        kind="corrections", filename="Shams - a thousand touches.idml",
        applied=548, flags=7, discrepancies=0, verified=True))
    assert "Corrections" in corr and "Applied: 548" in corr
    assert "Refused for a human: 7" in corr and "Clean: yes" in corr
    # No model dials — the engine is deterministic — so the settings group says so.
    assert "deterministic — no model, no cost" in corr


def test_the_review_email_reports_both_channels():
    """A proofread hands back corrections AND questions. The log said only the
    first, so the queries waiting for the author were invisible to the person
    reading the morning email."""
    _, body, html = notify.completion_for_job(
        _job(kind="review", applied=42, queried=9, judge_held=4))
    assert "Changes applied: 42" in body
    assert "Queries for the author: 9" in body
    # A judge withdraws a correction by turning it into a query, so the held
    # count is a share of the 9 above — never a fourth number to add on.
    assert "Held back by judge: 4 of those" in body
    # Both renderings are built from the one row list, and both are sent.
    for label, value in (("Queries for the author", "9"),
                         ("Held back by judge", "4 of those")):
        assert f">{label}</td><td>{value}</td>" in html


def test_a_degraded_review_says_so_in_the_email():
    """A review that finished but had a pass fail or skip is degraded. The log
    is the one place a scheduled run is ever seen, so it must say so here — not
    only in a summary.md nobody opens on an otherwise-clean green run."""
    _, body, html = notify.completion_for_job(_job(
        kind="review", applied=42,
        warnings=["Sapling: the grammar/style pass failed (network) and did "
                  "not run",
                  "continuity: over the token limit, so the read was skipped"]))
    assert "Run health" in body
    assert "DEGRADED — 2 pass(es) fell short" in body
    assert "Sapling: the grammar/style pass failed" in body
    assert "Run health" in html


def test_a_clean_review_says_nothing_about_run_health():
    _, body, html = notify.completion_for_job(_job(kind="review", applied=42))
    for text in (body, html):
        assert "Run health" not in text
        assert "DEGRADED" not in text


def test_a_review_with_nothing_to_ask_says_nothing_about_queries():
    """Zero rows are left out rather than printed: a clean run should read like
    a clean run, not like a column of noughts."""
    _, body, html = notify.completion_for_job(
        _job(kind="review", applied=42, queried=0, judge_held=0))
    for text in (body, html):
        assert "Queries for the author" not in text
        assert "Held back by judge" not in text


def test_a_record_without_the_count_fields_at_all_still_emails():
    """The completion log is built from whatever job-like record it is handed —
    app jobs, watched books — so a missing field has to read as "nothing to
    report" rather than take the email down with it."""
    job = _job(kind="review", applied=42)
    del job.queried, job.judge_held
    _, body, _ = notify.completion_for_job(job)
    assert "Changes applied: 42" in body
    assert "Queries for the author" not in body


def test_completion_for_job_reports_cost_and_elapsed():
    _, text, _ = notify.completion_for_job(_job())
    assert "$1.23" in text and "Elapsed: 2m 30s" in text


def test_completion_email_breaks_out_sapling_cost():
    """The total is model + Sapling; the email names Sapling's share rather than
    burying a per-character third-party charge in one number."""
    _, text, _ = notify.completion_for_job(
        _job(kind="review", cost=1.73, sapling_cost=0.50))
    assert "$1.73" in text
    assert "includes $0.50 Sapling grammar check" in text


def test_completion_email_lists_the_settings_used():
    _, text, _ = notify.completion_for_job(_job(
        kind="review", min_confidence="low", rounds=2, glossary_model="off",
        features={"sapling": True, "rewrite": True, "consistency": False}))
    assert "Confidence gate: low" in text
    assert "Review rounds: 2" in text
    assert "Glossary read: off" in text
    assert "Sapling grammar check" in text and "rewrite & compare" in text


def test_completion_email_flags_a_continuity_only_run():
    """The run that confused everyone reads unmistakably in the email now."""
    _, text, _ = notify.completion_for_job(_job(
        kind="review", continuity_only=True, features={"sapling": True}))
    assert "Continuity read only" in text
    assert "skipped" in text


def test_completion_links_to_the_drive_archive_when_there_is_one():
    _, text, html = notify.completion_for_job(
        _job(drive_folder_id="fold-9"))
    link = "https://drive.google.com/drive/folders/fold-9"
    assert link in text and link in html
    # And nothing about Drive when the job was never archived.
    _, plain, _ = notify.completion_for_job(_job())
    assert "drive.google.com" not in plain


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
