"""`docproof-warden`'s CLI: `init` writing a config and reporting missing
secrets without ever printing a value, `say`'s quiet-hours/rate-cap
refusals, `secret set` reading from stdin (never argv), `verb --dry-run`,
the `request`/`code-request`/`code-approved` round trips, and that
`install` writes valid, loadable plists to a directory the test controls
rather than the real `~/Library/LaunchAgents`.

Every test that touches `app.warden.secrets` fakes the Keychain — this repo
never lets a test call `keyring` for real."""
from __future__ import annotations

import io
import json
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.warden import cli, config as config_module, install as install_module, secrets as secrets_module
from app.warden.journal import Journal

OWNER = "+15551234567"


class FakeKeyring:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get_password(self, service, name):
        return self.store.get(name)

    def set_password(self, service, name, value):
        self.store[name] = value

    def delete_password(self, service, name):
        self.store.pop(name, None)


@pytest.fixture(autouse=True)
def clean_keyring(monkeypatch):
    """Every secret env var cleared, and `keyring` replaced with an
    in-memory fake — no test in this file ever touches the real Keychain."""
    fake = FakeKeyring()
    monkeypatch.setattr("keyring.get_password", fake.get_password)
    monkeypatch.setattr("keyring.set_password", fake.set_password)
    monkeypatch.setattr("keyring.delete_password", fake.delete_password)
    for env_name in secrets_module.ENV_VARS.values():
        monkeypatch.delenv(env_name, raising=False)
    return fake


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# --- init --------------------------------------------------------------

def test_init_writes_config_and_reports_every_secret_missing(tmp_path, capsys):
    code, out, err = run_cli(["--home", str(tmp_path), "init"], capsys)
    assert code == 0
    assert (tmp_path / "warden.yaml").is_file()
    assert "Still missing" in out
    for name in secrets_module.ENV_VARS:
        assert name in out


def test_init_leaves_an_existing_config_alone(tmp_path, capsys):
    cfg = config_module.WardenConfig(owner_handle="+19995550000")
    cfg.save(tmp_path)
    before = (tmp_path / "warden.yaml").read_text("utf-8")

    code, out, err = run_cli(["--home", str(tmp_path), "init"], capsys)

    assert code == 0
    assert "already exists" in out
    assert (tmp_path / "warden.yaml").read_text("utf-8") == before


def test_init_reports_nothing_missing_once_every_secret_is_set(tmp_path, capsys, clean_keyring):
    for name in secrets_module.ENV_VARS:
        clean_keyring.set_password(secrets_module.SERVICE, name, "x" * 30)
    code, out, err = run_cli(["--home", str(tmp_path), "init"], capsys)
    assert code == 0
    assert "Every known secret is configured." in out


# --- secret --------------------------------------------------------------

def test_secret_set_reads_the_value_from_stdin_not_argv(tmp_path, capsys, monkeypatch, clean_keyring):
    monkeypatch.setattr("sys.stdin", io.StringIO("super-secret-value\n"))
    code, out, err = run_cli(["--home", str(tmp_path), "secret", "set", "hubspot"], capsys)
    assert code == 0
    assert clean_keyring.store["hubspot"] == "super-secret-value"
    assert "super-secret-value" not in out


def test_secret_set_with_nothing_on_stdin_refuses(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, out, err = run_cli(["--home", str(tmp_path), "secret", "set", "hubspot"], capsys)
    assert code == 1
    assert "No value on stdin" in err


def test_secret_list_shows_missing_vs_configured(tmp_path, capsys, clean_keyring):
    clean_keyring.set_password(secrets_module.SERVICE, "hubspot", "x" * 30)
    code, out, err = run_cli(["--home", str(tmp_path), "secret", "list"], capsys)
    assert code == 0
    assert "hubspot: configured" in out
    assert "warden_token: missing" in out


def test_secret_delete_removes_it(tmp_path, capsys, clean_keyring):
    clean_keyring.set_password(secrets_module.SERVICE, "hubspot", "x" * 30)
    code, out, err = run_cli(["--home", str(tmp_path), "secret", "delete", "hubspot"], capsys)
    assert code == 0
    assert "hubspot" not in clean_keyring.store


# --- say: refusals -----------------------------------------------------

def _quiet_now() -> datetime:
    # 2026-09-23T04:00 UTC == 2026-09-23T00:00 America/New_York, inside the
    # default 23:00-07:00 quiet window.
    return datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)


def _daytime_now() -> datetime:
    return datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)  # 14:00 ET


