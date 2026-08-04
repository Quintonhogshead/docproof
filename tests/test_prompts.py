"""Editing the instructions that get sent: an override shadows the shipped
prompt for one key, reaches the assembled message, and resets cleanly."""
from __future__ import annotations

import pytest
import yaml

from app.prompts import (PromptError, assembled_passes, clear_override,
                         list_prompts, sample_user_turn, save_override)
from docproof.config import Config
from docproof.error_registry import load_error_types
from .test_error_types import ERROR_DIR

KEY = "comma_splice"


def _cfg(**over):
    cfg = Config(error_types=[KEY])
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_an_override_shadows_only_its_own_key(tmp_path):
    save_override(ERROR_DIR, tmp_path, KEY, "ONLY FLAG SEMICOLONS")

    registry = load_error_types(ERROR_DIR, [KEY, "spelling"],
                                override_dir=tmp_path)
    assert registry[KEY].detection_prompt == "ONLY FLAG SEMICOLONS"
    # The other type is untouched: an override directory holds what changed,
    # not a frozen copy of everything.
    shipped = yaml.safe_load((ERROR_DIR / "spelling.yaml").read_text())
    assert registry["spelling"].detection_prompt == \
        shipped["detection_prompt"].strip()


def test_an_override_is_a_complete_error_type(tmp_path):
    """It has to load on its own, so load_error_types needs no special case."""
    save_override(ERROR_DIR, tmp_path, KEY, "NEW TEXT")
    data = yaml.safe_load((tmp_path / f"{KEY}.yaml").read_text())
    assert data["key"] == KEY
    assert data["name"] and data["version"] and data["fix_guidance"]
    assert data["detection_prompt"].strip() == "NEW TEXT"


def test_the_edit_reaches_the_message_that_gets_sent(tmp_path):
    save_override(ERROR_DIR, tmp_path, KEY, "ZZZ-SENTINEL-ZZZ")
    passes = assembled_passes(_cfg(), ERROR_DIR, tmp_path)
    assert len(passes) == 1
    assert "ZZZ-SENTINEL-ZZZ" in passes[0]["system_prompt"]
    assert passes[0]["characters"] == len(passes[0]["system_prompt"])


def test_shipped_files_are_never_written(tmp_path):
    before = (ERROR_DIR / f"{KEY}.yaml").read_bytes()
    save_override(ERROR_DIR, tmp_path, KEY, "SOMETHING ELSE")
    clear_override(tmp_path, KEY)
    assert (ERROR_DIR / f"{KEY}.yaml").read_bytes() == before


def test_reset_puts_the_original_back(tmp_path):
    save_override(ERROR_DIR, tmp_path, KEY, "TEMPORARY")
    assert clear_override(tmp_path, KEY) is True
    assert clear_override(tmp_path, KEY) is False      # nothing left to undo

    registry = load_error_types(ERROR_DIR, [KEY], override_dir=tmp_path)
    shipped = yaml.safe_load((ERROR_DIR / f"{KEY}.yaml").read_text())
    assert registry[KEY].detection_prompt == shipped["detection_prompt"].strip()


def test_listing_marks_what_has_been_edited(tmp_path):
    rows = {r["key"]: r for r in list_prompts(ERROR_DIR, tmp_path,
                                              [KEY, "spelling"])}
    assert rows[KEY]["edited"] is False
    assert rows[KEY]["detection_prompt"] == rows[KEY]["default_detection_prompt"]

    save_override(ERROR_DIR, tmp_path, KEY, "EDITED")
    rows = {r["key"]: r for r in list_prompts(ERROR_DIR, tmp_path,
                                              [KEY, "spelling"])}
    assert rows[KEY]["edited"] is True
    assert rows[KEY]["detection_prompt"] == "EDITED"
    # The original stays visible, which is what makes "reset" trustworthy.
    assert rows[KEY]["default_detection_prompt"] != "EDITED"
    assert rows["spelling"]["edited"] is False


@pytest.mark.parametrize("key,prompt,match", [
    ("not_a_type", "text", "no error type"),
    (KEY, "   ", "cannot be empty"),
])
def test_a_bad_edit_is_refused(tmp_path, key, prompt, match):
    with pytest.raises(PromptError, match=match):
        save_override(ERROR_DIR, tmp_path, key, prompt)


def test_the_preview_shows_the_shape_a_document_arrives_in():
    turn = sample_user_turn()
    assert turn.startswith('<paragraph id="body-0000">')
    assert turn.rstrip().endswith("</paragraph>")
