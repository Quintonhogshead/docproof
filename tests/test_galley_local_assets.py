"""Required local data is present and changed content invalidates its identity."""
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from docproof import consistency, variants
from galley import local_assets as assets
from galley.fixed_policy import configuration


@pytest.fixture(autouse=True)
def fresh_worker(monkeypatch):
    monkeypatch.setattr(assets, "_WORKER_ASSET_IDENTITY", None)


def restart_worker():
    """Only tests simulate a fresh process; production must restart the worker."""
    assets._WORKER_ASSET_IDENTITY = None


@pytest.fixture
def settings():
    return configuration(), SimpleNamespace(variant=variants.load_variant("us"))


def test_real_installed_assets_have_repeatable_content_identity(settings):
    cfg, prepared = settings
    identity = assets.local_asset_identity(cfg, prepared)
    assert identity == assets.local_asset_identity(cfg, prepared)
    assert set(identity["packages"]) == {"spylls", "wordfreq"}
    for record in [*identity["dictionary"].values(), identity["wordfreq_english"],
                   *identity["variants"].values(), *identity["consistency"].values(),
                   *identity["implementations"].values()]:
        assert record["present"] and record["bytes"] > 0
        assert len(record["sha256"]) == 64
    assert Path(identity["wordfreq_english"]["path"]).name in {
        "large_en.msgpack.gz", "small_en.msgpack.gz"}
    assert {"docproof/toccheck.py", "docproof/headings.py", "docproof/variants.py",
            "docproof/spellscan.py"} <= set(identity["implementations"])


@pytest.mark.parametrize("extension", ["aff", "dic"])
def test_actual_configured_dictionary_bytes_are_bound_and_required(
        tmp_path, settings, extension):
    cfg, prepared = settings
    installed = assets.local_asset_identity(cfg, prepared)["dictionary"]
    base = tmp_path / "custom_dictionary"
    for suffix, record in installed.items():
        shutil.copyfile(record["path"], str(base) + "." + suffix)
    cfg.spellcheck.dictionary = str(base)
    restart_worker()
    first = assets.local_asset_identity(cfg, prepared)
    target = Path(str(base) + "." + extension)
    target.write_bytes(target.read_bytes() + b"\n")
    restart_worker()
    second = assets.local_asset_identity(cfg, prepared)
    assert first["dictionary"][extension]["sha256"] != second["dictionary"][extension]["sha256"]
    assert second["dictionary"][extension]["path"] == str(target.resolve())
    target.unlink()
    with pytest.raises(assets.LocalAssetError, match="unavailable"):
        assets.local_asset_identity(cfg, prepared)


@pytest.mark.parametrize("content", [None, b"", b"# Only a comment\n", b"one-form\n", b"\xff\tgray"])
def test_missing_or_empty_variant_table_cannot_be_reported_as_complete(
        tmp_path, monkeypatch, settings, content):
    cfg, prepared = settings
    monkeypatch.setattr(consistency, "_CONSISTENCY_DIR", tmp_path)
    if content is not None:
        (tmp_path / "varcon.tsv").write_bytes(content)
    with pytest.raises(assets.LocalAssetError, match="varcon.tsv"):
        assets.local_asset_identity(cfg, prepared)


def test_disabled_consistency_does_not_require_unused_rule_tables(tmp_path, monkeypatch, settings):
    cfg, prepared = settings
    cfg.consistency.enabled = False
    monkeypatch.setattr(consistency, "_CONSISTENCY_DIR", tmp_path)
    identity = assets.local_asset_identity(cfg, prepared)
    assert not identity["consistency"]["varcon.tsv"]["present"]
    assert not identity["consistency"]["chicago.yaml"]["present"]


def test_variant_yaml_and_rule_table_edits_change_the_asset_identity(tmp_path, monkeypatch, settings):
    cfg, prepared = settings
    rules, dialects = tmp_path / "rules", tmp_path / "variants"
    shutil.copytree(consistency._CONSISTENCY_DIR, rules)
    shutil.copytree(variants._dir(), dialects)
    monkeypatch.setattr(consistency, "_CONSISTENCY_DIR", rules)
    monkeypatch.setattr(variants, "_dir", lambda: dialects)
    first = assets.local_asset_identity(cfg, prepared)
    (rules / "varcon.tsv").write_bytes((rules / "varcon.tsv").read_bytes() + b"\ncustomone\tcustomtwo\n")
    (dialects / "us.yaml").write_bytes((dialects / "us.yaml").read_bytes() + b"\n# updated policy data\n")
    restart_worker()
    second = assets.local_asset_identity(cfg, prepared)
    assert first["consistency"]["varcon.tsv"]["sha256"] != second["consistency"]["varcon.tsv"]["sha256"]
    assert first["variants"]["us"]["sha256"] != second["variants"]["us"]["sha256"]


def test_missing_english_wordfreq_data_fails_without_using_another_language(monkeypatch, settings):
    import wordfreq
    cfg, prepared = settings
    monkeypatch.setattr(wordfreq, "available_languages", lambda wordlist: {"de": "/unused/German.msgpack.gz"})
    with pytest.raises(assets.LocalAssetError, match="English wordfreq"):
        assets.local_asset_identity(cfg, prepared)


def test_frequency_data_and_package_version_changes_are_bound(tmp_path, monkeypatch, settings):
    cfg, prepared = settings
    data = tmp_path / "large_en.msgpack.gz"
    shutil.copyfile(assets._english_wordfreq_data(), data)
    monkeypatch.setattr(assets, "_english_wordfreq_data", lambda: data)
    first = assets.local_asset_identity(cfg, prepared)
    data.write_bytes(data.read_bytes() + b"changed")
    installed_version = assets.metadata.version
    monkeypatch.setattr(assets.metadata, "version",
                        lambda package: "new-version" if package == "wordfreq" else installed_version(package))
    restart_worker()
    second = assets.local_asset_identity(cfg, prepared)
    assert first["wordfreq_english"]["sha256"] != second["wordfreq_english"]["sha256"]
    assert first["packages"]["wordfreq"] != second["packages"]["wordfreq"]


def test_running_worker_refuses_changed_assets_without_relabelling_cached_evidence(
        tmp_path, monkeypatch, settings):
    cfg, prepared = settings
    rules = tmp_path / "rules"
    shutil.copytree(consistency._CONSISTENCY_DIR, rules)
    monkeypatch.setattr(consistency, "_CONSISTENCY_DIR", rules)
    first = assets.local_asset_identity(cfg, prepared)
    assert assets.local_asset_identity(cfg, prepared) == first
    table = rules / "varcon.tsv"
    table.write_bytes(table.read_bytes() + b"\ncustomone\tcustomtwo\n")
    with pytest.raises(assets.LocalAssetError, match="Restart the worker"):
        assets.local_asset_identity(cfg, prepared)
    with pytest.raises(assets.LocalAssetError, match="Restart the worker"):
        assets.local_asset_identity(cfg, prepared)
    restart_worker()
    assert assets.local_asset_identity(cfg, prepared) != first
