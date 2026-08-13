from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (BaseModel, ConfigDict, Field, field_validator,
                      model_validator)


class APIConfig(BaseModel):
    model: str = "claude-opus-5"
    # Which vendor serves `model`. Only consulted for models the catalog
    # doesn't recognise — a known model brings its own provider.
    provider: Literal["anthropic", "openai", "gemini"] = "anthropic"
    max_retries: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=16000, ge=1)
    prompt_caching: bool = True
    # Reasoning depth. Medium is the shipped default: on a real manuscript it
    # caught ~40% more in-taxonomy errors than low for ~$0.16 more per book,
    # with trap false positives unchanged; high tripled output tokens for zero
    # further recall (Johnson Book 1 compare-vs-human, 2026-08). Ignored on
    # models that don't accept it. null omits the parameter entirely.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    # How many chunk requests a synchronous ("right now") review has in flight
    # at once. A review is one call per (pass, chunk), and they are independent,
    # so fetching several concurrently is most of the wall-clock win. Ordering,
    # cost accounting and resume are unaffected — findings are still assembled
    # in document order. 1 restores the old strictly-serial behaviour; keep it
    # modest so a big manuscript does not trip the provider's rate limit.
    concurrency: int = Field(default=8, ge=1)


class ChunkingConfig(BaseModel):
    token_budget: int = Field(default=2500, ge=1)         # soft target per chunk
    hard_cap_tokens: int = Field(default=8000, ge=1)      # beyond this, split paragraph
    # Paragraphs shorter than this reach the sweeps but not a model pass. The
    # default is 0 — every non-empty paragraph is reviewed — because in fiction
    # the short lines are dialogue ("“Who?” he asked."), which is exactly where
    # a missing word, a homophone slip, or a mispunctuated tag hides. A floor
    # skips them silently, and that was a recall hole. Raise it to reintroduce a
    # floor if 1–3 character fragments prove noisy — a call the eval scorecard
    # should drive, not a guess.
    min_paragraph_chars: int = Field(default=0, ge=0)
    # How many tokens of the previous chunk's trailing paragraphs to prepend to
    # each chunk as read-only context, so a pronoun or name whose antecedent
    # sits in the paragraph before still resolves. 0 disables it. Kept small on
    # purpose: context rides the user turn, so — unlike the cached system
    # prompt — it is billed on every chunk of every pass.
    context_token_budget: int = Field(default=300, ge=0)


class SkipConfig(BaseModel):
    """Which paragraphs a review leaves to one side, by Word/InDesign style id
    (fnmatch patterns).

    `styles` are skipped outright — neither reviewed nor swept. A table of
    contents regenerates from the headings, so editing it is pointless; a title
    or caption is set display text, not prose to proofread.

    `sweep_only` styles still get the deterministic sweeps but never a model
    pass. A chapter heading is exactly where a compound-number or stray-
    punctuation fix lives ("Chapter Twenty Four" -> "Twenty-Four"), yet it is
    not something to let the model rewrite. (The IDML path has no sweep-only
    channel, so it skips these outright, as it always has.)"""
    styles: list[str] = Field(default_factory=lambda: [
        "Title", "Subtitle", "TOC*", "Caption"])
    sweep_only: list[str] = Field(default_factory=lambda: ["Heading*"])

    def fully_skipped(self, style: str) -> bool:
        return any(fnmatch.fnmatchcase(style, pat) for pat in self.styles)

    def is_sweep_only(self, style: str) -> bool:
        return any(fnmatch.fnmatchcase(style, pat) for pat in self.sweep_only)


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