def _prep_owner_config(tmp_path, **overrides):
    cfg = config_module.WardenConfig(owner_handle=OWNER, imessage_enabled=True, **overrides)
    cfg.save(tmp_path)
    return cfg


def test_say_refuses_with_no_owner_handle_configured(tmp_path, capsys, monkeypatch):
    config_module.WardenConfig(owner_handle="").save(tmp_path)
    monkeypatch.setattr(cli, "_now", _daytime_now)
    code, out, err = run_cli(["--home", str(tmp_path), "say", "hi"], capsys)
    assert code == 1
    assert "owner_handle" in out


def test_say_refuses_during_quiet_hours_without_high(tmp_path, capsys, monkeypatch):
    _prep_owner_config(tmp_path)
    monkeypatch.setattr(cli, "_now", _quiet_now)
    sent = []
    monkeypatch.setattr(cli.imessage, "send", lambda *a, **k: sent.append(a) or True)
    code, out, err = run_cli(["--home", str(tmp_path), "say", "hi"], capsys)
    assert code == 1
    assert "quiet hours" in out
    assert sent == []


def test_say_high_bypasses_quiet_hours(tmp_path, capsys, monkeypatch):
    _prep_owner_config(tmp_path)
    monkeypatch.setattr(cli, "_now", _quiet_now)
    sent = []
    monkeypatch.setattr(cli.imessage, "send", lambda handle, text, **k: sent.append((handle, text)) or True)
    code, out, err = run_cli(["--home", str(tmp_path), "say", "hi there", "--high"], capsys)
    assert code == 0
    assert sent == [(OWNER, "hi there")]


def test_say_refuses_once_the_hourly_cap_is_spent(tmp_path, capsys, monkeypatch):
    _prep_owner_config(tmp_path, max_texts_per_hour=1)
    monkeypatch.setattr(cli, "_now", _daytime_now)
    with Journal(tmp_path / "journal.sqlite") as j:
        j.record_message("imessage", "out", OWNER, "an earlier text")

    sent = []
    monkeypatch.setattr(cli.imessage, "send", lambda *a, **k: sent.append(a) or True)
    code, out, err = run_cli(["--home", str(tmp_path), "say", "hi", "--high"], capsys)
    assert code == 1
    assert "Already sent" in out
    assert sent == []


