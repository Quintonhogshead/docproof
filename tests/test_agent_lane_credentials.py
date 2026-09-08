"""The sifter finds the subscription token on disk when nobody handed it one.

The first live `galley drive` ladder (2026-09-06) died at its first Claude
call: the driver exports CLAUDE_CODE_OAUTH_TOKEN for the brain's `claude -p`
process, but Claude Code does not pass its own token down to the Bash children
the brain spawns, so every `docproof` sifter probed `claude auth status`, got
loggedIn=False, and `build_provider` refused (correctly) to bill the API
instead. The lane the config pins has to be reachable from there, so the token
is read from the agent credentials file the machine already keeps."""
from __future__ import annotations

import stat

import pytest

from docproof import agent_lane
from docproof.config import Config
from docproof.providers import ProviderError, build_provider
from docproof.providers import subagent


@pytest.fixture
def creds(tmp_path, monkeypatch):
    """A well-formed, private ~/.galley/agent.env, found through the override."""
    path = tmp_path / "agent.env"
    path.write_text('export CLAUDE_CODE_OAUTH_TOKEN="sk-oat-from-file"\n'
                    "GALLEY_APP_URL=https://example.invalid\n",
                    encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(agent_lane.CREDENTIALS_FILE_ENV, str(path))
    monkeypatch.delenv(agent_lane.OAUTH_TOKEN_KEY, raising=False)
    return path


@pytest.fixture
def lane_probe(monkeypatch):
    """The SDK importable and the CLI absent, so `availability()` turns on the
    login check alone — which is the thing under test."""
    agent_lane.reset_login_probe()
    monkeypatch.setattr(agent_lane, "sdk", lambda hint: object())
    monkeypatch.setattr(agent_lane.shutil, "which", lambda name: None)
    yield
    agent_lane.reset_login_probe()


def _cfg():
    cfg = Config()
    cfg.api.model = "claude-sonnet-5"
    cfg.api.claude_lane = "subagent"
    return cfg


def test_the_credentials_file_makes_the_lane_reachable(creds, lane_probe):
    assert agent_lane.subscription_token() == "sk-oat-from-file"
    ok, _why = subagent.availability()
    assert ok is True
    provider = build_provider(_cfg(), api_key="k")
    assert provider.name == "subagent"


def test_the_token_reaches_the_child_so_probe_and_turn_agree(creds):
    """One seam: `child_env` is what `probe_login` runs `claude auth status`
    in AND what the session turn is given, so they cannot disagree."""
    env = agent_lane.child_env()
    assert env[agent_lane.OAUTH_TOKEN_KEY] == "sk-oat-from-file"
    assert env["ANTHROPIC_API_KEY"] == ""            # the billing fence stands


def test_with_no_token_anywhere_the_lane_still_fails_closed(tmp_path,
                                                            monkeypatch,
                                                            lane_probe):
    """The v0.188.0 guarantee: no silent fallback to API dollars."""
    monkeypatch.delenv(agent_lane.OAUTH_TOKEN_KEY, raising=False)
    monkeypatch.setenv(agent_lane.CREDENTIALS_FILE_ENV,
                       str(tmp_path / "absent.env"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    ok, why = subagent.availability()
    assert ok is False and "claude setup-token" in why
    with pytest.raises(ProviderError) as e:
        build_provider(_cfg(), api_key="k")
    assert "Refusing to fall back" in str(e.value)


def test_a_group_readable_credentials_file_is_refused_not_used(creds,
                                                               lane_probe,
                                                               tmp_path,
                                                               monkeypatch):
    creds.chmod(0o640)
    with pytest.raises(agent_lane.CredentialsError, match="chmod 600"):
        agent_lane.read_credentials(creds)
    assert agent_lane.file_token(creds) == ""
    assert agent_lane.OAUTH_TOKEN_KEY not in agent_lane.child_env()
    # and the lane fails closed rather than spending a token others can read
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    assert subagent.availability()[0] is False


def test_a_world_readable_file_is_refused_on_an_injected_stat(creds):
    fake = type("S", (), {"st_mode": stat.S_IFREG | 0o604})()
    with pytest.raises(agent_lane.CredentialsError, match="mode 0604"):
        agent_lane.read_credentials(creds, stat_fn=lambda _p: fake)


def test_the_environment_wins_when_both_are_present(creds, monkeypatch):
    monkeypatch.setenv(agent_lane.OAUTH_TOKEN_KEY, "sk-oat-from-env")
    assert agent_lane.subscription_token() == "sk-oat-from-env"
    # nothing is added to the child env: it already inherits the real one
    assert agent_lane.child_env() == {"ANTHROPIC_API_KEY": ""}


def test_the_default_path_is_the_agent_credentials_file(tmp_path, monkeypatch):
    monkeypatch.delenv(agent_lane.CREDENTIALS_FILE_ENV, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert agent_lane.credentials_path() == tmp_path / ".galley" / "agent.env"
    home_file = tmp_path / ".galley" / "agent.env"
    home_file.parent.mkdir(parents=True)
    home_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-home\n", encoding="utf-8")
    home_file.chmod(0o600)
    monkeypatch.delenv(agent_lane.OAUTH_TOKEN_KEY, raising=False)
    assert agent_lane.subscription_token() == "sk-home"


def test_a_credentials_file_without_a_token_is_not_a_login(tmp_path,
                                                           monkeypatch):
    path = tmp_path / "agent.env"
    path.write_text("GALLEY_APP_URL=https://example.invalid\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(agent_lane.CREDENTIALS_FILE_ENV, str(path))
    monkeypatch.delenv(agent_lane.OAUTH_TOKEN_KEY, raising=False)
    assert agent_lane.file_token() == ""
