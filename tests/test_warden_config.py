"""app/warden/config.py: load/save, defaults, and the extra-keys round trip."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.warden.config import CONFIG_FILE, WARDEN_HOME_ENV, Thresholds, WardenConfig, home


def test_home_default(monkeypatch):
    monkeypatch.delenv(WARDEN_HOME_ENV, raising=False)
    assert home() == Path.home() / ".docproof-warden"


def test_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(WARDEN_HOME_ENV, str(tmp_path / "custom"))
    assert home() == tmp_path / "custom"


def test_home_env_expands_user(monkeypatch):
    monkeypatch.setenv(WARDEN_HOME_ENV, "~/somewhere-warden")
    assert home() == Path.home() / "somewhere-warden"


def test_defaults():
    cfg = WardenConfig()
    assert cfg.app_url == "https://atmosphere-docproof.fly.dev"
    assert cfg.fly_app == "atmosphere-docproof"
    assert cfg.harness == "claude"
    assert cfg.owner_name == "Quinton"
    assert cfg.imessage_enabled is True
    assert cfg.team_emails == []
    assert "docwatch@" in cfg.inbox_senders
    assert cfg.thresholds == Thresholds()
    assert cfg.thresholds.agent_silent_factor == 3
    assert cfg.thresholds.request_expiry_h == 12
    assert cfg.paused is False
    assert cfg.extra == {}


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = WardenConfig.load(tmp_path)
    assert cfg == WardenConfig()


def test_save_creates_home_directory(tmp_path):
    root = tmp_path / "not-yet-created"
    WardenConfig().save(root)
    assert (root / CONFIG_FILE).is_file()


def test_save_and_load_round_trip(tmp_path):
    cfg = WardenConfig(
        app_url="https://staging.example",
        owner_handle="+15551234567",
        team_emails=["editor@atmospherepress.com", "designer@atmospherepress.com"],
        inbox_senders=["docwatch@", "custom@"],
        thresholds=Thresholds(agent_stalled_min=45, held_batch_h=48),
    )
    cfg.save(tmp_path)
    loaded = WardenConfig.load(tmp_path)

    assert loaded.app_url == "https://staging.example"
    assert loaded.owner_handle == "+15551234567"
    assert loaded.team_emails == [
        "editor@atmospherepress.com", "designer@atmospherepress.com"]
    assert loaded.inbox_senders == ["docwatch@", "custom@"]
    assert loaded.thresholds.agent_stalled_min == 45
    assert loaded.thresholds.held_batch_h == 48
    # Untouched threshold fields keep their own defaults.
    assert loaded.thresholds.agent_silent_factor == 3
    assert loaded.extra == {}


def test_unknown_top_level_keys_round_trip(tmp_path):
    (tmp_path / CONFIG_FILE).write_text(
        "app_url: https://x\nfuture_field: surprise\nanother: [1, 2]\n",
        encoding="utf-8")

    cfg = WardenConfig.load(tmp_path)
    assert cfg.app_url == "https://x"
    assert cfg.extra == {"future_field": "surprise", "another": [1, 2]}

    cfg.save(tmp_path)
    reloaded = WardenConfig.load(tmp_path)
    assert reloaded.extra == {"future_field": "surprise", "another": [1, 2]}
    assert reloaded.app_url == "https://x"


def test_unknown_threshold_keys_are_dropped_not_carried(tmp_path):
    # Sub-dict extras aren't round-tripped the way top-level ones are -- see
    # the config.py module docstring / this project's report for why.
    (tmp_path / CONFIG_FILE).write_text(
        "thresholds:\n  agent_stalled_min: 10\n  made_up_field: 1\n",
        encoding="utf-8")
    cfg = WardenConfig.load(tmp_path)
    assert cfg.thresholds.agent_stalled_min == 10
    assert not hasattr(cfg.thresholds, "made_up_field")
    assert cfg.extra == {}


def test_paused_is_never_written_to_yaml(tmp_path):
    WardenConfig(paused=True).save(tmp_path)
    text = (tmp_path / CONFIG_FILE).read_text("utf-8")
    data = yaml.safe_load(text)
    assert "paused" not in data


def test_hand_edited_paused_key_lands_in_extra_not_the_field(tmp_path):
    (tmp_path / CONFIG_FILE).write_text("paused: true\n", encoding="utf-8")
    cfg = WardenConfig.load(tmp_path)
    assert cfg.paused is False
    assert cfg.extra.get("paused") is True


def test_corrupt_yaml_falls_back_to_defaults(tmp_path):
    (tmp_path / CONFIG_FILE).write_text("app_url: [unterminated\n", encoding="utf-8")
    cfg = WardenConfig.load(tmp_path)
    assert cfg == WardenConfig()


def test_non_mapping_yaml_falls_back_to_defaults(tmp_path):
    (tmp_path / CONFIG_FILE).write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = WardenConfig.load(tmp_path)
    assert cfg == WardenConfig()


def test_saved_file_is_hand_editable_yaml(tmp_path):
    WardenConfig().save(tmp_path)
    text = (tmp_path / CONFIG_FILE).read_text("utf-8")
    assert "app_url:" in text
    assert "thresholds:" in text
    # Round-trips through PyYAML's own loader, not just ours.
    assert isinstance(yaml.safe_load(text), dict)
