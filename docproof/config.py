from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple

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
    # modest so a big manuscript does not trip the provider's rate limit. 1 is
    # strictly serial and overrides the per-vendor table below.
    concurrency: int = Field(default=8, ge=1)
    # Per-vendor overrides of the number above, because "modest" is a different
    # number at each vendor and the pass that is running decides which one
    # applies — a valve may confirm on an OpenAI model under an Anthropic
    # detector. 8 was set when the OpenAI keys were Tier-1 (200K TPM); they now
    # carry 2M TPM / 5K RPM, where 8 in flight leaves the ceiling untouched.
    # This is not a niche path: the engine default here is Haiku, but the app
    # ships gpt-5.6-luna as its detector AND its glossary reader (app/settings),
    # so most real reviews are the OpenAI ones this raises.
    # Anthropic keeps the conservative number: its headroom has never been
    # measured, and a 429 that outlives max_retries is not retried — it becomes
    # a silent coverage gap, not a loud failure. Guessing there is expensive.
    # Keyed by vendor id — "anthropic" | "openai" | "gemini". See
    # `Config.concurrency_for`.
    concurrency_by_provider: dict[str, int] = Field(
        default_factory=lambda: {"openai": 24})

    @field_validator("concurrency_by_provider")
    @classmethod
    def _known_providers(cls, value):
        """A misspelled vendor would silently do nothing — the lookup would miss
        and the floor would apply — so it is refused at load instead. Same three
        names as `provider`."""
        allowed = ("anthropic", "openai", "gemini")
        for name, n in value.items():
            if name not in allowed:
                raise ValueError(
                    f"api.concurrency_by_provider: '{name}' is not a provider; "
                    f"use one of {', '.join(allowed)}")
            if n < 1:
                raise ValueError(
                    f"api.concurrency_by_provider: '{name}' must be at least 1")
        return value


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
    # The interior design for the book output: page geometry, faces, running
    # heads, drop caps, and the subject-matter display fonts. Like the style
    # sheet, a file — a different look is a different YAML. The default is the
    # plain manuscript — Times New Roman, 12pt, US Letter, no ornaments — so a
    # manual run hands back a clean reading copy; the Atmosphere paperback sketch
    # (prep/book_design.yaml) is the richer alternate, chosen by pointing this
    # at it.
    book_design: str = "prep/book_manuscript.yaml"
    # The house InDesign template the "indesign" output flows the book into,
    # as IDML — trim, margins, master pages and the primary text frame. Like the
    # style sheet, a file: a different house look is a different template. The
    # paragraph styles themselves are written in from the style sheet, so a bare
    # template needs only the geometry.
    indesign_template: str = "prep/house_template.idml"
    # DocWatch's own interior, kept separate so the watched folder always hands
    # back the plain reading copy even if `book_design` above is later pointed at
    # a richer look. It happens to be the same plain manuscript that is now the
    # default; swapped in for `book_design` on watched-folder prep (see
    # JobRunner.config_for). Another basic-but-different look is a different YAML.
    watch_book_design: str = "prep/book_manuscript.yaml"
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
    # Which files a run writes when the caller doesn't say. "book" is the
    # reading copy for the author and the developmental editors — the
    # manuscript dressed as an Atmosphere paperback; "indesign" is the placed
    # file for the design team; "tracked" records the same decisions as Word
    # revisions.
    outputs: list[Literal["book", "indesign", "tracked"]] = Field(
        default_factory=lambda: ["book"])
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
    # The marketing-plan prompt — promo's third deliverable, generated by its
    # own call because it takes author/book metadata the teaser call does not.
    # Like generation_prompt, a YAML people edit, not code. See
    # config/promo/marketing_plan.yaml.
    plan_prompt: str = "promo/marketing_plan.yaml"
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
    # A paragraph whose double quotation marks do not balance — and whose
    # successor does not reopen with one, so it is not multi-paragraph speech
    # — gets a margin query. A question, never an edit: the fix (where the
    # missing mark goes) is a judgment. Double-primary variants only.
    unclosed_quote_queries: bool = True
    # Set heading-styled paragraphs (the skip config's sweep-only styles) in
    # Chicago title case, as word-level tracked changes: "the shape of things
    # to come" -> "The Shape of Things to Come". All-caps headings and words
    # carrying their own capitals (McCoy, EVTOL) are left as styled. This is
    # the typesetting half of a heading pass — no model ever reads one.
    heading_title_case: bool = True


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
    # Hygiene on the protected list itself: two protected names one edit apart
    # ("Hollingworth" beside "Hollingsworth") are more often one misspelled
    # character than two characters named alike. When one decisively owns the
    # book (dominance-to-one), the rarer is demoted to the adjudication pass;
    # when the counts are close, the pair is raised to the author as a query.
    # A rarer plural cased unlike its singular ("Evtols" beside "EVTOL") is
    # demoted regardless of the ratio. See docproof/spellscan.py.
    near_duplicates: bool = True
    near_duplicate_dominance: int = Field(default=5, ge=2)


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
    # The three mechanical scans a compound-word key scan structurally cannot do,
    # each a whole-document query pass that changes nothing:
    #   spelling_variants — different-letter spellings of one word via the VarCon
    #     table (grey/gray, toward/towards); enforced forms (the variant respell
    #     map) and author-owned words (the spell-scan lexicon) are left out.
    #   abbreviations — dotted vs undotted, dotted-lowercase vs caps (U.S./US,
    #     a.m./AM).
    #   acronym_case — an initialism in capitals in one place, title-cased in
    #     another (NASA/Nasa); needs the dictionary to tell an acronym from a word.
    spelling_variants: bool = True
    abbreviations: bool = True
    acronym_case: bool = True
    # Add the Merriam-Webster/Chicago preference to a spelling-variant query.
    # A press proofreading in British English can turn this off.
    chicago_notes: bool = True
    # Each mechanical scan emits one query per group; this bounds how many, so a
    # dialect-mixed manuscript cannot flood the query channel. The cap is logged
    # when it bites — coverage is never silently truncated.
    max_queries_per_kind: int = Field(default=40, ge=1)
    # Proper nouns known ahead of the run — typically extracted by
    # `docproof galley profile` from the manuscript's own capitalization
    # pattern, or typed in by an editor who already knows the cast list. Fed
    # into the three mechanical scans' `protected` set (pipeline.py) alongside
    # the spell scan's own lexicon, so a coined name the spell scan has not
    # independently earned yet (too rare, or only ever seen sentence-initial)
    # is still shielded from being read as "one term written two ways." Never
    # corrects anything by itself — protection only, same as `protected`
    # everywhere else in this module. See docproof/genre.py.
    seeded_names: list[str] = Field(default_factory=list)


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
    max_output_tokens: int = Field(default=16000, ge=1)
    # Where to pin the whole-book read per draft: a content-addressed cache
    # (keyed by text + model + effort + prompt) so re-reviewing an unchanged
    # draft reuses the read instead of paying for it again, and — since the read
    # is stochastic — every run of that draft sees the same glossary, so
    # case-drift findings stop wobbling run to run.
    # Unset does NOT mean off: it means the shared default folder, resolved at
    # the point of use by `cache_dir_for` (DOCPROOF_CACHE_DIR, else under
    # DOCPROOF_HOME). Setting DOCPROOF_CACHE_DIR empty is what turns caching off.
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
    # The whole-book reader. Defaults to the house reviewer so the pass needs no
    # key beyond the detector's; cross-book contradiction-finding is frontier
    # work, so the app's submission panel offers a stronger pick per book
    # (claude-opus-5, claude-sonnet-5) like the glossary's. Cacheable per draft
    # via cache_dir.
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # The ceiling covers the reader's THINKING as well as its findings, and a
    # frontier model at effort high reasoning over a whole book spends most of
    # it there. Sized with headroom because a cap is free until generated —
    # billing is per token produced, and the panel's estimate uses its own
    # fixed figure — while a truncated read is a fully billed call whose
    # findings are all lost. build_continuity retries one truncation at double
    # this, so the ceiling is the common case, not the last line of defense.
    max_output_tokens: int = Field(default=32000, ge=1)
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
    # A path pins the read per draft (text + model + effort + prompt):
    # re-reviewing an unchanged draft reuses the read instead of paying again,
    # and — since the read is stochastic — every run sees the same questions.
    # Unset is the shared default folder, not off; see glossary.cache_dir.
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


