"""The effort-tier ladder: named bundles of per-run controls.

A tier is a CLIENT-SIDE MACRO. Selecting one pre-fills the review panel's
existing controls (reviewer model, effort, glossary picker, rounds, judge /
meaning / fix pickers, the pass checkboxes) with a bundle's values; the body
POST /api/jobs receives those resolved controls. Any later deviation from the
selected tier's bundle is the implicit additional state, "Custom".

Every feature id below is a real FeatureSpec id (app/features.FEATURES_BY_ID);
every model id is a real catalog id (docproof.providers.catalog). The pytest in
tests/test_presets.py asserts both, so a rename in either place fails loudly here
rather than at run time.

Reviewer stays gpt-5.6-luna at every tier by product decision: depth comes from
effort, rounds and passes, never a dearer detector.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from docproof.profiles import CANDIDATE_ONLY, DETECTOR_ONLY

# The reviewer is fixed across the whole ladder by product decision.
REVIEWER = "gpt-5.6-luna"

# The seven ladder-controlled FEATURES ids, in ladder order. Each MUST be in
# app.features.FEATURES_BY_ID (asserted in tests).
LADDER_FEATURE_IDS = ("candidate_screening", "storysheet", "continuity",
                      "rewrite", "languagetool", "sapling", "meaning_check",
                      "fix_check")

# The picker opens here — the house default.
DEFAULT_TIER_ID = "standard"

SaplingPolicy = Literal["off", "if_keyed", "always"]


@dataclass(frozen=True)
class TierPreset:
    id: str
    name: str
    blurb: str

    # --- scalar per-run controls (map 1:1 onto JobRequest fields) ---
    model: str                        # -> JobRequest.model   (the reviewer)
    effort: str                       # -> JobRequest.effort  (one of EFFORT_LEVELS)
    glossary_model: str               # -> JobRequest.glossary_model (catalog id or "off")
    rounds: int                       # -> JobRequest.rounds  (1..4)
    min_confidence: str = "medium"    # -> JobRequest.min_confidence
    profile: str = ""                 # -> JobRequest.profile
    judge_model: str | None = None    # -> JobRequest.judge_model  (only vetted when rounds>1)
    meaning_model: str | None = None  # -> JobRequest.meaning_model (None => server default)
    fix_model: str | None = None      # -> JobRequest.fix_model     (None => server default)

    # --- the six always-boolean pass toggles (sapling handled separately) ---
    storysheet: bool = False
    continuity: bool = False
    rewrite: bool = False
    languagetool: bool = False
    meaning_check: bool = False
    fix_check: bool = False
    candidate_screening: bool = False

    # --- the one key-dependent toggle ---
    # "off"      never runs Sapling.
    # "if_keyed" runs it only when a Sapling key is configured, and the client's
    #            estimate reflects it only then.
    # "always"   runs it unconditionally; a keyless run skips it gracefully
    #            (SaplingConfig) and the tier card shows the missing-key note.
    sapling: SaplingPolicy = "off"
    # Additional switches for a purpose-built profile. Ordinary effort tiers
    # govern only the ladder toggles; isolated profiles also make every output
    # and safety state visible in the panel so their promise is obvious.
    extra_features: tuple[tuple[str, bool], ...] = ()

    def feature_controls(self) -> dict[str, bool]:
        values = {
            "candidate_screening": self.candidate_screening,
            "storysheet": self.storysheet,
            "continuity": self.continuity,
            "rewrite": self.rewrite,
            "languagetool": self.languagetool,
            "meaning_check": self.meaning_check,
            "fix_check": self.fix_check,
        }
        values.update(dict(self.extra_features))
        return values

    def features(self, sapling_keyed: bool) -> dict[str, bool]:
        """The {feature_id: bool} map, Sapling resolved against live key state."""
        sap = self.sapling == "always" or (self.sapling == "if_keyed" and sapling_keyed)
        values = self.feature_controls()
        values["sapling"] = sap
        return values

    def to_payload(self, *, sapling_keyed: bool) -> dict:
        """The subset of the POST /api/jobs body this tier pins. The client
        merges it over the panel's current request; keys not present here (e.g.
        variant, mode, file_ids) are the panel's own. `sapling_keyed` is
        /api/models -> keys['sapling']['configured']."""
        return {
            "model": self.model,
            "effort": self.effort,
            "glossary_model": self.glossary_model,
            "rounds": self.rounds,
            "judge_model": self.judge_model,
            "meaning_model": self.meaning_model,
            "fix_model": self.fix_model,
            "min_confidence": self.min_confidence,
            "profile": self.profile,
            "features": self.features(sapling_keyed),
        }

    def to_json(self) -> dict:
        """Declarative form for GET /api/presets. The client keeps the sapling
        POLICY and resolves it live against key status, so adding a Sapling key
        under Admin changes Hard's behaviour without a page reload."""
        return {
            "id": self.id,
            "name": self.name,
            "blurb": self.blurb,
            "default": self.id == DEFAULT_TIER_ID,
            "controls": {
                "model": self.model,
                "effort": self.effort,
                "glossary_model": self.glossary_model,
                "rounds": self.rounds,
                "judge_model": self.judge_model,
                "meaning_model": self.meaning_model,
                "fix_model": self.fix_model,
                "min_confidence": self.min_confidence,
                "profile": self.profile,
            },
            "features": self.feature_controls(),
            "sapling": self.sapling,
        }