def test_say_succeeds_and_journals_the_outbound_text(tmp_path, capsys, monkeypatch):
    _prep_owner_config(tmp_path)
    monkeypatch.setattr(cli, "_now", _daytime_now)
    sent = []
    monkeypatch.setattr(cli.imessage, "send", lambda handle, text, **k: sent.append((handle, text)) or True)
    code, out, err = run_cli(["--home", str(tmp_path), "say", "all clear"], capsys)
    assert code == 0
    assert sent == [(OWNER, "all clear")]
    with Journal(tmp_path / "journal.sqlite") as j:
        msgs = j.messages_since("imessage", "out", datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert any(m["text"] == "all clear" for m in msgs)


# --- verb ------------------------------------------------------------------

def test_verb_dry_run_prints_the_result_without_acting(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    code, out, err = run_cli(
        ["--home", str(tmp_path), "verb", "native-kickstart", "--dry-run",
         "label=com.docproof.interior-review-worker"], capsys)
    assert code == 0
    assert "Would kickstart" in out


def test_verb_unknown_name_exits_nonzero(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    code, out, err = run_cli(["--home", str(tmp_path), "verb", "not-a-real-verb"], capsys)
    assert code == 1
    assert "no such verb" in out


# --- request / code-request / code-approved --------------------------------

def test_request_list_and_answer(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    with Journal(tmp_path / "journal.sqlite") as j:
        req = j.open_request("action", "restart the app machine", {})
        number = req.number

    code, out, err = run_cli(["--home", str(tmp_path), "request", "list"], capsys)
    assert code == 0
    assert f"#{number}" in out

    code, out, err = run_cli(["--home", str(tmp_path), "request", "yes", str(number)], capsys)
    assert code == 0
    assert f"#{number} marked yes." in out
    with Journal(tmp_path / "journal.sqlite") as j:
        assert j.get_request(number).status == "yes"


def test_request_yes_on_an_unknown_number_fails_cleanly(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    code, out, err = run_cli(["--home", str(tmp_path), "request", "yes", "999"], capsys)
    assert code == 1
    assert "isn't open" in err


def test_code_request_then_code_approved_round_trip(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    code, out, err = run_cli(
        ["--home", str(tmp_path), "code-request", "--rule", "watch-signin-dead",
         "--cause", "redirect_uri_mismatch", "--files", "app/routes/watch.py",
         "--fix", "preflight the consent URL"], capsys)
    assert code == 0
    assert "Opened code request #1." in out

    code, out, err = run_cli(["--home", str(tmp_path), "code-approved"], capsys)
    assert code == 1
    assert "not approved" in err

    with Journal(tmp_path / "journal.sqlite") as j:
        j.answer_request(1, "yes", "Quinton")

    code, out, err = run_cli(["--home", str(tmp_path), "code-approved"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload == {"rule": "watch-signin-dead", "cause": "redirect_uri_mismatch",
                       "files": "app/routes/watch.py", "fix": "preflight the consent URL"}


def test_a_second_code_request_refuses_while_one_is_open(tmp_path, capsys):
    config_module.WardenConfig().save(tmp_path)
    run_cli(["--home", str(tmp_path), "code-request", "--rule", "r1", "--cause", "c1",
            "--files", "f1", "--fix", "fix1"], capsys)
    code, out, err = run_cli(
        ["--home", str(tmp_path), "code-request", "--rule", "r2", "--cause", "c2",
         "--files", "f2", "--fix", "fix2"], capsys)
    assert code == 1
    assert "Refused" in err


# --- install / uninstall: wiring, and real plist content -------------------

def test_install_passes_its_arguments_through(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.install_module, "install",
                        lambda **kw: calls.append(kw) or (Path("a"), Path("b")))
    code, out, err = run_cli(
        ["--home", str(tmp_path), "install", "--interval", "15",
         "--listen-interval", "3", "--agents-dir", str(tmp_path / "LaunchAgents")], capsys)
    assert code == 0
    assert calls[0]["interval_min"] == 15
    assert calls[0]["listen_interval_min"] == 3
    assert calls[0]["agents_dir"] == tmp_path / "LaunchAgents"
    assert calls[0]["home"] == tmp_path


def test_uninstall_passes_its_agents_dir_through(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.install_module, "uninstall",
                        lambda **kw: calls.append(kw) or [])
    run_cli(["--home", str(tmp_path), "uninstall", "--agents-dir",
            str(tmp_path / "LaunchAgents")], capsys)
    assert calls[0]["agents_dir"] == tmp_path / "LaunchAgents"


@dataclass
class FakeRun:
    calls: list = field(default_factory=list)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()


def test_install_writes_valid_plists_to_a_directory_the_test_controls(tmp_path):
    """`install_module.install` — the function `cmd_install` wires to — must
    never touch the real `~/Library/LaunchAgents`; `agents_dir` redirects
    it, and a real `run` fake keeps `launchctl` out of the test entirely."""
    agents_dir = tmp_path / "LaunchAgents"
    home = tmp_path / "warden-home"
    run = FakeRun()

    tick_plist, listen_plist = install_module.install(
        home=home, interval_min=15, listen_interval_min=3, run=run,
        agents_dir=agents_dir)

    assert tick_plist.parent == agents_dir
    assert listen_plist.parent == agents_dir

    tick_data = plistlib.loads(tick_plist.read_bytes())
    assert tick_data["Label"] == install_module.TICK_LABEL
    assert tick_data["StartInterval"] == 15 * 60
    assert tick_data["ProgramArguments"][-2:] == ["app.warden.cli", "tick"]

    listen_data = plistlib.loads(listen_plist.read_bytes())
    assert listen_data["Label"] == install_module.LISTEN_LABEL
    assert listen_data["StartInterval"] == 3 * 60
    assert listen_data["ProgramArguments"][-2:] == ["app.warden.cli", "listen"]

    assert (home / "logs").is_dir()
    # launchctl was asked to (re)load both, but nothing outside agents_dir/home.
    assert any("bootstrap" in c for c in run.calls)


def test_uninstall_removes_only_what_it_wrote(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    home = tmp_path / "warden-home"
    run = FakeRun()
    install_module.install(home=home, run=run, agents_dir=agents_dir)

    removed = install_module.uninstall(run=run, agents_dir=agents_dir)

    assert len(removed) == 2
    assert not any(p.exists() for p in removed)


def test_uninstall_reports_nothing_when_never_installed(tmp_path):
    removed = install_module.uninstall(run=FakeRun(), agents_dir=tmp_path / "LaunchAgents")
    assert removed == []
