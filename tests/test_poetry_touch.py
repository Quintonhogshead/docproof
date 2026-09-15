"""Poetry takes house MECHANICS and never a change to its STRUCTURE: the
`poetry-touch` stage runs the character- and word-level typed passes and the
glyph/spacing/word sweeps, and holds every sentence-level lane shut —
terminal periods, doubled words, dialogue rules, LanguageTool, repair,
smoothing, rewrite, consistency queries, the gates. The locked lanes stay off
under any genre. (Head proofreader's Dalton change list, 2026-09-15.)"""
from __future__ import annotations

from pathlib import Path

from docproof.config import load_config
from docproof.genre import apply_genre, available_genres
from docproof.stages import apply_stage, available_stages, enforce_locks

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


def test_poetry_stage_and_genre_ship():
    assert "poetry-touch" in available_stages()
    assert "poetry" in available_genres()


VERSE_TYPES = ["spelling", "homophone_confusion", "apostrophe_error", "capitalization",
               "serial_comma", "number_style", "missing_word", "ly_adverb_hyphen"]
VERSE_SWEEPS = ["sweep_ellipsis", "sweep_dash", "sweep_elision_apostrophe", "sweep_quote_pair",
                "sweep_trailing_space", "sweep_time_of_day", "sweep_compound_number",
                "sweep_century", "sweep_decade_apostrophe", "sweep_initialism"]
# Sentence-level judgments a poem never receives.
STRUCTURAL_SWEEPS = {"sweep_terminal_period", "sweep_doubled_word", "sweep_stacked_punctuation",
                     "sweep_dialogue_tag", "sweep_dialogue_splice", "sweep_quote_punctuation",
                     "sweep_nested_quote", "sweep_deity_capital"}


def test_poetry_touch_is_house_mechanics_never_structure():
    cfg, locks = apply_stage(load_config(CONFIG), "poetry-touch")
    keys = [k if isinstance(k, str) else (k.get("group") if isinstance(k, dict) else list(k))
            for k in cfg.error_types]
    flat = []
    for k in keys:
        flat.extend(k if isinstance(k, list) else [k])
    assert flat == VERSE_TYPES
    assert not {"comma_splice", "run_on_sentence", "subject_verb_agreement", "tense_shift",
                "dialogue_tag", "terminal_mark", "repeated_word", "unnecessary_comma"} & set(flat)
    assert list(cfg.sweeps) == VERSE_SWEEPS
    assert not STRUCTURAL_SWEEPS & set(cfg.sweeps)
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