class ChapterContinuityConfig(BaseModel):
    """The chapter-scoped continuity read: the third reading distance. The chunk
    detectors read a paragraph at a time and the whole-book continuity read reads
    everything at once; this reads one CHAPTER at a time, for the class of break
    that closes inside a chapter and is invisible to both — a character who sits
    down who never stood, someone who leaves a room then speaks in it, a cigarette
    lit twice, dawn that becomes evening in one scene, a reply to a question no one
    asked.

    Built on the taste judge's shape, not the book read's: a chapter-scoped reader
    has less context and over-proposes, so a skeptical judge is the precision gate.
    Read each chapter once, drop any break whose two quotes do not both land in
    that same chapter (the guardrail that also keeps it off the book read's
    territory), let a skeptical judge rule on the survivors, and cap the volume per
    chapter so the margin stays readable.

    Query-only by design and unconditionally so, exactly like the book read: every
    finding is force_query'd — a margin comment, never a tracked change — because
    which of two contradictory facts is right is the author's call. Dialogue is IN
    scope here, unlike the smoothing pass: a contradiction spoken aloud is still a
    contradiction. OFF by default, opt-in per run, whole-document only. See
    docproof/continuity.py."""
    enabled: bool = False
    # The chapter reader. Unset falls back to the whole-book read's model, then to
    # api.model — per-chapter reading is easier than whole-book needle-finding, so
    # a cheaper model may prove out here by eye.
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # Per chapter the read is small next to a whole book, but a reasoning model
    # spends most of this ceiling thinking; sized so a dense chapter's findings
    # are not truncated.
    max_output_tokens: int = Field(default=8000, ge=1)
    # The skeptical judge — the precision gate. Defaults to the house reviewer so
    # the pass stays on one key; a stronger judge (telling a real in-scene break
    # from a device the reader is meant to hold open is the hard part) is a per-
    # run pick, one model setting driving both this reader and the judge.
    judge_model: str = "gpt-5.6-luna"
    judge_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # The judge's visible output is tiny (a verdict is three fields) but on a
    # reasoning model the thinking counts against this; sized from the propose
    # ceiling like the smoothing judge, since a truncated batch returns no
    # verdicts at all and every candidate in it vanishes.
    judge_max_output_tokens: int = Field(default=16000, ge=1)
    batch_size: int = Field(default=40, ge=1)   # candidates per judge request
    # "How hard it looks" — one 1–5 dial (the panel's slider) that bundles the
    # judge's posture with the confidence floor, monotonically stricter → looser:
    #   1 Cautious (strict judge, high floor) · 2 Measured (strict, medium — the
    #   ship default) · 3 Thorough (strict, low) · 4 Searching (loose, medium) ·
    #   5 Exhaustive (loose, low). See sensitivity_profile() in continuity.py. A
    #   non-empty `judge_prompt` below overrides the posture the level selects.
    sensitivity: int = Field(default=2, ge=1, le=5)
    # The volume cap, per CHAPTER — the chapter is this pass's natural unit, so ten
    # questions in one chapter is where a margin stops being read. Ranked by the
    # judge's confidence; everything past the cap is dropped AND counted.
    max_per_chapter: int = Field(default=10, ge=1)
    # A unit below this many tokens (an epigraph, a part divider) is merged into a
    # neighbour rather than buying its own read; a unit above max_chapter_tokens is
    # size-split, so a headingless manuscript still reads in book-sized windows.
    min_chapter_tokens: int = Field(default=1000, ge=1)
    max_chapter_tokens: int = Field(default=120_000, ge=1)
    # A path pins each chapter's read per draft (text + model + effort + prompt):
    # re-reviewing a manuscript where one chapter changed re-reads only that
    # chapter. Unset is the shared default folder, not off; see glossary.cache_dir.
    cache_dir: str | None = None
    # The chapter reader's system prompt, editable per job in the panel like the
    # book read's. Empty (the default) uses the built-in one in continuity.py; a
    # non-empty value replaces it wholesale, and — being part of the cache key — a
    # changed prompt re-reads rather than returning a stale result.
    prompt: str = ""
    # The judge's system prompt, editable for tuning. Empty uses the built-in one.
    judge_prompt: str = ""

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if not self.enabled:
            return self
        if self.model is not None and lookup(self.model) is None:
            raise ValueError(
                f"chapter_continuity.model '{self.model}' is not in the catalog")
        if lookup(self.judge_model) is None:
            raise ValueError(
                f"chapter_continuity.judge_model '{self.judge_model}' "
                "is not in the catalog")
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
    max_output_tokens: int = Field(default=16000, ge=1)
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
    # Picky level: LanguageTool's stricter rule set (extra typography, register,
    # and hyphenation rules that the default level holds back). Measured on a
    # real literary manuscript it added almost nothing past the style advice the
    # pass already filters (1 candidate on 44k words), because most picky rules
    # ARE the style class dropped at DEFAULT_DISABLED_ISSUE_TYPES. The extra
    # candidates still route through the confirm valve, so picky never edits
    # blind — it only offers more for the valve to rule on. default.yaml turns
    # it on so every LanguageTool job runs at the strict level.
    picky: bool = False
    # Extra rule ids to drop, on top of the built-in artifact/style denylist
    # (unpaired-quote, sentence-start caps, whitespace, style advice).
    disabled_rules: list[str] = Field(default_factory=list)
    # Threads for the scan. The pass caps this at the usable CPU count
    # regardless, so 0 (auto) is one thread per core; on a single-core VM the
    # scan is serial no matter what. Lower it only to leave cores free.
    workers: int = Field(default=0, ge=0)
    # Characters of manuscript per request to the local server. Paragraphs are
    # joined by a blank line, which LanguageTool reads as a paragraph break, so
    # each is still analysed on its own — this only stops the fixed cost of a
    # request being paid once per paragraph, which on a book of ordinary
    # paragraphs is most of the scan. Measured on 28k words of prose, one
    # thread: 6.96s at one paragraph per request against 3.27s here, for a
    # candidate set that came back identical at every size tried (1 to 200
    # paragraphs a request). It is the lever that helps a one-core box, where the
    # thread pool cannot. 0 restores one request per paragraph.
    scan_chars: int = Field(default=20000, ge=0)
    max_output_tokens: int = Field(default=16000, ge=1)
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
    # Route every Sapling edit through the SHARED rewrite.confirm valve: an LLM
    # rules on each in literary context and KEEPs anything touching voice,
    # dialect, invented names, or style, so Sapling never edits blind. On by
    # default — the whole point of the pass on a novel. Off restores the older
    # behaviour (Sapling's edits fold straight in, gated only by the
    # deterministic sweeps/edit-guard), kept so a run can A/B raw vs valved and
    # measure the rejection rate.
    confirm: bool = True
    # Sapling's regional spelling variety: "", "us-variety", "gb-variety",
    # "au-variety", "ca-variety". Empty sends no preference.
    variety: str = ""
    # ERRANT classes to drop before the confirm valve, by category tail
    # ("PUNCT", "VERB:TENSE"), raw code ("R:ORTH"), or general bucket
    # ("Spelling"). On top of the always-on lexicon filter for author
    # names/coinages. Only consulted when `confirm` is on.
    disabled_error_types: list[str] = Field(default_factory=list)
    # Confirm-valve sizing/routing, mirroring LanguageToolConfig. edit_confidence
    # is the bar an affirmed edit clears to become a tracked change; a softer
    # affirmation is a margin query, never a silent change. High by default —
    # Sapling is confident and context-blind, so the LLM's doubt should ask, not
    # edit.
    max_output_tokens: int = Field(default=16000, ge=1)
    batch_size: int = Field(default=40, ge=1)     # candidates per confirm request
    edit_confidence: Literal["low", "medium", "high"] = "high"
    # The confirm model. Unset = api.model (the detector's) does its own
    # confirming; the prompts are short so a stronger model here is cheap.
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    # Whether a Sapling edit carries an explanatory margin comment. On, each one
    # reads like the model's own findings (a short line built from Sapling's
    # error code — see docproof/sapling.describe). Off, Sapling's changes apply
    # as silent tracked changes with nothing in the margin, for a cleaner file
    # when the correction speaks for itself. Rides under the global `comments`
    # switch: with all Word comments off, this changes nothing.
    comments: bool = True
    # What Sapling bills, in dollars per 1,000 characters of submitted text. Used
    # only to show an estimate before a run — Sapling itself is the source of
    # truth for the actual charge, and it never reaches the model-token cost math.
    cost_per_1k_chars: float = Field(default=0.025, ge=0)

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"sapling.confirm_model '{self.confirm_model}' "
                "is not in the catalog")
        return self