class PromoConfig(BaseModel):
    """Marketing copy from a finished manuscript: a teaser and a set of social
    posts, generated in one pass over the whole book. Like prep, the prompt is a
    file, so tuning the voice — or dropping in the house copy specs when they
    land — is a YAML edit, not a code change. The model is not set here: promo
    reads api.model like the other pipelines, so it is provider-agnostic and the
    app picks the model per run."""
    generation_prompt: str = "promo/generation.yaml"
    # How many social posts to ask for. The platform split across the twelve is
    # deferred to the copy specs; until then the model writes this many varied
    # posts. The count lives in the prompt and is checked after the answer comes
    # back — a strict JSON schema cannot carry a list length — so a stray count
    # is surfaced for a human, never a hard failure.
    post_count: int = Field(default=12, ge=1)
    # A whole novel goes to the model in a single call: best coherence, and the
    # volume is low. Beyond this estimated input size promo refuses rather than
    # send a request that would overflow the context window. Splitting a book
    # across calls is a planned extension, not silent behaviour.
    max_input_tokens: int = Field(default=180_000, ge=1)
    # Which files a run writes.
    outputs: list[Literal["teaser", "posts"]] = Field(
        default_factory=lambda: ["teaser", "posts"])
    # Flag capitalised terms in the copy that appear nowhere in the manuscript —
    # a cheap, deterministic guard against invented names. Surfaces, never blocks.
    verify: bool = True
    # The stronger, opt-in grounding check: a second model call that reads the
    # book and the copy and reports which factual claims the manuscript does not
    # support. Off by default because it re-reads the whole novel — a second
    # large-context call per run — so it is worth its cost only when a title
    # needs the extra assurance. Like `verify`, it surfaces, never blocks.
    verify_claims: bool = False
    verify_prompt: str = "promo/verify.yaml"


class NormalizeConfig(BaseModel):
    """The two edits the house brief allows outside the tracked-changes
    system. They are applied before ingest, so everything downstream measures
    against normalized text. Both are silent by design and neither is logged
    line by line — only counted. See docproof/normalize.py."""
    quotes: bool = True       # straight " and ' become curly, where certain
    spaces: bool = True       # runs of two or more spaces collapse to one


class StyleConfig(BaseModel):
    """House-style conventions the deterministic sweeps enforce where the right
    answer is a publisher's choice rather than a rule. Kept here and not in the
    per-English variant files because none of it flips on US/UK — a house sets
    it once for every manuscript it proofreads.

    `ellipsis` is how an ellipsis sits against the words around it:
      nbsp   — a non-breaking space before it, so it never wraps away from the
        word it trails, and a plain space after. The Atmosphere house
        convention (Bad[NBSP]… she trailed off), so it is the default.
      closed — no space before it, a plain space after only when a word
        follows ("I… guess"). For a manuscript or imprint that sets ellipses
        closed up instead of to the house convention.
      space  — a plain space on both sides.
    The trailing space is the same in every mode; only the lead differs."""
    ellipsis: Literal["nbsp", "closed", "space"] = "nbsp"


class EditGuardConfig(BaseModel):
    """A safety net against a model pass overstepping proofreading into
    rewriting. A proofreading fix is minimal by contract — analyzer.py tells the
    model 'the smallest possible fix ... change nothing else' — so an edit that
    replaces a large span, or adds a lot of new text, is almost always the model
    fabricating content (an invented dialogue tag) or re-typing a passage
    wholesale rather than correcting it. Such a finding is rejected before it can
    become a tracked change, and the rejection is counted so it stays visible.

    The thresholds are in characters of the *minimal* diff, measured after the
    common prefix and suffix are trimmed, so a small fix inside a long sentence
    is judged by what it actually changes, not by the sentence's length."""
    enabled: bool = True
    # Largest span, deleted or inserted, a single correction may touch. Beyond
    # this it is a wholesale re-type, not a proofreading edit.
    max_edit_chars: int = Field(default=64, ge=1)
    # Most net new characters a correction may add (inserted minus deleted). A
    # missing word or a spelled-out number adds a little; a fabricated clause or
    # dialogue attribution adds a lot. Deleting text is never capped this way —
    # removing words is not fabrication.
    max_added_chars: int = Field(default=16, ge=0)


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
    # How often a lower-case unknown word must recur before it counts as "used
    # throughout" rather than "seldom". It no longer buys protection — only a
    # name earns that — so this just sorts the model's evidence: a word seen at
    # least this often is passed on as the book's vocabulary, a word seen less
    # is raised as something to look at. See docproof/spellscan.py.
    min_occurrences: int = Field(default=2, ge=1)
    # suggest() costs about a quarter-second a word, so only this many
    # candidates get dictionary guesses. 0 skips them entirely; the
    # classification itself needs none.
    suggestion_limit: int = Field(default=25, ge=0)
    # Words that are always correct for this house, whatever the dictionary says.
    allowlist: list[str] = Field(default_factory=list)
    # Spellings the house never accepts, mapped to their correction. Never the
    # author's own, never merely noted: always raised as something to look at,
    # with the fix supplied directly because the right form is often two words
    # (a lot, as well) that a dictionary suggestion would not reach.
    denylist: dict[str, str] = Field(default_factory=lambda: {
        "alot": "a lot", "aswell": "as well", "infact": "in fact",
        "eachother": "each other", "atleast": "at least", "incase": "in case",
        "everytime": "every time", "abit": "a bit", "inspite": "in spite",
        "ofcourse": "of course"})


