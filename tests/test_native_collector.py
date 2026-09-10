import pytest

from app.watch.settings import WatchSettings
from docproof.interior import poller


def _settings(**changes):
    values = dict(
        corrections_enabled=True,
        corrections_engine="native",
        corrections_native_form_poll=True,
        corrections_native_start_after="2026-09-01T00:00:00Z",
        corrections_native_form_book_property="book_title",
        corrections_native_project_book_property="book_title",
    )
    values.update(changes)
    return WatchSettings(**values)


def test_collect_once_is_paused_before_reading_hubspot_when_native_is_disabled(tmp_path):
    WatchSettings().save(tmp_path)
    called = []

    result = poller.collect_once(tmp_path, get_key=lambda key: called.append(key))

    assert result == {"state": "paused"}
    assert called == []


def test_collect_once_requires_the_form_poll_gate(tmp_path):
    _settings(corrections_native_form_poll=False).save(tmp_path)
    with pytest.raises(ValueError, match="correction form"):
        poller.collect_once(tmp_path, get_key=lambda key: "token")


def test_collect_once_reads_only_hubspot_and_calls_native_intake(monkeypatch, tmp_path):
    _settings().save(tmp_path)
    key_calls = []
    intake_calls = []

    def get_key(name):
        key_calls.append(name)
        return "hubspot-token"

    def collect(home, ws, token, *, opener):
        intake_calls.append((home, ws, token, opener))
        return {"events": ["captured"]}

    monkeypatch.setattr("app.watch.native_intake.collect", collect)
    opener = object()
    result = poller.collect_once(tmp_path, get_key=get_key, opener=opener)

    assert result == {"events": ["captured"]}
    assert key_calls == ["hubspot"]
    assert intake_calls == [(tmp_path.resolve(), intake_calls[0][1], "hubspot-token", opener)]


def test_collect_once_supplies_hubspot_default_opener(monkeypatch, tmp_path):
    _settings().save(tmp_path)
    default_opener = lambda request, timeout=60: (request, timeout)
    seen = []

    monkeypatch.setattr("app.watch.hubspot._open_url", default_opener)
    monkeypatch.setattr(
        "app.watch.native_intake.collect",
        lambda home, ws, token, *, opener: seen.append(opener) or {"state": "ok"},
    )

    assert poller.collect_once(tmp_path, get_key=lambda key: "hubspot-token") == {"state": "ok"}
    assert seen == [default_opener]


def test_collect_once_records_only_a_sanitized_error(monkeypatch, tmp_path):
    _settings().save(tmp_path)
    recorded = []

    monkeypatch.setattr("app.watch.native_intake.collect",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("bearer-secret-must-not-persist")))
    monkeypatch.setattr("app.watch.native_queue.record_error",
                        lambda home, message: recorded.append((home, message)))

    with pytest.raises(RuntimeError, match="bearer-secret"):
        poller.collect_once(tmp_path, get_key=lambda key: "token")

    assert recorded == [(tmp_path.resolve(), "RuntimeError: native collection failed")]


def test_collector_loop_clamps_interval_and_stops_cleanly(monkeypatch, tmp_path):
    calls = []
    waits = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)
            return True

    monkeypatch.setattr(poller, "collect_once", lambda *args, **kwargs: calls.append(args[0]))
    poller._collector_loop(tmp_path, 1, Stop())

    assert calls == [tmp_path]
    assert waits == [60]


def test_continuous_cli_stops_daemon_collector_on_exit(monkeypatch, tmp_path):
    calls = []

    def collector(home, interval, stop):
        calls.append((home, interval, stop))
        stop.wait(30)

    monkeypatch.setattr(poller, "_collector_loop", collector)
    monkeypatch.setattr(poller, "poll_once", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    from docproof.interior.__main__ import main
    with pytest.raises(KeyboardInterrupt):
        main(["poll", "--watch-home", str(tmp_path), "--continuous", "--interval", "1"])

    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] == 60
    assert calls[0][2].is_set()
