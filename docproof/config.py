from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIConfig(BaseModel):
    model: str = "claude-opus-5"
    # Which vendor serves `model`. Only consulted for models the catalog
    # doesn't recognise — a known model brings its own provider.
    provider: Literal["anthropic", "openai", "gemini"] = "anthropic"
    max_retries: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=16000, ge=1)
    prompt_caching: bool = True
    # Reasoning depth. Grammar detection is a precise, well-specified task, so
    # a low setting is both cheaper and no less accurate here. Ignored on
    # models that don't accept it. null omits the parameter entirely.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"


class ChunkingConfig(BaseModel):
    token_budget: int = Field(default=2500, ge=1)         # soft target per chunk
    hard_cap_tokens: int = Field(default=8000, ge=1)      # beyond this, split paragraph
    min_paragraph_chars: int = Field(default=20, ge=0)


class SkipConfig(BaseModel):
    styles: list[str] = Field(default_factory=lambda: [
        "Heading*", "Title", "Subtitle", "TOC*", "Caption"])


class PrepConfig(BaseModel):
    """Manuscript prep: tagging a Word manuscript into a house InDesign style
    set. The style set and the prompt are both files, so a different publisher
    is a different YAML rather than a different build."""
    style_sheet: str = "prep/house_styles.yaml"
    tagging_prompt: str = "prep/tagging.yaml"
    # A window is one request. The paragraph cap is what keeps the model from
    # skipping entries in a long list; the token budget is what keeps a window
    # of dense prose from being far bigger than a window of dialogue.
    max_paragraphs_per_window: int = Field(default=120, ge=1)
    token_budget: int = Field(default=6000, ge=1)
    context_paragraphs: int = Field(default=8, ge=0)
    # How much of a long paragraph the model sees. It answers with ids and
    # labels, never text, so the tail of a 300-word paragraph would be billed
    # for nothing. 0 sends everything.
    preview_chars: int = Field(default=400, ge=0)
    # Which files a run writes when the caller doesn't say.
    outputs: list[Literal["indesign", "tracked"]] = Field(
        default_factory=lambda: ["indesign"])
    # Clear the export's fonts, sizes and colours from the placed file. Italics
    # and other meaningful run marks are always kept. Never applied to the
    # tracked file, where it would be hundreds of extra revisions to click.
    strip_direct_formatting: bool = True
    # Compare the author's words before and after, and refuse to ship a file
    # that fails. Turning this off is not recommended and says so in the notes.
    verify: bool = True


class NormalizeConfig(BaseModel):
    """The two edits the house brief allows outside the tracked-changes
    system. They are applied before ingest, so everything downstream measures
    against normalized text. Both are silent by design and neither is logged
    line by line — only counted. See docproof/normalize.py."""
    quotes: bool = True       # straight " and ' become curly, where certain
    spaces: bool = True       # runs of two or more spaces collapse to one


class SpellcheckConfig(BaseModel):
    """A dictionary scan that classifies rather than corrects. Its output is
    context for the model passes — the manuscript's own vocabulary as a
    do-not-flag list — never an edit. See docproof/spellscan.py."""
    enabled: bool = True
    # Which Hunspell set to read. Unset means "whatever the variant asks for",
    # which is almost always right. Set it to a name or a path to override —
    # useful when a variant's dictionary is not bundled and the press has its
    # own copy.
    dictionary: str | None = None
    # Seen this often, or written as a name, and an unknown word is the
    # author's coined term rather than a typo.
    min_occurrences: int = Field(default=2, ge=1)
    # suggest() costs about a quarter-second a word, so only this many
    # candidates get dictionary guesses. 0 skips them entirely; the
    # classification itself needs none.
    suggestion_limit: int = Field(default=25, ge=0)
    # Words that are always correct for this house, whatever the dictionary says.
    allowlist: list[str] = Field(default_factory=list)


class ConsistencyConfig(BaseModel):
    """One term written more than one way — the rule per-paragraph review
    cannot do, because it needs the whole book at once. Asks, never corrects.
    See docproof/consistency.py."""
    enabled: bool = True
    # Short keys collide by accident; the shorter the term, the more likely
    # two forms are unrelated English rather than one word spelled two ways.
    min_length: int = Field(default=7, ge=3)
    # How far the majority form must outnumber a minority one before the
    # minority reads as a slip rather than a second deliberate choice. 1 also
    # flags an even split, where there is no dominant form to recommend.
    min_dominance: int = Field(default=2, ge=1)


class PricingConfig(BaseModel):
    """Optional $/MTok rates for the cost estimate in summary.md.
    Leave unset to omit the estimate."""
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


class BatchConfig(BaseModel):
    """How much work may be sitting at one vendor at once.

    Batch APIs meter *enqueued* tokens across every batch you have in flight,
    not per batch — OpenAI's limit is a couple of million on the lower tiers,
    and it frees up as batches finish rather than as time passes. Four
    manuscripts dropped on the window at once used to go out inside a few
    seconds and the later ones came back rejected. See app/jobs.py."""
    # 0 turns the queue off entirely: every job is submitted the moment the
    # worker reaches it, which is what DocProof did before this existed.
    enqueued_token_ceiling: int = Field(default=1_800_000, ge=0)


