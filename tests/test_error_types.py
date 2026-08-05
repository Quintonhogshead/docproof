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
from docproof.validator import find_nth, shrink

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


def test_every_calibration_example_survives_the_validator():
    """An example teaches the model what a good finding looks like, so every
    example must be one the validator would actually accept: it has to anchor
    in its own paragraph and shrink to a real edit. A corrected_text that
    changes nothing (rejected_noop) or that quotes text the paragraph doesn't
    contain (rejected_no_anchor) teaches the model to produce findings the
    pipeline throws away."""
    for key, et in load_error_types(ERROR_DIR, _keys()).items():
        for i, ex in enumerate(et.examples):
            finding = ex.get("finding")
            if not finding:
                continue
            where = f"{key} example {i}"
            occurrence = finding.get("occurrence", 1)
            assert find_nth(ex["text"], finding["original_text"],
                            occurrence) != -1, f"{where}: quote does not anchor"
            _, deleted, inserted = shrink(finding["original_text"],
                                          finding["corrected_text"])
            if et.is_format:
                # corrected_text is the span to mark, not a rewritten
                # sentence, so the contract is containment rather than a diff.
                assert finding["corrected_text"] in ex["text"], (
                    f"{where}: a format type's corrected_text must be a span "
                    f"of the paragraph, verbatim")
            elif et.is_query:
                # A query type's examples must be the other way round: it asks
                # and never edits, so an example that changed the text would
                # teach it to do the one thing it must not.
                assert not deleted and not inserted, (
                    f"{where}: a query type's corrected_text must repeat "
                    f"original_text unchanged")
            else:
                assert deleted or inserted, (
                    f"{where}: corrected_text is identical to original_text")


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
    out, _ = MockAnalyzer(group, canned, itertools.count(1)).analyze_chunk(
        _chunk(), Usage())
    assert [f.error_type for f in out] == ["spelling", "repeated_word"]


def test_raw_finding_error_type_defaults_to_empty():
    rf = RawFinding.model_validate(
        {"para_id": "body-0000", "original_text": "a", "corrected_text": "b"})
    assert rf.error_type == ""


def _flat(text: str) -> str:
    """A prompt with its line wrapping collapsed. The YAML wraps at 78 columns,
    so a phrase these tests care about can straddle a newline."""
    return " ".join(text.split())


def test_verb_form_states_its_borders_with_its_neighbours():
    """It shares a pass with subject_verb_agreement and a subject with
    spelling and tense_shift. A type that does not name the neighbour owning a
    case gets it reported twice, and the validator throws the second away —
    which looks like agreement rather than the duplicate it is."""
    said = _flat(load_error_types(
        ERROR_DIR, ["verb_form"])["verb_form"].detection_prompt)
    for owner in ("spelling", "subject_verb_agreement", "tense_shift"):
        assert owner in said, owner
    # The forms with two accepted spellings are a house preference, not an
    # error, and this is the type most likely to be asked to enforce one.
    for both_right in ("lighted", "fitted", "dived", "snuck"):
        assert both_right in said, both_right
    # The rule runs one way: a bare past tense is correct and stays.
    assert "he written it" in said


def test_double_negative_leaves_the_deliberate_ones_alone():
    """Litotes is a figure of speech, not a fault. "Not unkind" flattened to
    "kind" is a worse sentence, and it is the failure this type would make
    most often if it only counted negatives."""
    et = load_error_types(ERROR_DIR, ["double_negative"])["double_negative"]
    # Collapsed, because the YAML wraps and a phrase can straddle a newline.
    said = _flat(et.detection_prompt)
    for keep in ("not unkind", "not unlike", "not impossible",
                 "don't disagree", "can't help but"):
        assert keep in said, keep
    # Two negatives in two clauses is one negative each, which is correct.
    assert "separate clauses" in said
    # Which negative goes is not arbitrary: taking the first one rewrites the
    # rhythm of a sentence nobody asked to have rewritten.
    assert "SECOND negative" in _flat(et.fix_guidance)
    assert "nothing → anything" in _flat(et.fix_guidance)


def test_adjective_adverb_knows_the_three_ways_it_would_be_wrong():
    """The rule is simple and the exceptions are not. Each of these is a whole
    class of correct English that an -ly reflex would break."""
    said = _flat(load_error_types(
        ERROR_DIR, ["adjective_adverb"])["adjective_adverb"].detection_prompt)
    # Linking verbs take adjectives: "felt badly" is the error, not "felt bad".
    assert "felt bad" in said
    assert "NOT \"felt badly\"" in said
    # Flat adverbs are adverbs already.
    for flat in ("drive slow", "hold tight", "work hard", "play fair"):
        assert flat in said, flat
    # A resultative describes the state, not the manner.
    assert "painted the door red" in said
    assert "wiped the lens clean" in said
    # And the two neighbours that own adjacent cases.
    for owner in ("comparative_form", "ly_adverb_hyphen"):
        assert owner in said, owner