class ChapterSweepConfig(BaseModel):
    """The frontier chapter sweep: one loose proofread instruction over
    chapter-sized windows, on a strong model, as a second detector.

    Complementary to the typed passes by construction — no error-type taxonomy
    (so no taxonomy blind spots) and chapter-scale context (so cross-sentence
    slips are visible). The 2026-08-23 Redding pilot found it catches the
    judgment-call band (lay/lie, everyday/every day, parallelism, suspended
    hyphens, counterfactual tense) the chunked passes glide past, while the
    sweeps keep the mechanical floor. Proposals are verbatim quote->correction
    pairs anchored fail-closed, then ruled on by the SHARED rewrite.confirm
    valve — only an affirmed error becomes a tracked change, a softer
    affirmation asks in the margin, and the ordinary validator/audit stand
    behind everything. Off by default: it is a frontier-priced read of the
    whole manuscript. Whole-document only. See docproof/chaptersweep.py."""
    enabled: bool = False
    model: str = "claude-fable-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "xhigh"
    # Window sizing: ~48k chars ≈ a long chapter per request. Bigger windows
    # buy context and lose retry granularity; a failed window is skipped and
    # reported, never fatal.
    window_chars: int = Field(default=48_000, ge=4_000)
    # The ceiling covers THINKING too (xhigh on a frontier model), and a
    # truncated structured reply parses as nothing — the 2026-08-23 Redding run
    # lost 2 of 6 windows (a third of the book unswept) at 32k. 64k does not
    # raise the cost of a clean window: output is billed as generated and clean
    # windows stop well short (3k–25k on that run). Only a window that would
    # have truncated spends more, bounded by the extra headroom — and 32k of
    # that spend was already being burned for nothing.
    max_output_tokens: int = Field(default=64_000, ge=1)
    # Confirm-valve sizing, mirroring Sapling/LanguageTool. The sweep model
    # proposes; the confirm judge disposes. Unset confirm_model = api.model.
    batch_size: int = Field(default=40, ge=1)     # candidates per confirm request
    edit_confidence: Literal["low", "medium", "high"] = "high"
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"

    @model_validator(mode="after")
    def _known_models(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"chapter_sweep.model '{self.model}' is not in the catalog")
        if self.enabled and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"chapter_sweep.confirm_model '{self.confirm_model}' "
                "is not in the catalog")
        return self


class RepairConfig(BaseModel):
    """The repair channel: route error-dense sentences to a strong model and fix
    each as one atomic cluster.

    Where every other correcting pass emits an atomic token edit, this one
    repairs a broken sentence as a whole — and it is triggered by the other
    passes' own output. A sentence in which the ordinary detectors flag at least
    ``error_threshold`` separate corrections is probably broken as a whole, so
    it is routed to a strong model (Fable by default) for one minimal repair. The
    diff becomes a cluster of co-dependent member edits sharing one cluster_id; a
    skeptical judge rules on the whole cluster — broken, minimally fixed,
    meaning-preserved — and only an affirmation at ``edit_confidence`` writes, a
    softer one asks in the margin. The members sit early in arbitration so the
    coherent repair supersedes the scattered token edits that triggered it, and
    they ship as separate tracked changes but stand or fall together:
    ``repair.enforce_cluster_atomicity`` withdraws the whole cluster if any
    member does not survive, so the run never ships half a repair.

    OFF by default and the highest-risk pass in the pipeline — judgment edits
    that write were the corrections engine's QA failure class — so it ships
    behind the trigger threshold, the judge, the meaning gate, and the atomicity
    enforcement, and is meant to be measured in shadow mode before it is trusted
    to write. Whole-document only. See docproof/repair.py and docs/repair.md."""
    enabled: bool = False
    # THE TRIGGER. A sentence in which the ordinary detectors flag at least this
    # many separate corrections is routed to the repair model. 3 is deliberately
    # conservative — a sentence needing three separate fixes is likely broken as
    # a whole, not merely dotted with isolated slips — and the judge still
    # declines any flagged sentence that turns out only stylistic. Lower to cast
    # a wider net (more sentences routed, more model cost, the judge the backstop);
    # a sentence under the threshold keeps its individual token edits as before.
    error_threshold: int = Field(default=3, ge=2)
    # The repair reader. A repair is a judgment call on the whole sentence, so it
    # defaults to the strongest model; the read is cheap because it is scoped to
    # only the triggered sentences, not the whole book.
    model: str = "claude-fable-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # The ceiling covers THINKING too on a reasoning model, and a truncated
    # structured reply parses as nothing; a window that truncates is halved and
    # re-asked, and anything still unruled is counted, never taken for "left
    # unchanged".
    max_output_tokens: int = Field(default=16_000, ge=1)
    batch_size: int = Field(default=20, ge=1)   # triggered sentences per request
    # The judge that disposes. Defaults to the house reviewer so the pass needs
    # no key beyond the reader's; a stronger judge is a cheap per-run pick since
    # its verdicts are short. Unset confirm_model = api.model.
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # Only a repair affirmed at this confidence writes; softer affirmations
    # become one margin question carrying the whole repair. High by default: a
    # repair that writes is the riskiest edit the tool makes.
    edit_confidence: Literal["low", "medium", "high"] = "high"
    # The deterministic half of the fabrication defence (the judge's
    # meaning-preserved ruling is the other half). A "repair" that added more
    # than this many net characters, or touched more than this many spans, is a
    # rewrite rather than a repair and is dropped before the judge — so a
    # paraphrase cannot ride in under the validator's guard exemption that repair
    # findings get. A dropped clause is a legitimate repair, hence a cap well
    # above the ordinary 16-char edit-guard growth limit.
    max_added_chars: int = Field(default=120, ge=1)
    max_members: int = Field(default=12, ge=1)
    # Both prompts, editable per job like the smoothing and round judges'. Empty
    # uses the built-in contract in repair.py; a non-empty value replaces it.
    repair_prompt: str = ""
    judge_prompt: str = ""

    @model_validator(mode="after")
    def _known_models(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"repair.model '{self.model}' is not in the catalog")
        if self.enabled and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"repair.confirm_model '{self.confirm_model}' "
                "is not in the catalog")
        return self