class ConsistencyConfig(BaseModel):
    """One term written more than one way — the rule per-paragraph review
    cannot do, because it needs the whole book at once. Terms ask, never
    correct; proper names with diacritic drift (Rian against Rían) correct
    when one spelling clearly owns the book, and ask otherwise.
    See docproof/consistency.py."""
    enabled: bool = True
    # Short keys collide by accident; the shorter the term, the more likely
    # two forms are unrelated English rather than one word spelled two ways.
    min_length: int = Field(default=7, ge=3)
    # How far the majority form must outnumber a minority one before the
    # minority reads as a slip rather than a second deliberate choice. 1 also
    # flags an even split, where there is no dominant form to recommend.
    min_dominance: int = Field(default=2, ge=1)
    # The proper-name diacritic scan, and its bar for correcting rather than
    # asking: the dominant spelling must outnumber every stray name_dominance
    # times over AND be seen at least name_min_count times. Below the bar the
    # group is asked about, not corrected.
    names: bool = True
    name_dominance: int = Field(default=5, ge=2)
    name_min_count: int = Field(default=20, ge=2)


class AdjudicateConfig(BaseModel):
    """The candidate-adjudication pass: real-word typos the plain passes glide
    over, found by cheap local signals (a non-word one edit from a common word;
    a spell-scan-protected word with a common twin) and ruled on by the model in
    context. Precision lives in the routing — only a model-affirmed correction
    at edit_confidence becomes a tracked change; a softer call is a margin query,
    a "keep" is nothing — so a wrong candidate costs at most a question.
    Whole-document only, like the consistency scan. See docproof/adjudicate.py."""
    enabled: bool = True
    # How far a common word must outnumber a protected coinage (in zipf points,
    # ~each point is 10x) before the coinage is treated as a possible
    # misspelling rather than the author's own word.
    near_miss_gap: float = Field(default=2.5, ge=0)
    # Short tokens are too edit-close to everything to judge cheaply.
    min_word_len: int = Field(default=4, ge=3)
    # A bound on how many sites go to the model, logged when it bites.
    max_candidates: int = Field(default=500, ge=1)
    # Candidates per adjudication request.
    batch_size: int = Field(default=40, ge=1)
    # The confidence at or above which an affirmed correction is applied as a
    # tracked change; anything softer is routed to the margin as a query.
    edit_confidence: Literal["low", "medium", "high"] = "high"


