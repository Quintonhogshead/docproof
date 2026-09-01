"""Workflow-stage presets (docproof/stages.py) and their composition with a
genre.

The load-bearing guarantee: a stage decides which LANES run and can LOCK them,
and a genre applied on top can never reopen a locked lane. That is what keeps a
mechanical wave mechanical whatever posture the book carries — the
contamination (self_help_business flipping edits-mode smoothing on during a
proofread) the stage layer removes.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from docproof.__main__ import _configure
from docproof.config import Config, load_config
from docproof.genre import materialize_genre_pack, write_genre_pack
from docproof.stages import (LOCKABLE_KEYS, apply_stage, available_stages,
                             enforce_locks, load_stage_preset)

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"

SHIPPED_STAGES = ("copyedit-wave", "external-judgment", "final-replay",
                  "mechanical-wave", "poetry-touch")


def test_available_stages_lists_every_shipped_preset():
    assert available_stages() == tuple(sorted(SHIPPED_STAGES))


@pytest.mark.parametrize("stage", SHIPPED_STAGES)
def test_shipped_stage_loads_and_only_locks_lockable_keys(stage):
    preset = load_stage_preset(stage)
    assert isinstance(preset.get("patch"), dict) and preset["patch"]
    assert set(preset.get("locks") or []) <= LOCKABLE_KEYS


def test_apply_stage_none_is_a_noop():
    cfg = load_config(CONFIG)
    before = cfg.model_dump()
    cfg, locks = apply_stage(cfg, None)
    assert cfg.model_dump() == before
    assert locks == {}


def test_unknown_stage_raises_with_the_available_list():
    with pytest.raises(ValueError, match="proof_only"):
        load_stage_preset("proof_only")


# --- the load-time validation of a preset ------------------------------------

def test_lock_outside_lockable_set_is_refused(tmp_path):
    bad = tmp_path / "rogue.yaml"
    bad.write_text(yaml.safe_dump({
        "name": "rogue",
        "patch": {"normalize": {"quotes": False}},
        "locks": ["normalize.quotes"],           # a mechanics key
    }))
    with pytest.raises(ValueError, match="not lockable"):
        load_stage_preset("rogue", stages_dir=tmp_path)


def test_lock_of_a_value_the_patch_does_not_set_is_refused(tmp_path):
    bad = tmp_path / "empty_lock.yaml"
    bad.write_text(yaml.safe_dump({
        "name": "empty_lock",
        "patch": {"rewrite": {"enabled": False}},
        "locks": ["smoothing.enabled"],          # never set in the patch
    }))
    with pytest.raises(ValueError, match="does not"):
        load_stage_preset("empty_lock", stages_dir=tmp_path)


def test_stage_patch_bad_value_fails_at_apply(tmp_path):
    bad = tmp_path / "malformed.yaml"
    bad.write_text(yaml.safe_dump({
        "name": "malformed",
        "patch": {"ensemble": {"verify_policy": "sometimes"}},   # not a Literal
    }))
    cfg = Config()
    with pytest.raises(Exception):
        apply_stage(cfg, "malformed", stages_dir=tmp_path)


# --- the core guarantee: a stage lock beats a genre --------------------------

def test_mechanical_wave_locks_the_copyedit_lane_against_any_genre():
    """self_help_business turns edits-mode smoothing AND the rewrite lever on.
    Under mechanical-wave they must stay off — the stage lock wins, and the
    violation is reported so nothing happens silently."""
    cfg, summary = materialize_genre_pack(
        CONFIG, "self_help_business", stage="mechanical-wave")
    assert cfg.smoothing.enabled is False
    assert cfg.smoothing.edits is False
    assert cfg.rewrite.enabled is False
    assert set(summary["stage_lock_violations"]) == {
        "smoothing.enabled", "smoothing.edits", "rewrite.enabled"}
    # And the recall recipe the stage carries is materialized.
    assert [d.model for d in cfg.ensemble.detectors] == [
        "gpt-5.6-luna", "claude-haiku-4-5"]
    assert cfg.ensemble.verifier_model == "gpt-5.6-luna"
    assert cfg.repair.enabled is True


def test_copyedit_wave_leaves_smoothing_unlocked_so_a_protective_genre_wins():
    """copyedit-wave turns smoothing on by default but does NOT lock it, so a
    voice-protective genre still holds the lane shut — even a copy-edit wave
    must not reword Scripture."""
    cfg, summary = materialize_genre_pack(
        CONFIG, "religious", stage="copyedit-wave")
    assert cfg.smoothing.enabled is False          # the genre won (unlocked)
    assert "smoothing.enabled" not in summary.get("stage_lock_violations", [])
    assert cfg.repair.enabled is False             # locked off either way


def test_copyedit_wave_enables_the_lane_for_a_permissive_genre():
    cfg, _ = materialize_genre_pack(
        CONFIG, "self_help_business", stage="copyedit-wave")
    assert cfg.smoothing.enabled is True
    assert cfg.smoothing.edits is True
    assert cfg.repair.enabled is False


def test_final_replay_zeroes_detection():
    cfg, _ = materialize_genre_pack(
        CONFIG, "general_fiction", stage="final-replay")
    assert cfg.error_types == []
    assert cfg.ensemble.detectors == []
    assert cfg.ensemble.verify_policy == "none"
    assert cfg.smoothing.enabled is False
    assert cfg.rewrite.enabled is False


# --- the live CLI path (_configure) applies stage then genre then locks ------

def test_configure_cli_applies_stage_lock_over_genre():
    args = Namespace(config=str(CONFIG), stage="mechanical-wave",
                     genre="self_help_business", profile=None,
                     error_types=None, min_confidence=None, variant=None,
                     dictionary=None, no_comments=False, out=None, model=None)
    cfg, _error_dir = _configure(args)
    assert cfg.smoothing.edits is False
    assert cfg.rewrite.enabled is False
    assert [d.model for d in cfg.ensemble.detectors] == [
        "gpt-5.6-luna", "claude-haiku-4-5"]


def test_written_stage_config_round_trips_and_is_self_contained(tmp_path):
    out = tmp_path / "bookws" / "mech.yaml"
    write_genre_pack(CONFIG, "religious", out, stage="mechanical-wave")
    reloaded = load_config(out)
    assert reloaded.smoothing.edits is False
    assert reloaded.rewrite.enabled is False
    from docproof.__main__ import _resolve_error_dir
    assert _resolve_error_dir(out).is_dir()
