"""Scoped discovery keeps command information intact without the full catalog."""
from __future__ import annotations

import json

import pytest

from docproof import __version__
from docproof.__main__ import main
from docproof.config import Config


def _read(capsys, *args):
    assert main(["capabilities", *args]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_full_manifest_and_json_flag_remain_compatible(capsys):
    manifest = _read(capsys)
    assert manifest == _read(capsys, "--json")
    assert set(manifest) == {
        "tool", "version", "commands", "config_sections", "genres", "stages"}
    assert manifest["tool"] == "docproof"
    assert manifest["version"] == __version__
    assert manifest["config_sections"] == sorted(Config.model_fields)
    commands = {c["name"]: c for c in manifest["commands"]}
    assert {"review", "sweep", "inventory", "galley", "capabilities"} <= commands.keys()
    assert "mechanical-wave" in manifest["stages"]
    assert "literary_memoir" in manifest["genres"]


@pytest.mark.parametrize("path", [
    ("review",), ("sweep",), ("galley",), ("capabilities",),
    ("galley", "profile"), ("galley", "approve"),
    ("galley", "verify"), ("galley", "genre-pack"),
])
def test_scoped_result_is_the_exact_full_manifest_subtree(capsys, path):
    choices = _read(capsys)["commands"]
    for name in path:
        expected = next(c for c in choices if c["name"] == name)
        choices = expected.get("subcommands", [])
    assert _read(capsys, *path) == expected
    assert _read(capsys, "--json", *path) == expected
    assert _read(capsys, *path, "--json") == expected


@pytest.mark.parametrize("path, message, choice", [
    (("verfiy",), "available commands under 'docproof'", "galley"),
    (("galley", "verfiy"), "available commands under 'galley'", "verify"),
    (("galley", "verify", "book.docx"), "has no subcommands",
     "docproof capabilities galley verify"),
])
def test_invalid_paths_fail_with_actionable_guidance(capsys, path, message, choice):
    with pytest.raises(SystemExit) as error:
        main(["capabilities", *path])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert choice in captured.err


def test_scoped_lookup_never_executes_the_selected_command(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("capabilities must not execute a command or call a model")

    monkeypatch.setattr("docproof.__main__.cmd_galley", forbidden)
    monkeypatch.setattr("docproof.__main__.cmd_review", forbidden)
    monkeypatch.setattr("docproof.__main__.build_provider", forbidden)
    assert _read(capsys, "galley", "verify")["name"] == "verify"
    assert _read(capsys, "review")["name"] == "review"