DETECTOR_ONLY_PRESET = TierPreset(
    id="detector",
    name="Detector only",
    blurb="The core detector adds Word tracked changes. No comments, silent "
          "normalization, or extra passes; defaults to overnight batch pricing.",
    model=REVIEWER,
    effort="low",
    glossary_model="off",
    rounds=1,
    min_confidence="medium",
    profile=DETECTOR_ONLY,
    extra_features=(
        ("factcheck", False), ("chapter_continuity", False),
        ("adjudicate", False), ("consistency", False),
        ("spellcheck", False), ("heading_case", False),
        ("residuals", False), ("smoothing", False),
        ("comments", False), ("sapling_comments", False),
        ("query_comments", False), ("not_applied_comments", False),
        ("change_log", False), ("report_explanations", False),
        ("normalize_quotes", False), ("normalize_spaces", False),
        ("edit_guard", True), ("audit", True),
        ("examination_graph", True), ("examination_judgment", False),
    ),
)


CANDIDATE_ONLY_PRESET = TierPreset(
    id="candidate",
    name="Candidate detector",
    blurb="The generate-and-screen detector runs alone, applying only "
          "correction-validated errors as guarded Word tracked changes.",
    model=REVIEWER,
    effort="low",
    glossary_model="off",
    rounds=1,
    min_confidence="medium",
    profile=CANDIDATE_ONLY,
    candidate_screening=True,
    extra_features=(
        ("factcheck", False), ("chapter_continuity", False),
        ("adjudicate", False), ("consistency", False),
        ("spellcheck", False), ("heading_case", False),
        ("residuals", False), ("smoothing", False),
        ("comments", False), ("sapling_comments", False),
        ("query_comments", False), ("not_applied_comments", False),
        ("change_log", False), ("report_explanations", False),
        ("normalize_quotes", False), ("normalize_spaces", False),
        ("edit_guard", True), ("audit", True),
        ("examination_graph", False), ("examination_judgment", False),
    ),
)


LIGHT_TOUCH = TierPreset(
    id="light",
    name="Light touch",
    blurb="A fast, cheap single pass — spelling, typos and the obvious "
          "mechanics, nothing heavy.",
    model=REVIEWER,
    effort="low",
    glossary_model="gpt-5.6-luna",
    rounds=1,
    min_confidence="medium",
    # every pass toggle off
    storysheet=False, continuity=False, rewrite=False, languagetool=False,
    meaning_check=False, fix_check=False, sapling="off",
)

STANDARD = TierPreset(
    id="standard",
    name="Standard",
    blurb="The house default: a full single review with the mechanical floor on.",
    model=REVIEWER,
    effort="medium",
    glossary_model="gpt-5.6-luna",
    rounds=1,
    min_confidence="medium",
    languagetool=True,                # the only lift over the shipped defaults
    storysheet=False, continuity=False, rewrite=False,
    meaning_check=False, fix_check=False, sapling="off",
)

HARD = TierPreset(
    id="hard",
    name="Hard",
    blurb="Two judged rounds, whole-book context and continuity, and a meaning "
          "check on every change.",
    model=REVIEWER,
    effort="high",
    glossary_model="gpt-5.6-luna",
    rounds=2,
    judge_model="claude-opus-5",      # explicit override of the gpt-5.6-sol default
    meaning_model="claude-fable-5",   # explicit frontier gate: the server default is
                                      # now the house reviewer (gpt-5.6-luna), so a tier
                                      # that wants a frontier judge must pin it here
    min_confidence="medium",
    storysheet=True,
    continuity=True,                  # NB: whole-book continuity has no per-run picker,
                                      # so it follows the Luna default even here
    languagetool=True,
    meaning_check=True,
    rewrite=False,
    fix_check=False,
    sapling="if_keyed",               # only when a Sapling key is configured
)

THE_HAMMER = TierPreset(
    id="hammer",
    name="The hammer",
    blurb="Everything on: three judged rounds, a frontier glossary read, "
          "rewrite-and-compare, and both final judge gates.",
    model=REVIEWER,
    effort="high",
    glossary_model="claude-opus-5",   # the one tier that pays for a frontier glossary
    rounds=3,
    judge_model="claude-opus-5",
    meaning_model="claude-fable-5",   # both gates pinned to a frontier judge explicitly;
    fix_model="claude-fable-5",       # the server default is now gpt-5.6-luna
    min_confidence="medium",
    candidate_screening=True,
    storysheet=True,
    continuity=True,
    rewrite=True,
    languagetool=True,
    meaning_check=True,
    fix_check=True,
    sapling="always",                 # unconditional; keyless run skips it gracefully
)

TIERS: tuple[TierPreset, ...] = (
    DETECTOR_ONLY_PRESET, CANDIDATE_ONLY_PRESET, LIGHT_TOUCH, STANDARD, HARD,
    THE_HAMMER)
TIERS_BY_ID: dict[str, TierPreset] = {t.id: t for t in TIERS}
