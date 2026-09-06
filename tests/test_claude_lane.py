"""api.claude_lane: Claude models run on the subscription, or not at all."""
from __future__ import annotations

import pytest

from docproof.config import Config
from docproof.providers import ProviderError, build_provider


def _cfg(**api):
    cfg = Config()
    for key, value in api.items():
        setattr(cfg.api, key, value)
    return cfg


def test_api_lane_still_builds_the_vendor_provider():
    """The default is unchanged: a server with no subscription bills the API."""
    cfg = _cfg(model="claude-sonnet-5", claude_lane="api")
    provider = build_provider(cfg, api_key="k")
    assert provider.name != "subagent"


def test_subagent_lane_routes_claude_off_the_api(monkeypatch):
    monkeypatch.setattr("docproof.providers.subagent.availability",
                        lambda: (True, "ok"))
    cfg = _cfg(model="claude-sonnet-5", claude_lane="subagent")
    provider = build_provider(cfg, api_key="k")
    assert provider.name == "subagent"
    assert provider.model == "claude-sonnet-5"


def test_subagent_lane_fails_closed_rather_than_billing(monkeypatch):
    """The whole point: an unavailable lane stops the run. Falling back to the
    API would spend money the config promised it would not."""
    monkeypatch.setattr("docproof.providers.subagent.availability",
                        lambda: (False, "not logged in"))
    cfg = _cfg(model="claude-sonnet-5", claude_lane="subagent")
    with pytest.raises(ProviderError) as e:
        build_provider(cfg, api_key="k")
    assert "not logged in" in str(e.value)
    assert "Refusing to fall back" in str(e.value)


def test_subagent_lane_leaves_other_vendors_alone(monkeypatch):
    """Luna is OpenAI and still bills — only Claude moves to the subscription."""
    monkeypatch.setattr("docproof.providers.subagent.availability",
                        lambda: (True, "ok"))
    cfg = _cfg(model="claude-sonnet-5", claude_lane="subagent")
    provider = build_provider(cfg, api_key="k", model="gpt-5.6-luna")
    assert provider.name != "subagent"


def test_subagent_lane_uses_its_own_concurrency_ceiling():
    """A Claude Code turn is a process, not a socket, so the vendor's API
    headroom is the wrong limit for it."""
    cfg = _cfg(model="claude-sonnet-5", claude_lane="subagent",
               concurrency=8, subagent_concurrency=3)
    assert cfg.concurrency_for("claude-sonnet-5") == 3
    # Luna is untouched by the subagent ceiling.
    assert cfg.concurrency_for("gpt-5.6-luna") == 24


def test_api_lane_ignores_the_subagent_ceiling():
    cfg = _cfg(model="claude-sonnet-5", claude_lane="api",
               concurrency=8, subagent_concurrency=3)
    assert cfg.concurrency_for("claude-sonnet-5") == 8


def test_serial_switch_still_wins_on_the_subagent_lane():
    cfg = _cfg(model="claude-sonnet-5", claude_lane="subagent",
               concurrency=1, subagent_concurrency=12)
    assert cfg.concurrency_for("claude-sonnet-5") == 1