class Config(BaseModel):
    # CLI flags overwrite fields after load; validate those too.
    model_config = ConfigDict(validate_assignment=True)

    api: APIConfig = Field(default_factory=APIConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    skip: SkipConfig = Field(default_factory=SkipConfig)
    prep: PrepConfig = Field(default_factory=PrepConfig)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)
    spellcheck: SpellcheckConfig = Field(default_factory=SpellcheckConfig)
    consistency: ConsistencyConfig = Field(default_factory=ConsistencyConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    # Which English this manuscript is written in. A handful of conventions
    # flip on it — which mark opens dialogue, decade apostrophes, percent
    # versus per cent, the that/which rule — and it selects the spell-scan
    # dictionary. Stated, never inferred: guessing the variant and then
    # applying its rules silently is how a U.K. manuscript comes back
    # Americanized. See docproof/variants.py.
    variant: Literal["us", "uk", "ca", "au"] = "us"
    # Whether this book's nonstandard grammar is the character's voice or the
    # author's error. Stated for the same reason the variant is: fourteen error
    # types assume it is voice and stay silent, which is right for most fiction
    # and silently finds nothing in a manuscript where it is not.
    # See docproof/voice.py.
    voice: Literal["preserve", "query", "correct"] = "preserve"
    min_confidence: Literal["low", "medium", "high"] = "medium"
    # Each entry is one API pass over the whole document: a bare key runs alone,
    # a list of keys runs as a single combined pass. Grouping trades a little
    # detection focus for a large cut in input tokens — see docs/error-types.md.
    error_types: list[str | list[str]] = Field(default_factory=list)
    # Deterministic house-style sweeps, run before any model pass so their
    # edits get first claim on a span. These cost nothing and, unlike a read,
    # can report their own final match count. An empty list turns them off.
    sweeps: list[str] = Field(default_factory=list)
    tracked_changes_policy: Literal["abort", "accept_all_first", "ignore"] = "abort"
    # Whether rejecting every tracked change must reproduce the ingested text.
    # strict refuses to write a file that fails; warn writes it and says so.
    # Turning this off removes the only check that would catch an edit made
    # without a revision mark around it.
    audit: Literal["strict", "warn", "off"] = "strict"
    output_dir: str = "output"
    comments: bool = True
    # The other half of the two-channel model: findings that ask rather than
    # correct become margin comments with no revision around them. Query-only
    # error types always do; this decides whether below-gate findings join
    # them, or stay in summary.md where only an editor will see them.
    query_comments: bool = True
    # The Word change log the press hands to an author alongside the
    # manuscript: what changed, what was only asked about, and — the part the
    # house brief insists on — what this pass did not cover.
    change_log: bool = True
    revision_author: str = "docproof"
    # Ask the model to justify each finding. The explanations become Word
    # margin comments, but they are also the bulk of the output tokens — and
    # output bills at roughly 5x input. Turn this off for a cheaper pass that
    # still applies every correction, just without the marginalia.
    report_explanations: bool = True
    # Where edited error-type prompts live. Files here shadow the shipped
    # config/error_types/*.yaml by key; missing keys fall through to the
    # originals, so an override directory only has to hold what changed.
    error_type_override_dir: str | None = None

    @field_validator("error_types")
    @classmethod
    def _validate_error_types(cls, value):
        seen: set[str] = set()
        for entry in value:
            keys = [entry] if isinstance(entry, str) else entry
            if not keys:
                raise ValueError("error_types: a group must list at least one key")
            for key in keys:
                if not key or not key.strip():
                    raise ValueError("error_types: keys must be non-empty")
                if key in seen:
                    raise ValueError(
                        f"error_types: '{key}' appears more than once; a key in "
                        f"two passes would review the document twice for it")
                seen.add(key)
        return value

    @field_validator("sweeps")
    @classmethod
    def _validate_sweeps(cls, value):
        from .sweeps import SWEEPS_BY_KEY
        seen: set[str] = set()
        for key in value:
            if key not in SWEEPS_BY_KEY:
                raise ValueError(
                    f"sweeps: unknown sweep '{key}'. "
                    f"Available: {', '.join(SWEEPS_BY_KEY)}")
            if key in seen:
                raise ValueError(f"sweeps: '{key}' appears more than once")
            seen.add(key)
        return value

    @property
    def error_type_groups(self) -> tuple[tuple[str, ...], ...]:
        """error_types normalized to one tuple per pass."""
        return tuple((entry,) if isinstance(entry, str) else tuple(entry)
                     for entry in self.error_types)

    @property
    def error_type_keys(self) -> tuple[str, ...]:
        """Every enabled key, flat, in pass order."""
        return tuple(k for group in self.error_type_groups for k in group)


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path.resolve()}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Configuration root must be a YAML mapping, not {type(raw).__name__}")
    return Config.model_validate(raw)
