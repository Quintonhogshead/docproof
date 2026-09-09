"""The Mac poller stays paused and native-only until its gates are complete."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.watch import drive, native_corrections
from app.watch.settings import WatchSettings
from docproof.interior.poller import poll_once


def _settings(**changes) -> WatchSettings:
    values = dict(
        corrections_enabled=True,
        corrections_engine="native",
        corrections_native_form_poll=True,
        corrections_native_start_after="2026-09-01T00:00:00Z",
        corrections_native_form_book_property="book_title",
        corrections_native_project_book_property="book_title",
        client_id="google-client",
        client_secret="google-secret",
        folder_id="folder-12345678",
    )
    values.update(changes)
    return WatchSettings(**values)


def _save(home: Path, settings: WatchSettings) -> None:
    settings.save(home)


def test_paused_worker_never_reads_credentials_or_network(tmp_path):
    calls: list[str] = []
    _save(tmp_path, WatchSettings())

    def forbidden(*args, **kwargs):
        calls.append("called")
        raise AssertionError("paused native worker reached an external boundary")

    report = poll_once(tmp_path, get_key=forbidden, opener=forbidden)

    assert report.failed == []
    receipt = json.loads((tmp_path / "native-worker.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "paused"
    assert calls == []


def test_non_native_engine_is_paused_before_native_stage(tmp_path, monkeypatch):
    _save(tmp_path, _settings(corrections_engine="idml"))
    called = []
    monkeypatch.setattr(native_corrections, "run_stage",
                        lambda *args, **kwargs: called.append((args, kwargs)))

    poll_once(tmp_path, get_key=lambda key: "secret", opener=lambda request: None)

    assert called == []
    receipt = json.loads((tmp_path / "native-worker.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "paused"


def test_missing_configuration_fails_before_any_network_or_keychain_read(tmp_path):
    # Native form polling is enabled, but the Drive connection is deliberately
    # absent. The worker must stop at its own configuration gate.
    _save(tmp_path, _settings(client_id="", client_secret="", folder_id=""))
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("called")
        raise AssertionError("configuration failure reached an external boundary")

    with pytest.raises(ValueError, match="Google Drive connection"):
        poll_once(tmp_path, get_key=forbidden, opener=forbidden)

    assert calls == []
    receipt = json.loads((tmp_path / "native-worker.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "error"
    assert "Google Drive connection" in receipt["error"]


def test_fully_configured_poll_runs_only_native_stage_and_keeps_credentials_separate(
        tmp_path, monkeypatch):
    _save(tmp_path, _settings())
    key_calls: list[str] = []
    refresh_calls: list[tuple[str, str, str]] = []
    stage_calls: list[dict] = []
    google_secret = "google-refresh-secret"
    hubspot_secret = "hubspot-private-app-secret"

    def get_key(name):
        key_calls.append(name)
        return {"google": google_secret, "hubspot": hubspot_secret}[name]

    def refresh(client_id, client_secret, refresh_token, *, opener):
        refresh_calls.append((client_id, client_secret, refresh_token))
        assert opener is not None
        return "google-access-token"

    def run_stage(*args, **kwargs):
        stage_calls.append({"token": args[0], "home": args[1],
                            "hs_token": kwargs["hs_token"], "mock": kwargs["mock"]})

    monkeypatch.setattr(drive, "refresh_access_token", refresh)
    monkeypatch.setattr(native_corrections, "run_stage", run_stage)

    poll_once(tmp_path, get_key=get_key, opener=lambda request: None)

    assert key_calls == ["google", "hubspot"]
    assert refresh_calls == [("google-client", "google-secret", google_secret)]
    assert stage_calls == [{"token": "google-access-token", "home": tmp_path.resolve(),
                            "hs_token": hubspot_secret, "mock": False}]
    receipt_text = (tmp_path / "native-worker.json").read_text(encoding="utf-8")
    assert google_secret not in receipt_text
    assert hubspot_secret not in receipt_text
    assert json.loads(receipt_text)["state"] == "idle"


def test_local_only_overrides_saved_upload_and_crm_settings(tmp_path, monkeypatch):
    ws = _settings()
    ws.corrections_native_auto_upload = True
    ws.hubspot_write_back = True
    _save(tmp_path, ws)
    monkeypatch.setattr(drive, "refresh_access_token", lambda *a, **k: "access")
    seen = []
    def stage(token, home, config, *args, **kwargs):
        seen.append((config.corrections_native_auto_upload, config.hubspot_write_back))
    monkeypatch.setattr(native_corrections, "run_stage", stage)
    poll_once(tmp_path, get_key=lambda key: "credential", local_only=True)
    assert seen == [(False, False)]
    receipt = json.loads((tmp_path / "native-worker.json").read_text())
    assert receipt["local_only"] is True
    assert receipt["drive_uploads_enabled"] is False


def test_dedicated_review_home_manual_check_cannot_run_other_stages(tmp_path, monkeypatch):
    from app.watch.tick import tick, TickReport
    from docproof.interior import poller
    ws = _settings()
    ws.corrections_native_worker_only = True
    seen = []
    def native_poll(home, **kwargs):
        seen.append(kwargs)
        return TickReport()
    monkeypatch.setattr(poller, "poll_once", native_poll)
    tick(tmp_path, ws)
    assert seen == [{"get_key": None, "opener": None, "local_only": True}]