class SmoothingConfig(BaseModel):
    """The line-editing pass: the half of a proofreader's job DocProof otherwise
    refuses. A line editor reads the manuscript and proposes small smoothings —
    a word doing no work, a preposition that is not the idiomatic one, an
    awkward coordination, a tense that reads rough, an ambiguous pronoun — and a
    skeptical taste judge culls them before any of them reach the author.

    Query-only by default: a mechanical error has a verifiable right answer
    and may be a tracked change, but a smoothing has no right answer, only a
    better one, and which is better is the author's call — so the shipped
    behaviour force_query's every finding into the margin. The ``edits``
    switch below is the explicit, per-run exception: with it on, a smoothing
    the judge affirms at HIGH confidence is applied as an ordinary tracked
    change (the author accepts or rejects it in Word) and softer affirmations
    still ask. Chosen for presses drowning in margin queries; the default
    stays ask-first.

    Voice risk is what the knobs defend against: dialogue is excluded by default
    (a character's diction is not the pipeline's to smooth), author coinages are
    filtered deterministically before the judge ever sees a candidate, and the
    volume is capped per 1,000 words so a book comes back with a handful of
    considered suggestions rather than a margin full of opinions. Suggestions
    dropped by that cap are counted in summary.md, never silently discarded.

    Echo — a distinctive word repeated close together — is deliberately NOT one
    of this pass's categories: the taxonomy already has `word_echo` for it, on
    the query channel, and two passes asking the author the same question about
    the same repetition is worse than either asking alone.

    OFF by default and opt-in per run: it costs a full manuscript read plus a
    judge, and it is the one pass whose output is taste rather than correctness.
    Whole-document only. See docproof/smoothing.py."""
    enabled: bool = False
    # How an affirmed smoothing reaches the author. False (the shipped
    # default): every finding is force_query'd — a margin comment, never an
    # edit, because a smoothing has no verifiable right answer. True: an
    # affirmation the judge holds at HIGH confidence is applied as an ordinary
    # tracked change (still through the shared validator/audit), and anything
    # softer keeps asking in the margin. An explicit per-run choice for presses
    # that would rather accept/reject in Word than answer a margin full of
    # questions — it trades the query pile for edits the author can reject.
    edits: bool = False
    # The proposing reader. Unset = api.model (the detector's). Restraint is
    # most of the job, so this is not the place to economize hard — but the
    # judge below is the one that decides what survives.
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    # The taste judge. Defaults to the house reviewer so the pass needs no key
    # beyond the detector's; it is the one that stands between the proposer's
    # enthusiasm and the author's margin, and telling a genuine smoothing from a
    # merely-conventional rephrasing is the hard part — so a stronger judge is a
    # per-run pick, cheap relative to the read since its prompts are short.
    judge_model: str = "gpt-5.6-luna"
    judge_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high"
    # Whether to smooth inside quoted speech. Off by default: voice risk
    # concentrates in dialogue, where "awkward" is frequently the point.
    include_dialogue: bool = False
    # The volume cap, in suggestions per 1,000 words of manuscript. Ranked by
    # judge confidence; everything past the cap is dropped AND counted in
    # summary.md. Atmosphere's own proofreaders smooth at roughly this rate.
    max_per_1000_words: float = Field(default=3.0, gt=0)
    # Drop anything the judge affirms softer than this, before the cap. A
    # suggestion the judge is lukewarm about costs the author more attention
    # than it earns. C3 of the recall roadmap is simply lowering this to "low"
    # and letting the ranked cap be the single volume control — no code change,
    # since "low" is already a value; it stays at "medium" by default because it
    # surfaces the judge's defensible-but-skippable calls, the most FP-prone.
    min_confidence: Literal["low", "medium", "high"] = "medium"
    batch_size: int = Field(default=40, ge=1)   # candidates per judge request
    # The proposing read. Sized like the judge below and for the same reason: on
    # a reasoning model the ceiling covers the THINKING too, so 4,000 truncated a
    # dense window on real input — and unlike the judge, a truncated propose read
    # is dropped whole and shows up only as fewer suggestions, which reads as
    # restraint. An unused ceiling is free; you are billed for tokens generated,
    # never for the cap. propose() also counts any read that still fails, so a
    # residual truncation is reported rather than mistaken for silence.
    max_output_tokens: int = Field(default=16000, ge=1)
    # The judge gets its own, much larger ceiling. Its VISIBLE output is tiny —
    # a verdict is three fields — but on a reasoning model the thinking counts
    # against this budget too, and judging forty literary calls at high effort
    # burns far more of it than the verdicts occupy. Sized from the propose
    # ceiling it truncates, and a truncated batch returns no verdicts at all:
    # every candidate in it vanishes, and the run reports a restrained pass
    # rather than a failed one. Measured that on the first real book.
    judge_max_output_tokens: int = Field(default=16000, ge=1)
    # Both system prompts, editable per job the way the round judge's is. Empty
    # (the default) uses the built-in one in smoothing.py; a non-empty value
    # replaces it wholesale.
    propose_prompt: str = ""
    judge_prompt: str = ""

    # --- Tier C: opt-in recall levers ---------------------------------------
    # Each of these defaults to today's behaviour, so a run that sets none of
    # them is byte-for-byte the shipped pass. They exist so the proposer's
    # restraint, the judge's preference-veto, the window size, and the dialogue
    # skip can each be loosened and MEASURED without changing what a production
    # run does until a flag is set. None is a proven recall gain yet (the pass
    # reads statistically indistinguishable from null on the current corpus), so
    # none ships on. See docproof/smoothing.py and the taste-pass recall memo.

    # C1 — where the proposer's restraint lives. "restrained" is the shipped
    # PROPOSE_SYSTEM; "open" swaps in PROPOSE_SYSTEM_OPEN, which keeps every
    # voice-safety constraint (the NEVER-touch block, single-sentence,
    # meaning-identical, the mechanical-error wall) VERBATIM but drops the "say
    # almost nothing" framing, leaving the skeptical judge as the sole taste
    # gate. The proposer is the binding constraint on how much this pass
    # surfaces — a fully-open, unjudged read still finds only ~100 on a whole
    # novel — so this is the lever with the most movement, and the most voice
    # risk, which is why it is off by default and gated on measurement.
    proposer_restraint: Literal["restrained", "open"] = "restrained"
    # C2 — how much manuscript goes into one propose read. Smaller windows probe
    # whether a "most paragraphs get nothing" proposer satisfices: it surfaces a
    # similar handful whatever the window holds, so per-paragraph recall would
    # fall as the window grows. Defaults are today's constants; ~5000 / ~24 is
    # the setting to test. Smaller windows also give less cross-paragraph
    # context, on which the rhetorical-repetition protections partly rely — run
    # it as an isolated A/B, not alongside another proposer change.
    propose_chars: int = Field(default=12_000, ge=1)
    propose_max_paras: int = Field(default=60, ge=1)
    # C4 — the judge's HARSHNESS, as a dial rather than a fixed prompt. The judge
    # is what decides how much of the proposer's output reaches the author, and
    # the right setting differs by manuscript and by author, so it is a selector
    # like the reasoning-effort knob. Four levels, least- to most-rejecting:
    #   lenient  — lean toward keeping; reject only voice damage / no improvement
    #   balanced — judge on merits; keep what earns its place, need not reject most
    #   strict   — DEFAULT TO NO, expect to reject most (the shipped JUDGE_SYSTEM)
    #   severe   — keep only the undeniable handful
    # "strict" is the shipped prompt byte-for-byte, so the default changes nothing.
    # Across the whole dial the three voice-SAFETY vetoes (dialect/idiolect/coined/
    # character-voice, fragment/rhetorical-repetition, meaning/emphasis/rhythm)
    # hold verbatim — leniency buys back the merely-conventional and preference
    # rejects, never the voice line. See JUDGE_SYSTEMS in docproof/smoothing.py.
    judge_harshness: Literal["lenient", "balanced", "strict", "severe"] = "strict"
    # C5 — clarity-only smoothing INSIDE dialogue. Dialogue is skipped wholesale
    # by default; `include_dialogue` above is the all-or-nothing opt-in, and this
    # is the middle setting. A candidate that overlaps quoted speech is normally
    # dropped before the judge; if its category is listed here it survives to the
    # judge instead. Only "clarity" is permitted (enforced below): an ambiguous
    # pronoun can be a real error even in speech, whereas tightening or
    # re-idioming a character's diction is exactly the voice damage the dialogue
    # skip exists to prevent. Empty (the default) is today's total skip. The 9
    # dialect/idiolect silence-trap items are the tripwire if this is widened.
    dialogue_categories: list[str] = Field(default_factory=list)

    @field_validator("dialogue_categories")
    @classmethod
    def _clarity_only_in_dialogue(cls, value):
        """Only clarity may be smoothed inside dialogue. An ambiguous pronoun can
        be a genuine error even in speech; tightening or re-idioming a
        character's line is the voice damage the dialogue skip exists to prevent,
        so the wider categories are refused at load rather than silently accepted
        and quietly changing a character's diction."""
        bad = [c for c in value if c != "clarity"]
        if bad:
            raise ValueError(
                "smoothing.dialogue_categories may contain only 'clarity'; got "
                f"{bad}. Widening dialogue smoothing past clarity is the voice "
                "risk the dialogue skip is there to prevent.")
        return value

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if not self.enabled:
            return self
        if self.model is not None and lookup(self.model) is None:
            raise ValueError(
                f"smoothing.model '{self.model}' is not in the catalog")
        if lookup(self.judge_model) is None:
            raise ValueError(
                f"smoothing.judge_model '{self.judge_model}' "
                "is not in the catalog")
        return self


class DetectorSpec(BaseModel):
    """One reviewer in an ensemble: a model and how hard it thinks. The provider
    is read from the catalog, exactly as api.model is, so a detector is just a
    model id. Two identical specs is a self-ensemble (the same model sampled
    twice); different vendors is the diverse one — both are configuration, not
    code."""
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"


class FactcheckConfig(BaseModel):
    """One whole-book read for REAL-WORLD factual slips — institutional names
    and acronym expansions, historical figures and events, geography — the
    class the human second reviewer caught 3-of-4 of and the pipeline caught
    none (DP-004). Every catch is a margin query, never an edit: fiction
    bends the world on purpose, so a fact is the author's to settle. Additive
    and best-effort like the glossary; cached per draft; priced for its own
    model. Off by default: a whole extra read, opt-in until proven — the same
    bar every other added read clears. See docproof/factcheck.py."""
    enabled: bool = False
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    max_output_tokens: int = Field(default=16000, ge=1)
    # The margin is an author's attention: past this many, the rest are
    # logged, not placed.
    max_queries: int = Field(default=40, ge=1)
    # None = the shared whole-book cache (see default_cache_dir).
    cache_dir: str | None = None


class AnachronismScanConfig(BaseModel):
    """Query-only: flags vocabulary that reads as later than the manuscript's
    own stated era — the same external-authority risk factcheck carries, so
    the same rule applies: every catch is a margin question, never an edit.
    Deterministic (a curated word -> earliest-plausible-year table, sanity-
    checked against wordfreq so a typo in the table cannot silently misfire),
    no API call. See docproof/genrescans.py."""
    enabled: bool = False
    # The manuscript's own setting, as a year AD the vocabulary should
    # predate. None means "not stated," and the scan is a deliberate no-op
    # even when enabled — guessing the era and then flagging against the
    # guess is exactly the hallucination risk this pass exists to avoid.
    # `docproof galley profile` never fills this in on its own for the same
    # reason; a human (or the genre-pack `--profile` flow, which still only
    # carries forward a year the profile pass was explicitly told) sets it.
    era: int | None = None
    max_queries: int = Field(default=40, ge=1)


