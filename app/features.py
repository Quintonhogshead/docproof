"""The optional passes a review can turn on or off, as one catalogue.

Every togglable feature in the pipeline is an `enabled` flag (or, for the
audit, a mode) somewhere in `docproof.config.Config`. This module is the single
place that maps a short, stable id — the thing the submission panel shows and
the job record carries — onto the config field it flips. Keep the catalogue and
the config in step here, not in three separate places: the API that renders the
switches, the validation that rejects a bad id, and the wiring that applies the
choice to a run all read from `FEATURES`.

A per-run choice is a `{id: bool}` map on the job. Applying it is
last-writer-wins over the loaded config, so a switch the user leaves alone (sent
at its current value) is a harmless no-op, and one they flip is a visible,
deliberate change to this one review.

Ensemble review is deliberately absent: it is not a boolean — it needs a roster
of detector models and a verifier — so a bare switch cannot express it. It stays
a config-file choice until it earns a UI of its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from docproof.config import Config


def _get(cfg: Config, path: tuple[str, ...]):
    obj = cfg
    for part in path:
        obj = getattr(obj, part)
    return obj


def _set(cfg: Config, path: tuple[str, ...], value) -> None:
    obj = cfg
    for part in path[:-1]:
        obj = getattr(obj, part)
    setattr(obj, path[-1], value)          # validate_assignment re-checks it


@dataclass(frozen=True)
class FeatureSpec:
    """One switch. `path` is where its value lives in the config tree; `on_value`
    and `off_value` are what the two switch positions write there — booleans for
    the ordinary enabled flags, and the two audit modes for the audit, which is a
    string but reads as on/off to a person."""
    id: str
    label: str
    blurb: str
    group: str                              # "pass" | "output" | "safety"
    path: tuple[str, ...]
    # Materially changes what a review costs (a whole-book read, a second pass),
    # so the panel warns the estimate does not include it.
    heavy: bool = False
    on_value: object = True
    off_value: object = False

    def read(self, cfg: Config) -> bool:
        return _get(cfg, self.path) != self.off_value

    def write(self, cfg: Config, on: bool) -> None:
        _set(cfg, self.path, self.on_value if on else self.off_value)


FEATURES: tuple[FeatureSpec, ...] = (
    # -- extra reads and passes, off by default: recall for cost --------------
    FeatureSpec(
        "storysheet", "Story sheet — tense & pronoun context",
        "One whole-book read that tells the page-by-page pass the story's tense, "
        "point of view, and each character's pronouns, so it can catch a tense "
        "slip or wrong pronoun that reads fine in a single paragraph. Adds a "
        "whole-book read.", "pass", ("storysheet", "enabled"), heavy=True),
    FeatureSpec(
        "continuity", "Continuity read — contradictions across the book",
        "One whole-book read that flags facts the manuscript contradicts about "
        "itself — a timeline that doesn't add up, an age or date that breaks, an "
        "eye colour or a name that drifts. Asks, never edits. Adds a whole-book "
        "read; a free date-versus-weekday check rides along.",
        "pass", ("continuity", "enabled"), heavy=True),
    FeatureSpec(
        "rewrite", "Rewrite-and-compare pass",
        "The model retypes each paragraph with the smallest possible edits, and "
        "the differences become candidates a skeptical confirm step rules on. A "
        "strong recall lever — but roughly a second full pass in cost.",
        "pass", ("rewrite", "enabled"), heavy=True),
    FeatureSpec(
        "languagetool", "LanguageTool mechanical floor",
        "A local rules checker (commas, missing words, compound-modifier "
        "hyphenation) whose suggestions the confirm step vets in context. No "
        "per-call cost; runs on the server's own Java.",
        "pass", ("languagetool", "enabled")),
    FeatureSpec(
        "sapling", "Sapling grammar check",
        "Also run the manuscript through Sapling.ai's hosted grammar and "
        "spelling checker and fold its suggestions into the tracked changes, "
        "after the house-style sweeps (which keep any span they both touch). A "
        "third-party service billed per character — its cost is added to the "
        "estimate separately from the model.",
        "pass", ("sapling", "enabled"), heavy=True),
    FeatureSpec(
        "smoothing", "Line-editing suggestions (smoothing)",
        "A separate line editor reads the whole manuscript and proposes small "
        "smoothings — a word doing no work, an unidiomatic preposition, an "
        "awkward clause — which a skeptical taste judge then culls. Every one "
        "reaches the author as a margin QUESTION, never a tracked change: a "
        "smoothing has no single right answer, only a better wording, and that "
        "is the author's call. A full-book read plus a judge, billed on top of "
        "the estimate. Tune how much it surfaces with the proposer and judge "
        "controls in Advanced options.",
        "pass", ("smoothing", "enabled"), heavy=True),
    FeatureSpec(
        "meaning_check", "Meaning check — read every change before it ships",
        "One strong model reads every correction this run would make — the "
        "model's own, Sapling's, LanguageTool's, all of them together — and asks "
        "the one thing the other checks do not: does the sentence still mean what "
        "the author meant? Anything it will not vouch for becomes a margin "
        "question instead of a silent change. Costs by the number of changes, not "
        "the length of the book.",
        "pass", ("meaning_check", "enabled"), heavy=True),
    FeatureSpec(
        "fix_check", "Fix check — read every change before it ships",
        "The same last read, asking the other question: is the correction "
        "actually right? A fix can leave the meaning perfectly intact and still "
        "be the wrong repair — “their” corrected to “there” where "
        "“they're” was wanted, a verb put in the wrong form, a semicolon "
        "the clauses won't carry. Anything it won't vouch for becomes a margin "
        "question instead of a silent change.",
        "pass", ("fix_check", "enabled"), heavy=True),
    # -- passes on by default: cheap, local-ish, here so they can be turned off -
    FeatureSpec(
        "adjudicate", "Real-word typo adjudication",
        "Finds real-word typos the plain passes glide over (a “staired” "
        "for “stared”) and rules on each in context — an affirmed fix "
        "edits, a softer call only asks.", "pass", ("adjudicate", "enabled")),
    FeatureSpec(
        "consistency", "Consistency scan",
        "One term spelled two ways across the whole book — asks about it, or "
        "corrects a clearly dominant name spelling. Needs the whole document at "
        "once, so a page-by-page pass cannot do it.",
        "pass", ("consistency", "enabled")),
    FeatureSpec(
        "spellcheck", "Spell scan (vocabulary)",
        "Builds the book's own vocabulary as a do-not-flag list for the model "
        "passes, so a coined name is not read as a typo. Classifies, never "
        "edits.", "pass", ("spellcheck", "enabled")),
    # -- what the run writes ---------------------------------------------------
    FeatureSpec(
        "comments", "Margin comments",
        "Findings that ask rather than correct become Word margin comments. Off "
        "leaves them in the summary only.", "output", ("comments",)),
    FeatureSpec(
        "sapling_comments", "Explain Sapling changes",
        "Give each Sapling grammar edit a margin comment naming what it fixed. "
        "Off applies Sapling's changes silently as tracked changes, with nothing "
        "in the margin. No effect unless the Sapling pass is on.",
        "output", ("sapling", "comments")),
    FeatureSpec(
        "query_comments", "Below-gate queries as comments",
        "Also surface softer, below-threshold findings as margin queries, "
        "instead of only in summary.md where an editor alone would see them.",
        "output", ("query_comments",)),
    FeatureSpec(
        "change_log", "Change-log document",
        "The Word change log handed to the author alongside the manuscript: what "
        "changed, what was only asked about, and what this pass did not cover.",
        "output", ("change_log",)),
    FeatureSpec(
        "report_explanations", "Explain each change",
        "Ask the model to justify each finding. The reasons are most of the "
        "output tokens — which bill at roughly 5× input — so off is a "
        "materially cheaper pass that still applies every correction.",
        "output", ("report_explanations",)),
    FeatureSpec(
        "normalize_quotes", "Curl straight quotes",
        "Convert straight quotes to curly before the review, where the direction "
        "is unambiguous. Silent and not tracked, by house brief.",
        "output", ("normalize", "quotes")),
    FeatureSpec(
        "normalize_spaces", "Collapse double spaces",
        "Collapse runs of two or more spaces to one before the review. Silent "
        "and not tracked.", "output", ("normalize", "spaces")),
    # -- safety nets: on, and here with a warning because off is a real risk ---
    FeatureSpec(
        "edit_guard", "Overstep guard",
        "Reject any single “fix” that rewrites too large a span or adds "
        "too much text — the guard against the model paraphrasing instead of "
        "proofreading. Leave it on unless you know why you are turning it off.",
        "safety", ("edit_guard", "enabled")),
    FeatureSpec(
        "audit", "Reject-all integrity audit",
        "Refuse to ship a file where any text changed without a tracked change "
        "around it. This is the one check that catches a silent edit; turning it "
        "off removes that safety net entirely.",
        "safety", ("audit",), on_value="strict", off_value="off"),
)

FEATURES_BY_ID: dict[str, FeatureSpec] = {f.id: f for f in FEATURES}


def apply_features(cfg: Config, features: dict | None) -> None:
    """Write a per-run `{id: bool}` map onto a loaded config. Unknown ids are
    ignored here — the API rejects them at the door, and a job record written by
    a newer build should not crash an older one that has dropped a feature."""
    for fid, on in (features or {}).items():
        spec = FEATURES_BY_ID.get(fid)
        if spec is not None:
            spec.write(cfg, bool(on))


def unknown_features(features: dict | None) -> list[str]:
    """Ids in the request the catalogue does not know — a 400, not a silent
    no-op, so a typo'd feature name is caught rather than quietly ignored."""
    return [k for k in (features or {}) if k not in FEATURES_BY_ID]


