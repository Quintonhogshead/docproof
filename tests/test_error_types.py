"""The error-type contract: every YAML loads, every enabled key exists, and a
pass covering several types stays wired to the right keys."""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from pydantic import ValidationError

from docproof.analyzer import (MockAnalyzer, RawFinding, build_output_model,
                               build_system_prompt)
from docproof.config import Config, load_config
from docproof.error_registry import load_error_types
from docproof.models import Chunk, ParagraphRef, Usage

ERROR_DIR = Path(__file__).parent.parent / "config" / "error_types"
DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


def _keys() -> list[str]:
    return sorted(p.stem for p in ERROR_DIR.glob("*.yaml")
                  if not p.stem.startswith("_"))


def test_every_error_type_file_loads():
    registry = load_error_types(ERROR_DIR, _keys())
    assert registry, "no error types found"
    for key, et in registry.items():
        assert et.key == key
        assert et.detection_prompt and et.fix_guidance
        # The do-not-flag list is where false positives go to die.
        assert "Do NOT flag" in et.detection_prompt, key
        assert et.examples, f"{key} has no calibration examples"
        for ex in et.examples:
            assert "text" in ex, key
            if ex.get("finding"):
                assert ex["finding"]["original_text"] == ex["text"], (
                    f"{key}: example original_text must quote the whole "
                    f"paragraph text verbatim")


def test_default_config_enables_existing_types():
    cfg = load_config(DEFAULT_CONFIG)
    missing = set(cfg.error_type_keys) - set(_keys())
    assert not missing, f"enabled but undefined: {missing}"
    load_error_types(ERROR_DIR, cfg.error_type_keys)


def test_groups_normalize_bare_keys_and_lists():
    cfg = Config(error_types=["comma_splice", ["spelling", "repeated_word"]])
    assert cfg.error_type_groups == (("comma_splice",),
                                     ("spelling", "repeated_word"))
    assert cfg.error_type_keys == ("comma_splice", "spelling", "repeated_word")


def test_duplicate_key_across_passes_is_rejected():
    with pytest.raises(ValidationError, match="more than once"):
        Config(error_types=["spelling", ["spelling", "repeated_word"]])


def test_empty_group_is_rejected():
    with pytest.raises(ValidationError, match="at least one key"):
        Config(error_types=[[]])


def test_output_model_constrains_error_type_to_the_pass():
    model = build_output_model(("spelling", "repeated_word"))
    schema = model.model_json_schema()
    defn = next(iter(schema["$defs"].values()))
    assert defn["properties"]["error_type"]["enum"] == ["spelling",
                                                        "repeated_word"]
    with pytest.raises(ValidationError):
        model.model_validate({"findings": [
            {"para_id": "body-0000", "error_type": "comma_splice",
             "original_text": "a", "corrected_text": "b"}]})


def test_system_prompt_covers_every_type_in_the_pass():
    registry = load_error_types(ERROR_DIR, ["spelling", "comma_splice"])
    prompt = build_system_prompt(list(registry.values()))
    assert "ERROR TYPE: spelling" in prompt
    assert "ERROR TYPE: comma_splice" in prompt
    assert "This pass covers 2 error types" in prompt
    # Calibration examples must carry the label the schema demands.
    assert '"error_type": "spelling"' in prompt

    solo = build_system_prompt(list(load_error_types(
        ERROR_DIR, ["spelling"]).values()))
    assert "This pass covers" not in solo


def _chunk() -> Chunk:
    para = ParagraphRef(para_id="body-0000", part="word/document.xml",
                        location="body", text="Some text.", style="Normal")
    return Chunk(chunk_id="chunk-000", paragraphs=(para,), est_tokens=10)


def test_mock_analyzer_labels_and_filters_by_pass():
    registry = load_error_types(ERROR_DIR, ["spelling", "repeated_word"])
    group = list(registry.values())
    canned = [
        # unlabelled → belongs to the first type in the pass
        {"para_id": "body-0000", "original_text": "a", "corrected_text": "b"},
        {"para_id": "body-0000", "error_type": "repeated_word",
         "original_text": "c", "corrected_text": "d"},
        # owned by a different pass → not replayed here
        {"para_id": "body-0000", "error_type": "comma_splice",
         "original_text": "e", "corrected_text": "f"},
    ]
    out = MockAnalyzer(group, canned, itertools.count(1)).analyze_chunk(
        _chunk(), Usage())
    assert [f.error_type for f in out] == ["spelling", "repeated_word"]


def test_raw_finding_error_type_defaults_to_empty():
    rf = RawFinding.model_validate(
        {"para_id": "body-0000", "original_text": "a", "corrected_text": "b"})
    assert rf.error_type == ""
