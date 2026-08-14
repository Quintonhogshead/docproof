"""How wide a pass runs, and where the whole-book reads are cached.

Both are decided in config, and both are easy to get silently wrong: a pool
sized from the wrong vendor's allowance fails as a rate limit nobody sees, and a
cache folder that resolves differently between two runs of the same draft simply
never hits and quietly costs money.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from docproof.config import (Config, cache_dir_for, default_cache_dir,
                             load_config)

ROOT = Path(__file__).parent.parent
CONFIG = str(ROOT / "config" / "default.yaml")


# --- concurrency -------------------------------------------------------------

def test_the_vendor_table_decides_not_the_pass():
    cfg = Config()
    cfg.api.concurrency = 8
    cfg.api.concurrency_by_provider = {"openai": 24}
    # An Anthropic model gets the floor; an OpenAI one gets the raise. Asked
    # with no model at all it answers about api.model.
    assert cfg.concurrency_for("claude-haiku-4-5") == 8
    assert cfg.concurrency_for("gpt-5.6-luna") == 24
    cfg.api.model = "gpt-5.6-luna"
    assert cfg.concurrency_for() == 24


def test_a_model_the_catalog_does_not_know_falls_back_to_api_provider():
    cfg = Config()
    cfg.api.concurrency = 8
    cfg.api.concurrency_by_provider = {"openai": 24}
    cfg.api.provider = "openai"
    assert cfg.concurrency_for("some-finetune-nobody-catalogued") == 24
    cfg.api.provider = "anthropic"
    assert cfg.concurrency_for("some-finetune-nobody-catalogued") == 8


def test_concurrency_one_beats_the_vendor_table():
    """The serial escape hatch has to be absolute. A vendor entry out-voting it
    would leave "make it serial" quietly doing nothing on the very runs — the
    OpenAI ones — where someone is most likely to be chasing a threading bug."""
    cfg = Config()
    cfg.api.concurrency = 1
    cfg.api.concurrency_by_provider = {"openai": 24}
    assert cfg.concurrency_for("gpt-5.6-luna") == 1
    assert cfg.concurrency_for("claude-haiku-4-5") == 1


def test_a_misspelled_vendor_is_refused_rather_than_ignored():
    with pytest.raises(ValidationError, match="not a provider"):
        Config.model_validate({"api": {"concurrency_by_provider": {"OpenAI": 24}}})
    with pytest.raises(ValidationError, match="at least 1"):
        Config.model_validate({"api": {"concurrency_by_provider": {"openai": 0}}})


def test_the_shipped_config_raises_only_openai():
    cfg = load_config(CONFIG)
    assert cfg.api.concurrency == 8
    assert cfg.api.concurrency_by_provider == {"openai": 24}


# --- the whole-book cache folder ---------------------------------------------

def test_the_cache_folder_follows_docproof_home(monkeypatch, tmp_path):
    """On the hosted build DOCPROOF_HOME is the mounted volume, so the cache
    outlives a deploy instead of being re-earned on every one."""
    monkeypatch.delenv("DOCPROOF_CACHE_DIR", raising=False)
    monkeypatch.setenv("DOCPROOF_HOME", str(tmp_path / "home"))
    assert default_cache_dir() == str(tmp_path / "home" / "cache" / "whole-book")


def test_an_empty_cache_dir_env_turns_caching_off(monkeypatch):
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", "")
    assert default_cache_dir() is None
    assert cache_dir_for(None) is None


def test_an_unset_cache_dir_resolves_to_the_shared_default(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", str(tmp_path / "c"))
    assert cache_dir_for(None) == str(tmp_path / "c")
    assert cache_dir_for("") == str(tmp_path / "c")


def test_a_path_set_in_the_file_wins_over_the_default(monkeypatch, tmp_path):
    """The default only ever fills a blank — a press that pins one pass's cache
    somewhere of its own keeps it."""
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", str(tmp_path / "shared"))
    assert cache_dir_for("/somewhere/of/mine") == "/somewhere/of/mine"


def test_the_cache_folder_never_reaches_the_checkpoint_fingerprint(
        monkeypatch, tmp_path):
    """The app fingerprints a resumable run with cfg.model_dump(mode="json") and
    discards the checkpoint whenever that moves. Resolving the folder at the
    point of use — not into the Config — is what keeps an environment-derived
    absolute path from invalidating a review's paid-for calls when someone turns
    the cache off, or resumes under a different HOME."""
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", str(tmp_path / "one"))
    first = load_config(CONFIG).model_dump(mode="json")
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", str(tmp_path / "somewhere-else"))
    second = load_config(CONFIG).model_dump(mode="json")
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", "")
    third = load_config(CONFIG).model_dump(mode="json")

    assert first == second == third
    dumped = json.dumps(first)
    assert "whole-book" not in dumped and str(tmp_path) not in dumped