def _cost_meta(fid: str, cfg: Config) -> dict | None:
    """How a heavy pass scales, so the panel's estimate can move when it is
    switched on. The client has the book's token count and every model's rates;
    what it cannot know is which model each pass runs on, so that is what travels
    here. A null model means "the detector's own model" — the client falls back
    to the one picked above. glossary is absent: it is priced from its own reader
    picker, not a switch.

    - read:    one whole-book read, input-dominated (story sheet).
    - retype:  a rewrite of the whole book per sample, output-heavy; approximate.
    - confirm: local rules propose for free, only the confirm calls cost, and
               their number tracks the book's candidates — approximate.
    - grammar: a hosted API billed per character of the manuscript, with no model
               token cost at all — so it carries its own $/1k-char rate rather
               than a model to price against.
    - judge:   one short call per paragraph that has changes, so its cost tracks
               the number of findings rather than the book — the client scales it
               off the same candidate count `confirm` uses, and the model is the
               judge's own, which is usually a frontier one."""
    if fid == "storysheet":
        return {"kind": "read", "model": cfg.storysheet.model}
    if fid == "continuity":
        return {"kind": "read", "model": cfg.continuity.model}
    if fid == "rewrite":
        return {"kind": "retype", "model": cfg.rewrite.model,
                "samples": cfg.rewrite.samples}
    if fid == "languagetool":
        return {"kind": "confirm", "model": cfg.languagetool.confirm_model}
    if fid == "sapling":
        return {"kind": "grammar", "rate_per_1k": cfg.sapling.cost_per_1k_chars}
    if fid == "meaning_check":
        return {"kind": "judge", "model": cfg.meaning_check.model}
    if fid == "fix_check":
        return {"kind": "judge", "model": cfg.fix_check.model}
    return None


def feature_catalog(cfg: Config) -> list[dict]:
    """The switches to render, each with the value it would take on this run if
    left untouched — read off the config the run would actually use, so the
    panel opens showing what the pipeline does today. Heavy passes also carry a
    `cost` hint so the estimate can move with them; the rest carry cost=null."""
    return [{"id": f.id, "label": f.label, "blurb": f.blurb, "group": f.group,
             "heavy": f.heavy, "default": f.read(cfg),
             "cost": _cost_meta(f.id, cfg)} for f in FEATURES]
