"""The web build's provider-key store: a name→secret table on the volume."""
from __future__ import annotations

import pytest

from app.keystore import KeyStore


@pytest.fixture
def store(tmp_path):
    return KeyStore(tmp_path / "secrets.db")


def test_set_get_delete(store):
    assert store.get("anthropic") is None
    store.set("anthropic", "sk-ant-123")
    assert store.get("anthropic") == "sk-ant-123"
    store.delete("anthropic")
    assert store.get("anthropic") is None


def test_set_replaces(store):
    store.set("openai", "one")
    store.set("openai", "two")
    assert store.get("openai") == "two"
    assert store.names() == ["openai"]


def test_survives_reopen(tmp_path):
    KeyStore(tmp_path / "secrets.db").set("gemini", "AIza-x")
    assert KeyStore(tmp_path / "secrets.db").get("gemini") == "AIza-x"


def test_names_lists_only_what_is_set(store):
    store.set("anthropic", "a")
    store.set("gemini", "g")
    assert set(store.names()) == {"anthropic", "gemini"}
