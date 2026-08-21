"""Named, reproducible review configurations.

Profiles are deliberately applied to a fully loaded :class:`Config`.  That
keeps the house taxonomy and prompts in one place while making an experiment's
boundaries explicit.  The resolved config is what batch manifests persist, so
submit and collect cannot drift if the house defaults change while a job is at
the provider.
"""
from __future__ import annotations

from .config import (Config, examination_graph_killed,
                     examination_production_verdicts_killed)


DETECTOR_ONLY = "detector-only"
PROFILE_KEYS = (DETECTOR_ONLY,)

# These detector types intentionally produce questions instead of edits.  The
# detector-only profile promises a manuscript containing tracked revisions and
# no margin-query channel, so they cannot ride an otherwise edit-capable group.
QUERY_ONLY_TYPES = frozenset({
    "speaker_change", "terminal_mark", "general_error", "word_echo",
})


def apply_profile(cfg: Config, profile: str | None) -> Config:
    """Apply ``profile`` in place and return ``cfg`` for convenient chaining."""
    if not profile:
        return cfg
    if profile != DETECTOR_ONLY:
        raise ValueError(
            f"Unknown review profile {profile!r}; choose from "
            f"{', '.join(PROFILE_KEYS)}")
    return apply_detector_only(cfg)


def apply_detector_only(cfg: Config) -> Config:
    """Isolate the production detector and emit tracked changes only.

    Phase 2 remains an observation layer: its explicit paragraph receipts are
    required and reported, but it cannot create a finding or edit.  The edit
    guard and reject-all audit stay on because they are integrity checks, not
    alternative edit sources.
    """
    cfg.api.model = "gpt-5.6-luna"
    cfg.api.effort = "low"

    # No silent document rewriting and no comment/query/change-log channels.
    cfg.normalize.quotes = False
    cfg.normalize.spaces = False
    cfg.style.unclosed_quote_queries = False
    cfg.style.heading_title_case = False
    cfg.comments = False
    cfg.query_comments = False
    cfg.not_applied_comments = False
    cfg.excluded_words_comment = False
    cfg.change_log = False
    cfg.report_explanations = False

    # No mechanical, whole-book, second-opinion, or hosted add-on sources.
    for stage in (
        cfg.spellcheck, cfg.consistency, cfg.glossary, cfg.storysheet,
        cfg.continuity, cfg.chapter_continuity, cfg.adjudicate, cfg.rewrite,
        cfg.languagetool, cfg.sapling, cfg.smoothing, cfg.factcheck,
        cfg.residuals, cfg.meaning_check, cfg.fix_check,
    ):
        stage.enabled = False
    cfg.sweeps = []
    cfg.low_confidence.confirm = False
    cfg.ensemble.detectors = []
    cfg.ensemble.verifier_model = None
    cfg.rounds.count = 1

    # Preserve the house grouping for the edit-capable core categories while
    # removing types whose only valid output is a margin question.
    cfg.error_types = [
        [key for key in group if key not in QUERY_ONLY_TYPES]
        for group in cfg.error_type_groups
        if any(key not in QUERY_ONLY_TYPES for key in group)
    ]

    cfg.edit_guard.enabled = True
    cfg.audit = "strict"

    # Deployment brakes still win.  A profile must never re-enable a killed
    # experiment merely because it is applied after load_config().
    graph_on = not examination_graph_killed()
    receipts_on = graph_on and not examination_production_verdicts_killed()
    cfg.examination_graph.enabled = graph_on
    cfg.examination_graph.production_verdicts = receipts_on
    cfg.examination_graph.spell_sites = False
    cfg.examination_graph.judgment.enabled = False
    return cfg