class CitationFormatScanConfig(BaseModel):
    """Query-only: deterministic regex families that catch two citation
    styles mixed in one manuscript (parenthetical author-year beside numbered-
    bracket). Non-fiction only — a novel has no citations to be inconsistent
    about, so this stays off unless a genre pack turns it on. See
    docproof/genrescans.py."""
    enabled: bool = False
    # Each style needs at least this many occurrences before it counts as "a
    # style this book uses" rather than an isolated one-off match.
    min_occurrences: int = Field(default=2, ge=1)
    max_queries: int = Field(default=40, ge=1)


class ReadingLevelScanConfig(BaseModel):
    """Query-only: flags paragraphs whose reading level sits far outside the
    book's own target band, scored with the Automated Readability Index (chars
    per word, words per sentence — no syllable counter needed) plus the mean
    word rarity of its content words (wordfreq's zipf scale, reused as-is from
    docproof/adjudicate.py). Self-referential by default: with `target_ari`
    unset, the band is centered on the manuscript's OWN median paragraph, so
    the scan asks "is this paragraph unlike the rest of the book" rather than
    imposing an outside notion of what the genre should read like. See
    docproof/genrescans.py."""
    enabled: bool = False
    target_ari: float | None = None
    tolerance: float = Field(default=6.0, gt=0)
    # A paragraph shorter than this many words has too little signal for the
    # formula to mean anything (a one-line scene break, a salutation).
    min_words: int = Field(default=40, ge=1)
    max_queries: int = Field(default=40, ge=1)


class FlightsConfig(BaseModel):
    """The copy-edit flights lane (docproof/flights.py).

    ``posture`` is the judge's default stance — the measured 24%↔57%
    accepted-recall dial. "strict" defaults to keeping the original;
    "lenient" leans toward accepting (same hard vetoes either way). Genre
    posture presets set this per manuscript (config/genres/*.yaml); the
    ``docproof galley flights --posture`` flag overrides it per run."""
    posture: Literal["strict", "lenient"] = "strict"


class GenreScansConfig(BaseModel):
    """Three opt-in, deterministic (or wordfreq-only), whole-document QUERY-
    ONLY scans a genre pack may turn on — see docproof/genrescans.py and
    docproof/genre.py. Off by default like every other added whole-book pass;
    nothing here ever writes a tracked change, by construction (every finding
    sets force_query)."""
    anachronism: AnachronismScanConfig = Field(
        default_factory=AnachronismScanConfig)
    citation_format: CitationFormatScanConfig = Field(
        default_factory=CitationFormatScanConfig)
    reading_level: ReadingLevelScanConfig = Field(
        default_factory=ReadingLevelScanConfig)


class ResidualsConfig(BaseModel):
    """After validation, re-scan the reviewed text for number-rule trigger
    sites (bare numerals to one hundred, percent signs, digit ordinals) that
    no validated edit touched, and raise each as a margin query. A rule
    applied to some-but-not-all of its matches erodes reviewer trust faster
    than a rule that is absent; this makes the leftover visible. Queries only
    — nothing is edited. See docproof/residuals.py."""
    enabled: bool = True
    # Per rule, so a statistics-heavy manuscript cannot flood the margin. The
    # overflow is logged, never silently dropped.
    max_per_rule: int = Field(default=150, ge=1)


class RecurrenceConfig(BaseModel):
    """After validation, a free deterministic post-pass that closes the
    catch-it-here-miss-it-there gap: every validated edit that swaps one verbatim
    word or short phrase for another is searched across the whole document, and
    each *other* occurrence of the original surface is re-emitted as a finding
    proposing the same swap. A typo fixed in chapter 2 but missed in chapter 9 is
    now fixed in both, and every detector's reach becomes the whole book at once.

    Safety is deterministic and layered (see docproof/adjudicate.py):
    only whole-word/short-phrase alphabetic swaps propagate; a surface the
    validated edits disagree about (changed to X here, Y there — the effect/affect
    case) is dropped as ambiguous; a real dictionary word is context-dependent so
    its recurrences are raised as margin QUERIES, while a genuine non-word typo or
    proper-name misspelling propagates as a tracked edit; sites the run already
    edits are skipped and the output is re-validated, so the validator dedups any
    span; and the spell scan's protected lexicon is never swept. Whole-document
    only — a partial run must not edit text it was not asked to read."""
    enabled: bool = True
    # A bound per surface, so one very common word cannot flood the document.
    # The overflow is logged, never silently dropped.
    max_sites_per_surface: int = Field(default=200, ge=1)


class ExaminationJudgmentConfig(BaseModel):
    """Optional paid judgment over precise examination sites.

    This is a separate switch from site accounting because measurement is free
    while model judgment is not. Phase 1B remains shadow-only: verdicts are
    compared with the production reviewer but can never become Findings.
    """
    enabled: bool = False
    primary_model: str = "gpt-5.6-luna"
    primary_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = \
        "low"
    escalation_model: str | None = None
    escalation_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = \
        "high"
    batch_size: int = Field(default=100, ge=1, le=200)
    max_output_tokens: int = Field(default=16_000, ge=1)
    max_missing_retries: int = Field(default=1, ge=0, le=3)
    max_sites: int = Field(default=2_000, ge=1)
    max_cost_usd: float = Field(default=2.0, gt=0)
    sample_rate: float = Field(default=0.10, gt=0.0, le=1.0)
    # Phase 1B starts only with exact, locally generated candidates. The broad
    # paragraph/category placeholders remain useful coverage measurements but
    # are not correction-shaped sites and must not be billed as if they were.
    eligible_generator_prefixes: tuple[str, ...] = (
        "deterministic.spellscan",
        "deterministic.adjudicate",
    )
    allow_legacy_obligations: bool = False


class ExaminationGraphConfig(BaseModel):
    """Examination ledger, isolated behind a shadow-mode flag.

    Shadow mode may write ledger/report artifacts but is never allowed to feed
    findings into the validator or tracked-change writer.  ``Config()`` keeps it
    off for API/backward compatibility; the shipped YAML opts into measurement
    explicitly and can roll back by changing one value to false.
    """
    enabled: bool = False
    mode: Literal["shadow"] = "shadow"
    model_obligations: bool = True
    # Phase 2 makes a successful production detector response explicitly name
    # every paragraph it reviewed.  A named paragraph with no finding is then a
    # model pass for that paragraph/category obligation; a missing name stays
    # pending.  The verdict is ledger evidence only and cannot create a Finding.
    production_verdicts: bool = False
    spell_sites: bool = True
    max_sites: int = Field(default=200_000, ge=1)
    write_ledger: bool = True
    write_report: bool = True
    ledger_filename: str = "examination-ledger.jsonl.gz"
    report_json_filename: str = "examination-coverage.json"
    report_filename: str = "examination-coverage.md"
    evaluation_filename: str = "examination-evaluation.json"
    evaluation_key_filename: str = "examination-evaluation-key.json"
    judgment: ExaminationJudgmentConfig = Field(
        default_factory=ExaminationJudgmentConfig)

    @field_validator("ledger_filename", "report_json_filename", "report_filename",
                     "evaluation_filename", "evaluation_key_filename")
    @classmethod
    def _plain_filename(cls, value):
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("examination artifact names must be plain filenames")
        return value

    @model_validator(mode="after")
    def _production_verdicts_need_obligations(self):
        if self.production_verdicts and not self.model_obligations:
            raise ValueError(
                "examination_graph.production_verdicts requires "
                "model_obligations")
        return self


