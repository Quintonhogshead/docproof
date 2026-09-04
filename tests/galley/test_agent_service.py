"""Keeping the proofing agent running: the launchd LaunchAgent on macOS, the
systemd user unit on Linux, and the CLI verb that writes either.

The agent itself is platform-neutral; only this is not, so this is where the
platform is named. Nothing here runs `launchctl` or `systemctl` — the runner is
injected, and what it was asked to do is the assertion.
"""
from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from docproof.__main__ import main
from galley import agent as ga

APP = "https://atmosphere-docproof.fly.dev"
TOKEN = "s3cret-token-long-enough-to-be-real"


class FakeRun:
    """`subprocess.run`, recorded rather than run."""

    def __init__(self, *, fail: str = ""):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        rc = 1 if self.fail and self.fail in " ".join(argv) else 0
        return type("R", (), {"returncode": rc, "stderr": "nope\n",
                              "stdout": ""})()

    @property
    def flat(self) -> str:
        return " | ".join(" ".join(c) for c in self.calls)


@pytest.fixture()
def env_file(tmp_path) -> Path:
    path = tmp_path / "agent.env"
    path.write_text(f"{ga.OAUTH_KEY}=tok\n{ga.APP_URL_KEY}={APP}\n"
                    f"{ga.AGENT_TOKEN_KEY}={TOKEN}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --- the command line the service runs ----------------------------------------

def test_the_program_spells_everything_out(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/usr/local/bin/docproof")
    argv = ga.program(workspace_root=tmp_path / "ws",
                      env_file=tmp_path / "agent.env", poll_interval_s=300)
    assert argv[:3] == ["/usr/local/bin/docproof", "galley", "agent"]
    assert "--workspace-root" in argv and str(tmp_path / "ws") in argv
    assert "--env-file" in argv and str(tmp_path / "agent.env") in argv
    assert argv[argv.index("--poll-interval") + 1] == "300"


# --- macOS --------------------------------------------------------------------

def test_the_launch_agent_keeps_the_poller_alive(tmp_path):
    root = tmp_path / "ws"
    payload = plistlib.loads(ga.plist_content(
        command=["/bin/docproof", "galley", "agent"],
        log_path=root / ga.LOG_NAME, workspace_root=root))
    assert payload["Label"] == ga.LABEL == "com.atmosphere.galley-agent"
    # A poller, not a scheduled pass: it runs at load and is restarted if it
    # dies, with a floor under the restart so a crash loop backs off.
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 60
    assert payload["StandardOutPath"] == str(root / "agent.log")
    assert payload["StandardErrorPath"] == str(root / "agent.log")
    assert payload["EnvironmentVariables"]["PATH"] == ga.PATH
    assert payload["ProcessType"] == "Background"


def test_install_on_macos_writes_and_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    run = FakeRun()
    target = tmp_path / "com.atmosphere.galley-agent.plist"
    where = ga.install(workspace_root=tmp_path / "ws",
                       env_file=tmp_path / "agent.env", run=run, path=target,
                       platform="darwin",
                       wrapper_source=tmp_path / "nothing.sh")
    assert where == target and target.is_file()
    assert "launchctl bootout" in run.flat
    assert "launchctl bootstrap" in run.flat
    # The workspace root is made, because the log lives in it.
    assert (tmp_path / "ws").is_dir()


def test_a_refused_bootstrap_says_what_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    run = FakeRun(fail="bootstrap")
    with pytest.raises(ga.AgentError, match="macOS would not start"):
        ga.install(workspace_root=tmp_path / "ws",
                   env_file=tmp_path / "agent.env", run=run,
                   path=tmp_path / "a.plist", platform="darwin",
                   wrapper_source=tmp_path / "nothing.sh")


def test_uninstall_on_macos_removes_and_reports(tmp_path):
    run = FakeRun()
    target = tmp_path / "a.plist"
    assert ga.uninstall(run=run, path=target, platform="darwin") is False
    target.write_text("x")
    assert ga.uninstall(run=run, path=target, platform="darwin") is True
    assert not target.exists()
    assert "launchctl bootout" in run.flat


# --- Linux --------------------------------------------------------------------

def test_the_systemd_unit_restarts_and_logs_to_the_same_file(tmp_path):
    root = tmp_path / "ws"
    unit = ga.unit_content(command=["/bin/docproof", "galley", "agent"],
                           log_path=root / ga.LOG_NAME, workspace_root=root)
    assert "ExecStart=/bin/docproof galley agent" in unit
    assert "Restart=always" in unit               # launchd's KeepAlive
    assert "RestartSec=60" in unit                # …and its ThrottleInterval
    assert f"StandardOutput=append:{root / 'agent.log'}" in unit
    assert f"StandardError=append:{root / 'agent.log'}" in unit
    assert "WantedBy=default.target" in unit
    assert f"Environment=PATH={ga.PATH}" in unit
    assert "After=network-online.target" in unit


def test_a_path_with_a_space_is_quoted_in_the_unit(tmp_path):
    unit = ga.unit_content(
        command=["/bin/docproof", "--workspace-root", "/home/q/my books"],
        log_path=tmp_path / "agent.log", workspace_root=tmp_path)
    assert 'ExecStart=/bin/docproof --workspace-root "/home/q/my books"' in unit


def test_install_on_linux_enables_the_unit_and_linger(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    run = FakeRun()
    target = tmp_path / "galley-agent.service"
    where = ga.install(workspace_root=tmp_path / "ws",
                       env_file=tmp_path / "agent.env", run=run, path=target,
                       platform="linux", wrapper_source=tmp_path / "no.sh")
    assert where == target and target.is_file()
    assert "systemctl --user daemon-reload" in run.flat
    # Lingering is what keeps it running after the SSH session ends.
    assert "loginctl enable-linger" in run.flat
    assert "systemctl --user enable --now galley-agent.service" in run.flat


def test_linux_install_survives_a_refused_linger(tmp_path, monkeypatch):
    """`enable-linger` needs root. Without it the agent still runs for this
    session, so a refusal is reported, not raised."""
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    run = FakeRun(fail="enable-linger")
    where = ga.install(workspace_root=tmp_path / "ws",
                       env_file=tmp_path / "agent.env", run=run,
                       path=tmp_path / "u.service", platform="linux",
                       wrapper_source=tmp_path / "no.sh")
    assert where.is_file()


def test_a_refused_systemctl_enable_says_what_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    run = FakeRun(fail="enable --now")
    with pytest.raises(ga.AgentError, match="systemd would not start"):
        ga.install(workspace_root=tmp_path / "ws",
                   env_file=tmp_path / "agent.env", run=run,
                   path=tmp_path / "u.service", platform="linux",
                   wrapper_source=tmp_path / "no.sh")


def test_uninstall_on_linux_disables_and_removes(tmp_path):
    run = FakeRun()
    target = tmp_path / "u.service"
    assert ga.uninstall(run=run, path=target, platform="linux") is False
    target.write_text("x")
    assert ga.uninstall(run=run, path=target, platform="linux") is True
    assert not target.exists()
    assert "systemctl --user disable --now galley-agent.service" in run.flat


def test_the_platform_picks_the_service_file(monkeypatch):
    assert ga.is_linux("linux") and not ga.is_linux("darwin")
    assert ga.service_path("linux").name == "galley-agent.service"
    assert ga.service_path("darwin").name == f"{ga.LABEL}.plist"
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert ga.units_dir() == Path("/tmp/xdg/systemd/user")


# --- the wrapper refresh ------------------------------------------------------

def test_install_refreshes_the_galley_run_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "executable", lambda: "/bin/docproof")
    source = tmp_path / "repo-galley-run.sh"
    source.write_text("#!/usr/bin/env bash\n# the new one\n", encoding="utf-8")
    dest = tmp_path / "bin" / "galley-run.sh"
    dest.parent.mkdir()
    dest.write_text("#!/usr/bin/env bash\n# the old one\n", encoding="utf-8")

    ga.install(workspace_root=tmp_path / "ws", env_file=tmp_path / "agent.env",
               run=FakeRun(), path=tmp_path / "a.plist", platform="darwin",
               wrapper_source=source, wrapper_dest=dest)
    assert "the new one" in dest.read_text("utf-8")
    # The old one is kept — it is somebody's working script until it is not.
    assert "the old one" in (tmp_path / "bin" / "galley-run.sh.bak"
                             ).read_text("utf-8")


def test_the_wrapper_refresh_is_a_no_op_when_it_already_matches(tmp_path):
    source = tmp_path / "a.sh"
    source.write_text("same\n", encoding="utf-8")
    dest = tmp_path / "b.sh"
    dest.write_text("same\n", encoding="utf-8")
    assert ga.refresh_wrapper(source=source, dest=dest) == dest
    assert not (tmp_path / "b.sh.bak").exists()


def test_the_repo_ships_the_wrapper_the_install_copies():
    source = Path(ga.__file__).resolve().parent / "practitioner" / "galley-run.sh"
    assert source.is_file()
    assert "docproof galley drive" in source.read_text("utf-8")


# --- the CLI verb -------------------------------------------------------------

def test_agent_status_prints_the_ledger(env_file, tmp_path, capsys):
    root = tmp_path / "ws"
    ledger = ga.Ledger.load(root / ga.LEDGER_NAME)
    ledger.record("drive-1", ga.CLAIMED, name="Test - Book 1.docx")
    rc = main(["galley", "agent", "--env-file", str(env_file),
               "--workspace-root", str(root), "--status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Test - Book 1.docx" in out
    assert "claimed" in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["pending"] == ["drive-1"]


def test_agent_once_polls_and_exits(env_file, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ga, "fetch_awaiting", lambda _env, opener=None: [])
    rc = main(["galley", "agent", "--env-file", str(env_file),
               "--workspace-root", str(tmp_path / "ws"), "--once", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["looked_at"] == 0


def test_the_cli_refuses_an_unreadable_env_file(tmp_path, capsys):
    bad = tmp_path / "agent.env"
    bad.write_text(f"{ga.OAUTH_KEY}=tok\n", encoding="utf-8")
    bad.chmod(0o644)
    rc = main(["galley", "agent", "--env-file", str(bad),
               "--workspace-root", str(tmp_path / "ws"), "--once"])
    assert rc == 2
    assert "readable by other accounts" in capsys.readouterr().err

    rc = main(["galley", "agent", "--env-file", str(tmp_path / "gone.env"),
               "--once"])
    assert rc == 2
    assert "No agent credentials" in capsys.readouterr().err


def test_the_cli_uninstalls(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ga, "uninstall", lambda **_kw: False)
    monkeypatch.setattr(ga, "service_path", lambda *_a, **_kw: Path("/x/y"))
    rc = main(["galley", "agent", "--uninstall"])
    assert rc == 0
    assert "no agent service at /x/y" in capsys.readouterr().out


def test_the_budget_help_no_longer_says_twenty(capsys):
    with pytest.raises(SystemExit):
        main(["galley", "drive", "--help"])
    out = capsys.readouterr().out
    assert "$20" not in out
    assert "$10" in out
