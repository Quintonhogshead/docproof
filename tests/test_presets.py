"""The effort-tier presets: pure-data assertions plus one endpoint smoke test.
No network, no keys — the bundles are static configuration."""
from __future__ import annotations

from app.features import FEATURES_BY_ID
from app.presets import (TIERS, TIERS_BY_ID, DEFAULT_TIER_ID,
                         LADDER_FEATURE_IDS, CANDIDATE_ONLY_PRESET,
                         DETECTOR_ONLY_PRESET, LIGHT_TOUCH, STANDARD, HARD,
                         THE_HAMMER, REVIEWER)
from app.settings import Settings, EFFORT_LEVELS, CONFIG_PATH
from docproof.config import load_config
from docproof.providers.catalog import lookup

CONFIDENCE = {"low", "medium", "high"}


def test_six_tiers_present_with_exact_names():
    assert [t.id for t in TIERS] == [
        "detector", "candidate", "light", "standard", "hard", "hammer"]
    assert {t.id: t.name for t in TIERS} == {
        "detector": "Detector only", "candidate": "Candidate detector",
        "light": "Light touch",
        "standard": "Standard",
        "hard": "Hard", "hammer": "The hammer"}
    assert DEFAULT_TIER_ID == "standard" and "standard" in TIERS_BY_ID


def test_ladder_feature_ids_are_all_real():
    for fid in LADDER_FEATURE_IDS:
        assert fid in FEATURES_BY_ID          # id survives a FEATURES rename


def test_every_feature_key_is_a_real_feature_id():
    for t in TIERS:
        for keyed in (False, True):
            for fid in t.features(keyed):
                assert fid in FEATURES_BY_ID
            if t not in (DETECTOR_ONLY_PRESET, CANDIDATE_ONLY_PRESET):
                # Ordinary effort tiers govern exactly the ladder ids.
                assert set(t.features(keyed)) == set(LADDER_FEATURE_IDS)


def test_every_model_id_is_in_the_catalog():
    for t in TIERS:
        assert t.model == REVIEWER            # reviewer fixed by product decision
        for m in (t.model, t.glossary_model, t.judge_model,
                  t.meaning_model, t.fix_model):
            if m and m != "off":
                assert lookup(m) is not None, m


def test_control_domains():
    for t in TIERS:
        assert t.effort in EFFORT_LEVELS
        assert t.min_confidence in CONFIDENCE
        assert 1 <= t.rounds <= 4


def test_standard_is_shipped_defaults_plus_languagetool():
    s = Settings()
    cfg = load_config(CONFIG_PATH)
    p = STANDARD.to_payload(sapling_keyed=False)
    # scalar controls == the app's shipped defaults
    assert p["model"] == s.model == "gpt-5.6-luna"
    assert p["effort"] == s.effort == "medium"
    assert p["glossary_model"] == s.glossary_model == "gpt-5.6-luna"
    assert p["rounds"] == s.rounds == 1
    assert p["min_confidence"] == s.min_confidence == "medium"
    assert p["judge_model"] is None
    assert p["meaning_model"] is None and p["fix_model"] is None
    # every pass is at its shipped-off default EXCEPT languagetool, which
    # Standard lifts to on — the single delta from the shipped baseline
    for fid in ("candidate_screening", "storysheet", "continuity", "rewrite", "sapling",
                "meaning_check", "fix_check"):
        assert FEATURES_BY_ID[fid].read(cfg) is False   # shipped default: off
        assert p["features"][fid] is False
    assert FEATURES_BY_ID["languagetool"].read(cfg) is False  # shipped: off
    assert p["features"]["languagetool"] is True             # Standard: on


def test_light_touch_turns_everything_off_at_low_effort():
    p = LIGHT_TOUCH.to_payload(sapling_keyed=True)   # even keyed, sapling stays off
    assert p["effort"] == "low"
    assert all(v is False for v in p["features"].values())