class CandidateScreeningConfig(BaseModel):
    """Explicit candidate generation, screening, and guarded application.

    ``mode`` is the single operator-facing switch: ``off`` removes the lane,
    ``shadow`` records verdicts without creating findings, and ``apply`` sends
    only correction-validated error verdicts through the ordinary Finding ->
    validator -> tracked-change path. Generation and deterministic screening
    are local; ``judgment_enabled`` controls the paid ambiguous tail.
    """
    mode: Literal["off", "shadow", "apply"] = "off"
    candidate_types: tuple[str, ...] = (
        "dialogue_tag_punctuation", "quote_balance", "introductory_comma",
        "direct_address_comma", "number_style", "currency_style",
        "repeated_word", "word_echo", "heading_sequence",
        "list_punctuation", "punctuation_style", "homophone",
        "compound_sentence_comma", "term_consistency", "grammar",
    )
    # P2-01/02: reuse the free local analyzers (sweeps, unbalanced-quote and
    # term-consistency scans) as candidate sources so standalone candidate mode
    # is not limited to the per-paragraph regex generators. Everything they find
    # still flows through the ledger as candidates, never straight to the output.
    reuse_local_analyzers: bool = True
    # LanguageTool's mechanical floor is a strong grammar source but spins up an
    # ~850MB JVM; keep it opt-in so a small box is never surprised by it.
    languagetool_floor: bool = False
    max_candidates: int = Field(default=200_000, ge=1)
    # 40 candidates per judge request, like every other pass: the Johnson canary
    # showed 100-candidate packets invite ID slips (duplicate/hallucinated IDs)
    # and make each retry expensive.
    batch_size: int = Field(default=40, ge=1, le=200)
    judgment_enabled: bool = True
    # Send the primary judgment as one vendor batch (50% cheaper) instead of a
    # packet-at-a-time synchronous sweep. The lane block-polls the batch to
    # completion inside its screen() call, then re-judges any packet the batch
    # could not resolve on the synchronous focused-retry path. Escalation stays
    # synchronous (it is small and can only be built once primary verdicts are
    # in). Off by default: the synchronous path is unchanged.
    batch: bool = False
    # How the block-poll waits on the primary batch. Poll every interval; give
    # up after max_wait and drop every unresolved packet to the synchronous
    # path (never a silent loss). 24h matches the vendor completion window.
    batch_poll_interval_seconds: float = Field(default=20.0, gt=0)
    batch_max_wait_seconds: float = Field(default=86_400.0, gt=0)
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    escalation_model: str | None = None
    escalation_effort: Literal[
        "low", "medium", "high", "xhigh", "max"] | None = "high"
    max_output_tokens: int = Field(default=16_000, ge=1)
    max_missing_retries: int = Field(default=1, ge=0, le=3)
    max_cost_usd: float = Field(default=2.0, gt=0)
    write_ledger: bool = True
    write_report: bool = True
    ledger_filename: str = "candidate-screening-ledger.jsonl.gz"
    report_json_filename: str = "candidate-screening-report.json"
    report_filename: str = "candidate-screening-report.md"

    @field_validator("candidate_types")
    @classmethod
    def _known_candidate_types(cls, value):
        from .candidate_generators import INITIAL_CANDIDATE_TYPES
        unknown = set(value) - set(INITIAL_CANDIDATE_TYPES)
        if unknown:
            raise ValueError(f"unknown candidate type(s): {sorted(unknown)}")
        if len(set(value)) != len(value):
            raise ValueError("candidate_types contains duplicates")
        return value

    @field_validator("ledger_filename", "report_json_filename",
                     "report_filename")
    @classmethod
    def _plain_candidate_filename(cls, value):
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError(
                "candidate screening artifact names must be plain filenames")
        return value


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
    max_output_tokens: int = Field(default=12000, ge=1)
    # A path pins the read per draft, like glossary.cache_dir — and, like it,
    # unset means the shared default folder rather than no cache.
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


class LowConfidenceConfig(BaseModel):
    """What becomes of a model EDIT that falls below `min_confidence`.

    By default the gate's original, precision-first behaviour holds: the edit
    never becomes a tracked change, and (with query_comments on) surfaces only as
    a margin comment. The type prompts mark anything inside dialogue "low" on
    purpose — dialect and voice are usually deliberate there — but that also
    strands the real dialogue MECHANICS (a missing comma before a tag, its/it's
    inside a quote): the densest error zone in fiction can never reach the
    manuscript as an edit, only as a question.

    With `confirm` on, each below-gate edit is re-ruled by an LLM in literary
    context through the SHARED rewrite.confirm valve: one affirmed at
    `edit_confidence` is PROMOTED to a tracked change, a softer affirmation
    becomes a margin query, and a "not an error" verdict drops it. That recovers
    the genuine catch without lowering the gate for everything — precision is
    restored downstream instead of up front. Queries and formatting marks are not
    edits and never enter the valve; nor does anything already above the gate.

    Off by default: it is a paid pass, so measure the recall/precision delta on
    the private harness before shipping it on, exactly as rewrite/languagetool/
    sapling are gated. Knobs mirror SaplingConfig's confirm block."""
    confirm: bool = False
    max_output_tokens: int = Field(default=16000, ge=1)
    batch_size: int = Field(default=40, ge=1)     # candidates per confirm request
    edit_confidence: Literal["low", "medium", "high"] = "high"
    # The confirm model. Unset = api.model (the detector's) does its own
    # confirming; the prompts are short so a stronger model here is cheap.
    confirm_model: str | None = None
    confirm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.confirm and self.confirm_model is not None and \
                lookup(self.confirm_model) is None:
            raise ValueError(
                f"low_confidence.confirm_model '{self.confirm_model}' "
                "is not in the catalog")
        return self


class JudgeGateConfig(BaseModel):
    """One judge gate: a strong model reading every proposed change, from every
    source, immediately before the tracked changes are written.

    Two of these ship — see MeaningCheckConfig and FixCheckConfig for the
    question each one asks. Everything below is common to both, because the only
    thing that differs between judges is the question: a change the judge will
    not vouch for is downgraded to a margin question with its reason attached,
    never dropped and never silently applied, and fail-open throughout so an
    unanswerable call leaves the change untouched.

    Off by default — these are paid passes — but the cheapest paid passes in the
    pipeline to run well, because their cost tracks the number of CHANGES rather
    than the length of the book, which is what makes a frontier `model`
    affordable here. See docproof/judges.py."""
    # The gate's own key in docproof.judges.SPECS, for error messages and for
    # the record a run leaves behind. Set by each subclass, never by config.
    judge_key: ClassVar[str] = ""

    enabled: bool = False
    # The judge. Defaults to the house reviewer so the gate needs no key beyond
    # the detector's; a frontier model can be the point of these passes — they
    # are the last reader before the author, and each sees a few hundred short
    # prompts, not the whole manuscript — so a stronger judge is a per-run pick.
    model: str = "gpt-5.6-luna"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Which changes are read. "model_sources" is every change proposed by a
    # model or an outside checker — the detector passes, rewrite, adjudicate,
    # low-confidence promotions, LanguageTool and Sapling — and is the default.
    # "all" adds the deterministic house-style sweeps and the consistency scan,
    # which are scripted, punctuation-sized, and cannot be wrong in the ways
    # these gates look for; paying a frontier model to confirm that is usually
    # waste, but the option is here for a run that wants nothing reaching the
    # author unread.
    scope: Literal["model_sources", "all"] = "model_sources"
    # Treat an "unsure" verdict as a downgrade. On by default: the gate exists to
    # stop a silent change, and a judge that cannot vouch for one has not vouched
    # for it. Off applies anything not positively flagged.
    flag_unsure: bool = True
    # Mostly reasoning tokens (frontier judge at effort high). At 12k the
    # 2026-08-23 Redding run truncated on one heavily corrected paragraph and
    # its 7 changes were applied UNREAD — the fail-open worst case. The judge
    # models here are cheap relative to the detectors, and a clean reply stops
    # far short of the ceiling, so the raise costs only on calls that would
    # otherwise have truncated.
    max_output_tokens: int = Field(default=24000, ge=1)
    # The judge's instructions, meant to be edited per job in the review panel.
    # Empty uses the built-in default (docproof.judges.default_prompt(key)), so
    # clearing the field reverts to it.
    prompt: str = ""

    @model_validator(mode="after")
    def _known_model(self):
        from .providers.catalog import lookup
        if self.enabled and lookup(self.model) is None:
            raise ValueError(
                f"{self.judge_key}_check.model '{self.model}' "
                "is not in the catalog")
        return self


class MeaningCheckConfig(JudgeGateConfig):
    """Does the corrected sentence still MEAN what the original meant?

    A fix can be defensible as grammar and still change the book — a dropped
    negation, a homophone resolved the wrong way, a tense that moves when an
    event happened. Every other check in the pipeline asks whether something is
    an error and whether the fix is right; this asks the one question none of
    them do, over the changes that survived all of them, source-blind: a Sapling
    suggestion, a LanguageTool comma, a rewrite diff and a detector finding
    arrive here as the same kind of object."""
    judge_key: ClassVar[str] = "meaning"


class FixCheckConfig(JudgeGateConfig):
    """Is the replacement the CORRECT fix?

    Separate from the meaning gate on purpose, and separately switchable. A
    change can preserve the sense perfectly and still be the wrong repair —
    "their" corrected to "there" where "they're" was wanted, a verb put in the
    wrong form for its subject, a semicolon the clauses will not carry, an
    agreement error the fix creates rather than removes. One judge asked to weigh
    both questions at once does neither well, so each gets its own pass, its own
    prompt, and its own model."""
    judge_key: ClassVar[str] = "fix"


class PassSpec(NamedTuple):
    """One DEFINED error-type category from `error_types`: its keys, how many
    times to read it, and an optional per-category chunk budget (None = the
    global `chunking.token_budget`). This is the category as written; expanding
    `passes` into individual API passes, and resolving the budget into a chunk
    set, happens in the pipeline where the chunks live."""
    keys: tuple[str, ...]
    passes: int = 1
    token_budget: int | None = None