class GlossaryConfig(BaseModel):
    """The whole-book glossary pass: a strong model reads the entire manuscript
    once, before the detector passes, and returns the book's proper nouns (with
    canonical casing) plus suspected misspellings — including real-word errors
    both a dictionary and a frequency signal are blind to. The suspects become
    adjudication candidates (ruled on in context, soft calls asked not applied);
    the casing feeds a query-channel case-drift check. Whole-document only, and
    priced for its own model since a frontier reader earns its cost here. See
    docproof/glossary.py."""
    enabled: bool = True
    # The one-time whole-book reader, cacheable per draft, and the only pass that
    # catches valid-word-for-valid-word errors. Luna is the shipped default:
    # cheap (~$0.04/book) and enough for the obvious real-word errors. A frontier
    # model (Opus) adds the subtle semantic tail (providence/provenance) at ~40x
    # the cost; the app's submission panel offers the pick per book.
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    max_output_tokens: int = Field(default=8000, ge=1)
    # Pin the whole-book read per draft. A path enables a content-addressed
    # cache (keyed by text + model + prompt): re-reviewing an unchanged draft
    # reuses the read instead of paying for it again, and — since the read is
    # stochastic — every run of that draft sees the same glossary, so case-drift
    # findings stop wobbling run to run. Unset (the default) disables the cache.
    cache_dir: str | None = None
    # Raise the glossary's casing drift as margin queries. Off leaves only the
    # suspected-misspelling half (which the adjudication pass carries).
    case_drift: bool = True
    # How the case-drift check finds a proper noun's stray casings. scan (the
    # default) discovers them deterministically — for each catalogued proper
    # noun it reads the whole book and tallies every casing the phrase actually
    # takes, so a stray is caught whether or not the glossary model happened to
    # list it as a variant (it usually does not). Off falls back to the model's
    # self-reported variants only. A leading article (the/a/an) is normalised
    # out before comparing, so "the Upper City" is never flagged against a
    # "The Upper City" canonical — only a content word's casing counts.
    case_drift_scan: bool = True
    # Routing for a discovered stray. By default a stray is asked about (a
    # margin query, never a silent recase — "the upper city" may be a common
    # description, not the place). But when the canonical casing overwhelmingly
    # owns the book — a multi-word proper noun seen at least case_edit_min_count
    # times and outnumbering its strays case_edit_dominance-to-one — the stray
    # is corrected as a tracked change instead, the way the consistency scan
    # corrects a clearly-dominant name spelling. Single-word terms are always
    # asked (they collide with common nouns: a weather "squall" vs "The Squall").
    case_edit_dominance: int = Field(default=5, ge=2)
    case_edit_min_count: int = Field(default=8, ge=2)

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"glossary.model '{self.model}' is not in the catalog")
        return self


class ContinuityConfig(BaseModel):
    """The whole-book continuity read: one frontier read of the entire manuscript
    that flags facts the book contradicts about itself — a timeline that does not
    add up, an age or date the arithmetic breaks, an attribute that drifts, an
    object whose state changes with no cause. The one class of error the chunked
    detectors are structurally blind to: a per-chunk pass never sees chapter 1 and
    chapter 20 together. Query-only by design — every finding is a margin comment,
    never a tracked change, because which of two contradictory facts is right is
    the author's to settle, not the pipeline's. A deterministic date->weekday
    checker rides along at no API cost. OFF by default until proven on a
    real-manuscript compare, like rewrite. Whole-document only, and priced for its
    own model since a frontier reader earns its cost here. See
    docproof/continuity.py."""
    enabled: bool = False
    # The whole-book reader. Cross-book contradiction-finding is frontier work;
    # claude-opus-5 is the half-price alternative and claude-sonnet-5 cheaper
    # still — the app's submission panel offers the pick per book, like the
    # glossary's. Cacheable per draft via cache_dir.
    model: str = "claude-fable-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    max_output_tokens: int = Field(default=8000, ge=1)
    # A cap on margin comments, most significant first — the model is asked to
    # order them, and any past this many are dropped.
    max_queries: int = Field(default=40, ge=1)
    # Drop anything softer than this. A wrong continuity question costs an editor
    # more trust than a missed one earns, so "low" is dropped by default.
    min_confidence: Literal["low", "medium", "high"] = "medium"
    # The deterministic date->weekday check: no API, no cost, always a query (a
    # story may run on its own calendar). Runs even when the model read is off.
    calendar_check: bool = True
    # Refuse a book too big for one read rather than truncating it — a truncated
    # read would silently miss every contradiction past the cut. Mirrors promo's
    # refuse-don't-overflow guard.
    max_input_tokens: int = Field(default=400_000, ge=1)
    # A path pins the read per draft (text + model + prompt): re-reviewing an
    # unchanged draft reuses the read instead of paying again, and — since the
    # read is stochastic — every run of that draft sees the same questions.
    cache_dir: str | None = None
    # The reader's system prompt, editable per job in the panel the way the
    # round judge's is. Empty (the default) uses the built-in one in
    # continuity.py; a non-empty value replaces it wholesale, and — being part
    # of the cache key — a changed prompt re-reads rather than returning a
    # result the old prompt produced.
    prompt: str = ""

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"continuity.model '{self.model}' is not in the catalog")
        return self