def test_detector_only_is_an_explicit_tracked_changes_profile():
    p = DETECTOR_ONLY_PRESET.to_payload(sapling_keyed=True)
    assert p["profile"] == "detector-only"
    assert p["effort"] == "low" and p["rounds"] == 1
    assert p["glossary_model"] == "off"
    for fid in ("comments", "query_comments", "not_applied_comments",
                "change_log", "report_explanations", "normalize_quotes",
                "normalize_spaces", "adjudicate", "spellcheck", "sapling"):
        assert p["features"][fid] is False
    assert p["features"]["edit_guard"] is True
    assert p["features"]["audit"] is True
    assert p["features"]["examination_graph"] is True
    assert p["features"]["examination_judgment"] is False


def test_candidate_detector_is_an_explicit_standalone_profile():
    p = CANDIDATE_ONLY_PRESET.to_payload(sapling_keyed=True)
    assert p["profile"] == "candidate-only"
    assert p["features"]["candidate_screening"] is True
    assert p["effort"] == "low" and p["rounds"] == 1
    for fid in ("storysheet", "continuity", "rewrite", "languagetool",
                "sapling", "meaning_check", "fix_check", "comments",
                "examination_graph"):
        assert p["features"][fid] is False
    assert p["features"]["edit_guard"] is True
    assert p["features"]["audit"] is True


def test_hard_bundle():
    p = HARD.to_payload(sapling_keyed=False)
    assert p["effort"] == "high" and p["rounds"] == 2
    assert p["judge_model"] == "claude-opus-5"        # explicit override of the
    assert load_config(CONFIG_PATH).rounds.judge_model == "gpt-5.6-sol"  # default
    assert p["glossary_model"] == "gpt-5.6-luna"
    assert p["features"]["storysheet"] and p["features"]["continuity"]
    assert p["features"]["languagetool"] and p["features"]["meaning_check"]
    assert p["features"]["rewrite"] is False and p["features"]["fix_check"] is False
    # meaning gate pinned to a frontier judge explicitly; the server default is
    # now the house reviewer, so a frontier tier must name its own judge.
    assert p["meaning_model"] == "claude-fable-5"
    assert load_config(CONFIG_PATH).meaning_check.model == "gpt-5.6-luna"


def test_hammer_bundle():
    p = THE_HAMMER.to_payload(sapling_keyed=False)
    assert p["effort"] == "high" and p["rounds"] == 3
    assert p["glossary_model"] == "claude-opus-5"
    assert p["judge_model"] == "claude-opus-5"
    assert all(p["features"][f] for f in LADDER_FEATURE_IDS)   # every toggle on
    # both gates pinned to a frontier judge explicitly; server default is now Luna
    assert p["meaning_model"] == "claude-fable-5"
    assert p["fix_model"] == "claude-fable-5"
    assert load_config(CONFIG_PATH).fix_check.model == "gpt-5.6-luna"


def test_sapling_policy_resolution():
    # Hard: on ONLY when keyed
    assert HARD.features(False)["sapling"] is False
    assert HARD.features(True)["sapling"] is True
    # Hammer: on unconditionally
    assert THE_HAMMER.features(False)["sapling"] is True
    assert THE_HAMMER.features(True)["sapling"] is True
    # Light / Standard: never
    assert LIGHT_TOUCH.features(True)["sapling"] is False
    assert STANDARD.features(True)["sapling"] is False


def test_endpoint_shape(tmp_path):
    # Uses the repo's real app factory (create_app) and its TestClient harness.
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(tmp_path, start_runner=False)
    with TestClient(app) as client:
        body = client.get("/api/presets").json()
    assert body["default"] == "standard"
    ids = [t["id"] for t in body["tiers"]]
    assert ids == [
        "detector", "candidate", "light", "standard", "hard", "hammer"]
    for t in body["tiers"]:
        assert set(t["features"]) <= set(FEATURES_BY_ID)
        assert t["sapling"] in ("off", "if_keyed", "always")
        assert set(t["controls"]) == {
            "model", "effort", "glossary_model", "rounds", "judge_model",
            "meaning_model", "fix_model", "min_confidence", "profile"}
    assert sum(t["default"] for t in body["tiers"]) == 1