_ERROR_ENTRY_KEYS = frozenset({"group", "passes", "token_budget"})


def _normalize_error_entry(entry) -> PassSpec:
    """One `error_types` entry -> PassSpec. Accepts a bare key, a list of keys,
    or a mapping {group, passes?, token_budget?}. Raises ValueError on a shape
    the schema does not allow (the field validator relies on this)."""
    if isinstance(entry, str):
        return PassSpec((entry,))
    if isinstance(entry, (list, tuple)):
        return PassSpec(tuple(entry))
    if isinstance(entry, dict):
        unknown = set(entry) - _ERROR_ENTRY_KEYS
        if unknown:
            raise ValueError(
                f"error_types: unknown key(s) {sorted(unknown)} in a group "
                f"mapping; allowed: {sorted(_ERROR_ENTRY_KEYS)}")
        group = entry.get("group")
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, (list, tuple)) or not group:
            raise ValueError(
                "error_types: a group mapping needs a non-empty 'group' list")
        passes = entry.get("passes", 1)
        if not isinstance(passes, int) or isinstance(passes, bool) or passes < 1:
            raise ValueError(
                f"error_types: 'passes' must be an integer >= 1, got {passes!r}")
        budget = entry.get("token_budget")
        if budget is not None and (
                not isinstance(budget, int) or isinstance(budget, bool)
                or budget < 1):
            raise ValueError(
                f"error_types: 'token_budget' must be an integer >= 1, "
                f"got {budget!r}")
        return PassSpec(tuple(group), passes, budget)
    raise ValueError(
        "error_types: each entry must be a key, a list of keys, or a mapping "
        "with a 'group' list")


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
    chapter_continuity: ChapterContinuityConfig = Field(
        default_factory=ChapterContinuityConfig)
    adjudicate: AdjudicateConfig = Field(default_factory=AdjudicateConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    languagetool: LanguageToolConfig = Field(default_factory=LanguageToolConfig)
    sapling: SaplingConfig = Field(default_factory=SaplingConfig)
    chapter_sweep: ChapterSweepConfig = Field(default_factory=ChapterSweepConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    smoothing: SmoothingConfig = Field(default_factory=SmoothingConfig)
    factcheck: FactcheckConfig = Field(default_factory=FactcheckConfig)
    genre_scans: GenreScansConfig = Field(default_factory=GenreScansConfig)
    flights: FlightsConfig = Field(default_factory=FlightsConfig)
    residuals: ResidualsConfig = Field(default_factory=ResidualsConfig)
    recurrence: RecurrenceConfig = Field(default_factory=RecurrenceConfig)
    examination_graph: ExaminationGraphConfig = Field(
        default_factory=ExaminationGraphConfig)
    candidate_screening: CandidateScreeningConfig = Field(
        default_factory=CandidateScreeningConfig)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)
    rounds: RoundsConfig = Field(default_factory=RoundsConfig)
    low_confidence: LowConfidenceConfig = Field(default_factory=LowConfidenceConfig)
    meaning_check: MeaningCheckConfig = Field(default_factory=MeaningCheckConfig)
    fix_check: FixCheckConfig = Field(default_factory=FixCheckConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    # Which English this manuscript is written in. A handful of conventions
    # flip on it — which mark opens dialogue, decade apostrophes, percent
    # versus per cent, the that/which rule — and it selects the spell-scan
    # dictionary. Stated, never inferred: guessing the variant and then
    # applying its rules silently is how a U.K. manuscript comes back
    # Americanized. See docproof/variants.py.
    variant: Literal["us", "uk", "ca", "au"] = "us"
    min_confidence: Literal["low", "medium", "high"] = "medium"
    # Each entry is one error-type category: a bare key runs alone, a list of
    # keys runs as a single combined pass, and a mapping {group, passes?,
    # token_budget?} tunes that one category — how many times it is read and at
    # what chunk size — without touching the global budget or the other
    # categories. Grouping trades a little detection focus for a large cut in
    # input tokens — see docs/error-types.md.
    error_types: list[str | list[str] | dict] = Field(default_factory=list)
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
    # Beyond this many identical rule explanations, only the first edit keeps
    # its margin comment (with a count and a pointer at the change log); the
    # rest apply silently. 0 leaves every comment in place. Tracked changes
    # are never collapsed — this is about the note beside them, not the edit.
    comment_collapse: int = Field(default=3, ge=0)
    # The other half of the two-channel model: findings that ask rather than
    # correct become margin comments with no revision around them. Query-only
    # error types always do; this decides whether below-gate findings join
    # them, or stay in summary.md where only an editor will see them.
    query_comments: bool = True
    # Whether a correction the tool chose NOT to make leaves a margin comment on
    # the document. Three kinds: an edit a judge gate or the verifier withdrew
    # (the "Not applied: …" note), a below-gate catch, an oversized fix. OFF by
    # default: these are recorded in the change log and findings.json, but the
    # delivered document stays clean of them — an author reads the changes and
    # the genuine queries, not a running commentary on what was declined. Turn
    # it on to put them back in the margin (then `query_comments` still decides
    # the below-gate/oversized ones, exactly as before). A genuine query — a
    # question from an asking pass (continuity, fact-check, consistency, an
    # unconverted number-rule site) — is unaffected and always shown.
    not_applied_comments: bool = False
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
    # The merge desk's per-lane author override (docproof/mergedesk.py,
    # Finding.lane): a finding whose lane has an entry here is attributed to
    # that name instead of `revision_author` when its tracked change or
    # comment is written, so a merged deliverable can carry two authors —
    # Word's "Show Markup > Specific People" then filters one lane from the
    # other. A lane with no entry (including "", every finding outside a
    # merged run) falls back to `revision_author` unchanged, so a run that
    # never tags a lane writes byte-identical output to before this existed.
    lane_authors: dict[str, str] = Field(default_factory=lambda: {
        "copyedit": "Atmosphere Press Copy Editor",
    })
    # Ask the model to justify each finding. The explanations become Word
    # margin comments, but they are also the bulk of the output tokens — and
    # output bills at roughly 5x input. Turn this off for a cheaper pass that
    # still applies every correction, just without the marginalia.
    report_explanations: bool = True
    # Where edited error-type prompts live. Files here shadow the shipped
    # config/error_types/*.yaml by key; missing keys fall through to the
    # originals, so an override directory only has to hold what changed.
    error_type_override_dir: str | None = None

    # Path to an intent-zones JSON (docproof/intent_zones.py). When set, the
    # deterministic sweep layer consults the resolved zones before its findings
    # reach the author: a sweep whose edit falls inside a protected span, and
    # whose kind that span's permission forbids, is downgraded to a query rather
    # than auto-applied. None (default) = no zones, behaviour unchanged.
    intent_zones_file: str | None = None

    @field_validator("error_types")
    @classmethod
    def _validate_error_types(cls, value):
        seen: set[str] = set()
        for entry in value:
            spec = _normalize_error_entry(entry)   # raises on a bad shape
            if not spec.keys:
                raise ValueError("error_types: a group must list at least one key")
            for key in spec.keys:
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("error_types: keys must be non-empty strings")
                if key in seen:
                    # A key belongs to exactly one category. To read a category
                    # more than once use its `passes:`, not the same key twice —
                    # two groups sharing a key would double every finding.
                    raise ValueError(
                        f"error_types: '{key}' appears more than once; put a key "
                        f"in a single category and use 'passes' to repeat it")
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
    def error_type_specs(self) -> tuple[PassSpec, ...]:
        """One PassSpec per DEFINED category, carrying its repeat count and its
        optional per-category chunk budget. Repeats are NOT expanded here — a
        category with passes=2 is one PassSpec whose `passes` is 2; the pipeline
        expands it once it has the chunks."""
        return tuple(_normalize_error_entry(entry) for entry in self.error_types)

    @property
    def error_type_groups(self) -> tuple[tuple[str, ...], ...]:
        """The keys of each defined category, one tuple per category. Repeat
        counts and budgets do not change which types exist, so this stays the
        keys-only view every downstream (query/format channels, labels) reads."""
        return tuple(spec.keys for spec in self.error_type_specs)

    @property
    def error_type_keys(self) -> tuple[str, ...]:
        """Every enabled key, flat, in category order (each key once, however
        many times its category is read)."""
        return tuple(k for group in self.error_type_groups for k in group)

    def category_states(self) -> list[dict]:
        """Each defined error-type category as a per-run knob the review panel
        can render and pre-fill. The `id` is content-based (its keys joined) so
        a knob keyed by it still matches when a batch is collected days later
        and the config is re-read; the group's ORDER in error_types is not a
        stable handle, its keys are. `token_budget` is the category's own chunk
        budget (None = it uses the global one, surfaced as `default_token_budget`
        so the panel shows the real size it would fall back to)."""
        default_budget = self.chunking.token_budget
        return [{"id": "+".join(spec.keys),
                 "keys": list(spec.keys),
                 "passes": spec.passes,
                 "token_budget": spec.token_budget,
                 "default_token_budget": default_budget}
                for spec in self.error_type_specs]

    def apply_category_knobs(self, knobs: dict | None) -> None:
        """Layer per-run per-category knobs onto error_types. `knobs` maps a
        category id (see category_states) to a mapping with an optional 'passes'
        and/or 'token_budget'; an id matching no category is ignored. Only the
        touched categories are rewritten to the mapping form — the rest keep the
        form they were written in — and a knob that resolves to the defaults
        (passes 1, no budget) collapses back to the plain key/list form so the
        pass plan is byte-for-byte unchanged. Reassigns error_types so the field
        validator re-checks the result."""
        if not knobs:
            return
        rewritten: list = []
        for entry in self.error_types:
            spec = _normalize_error_entry(entry)
            knob = knobs.get("+".join(spec.keys))
            if not knob:
                rewritten.append(entry)
                continue
            passes = knob.get("passes", spec.passes)
            budget = knob.get("token_budget", spec.token_budget)
            mapping: dict = {"group": list(spec.keys)}
            if passes and passes != 1:
                mapping["passes"] = passes
            if budget is not None:
                mapping["token_budget"] = budget
            if len(mapping) == 1:                     # resolved to defaults
                rewritten.append(list(spec.keys) if len(spec.keys) > 1
                                 else spec.keys[0])
            else:
                rewritten.append(mapping)
        self.error_types = rewritten

    def concurrency_for(self, model: str | None = None) -> int:
        """How many calls a pass on `model` may keep in flight.

        The vendor decides, not the pass: `api.concurrency` is what every
        provider gets, and `api.concurrency_by_provider` overrides it for the
        ones whose headroom we have actually measured. Ask with the model the
        pass is about to call — a confirm valve often runs a different model,
        sometimes at a different vendor, than the detector that ran before it.
        An unknown model falls back to `api.provider`, as `build_provider` does.

        `api.concurrency: 1` wins over every vendor entry. It is the documented
        way to make a run strictly serial — the first thing to reach for when
        chasing a threading bug or a rate limit — and a switch that quietly did
        nothing because a vendor table out-voted it would be worse than no
        switch at all."""
        if self.api.concurrency == 1:
            return 1
        from .providers.catalog import provider_for
        name = provider_for(model or self.api.model, self.api.provider)
        return max(1, self.api.concurrency_by_provider.get(
            name, self.api.concurrency))


CACHE_DIR_ENV = "DOCPROOF_CACHE_DIR"
EXAMINATION_GRAPH_ENV = "DOCPROOF_EXAMINATION_GRAPH"
EXAMINATION_JUDGMENT_ENV = "DOCPROOF_EXAMINATION_JUDGMENT"
EXAMINATION_PRODUCTION_VERDICTS_ENV = \
    "DOCPROOF_EXAMINATION_PRODUCTION_VERDICTS"
CANDIDATE_SCREENING_ENV = "DOCPROOF_CANDIDATE_SCREENING"
CANDIDATE_APPLY_ENV = "DOCPROOF_CANDIDATE_APPLY"

# Release gate for candidate-screening Apply mode (P0-01 containment).
# While the subsystem's validation gates (P4-04) have not been cleared, Apply is
# not a released capability: any request for it is contained to Shadow so the
# lane can generate, screen, and record a ledger without ever mutating a
# document. Flip this to True (or set DOCPROOF_CANDIDATE_APPLY=1 for a single
# deployment) only after the release gates pass and the change is approved.
CANDIDATE_APPLY_RELEASED = False


def examination_graph_killed() -> bool:
    """The deployment-wide emergency brake for shadow instrumentation.

    Only false-like values override configuration. This is intentionally a kill
    switch, not a second source of truth that can enable a feature a job/config
    turned off.
    """
    value = os.environ.get(EXAMINATION_GRAPH_ENV, "").strip().lower()
    return value in {"0", "false", "off", "no", "disabled"}


def examination_judgment_killed() -> bool:
    """Deployment-wide brake for the paid Phase-1B lane only."""
    value = os.environ.get(EXAMINATION_JUDGMENT_ENV, "").strip().lower()
    return value in {"0", "false", "off", "no", "disabled"}


def examination_production_verdicts_killed() -> bool:
    """Deployment-wide brake for Phase 2's production prompt receipts."""
    value = os.environ.get(
        EXAMINATION_PRODUCTION_VERDICTS_ENV, "").strip().lower()
    return value in {"0", "false", "off", "no", "disabled"}


def examination_production_verdicts_enabled(cfg) -> bool:
    """The effective Phase 2 switch for loaded configs and stored jobs."""
    graph = getattr(cfg, "examination_graph", cfg)
    return bool(
        graph.enabled and graph.production_verdicts
        and not examination_graph_killed()
        and not examination_production_verdicts_killed())


def candidate_screening_killed() -> bool:
    """Deployment-wide brake for the candidate screening lane."""
    value = os.environ.get(CANDIDATE_SCREENING_ENV, "").strip().lower()
    return value in {"0", "false", "off", "no", "disabled"}


def candidate_screening_enabled(cfg) -> bool:
    """Effective switch for loaded configuration and stored jobs."""
    candidate_cfg = getattr(cfg, "candidate_screening", cfg)
    return bool(candidate_cfg.mode != "off" and not candidate_screening_killed())


def candidate_apply_released() -> bool:
    """Whether candidate-screening Apply is a released, document-mutating mode.

    Defaults to the module gate; a single deployment can override with the
    environment variable only to *enable* it (a truthy value), never to force it
    off — the gate itself is the conservative floor.
    """
    if CANDIDATE_APPLY_RELEASED:
        return True
    value = os.environ.get(CANDIDATE_APPLY_ENV, "").strip().lower()
    return value in {"1", "true", "on", "yes", "enabled", "released"}


def resolve_candidate_mode(mode: str) -> str:
    """Clamp a requested candidate-screening mode to what is released (P0-01).

    ``apply`` is downgraded to ``shadow`` until the release gate opens, so no
    configuration, profile, or UI selection can mutate a document while the
    subsystem is unvalidated. ``off`` and ``shadow`` pass through unchanged.
    """
    if mode == "apply" and not candidate_apply_released():
        return "shadow"
    return mode


def default_cache_dir() -> str | None:
    """Where the whole-book reads are pinned, or None to re-read every time.

    The glossary, the story sheet and the continuity read each cost one call
    over the entire manuscript, and each is keyed by (text, model, prompt) — so
    the same draft read twice is the same answer bought twice. It happens more
    than it sounds: a batch review runs `prepare` at submit AND again at
    collect, so the story sheet alone was billed twice for every batch job, and
    re-running a draft (a fixed typo, a different detector, an eval sweep) paid
    for all three again.

    DOCPROOF_CACHE_DIR names the folder; set it EMPTY to turn the cache off and
    go back to re-reading, which is what the test suite does and what an eval
    measuring run-to-run variance in the whole-book passes should do. Otherwise
    it sits under DOCPROOF_HOME when the deployment sets one — on Fly that is
    the mounted volume, so the cache outlives a deploy — and under ~/.docproof
    when nothing does."""
    env = os.environ.get(CACHE_DIR_ENV)
    if env is not None:
        env = env.strip()
        return str(Path(env).expanduser()) if env else None
    home = os.environ.get("DOCPROOF_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".docproof"
    return str(root / "cache" / "whole-book")


def cache_dir_for(configured: str | None) -> str | None:
    """The folder a whole-book pass should cache in: what the config asked for,
    else the shared default.

    Resolved HERE, at the point of use, and deliberately not written back into
    the Config. The app builds the resumable checkpoint's fingerprint out of
    `cfg.model_dump(mode="json")` and throws the checkpoint away whenever it
    moves, so an absolute, HOME-derived path living in the config would put
    environment state inside that fingerprint: turning the cache off for an eval
    run, or resuming under a different HOME, would silently discard a review's
    paid-for calls. Where a pass caches is not something the manuscript's
    results depend on, and it should not be able to invalidate them."""
    return configured or default_cache_dir()


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path.resolve()}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Configuration root must be a YAML mapping, not {type(raw).__name__}")
    cfg = Config.model_validate(raw)
    if examination_graph_killed():
        cfg.examination_graph.enabled = False
    if examination_production_verdicts_killed():
        cfg.examination_graph.production_verdicts = False
    if examination_judgment_killed():
        cfg.examination_graph.judgment.enabled = False
    return cfg