class RewriteConfig(BaseModel):
    """The rewrite-then-diff pass: the model retypes each paragraph minimal-edit,
    the diff against the source becomes candidates, and a skeptical confirm pass
    rules on each in context — precision in the routing, like adjudication. Off
    by default: a strong recall lever (locates ~25% of the detector's misses on
    Johnson) but a whole extra pass in cost, so it ships opt-in until proven on a
    real-manuscript compare. The output-heavy retype rides the review batch when
    `model` matches api.model (the default), so it gets the batch discount and
    avoids the sync rate-limit wall; only the light confirm runs at collect.
    Whole-document only. See docproof/rewrite.py."""
    enabled: bool = False
    # The rewriter's own model (a cheap one is enough — retyping is far easier
    # than needle-finding). Defaults to whatever api.model is when unset.
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    max_output_tokens: int = Field(default=8000, ge=1)
    # Candidate size guards: a diff bigger than these is a paraphrase, not a fix.
    max_span: int = Field(default=48, ge=1)     # chars in the delete or insert
    max_added: int = Field(default=24, ge=0)    # net chars a fix may add
    # How many independent retypes to take of each chunk. The retype is
    # stochastic — two passes catch overlapping but not identical errors — so
    # taking several and unioning their diffs lifts recall (the confirm step
    # still rules on each candidate, so extra candidates cost precision nothing,
    # only tokens). 1 is a single pass (no ensemble); each extra sample is
    # another whole retype in cost. Rides the batch like the first.
    samples: int = Field(default=1, ge=1)
    # When taking several samples, point each at a different class of error
    # (small omissions, agreement, ...) rather than asking the same question
    # twice — diverse sampling covers more than redundant sampling at the same
    # cost. Off makes every sample the plain prompt. No effect at samples=1.
    diverse: bool = True
    # Concurrency for the SYNCHRONOUS retype path only (run_sync, or the
    # different-model fallback at collect). When the retype rides the review
    # batch — the default, model unset — the batch handles concurrency and this
    # is unused.
    workers: int = Field(default=8, ge=1)
    # Candidates per confirm request.
    batch_size: int = Field(default=40, ge=1)
    # The confidence at or above which an affirmed correction edits; softer is a
    # margin query. High by default — a rewrite fix can be right-place-wrong-word.
    edit_confidence: Literal["low", "medium", "high"] = "high"
    # The confirm step is the gate: every real error it wrongly rejects is a miss
    # we paid to generate and threw away. It is cheap (short prompts), so a
    # stronger model here can recover recall for little cost. Unset = the rewrite
    # model does its own confirming (current behaviour). A different vendor's id
    # is fine — confirm runs synchronously, it never has to share the batch.
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    # Write the candidates confirm KEPT (ruled not-an-error) to
    # rewrite_rejects.json beside the findings. Diagnostic: overlaying them on a
    # human proofread shows how much real recall the gate is discarding.
    log_rejects: bool = False

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"rewrite.confirm_model '{self.confirm_model}' is not in the catalog")
        if self.enabled and self.model is not None and lookup(self.model) is None:
            raise ValueError(
                f"rewrite.model '{self.model}' is not in the catalog")
        return self


class LanguageToolConfig(BaseModel):
    """LanguageTool mechanical-floor pass. A LOCAL, rules-based checker (≈1,600
    English rules + n-gram perplexity) run as a Java sidecar — no network, no
    per-call cost, no client text leaving the machine. It proposes candidates —
    commas, missing words, compound-modifier hyphenation — that the SHARED
    rewrite.confirm valve rules on in literary context, so LanguageTool never
    edits blind; its own noise (a name read as a misspelling, an intentional
    repeat, an over-eager hyphen) is caught at the valve. Orthogonal to the LLM
    detector (~5% catch overlap) and strongest on exactly its weak tail; it does
    NOT see mid-sentence capitalization or word-choice. Off by default: needs
    Java + the LanguageTool jar (auto-downloaded, ~260 MB, cached). Whole-document
    only. See docproof/languagetool.py."""
    enabled: bool = False
    dictionary: str = "en-US"           # LanguageTool language code
    # Extra rule ids to drop, on top of the built-in artifact/style denylist
    # (unpaired-quote, sentence-start caps, whitespace, style advice).
    disabled_rules: list[str] = Field(default_factory=list)
    max_output_tokens: int = Field(default=4000, ge=1)
    batch_size: int = Field(default=40, ge=1)     # candidates per confirm request
    # The confidence at or above which an affirmed fix edits; softer is a margin
    # query. High by default — LanguageTool is deterministic but context-blind.
    edit_confidence: Literal["low", "medium", "high"] = "high"
    # The confirm model. Unset = api.model (the detector's) does its own
    # confirming; the prompts are short so a stronger model here is cheap.
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"languagetool.confirm_model '{self.confirm_model}' "
                "is not in the catalog")
        return self


