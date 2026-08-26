"""Proper-noun triage and the profile-correction overlay
(docproof/profile_corrections.py): the raw profile is preserved, corrections
overlay it, and only vetted names seed the config."""
from __future__ import annotations

from pathlib import Path

import pytest

from docproof.genre import materialize_genre_pack
from docproof.genre_profile import (ChapterProfile, Profile,
                                     ProperNounCandidate)
from docproof.profile_corrections import (NOUN_CLASSES, ProfileCorrections,
                                          apply_corrections, seedable_names,
                                          triage_proper_nouns)

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


def _profile(nouns):
    return Profile(source="b.docx", word_count=1000, paragraph_count=50,
                   proper_nouns=[ProperNounCandidate(name=n, count=c)
                                 for n, c in nouns])


# --- triage ------------------------------------------------------------------

def test_triage_flags_near_matches_as_suspect():
    p = _profile([("Deut", 3), ("Deute", 5), ("Zylandria", 6)])
    by = {e.name: e.suggested_class for e in triage_proper_nouns(p)}
    assert by["Deut"] == "suspect"
    assert by["Deute"] == "suspect"
    assert by["Zylandria"] == "protect"


def test_triage_rejects_a_common_word():
    p = _profile([("River", 12)])
    assert triage_proper_nouns(p)[0].suggested_class == "reject"


def test_triage_enforces_a_frequent_invented_name():
    p = _profile([("Yahweh", 40)])
    assert triage_proper_nouns(p)[0].suggested_class == "enforce"


def test_triage_flags_tokenization_artifacts():
    p = _profile([("X", 2), ("Cap3", 1), ("--dash", 1)])
    classes = {e.name: e.suggested_class for e in triage_proper_nouns(p)}
    assert classes["X"] == "suspect"
    assert classes["Cap3"] == "suspect"
    assert classes["--dash"] == "suspect"


def test_triage_output_is_stable_by_count_then_name():
    p = _profile([("Bane", 5), ("Aria", 5), ("Zed", 9)])
    order = [e.name for e in triage_proper_nouns(p)]
    assert order == ["Zed", "Aria", "Bane"]


# --- the overlay preserves the raw profile -----------------------------------

def test_apply_corrections_never_mutates_the_raw_profile():
    p = _profile([("Deut", 3), ("Yahweh", 40)])
    corr = ProfileCorrections(proper_noun_classes={"Deut": "reject"},
                              recommended_preset="religious")
    corrected = apply_corrections(p, corr)
    assert len(p.proper_nouns) == 2                 # raw untouched
    assert p.recommended_preset == "general_fiction"
    assert corrected.recommended_preset == "religious"
    assert [n.name for n in corrected.proper_nouns] == ["Yahweh"]


def test_corrected_chapter_map_replaces_the_deterministic_one():
    p = _profile([("A", 1)])
    p.chapters = [ChapterProfile(index=0, title="Wrong", word_count=100),
                  ChapterProfile(index=1, title="Split", word_count=100)]
    corr = ProfileCorrections(
        chapters=[ChapterProfile(index=0, title="One True Chapter",
                                 word_count=200)])
    corrected = apply_corrections(p, corr)
    assert len(p.chapters) == 2                       # raw untouched
    assert [c.title for c in corrected.chapters] == ["One True Chapter"]


def test_seedable_names_drops_reject_and_suspect_keeps_unclassified():
    p = _profile([("Deut", 3), ("Deute", 5), ("Yahweh", 40), ("Neona", 2)])
    corr = ProfileCorrections(proper_noun_classes={
        "Deut": "reject", "Deute": "suspect", "Yahweh": "enforce"})
    # Neona is unclassified -> kept.
    assert set(seedable_names(p, corr)) == {"Yahweh", "Neona"}


def test_seedable_names_without_corrections_keeps_all():
    p = _profile([("A", 1), ("B", 2)])
    assert set(seedable_names(p)) == {"A", "B"}


def test_bad_noun_class_is_refused_at_construction():
    with pytest.raises(ValueError, match="proper_noun_classes"):
        ProfileCorrections(proper_noun_classes={"X": "banish"})


def test_noun_classes_constant():
    assert set(NOUN_CLASSES) == {"protect", "enforce", "reject", "suspect"}


# --- genre-pack integration --------------------------------------------------

def test_genre_pack_seeds_only_vetted_names_with_corrections():
    p = _profile([("Deut", 3), ("Yahweh", 40), ("Zylandria", 6)])
    corr = ProfileCorrections(proper_noun_classes={"Deut": "reject"})
    cfg, summary = materialize_genre_pack(
        CONFIG, "religious", profile=p, corrections=corr)
    assert "Deut" not in cfg.consistency.seeded_names
    assert {"Yahweh", "Zylandria"} <= set(cfg.consistency.seeded_names)
    assert summary.get("corrections_applied") is True


def test_genre_pack_without_corrections_seeds_all():
    p = _profile([("Deut", 3), ("Yahweh", 40)])
    cfg, _ = materialize_genre_pack(CONFIG, "religious", profile=p)
    assert {"Deut", "Yahweh"} <= set(cfg.consistency.seeded_names)
