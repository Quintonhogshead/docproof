"""Poetry is feather-soft: the `poetry-touch` stage + `poetry` genre proof verse
for real-word misspellings only. Everything else — punctuation, capitalization,
numbers, sweeps, LanguageTool, repair, smoothing, rewrite, consistency queries,
the gates — is off, and the locked lanes stay off under any genre."""
from __future__ import annotations

from pathlib import Path

from docproof.config import load_config
from docproof.genre import apply_genre, available_genres
from docproof.stages import apply_stage, available_stages, enforce_locks

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


def test_poetry_stage_and_genre_ship():
    assert "poetry-touch" in available_stages()
    assert "poetry" in available_genres()


def test_poetry_touch_is_spelling_only():
    cfg, locks = apply_stage(load_config(CONFIG), "poetry-touch")
    keys = [k if isinstance(k, str) else (k.get("group") if isinstance(k, dict) else list(k))
            for k in cfg.error_types]
    flat = []
    for k in keys:
        flat.extend(k if isinstance(k, list) else [k])
    assert flat == ["spelling"]
    assert list(cfg.sweeps) == []
    assert cfg.spellcheck.enabled is True
    assert cfg.languagetool.enabled is False
    assert cfg.repair.enabled is False
    assert cfg.smoothing.enabled is False and cfg.smoothing.edits is False
    assert cfg.rewrite.enabled is False
    assert cfg.consistency.enabled is False
    assert cfg.residuals.enabled is False
    assert cfg.meaning_check.enabled is False and cfg.fix_check.enabled is False
    assert cfg.candidate_screening.mode == "off"
    assert not cfg.ensemble.enabled
    assert cfg.flights.posture == "strict"
    assert locks  # the stage locks its lanes


def test_poetry_locks_hold_against_a_permissive_genre():
    cfg, locks = apply_stage(load_config(CONFIG), "poetry-touch")
    cfg = apply_genre(cfg, "self_help_business")
    cfg = cfg[0] if isinstance(cfg, tuple) else cfg
    violated = enforce_locks(cfg, locks)
    assert cfg.smoothing.enabled is False and cfg.rewrite.enabled is False
    assert cfg.repair.enabled is False
    assert "smoothing.enabled" in violated or cfg.smoothing.enabled is False


def test_poetry_genre_sets_the_softest_posture():
    cfg = apply_genre(load_config(CONFIG), "poetry")
    cfg = cfg[0] if isinstance(cfg, tuple) else cfg
    assert cfg.flights.posture == "strict"
    assert cfg.smoothing.enabled is False and cfg.rewrite.enabled is False