class SaplingConfig(BaseModel):
    """Sapling.ai grammar/spell pass. A HOSTED grammar checker (network call,
    per-use cost) run over the whole manuscript, its suggestions folded into the
    tracked changes alongside the sweeps and the model — validated after both, so
    a house-style sweep keeps first claim on a span and a Sapling edit that
    overlaps one is dropped. Off by default and opt-in per run: it is a
    third-party service that costs money. Needs SAPLING_API_KEY in the
    environment (the web build loads the admin-set key there); with none set the
    pass is skipped with a warning rather than failing the review.
    Whole-document only. See docproof/sapling.py."""
    enabled: bool = False
    # Sapling's regional spelling variety: "", "us-variety", "gb-variety",
    # "au-variety", "ca-variety". Empty sends no preference.
    variety: str = ""
    # What Sapling bills, in dollars per 1,000 characters of submitted text. Used
    # only to show an estimate before a run — Sapling itself is the source of
    # truth for the actual charge, and it never reaches the model-token cost math.
    cost_per_1k_chars: float = Field(default=0.025, ge=0)


class DetectorSpec(BaseModel):
    """One reviewer in an ensemble: a model and how hard it thinks. The provider
    is read from the catalog, exactly as api.model is, so a detector is just a
    model id. Two identical specs is a self-ensemble (the same model sampled
    twice); different vendors is the diverse one — both are configuration, not
    code."""
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"


class EnsembleConfig(BaseModel):
    """Several detectors reviewing each chunk, their findings merged by
    agreement, then a stronger verifier adjudicating before anything reaches the
    author. UNION for recall, verifier for precision — a miss problem is not
    solved by voting (intersection would only lower recall).

    An empty `detectors` list is the default and means single-detector mode:
    api.model runs once, nothing is merged, no verifier runs — byte-for-byte the
    behaviour that shipped before the ensemble existed. The moment `detectors`
    is non-empty the fan-out, merge and (optional) verifier turn on."""
    detectors: list[DetectorSpec] = Field(default_factory=list)
    # The overseer. None disables verification whatever verify_policy says.
    # Meant to be a stronger model than the detectors, thinking harder — it sees
    # far fewer calls (one per disputed finding, not per chunk), so it is a small
    # fraction of the spend even at a premium tier.
    verifier_model: str | None = None
    verifier_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # Which findings the verifier judges: only those the detectors disagree on
    # (cheapest, and consensus precision is measured before it is trusted),
    # every finding, or nothing.
    verify_policy: Literal["disputed", "all", "none"] = "disputed"
    # Raise a consensus finding's confidence one notch (low->medium->high) — the
    # more detectors agree, the surer the finding. Off until the eval says the
    # promotion is earned.
    consensus_confidence_bump: bool = False

    @property
    def enabled(self) -> bool:
        return len(self.detectors) > 0

    @property
    def verifies(self) -> bool:
        """Whether a verifier will actually run. This is the condition that
        makes relaxing a detector prompt safe — the recall-tuned variant is only
        used when a second reviewer is there to catch its false positives."""
        return (self.enabled and bool(self.verifier_model)
                and self.verify_policy != "none")

    @model_validator(mode="after")
    def _known_models(self):
        from .providers.catalog import lookup
        unknown = [d.model for d in self.detectors if lookup(d.model) is None]
        if self.verifier_model and lookup(self.verifier_model) is None:
            unknown.append(self.verifier_model)
        if unknown:
            raise ValueError(
                f"ensemble references model(s) the catalog does not know: "
                f"{', '.join(sorted(set(unknown)))}. Add them to "
                f"providers/catalog.py or fix the ids.")
        return self


class PricingConfig(BaseModel):
    """Optional $/MTok rates for the cost estimate in summary.md.
    Leave unset to omit the estimate."""
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


class StorySheetConfig(BaseModel):
    """The whole-book story sheet: a strong model reads the manuscript once,
    before the detector passes, and returns the narrative facts a per-paragraph
    check cannot know — who narrates, in what person and tense, and each
    character's pronouns. Injected into every detector's system prompt (cached
    per document, like the vocabulary), it lets a paragraph-in-isolation pass
    catch a pronoun, name, or tense that is wrong for the STORY — the wrong-word
    errors it otherwise glides over. Off by default: a whole extra read, shipping
    opt-in until proven. Whole-document only. See docproof/storysheet.py."""
    enabled: bool = False
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    max_output_tokens: int = Field(default=4000, ge=1)
    # A path pins the read per draft, like glossary.cache_dir.
    cache_dir: str | None = None

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"storysheet.model '{self.model}' is not in the catalog")
        return self


class RoundsConfig(BaseModel):
    """Multi-round review. The manuscript is reviewed `count` times; each round
    reads the previous round's corrected text, and a strong judge adjudicates
    every model-generated correction between rounds. All rounds' approved changes
    are composed into one tracked-change document against the original at the end
    (docproof/editlayer.py). Off by default: count 1 is a single ordinary review,
    byte-for-byte. See docproof/verifier.py (the judge) and docproof/editlayer.py."""
    count: int = Field(default=1, ge=1, le=4)
    judge_model: str = "gpt-5.6-sol"
    judge_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # The judge's instructions, meant to be edited per job in the review panel.
    # Empty uses the built-in default (docproof.verifier.default_judge_prompt()),
    # so clearing the field reverts to it.
    judge_prompt: str = ""
    # Stop early once a round approves fewer than this many new corrections.
    min_new_edits: int = Field(default=1, ge=0)
    # Reuse round 1's whole-book reads (glossary, story sheet) in later rounds:
    # mechanical fixes change no names or narration, and it kills the run-to-run
    # glossary wobble.
    reuse_whole_book_reads: bool = True

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.count > 1 and lookup(self.judge_model) is None:
            raise ValueError(
                f"rounds.judge_model '{self.judge_model}' is not in the catalog")
        return self


class Config(BaseModel):
    # CLI flags overwrite fields after load; validate those too.
    model_config = ConfigDict(validate_assignment=True)

    api: APIConfig = Field(default_factory=APIConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    skip: SkipConfig = Field(default_factory=SkipConfig)
    prep: PrepConfig = Field(default_factory=PrepConfig)
    promo: PromoConfig = Field(default_factory=PromoConfig)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    edit_guard: EditGuardConfig = Field(default_factory=EditGuardConfig)
    spellcheck: SpellcheckConfig = Field(default_factory=SpellcheckConfig)
    consistency: ConsistencyConfig = Field(default_factory=ConsistencyConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    storysheet: StorySheetConfig = Field(default_factory=StorySheetConfig)
    continuity: ContinuityConfig = Field(default_factory=ContinuityConfig)
    adjudicate: AdjudicateConfig = Field(default_factory=AdjudicateConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    languagetool: LanguageToolConfig = Field(default_factory=LanguageToolConfig)
    sapling: SaplingConfig = Field(default_factory=SaplingConfig)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)
    rounds: RoundsConfig = Field(default_factory=RoundsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    # Which English this manuscript is written in. A handful of conventions
    # flip on it — which mark opens dialogue, decade apostrophes, percent
    # versus per cent, the that/which rule — and it selects the spell-scan
    # dictionary. Stated, never inferred: guessing the variant and then
    # applying its rules silently is how a U.K. manuscript comes back
    # Americanized. See docproof/variants.py.
    variant: Literal["us", "uk", "ca", "au"] = "us"
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
    # A single comment at the top of the reviewed file naming the words the
    # spell scan left out of the check — protected as the author's own and never
    # flagged. It puts "these were taken on trust, verify them" in front of the
    # proofreader instead of only in the change log. Rides on `comments`: a run
    # with Word comments off writes none of these either.
    excluded_words_comment: bool = True
    # The Word change log the press hands to an author alongside the
    # manuscript: what changed, what was only asked about, and — the part the
    # house brief insists on — what this pass did not cover.
    change_log: bool = True
    revision_author: str = "Atmosphere Press Proofreader"
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
