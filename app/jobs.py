"""The job queue behind the app.

One record per document per run. "Run now" jobs go through a worker thread;
batch jobs are submitted and then picked up by a ticker that polls the vendor
and collects results when they land. No state lives only in memory — every
record is a file, so closing the app (or losing power) costs at most the
in-flight sync job.
"""
from __future__ import annotations

import faulthandler
import json
import logging
import os
import queue
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docproof import batch as batchlib
from docproof import prep as preplib
from docproof import promo as promolib
from docproof.batch import pass_prompts
from docproof.checkpoint import Checkpoint
from docproof.config import Config, load_config
from docproof.formats import get_format
from docproof.audit import AuditError
from docproof.ingest import IngestError
from docproof.pipeline import (JobCancelled, content_hash, finish, prepare,
                               read_meaning_held, run_sync)
from docproof.models import CoverageLedger, Usage
from docproof.prep.convert import ConversionError
from docproof.prep.styles import StyleSheetError
from docproof.prep.verify import VerificationFailed
from docproof.promo import PromoError, PromoTooLarge
from docproof.providers import ProviderError, build_provider, cost_of_usage, \
    lookup, provider_for
from docproof.utils.files import write_atomic

from . import features
from .settings import Paths, Settings, get_api_key
from .spending import LedgerEntry, SpendingLedger
from .usage import _totals_for

log = logging.getLogger("docproof.app.jobs")

APP_MANIFEST = "app.json"
POLL_SECONDS = 120
# How many times the ticker will start collecting one batch before giving up and
# marking it failed. A collect that raises fails on the first try; this cap is
# for a collect that keeps killing the process (an OOM under the LanguageTool
# JVM, a machine restart) and so never reaches the except — without it the ticker
# retries forever and the card reads "almost done" the whole time. Three leaves
# room for a transient blip while still converging on a visible failure.
MAX_COLLECT_ATTEMPTS = 3

# How long a single heavy engine call (batch submit's prepare, or a collect's
# full-book rewrite) may run before the app starts saying so in the logs. Not a
# kill switch — Python threads can't be killed, and abandoning one leaves it
# racing its replacement — just visibility: a stack dump names the hang so the
# next freeze is diagnosable from `fly logs` rather than guesswork.
ENGINE_STALL_SECONDS = int(os.environ.get("DOCPROOF_STALL_MINUTES", "45")) * 60

# State → what the user reads. Keep the vocabulary out of the vendor's world:
# no "batch", no "API", no "chunks".
PLAIN_STATE = {
    "scheduled": "Waiting until {when}",
    "queued": "Waiting to start",
    "running": "Reviewing ({done} of {total} sections)",
    "waiting": "Processing overnight — check back in the morning",
    "collecting": "Almost done — writing your document",
    "done": "Ready",
    "failed": "Needs attention",
    "cancelled": "Cancelled",
}

# The steps a running review moves through, in order. The per-chunk detector
# loop ("reviewing") is the one with a real count; the rest are whole-book passes
# that run around it, each a single stretch of work with no per-call progress —
# so the card names the step instead of leaving the bar frozen at 100%. Keyed by
# the stage ids the pipeline's on_phase callback emits (run_sync, finish, and
# batch collect all speak it now), plus "preparing", which the app also sets
# itself ahead of the run. See Job.plain_state.
STAGE_STATE = {
    "preparing": "Reading your manuscript",
    "reviewing": "Reviewing ({done} of {total} sections)",
    "glossary": "Building the glossary for the whole book",
    "factcheck": "Checking the book against the world",
    "adjudicate": "Checking for real-word typos",
    "rewrite": "Rewriting and comparing, line by line",
    "languagetool": "Running the mechanical check",
    "continuity": "Reading the whole book for continuity",
    # Multi-round only: the between-rounds judge that rules on each round's
    # corrections before the next round reads the corrected text.
    "round_judge": "Putting this round's corrections to the judge",
    # The finish()-time passes, which used to hide under "writing" — on a big
    # book, many minutes of whole-book reads and judges with no sign of which
    # was running. Same ids as the feature switches wherever one exists.
    "verify": "Cross-checking the detectors' findings",
    "sapling": "Running the Sapling grammar check",
    "low_confidence": "Second look at the softer catches",
    "smoothing": "Reading for line-editing suggestions",
    "chapter_continuity": "Reading each chapter for continuity",
    "meaning_check": "Checking every change keeps your meaning",
    "fix_check": "Checking every fix is the right fix",
    "writing": "Almost done — writing your document",
    # A re-judge is not the pipeline above: it runs the gates over a finished
    # run's corrections and writes a new deliverable, emitting this one stage and
    # no other. Borrowing "reviewing" made the card claim a section count and a
    # step tracker for passes that never run. See JobRunner.rejudge.
    "judging": "Putting the corrections to the judges",
}

# Prep does a different job, so it says so. Only the states that differ.
PREP_STATE = {
    "running": "Reading your manuscript ({done} of {total})",
    "collecting": "Almost done — writing your files",
}

# Promo, likewise: one call over the whole book, then two documents written.
PROMO_STATE = {
    "running": "Reading the book and writing your copy",
    "collecting": "Almost done — writing your files",
}

# Corrections is deterministic and quick: no model, no vendor, no batch. It
# anchors the edit list to the exported IDML and writes the corrected file, so
# the card only ever passes through these before "Ready".
CORR_STATE = {
    "running": "Applying your corrections",
    "collecting": "Almost done — writing your file",
}

# …except that it is not always quick: with the second look and the last tier
# switched on, a proof full of open queries is minutes of frontier reads, and
# the card used to sit on a single "Applying your corrections" for all of it.
# These are the steps apply_corrections emits through its `progress` hook,
# keyed the same way STAGE_STATE is. See _run_corrections.
CORR_STAGE = {
    "reading": "Reading the corrections list",
    "pagemap": "Matching the proof's pages to the book ({done} of {total})",
    "second_look": "Second look at the reviewer's open notes "
                   "({done} of {total})",
    "probing": "Checking which corrections still need an anchor",
    "reanchor": "Re-quoting the corrections that didn't match",
    "merge": "Merging the corrections that overlap",
    "escalate": "Settling the last queries against the whole book "
                "({done} of {total})",
    "sanity": "Checking each correction before it is applied "
              "({done} of {total})",
    "applying": "Applying your corrections ({total} in all)",
    "verifying": "Checking the corrected file against the list",
    "writing": "Almost done — writing your file",
}

PREP_OUTPUTS = {"book": ["book"], "indesign": ["indesign"],
                "tracked": ["tracked"], "both": ["indesign", "tracked"],
                "all": ["book", "indesign", "tracked"]}


class _ReplayOnly:
    """A provider that refuses to call out. `download_anyway` replays a
    completed run from its checkpoint, so every call should already be cached;
    if one is not, this raises instead of quietly paying for a re-review."""
    name = "replay-only"

    def complete_structured(self, **kwargs):
        raise RuntimeError(
            "download-anyway expected every call to be in the checkpoint, but "
            "one was missing — refusing to re-run the review at cost.")


def _free_finish(cfg: Config) -> None:
    """Force off every pass that spends inside finish(), so a download-anyway
    rebuild costs what it promises: nothing.

    The mirror of docproof.rejudge._gates_only, needed for the same reason: the
    rebuild inherits the ORIGINAL run's config, so a book reviewed with Sapling
    on would silently pay Sapling (a per-character bill plus its confirm-valve
    model calls) again — likewise the low-confidence valve and the ensemble's
    overseer-verifier, each a model call finish() makes on its own account. Two
    deliberate differences from the re-judge:

      * the judge gates come off TOO — this path replays their recorded
        verdicts (read_meaning_held) rather than paying to rule again;
      * the ensemble's detectors stay, because the replayed findings are raw
        per-detector output and finish() still has to fold them by agreement —
        a free, local merge. Only the verifier, a model call, is disarmed."""
    cfg.meaning_check.enabled = False       # the meaning gate's judge
    cfg.fix_check.enabled = False           # the fix check's judge
    cfg.sapling.enabled = False             # per-character bill + confirm valve
    cfg.smoothing.enabled = False           # a whole-book read plus a judge
    cfg.chapter_continuity.enabled = False  # per-chapter read plus a judge
    cfg.low_confidence.confirm = False      # the below-gate promotion valve
    cfg.ensemble.verifier_model = None      # the overseer-verifier


@dataclass
class Job:
    id: str
    filename: str
    source_path: str
    model: str
    mode: str                      # "now" | "batch"
    state: str = "queued"
    group_id: str = ""
    schedule_at: str | None = None      # "HH:MM" local, batch mode only
    done: int = 0
    total: int = 0
    # Which step a running review is on, so the results card reads the truth
    # while a whole-book pass (glossary, rewrite, …) runs after the per-chunk
    # loop — where done/total would otherwise sit frozen at 100%. Set by the
    # pipeline's on_phase callback; "" on older records and non-review jobs, and
    # cleared when the job finishes. See STAGE_STATE and _run_now.
    stage: str = ""
    # When the current stage began (UTC ISO), stamped by JobStore whenever
    # `stage` changes value. The no-count whole-book passes (preparing, glossary,
    # rewrite, …) write nothing else while they run, so the card would otherwise
    # sit frozen with no sign it is alive: this lets it show time-in-stage. "" on
    # older records and until the first stage is set. See Job.to_api and app.js.
    stage_since: str = ""
    error: str | None = None
    applied: int | None = None
    # The review's other channel. `applied` counts tracked changes — the
    # corrections; `queried` counts the margin comments, which are questions
    # the author answers and which change nothing. `judge_held` is how many of
    # those questions are corrections a judge gate withdrew rather than let
    # through. Both are 0 on older records and on jobs that are not reviews;
    # see docproof.pipeline.Outputs, which counts them.
    queried: int = 0
    judge_held: int = 0
    # Non-fatal warnings from a finished review — chiefly paid passes that fell
    # open and produced nothing (a dead or unkeyed judge/continuity/glossary
    # model). Empty on a clean run and on older records; the card shows them so a
    # "done" review that quietly skipped a pass does not read as a clean one. The
    # full accounting is in summary.md's Coverage section.
    warnings: list[str] = field(default_factory=list)
    results_dir: str | None = None
    min_confidence: str = "medium"
    # Which English this manuscript is written in: "us" | "uk" | "ca" | "au".
    # Empty means whatever config/default.yaml says, which is what the watcher
    # submits and what every job recorded before this field existed meant. It
    # lives on the job rather than in the runner because a batch is collected
    # days after it is submitted, and a setting the manifest cannot carry is a
    # setting that quietly reverts. See docproof/variants.py.
    variant: str = ""
    # Reasoning depth for the model. Older records predate this field; the
    # default keeps them on the shipped "low" behaviour.
    effort: str = "low"
    # Which model reads the whole book for the glossary pass, "off" to skip it,
    # or None on older records (config_for then leaves the config default).
    glossary_model: str | None = None
    # Per-run pass toggles: {feature_id: on}. Empty (older records, or a run that
    # touched nothing) means "use the config defaults". See app/features.py.
    features: dict[str, bool] = field(default_factory=dict)
    # Per-run per-category tuning: {category_id: {passes?, token_budget?}}. The
    # id is content-based (its error-type keys joined). Empty (older records, or
    # a run that touched nothing) leaves the config's per-category defaults. See
    # Config.category_states / Config.apply_category_knobs.
    category_knobs: dict = field(default_factory=dict)
    # Multi-round review: review the manuscript this many times, each round
    # reading the previous round's corrections, with a strong judge between
    # rounds. 1 (older records, or the ordinary run) is a single review. The
    # judge's instructions, edited on the panel; empty means the built-in
    # default. See docproof/rounds.py and docproof/verifier.py.
    rounds: int = 1
    judge_prompt: str = ""
    # Which model rules on corrections between rounds. Empty (older records, or a
    # run that didn't touch the picker) means the config default. See catalog.py.
    judge_model: str = ""
    # The continuity read's editable system prompt, edited on the panel; empty
    # (older records, or a run that left it alone) means the built-in default.
    # And whether this run is continuity-only — the whole-book contradiction read
    # by itself, with every detector pass and sweep stripped off. See
    # docproof/continuity.py and JobStore.config_for.
    continuity_prompt: str = ""
    continuity_only: bool = False
    # The whole-book continuity reader's model. Empty (older records, or a run that
    # left it alone) means the config default — now the house reviewer, so a
    # frontier whole-book read is an opt-in per-run pick, like the glossary's.
    continuity_model: str = ""
    # The chapter-continuity reader's editable system prompt, edited on the panel;
    # empty (older records, or a run that left it alone) means the built-in
    # default. Its on/off rides the `features` map. See docproof/continuity.py.
    chapter_continuity_prompt: str = ""
    # The chapter-continuity model (one pick sets both reader and judge) and the
    # 1–5 sensitivity dial. Empty/None (older records, or a run that left them
    # alone) means the config default.
    chapter_continuity_model: str = ""
    chapter_continuity_sensitivity: int | None = None
    # The two judge gates — one reads every proposed change for whether it moves
    # the sentence's sense, the other for whether the fix is right — each with
    # its own model and editable instructions. Empty (older records, or a run
    # that left the picker alone) means the config default. The gates themselves
    # are the "meaning_check" and "fix_check" feature toggles. See
    # docproof/judges.py and JobStore.config_for.
    meaning_model: str = ""
    meaning_prompt: str = ""
    fix_model: str = ""
    fix_prompt: str = ""
    # Which effort tier the submitter picked, purely as a label for the results
    # card ("Light touch", "Standard", "Hard", "The hammer", or "" for a custom
    # or older run). The tier is a client-side macro over the controls above, so
    # this changes nothing about how the job runs; it only records what was
    # chosen. Empty on older records and on any run that didn't send one.
    preset: str = ""
    # The smoothing pass's two dials, applied only when the "smoothing" feature is
    # on. proposer_restraint = how much the line editor surfaces; judge_harshness
    # = how hard the taste judge culls it. The defaults are the shipped config
    # defaults, so older records and runs that never touched the pass behave
    # exactly as before. See docproof/smoothing.py and JobStore.config_for.
    proposer_restraint: str = "restrained"
    judge_harshness: str = "strict"
    # Multi-round progress: which round a running multi-round review is on, and
    # how many it will run. Both 0 on single reviews and older records; the card
    # reads them only when total_rounds > 1. Set by _run_rounds' on_progress
    # callback as the driver moves through its rounds. See plain_state.
    review_round: int = 0
    total_rounds: int = 0
    # Which sections the user picked, or None for the whole document.
    selection: list[str] | None = None
    created_at: str = ""
    updated_at: str = ""
    # What this job is: a grammar review, or manuscript prep for the house
    # InDesign template. Older records have no `kind` and are reviews.
    kind: str = "review"               # review | prep | promo | corrections
    prep_output: str = "indesign"      # book | indesign | tracked | both | all
    # Corrections only: the corrections list the designer's exported IDML is to
    # be edited by, as the JSON the parser accepts (docproof.corrections.parse).
    # Kept on the record — not a model prompt, just the input — so a retry
    # replays it and the completion log can say how many it carried. Empty on
    # every other kind.
    corrections: str = ""
    # Corrections from a marked-up PDF: the reviewer comments the edits were read
    # from, as JSON [{id, page, kind, instruction, anchor}, …]. Threaded into the
    # run so every comment is accounted for in the change log — including any the
    # model turned into no edit. Empty for a typed/pasted list.
    corrections_comments: str = ""
    # Corrections from a marked-up PDF: the text of every page of the proof, in
    # order, as JSON ["page 1 text", …]. An IDML has no pages, so this is what
    # lets a mark on page 49 be narrowed to the run of book text page 49 set —
    # without it, a correction to a comma has every comma in the book to choose
    # from and can only be flagged. Empty for a typed/pasted list.
    corrections_pages: str = ""
    # Corrections only: run the opt-in model sanity gate before applying, holding
    # a doubtful edit back for a human. Off keeps the run deterministic and free.
    corrections_sanity: bool = False
    # Corrections only: run the opt-in second look before applying — a stronger
    # model re-reads the notes the extractor left as queries (a reviewer offering
    # alternatives, a conditional the page settles) and commits the delegated
    # ones to concrete edits. Off keeps every query a human's.
    corrections_second_look: bool = False
    # Corrections only: run the last tier — a frontier model given the whole book
    # for the queries the second look declined. Its own flag now (resolved from the
    # request, which defaults it to follow the second look), so its frontier spend
    # is asked for or refused on its own rather than riding the cheaper pass.
    corrections_escalate: bool = False
    # Prep, book output only: the operator's per-job answers for the sketch —
    # subject matter (picks the title-page face), running-head title and
    # author. Empty means "use what the detector reads off the opening pages",
    # the same sentinel the prompt overrides use. `prep_book` is written back
    # when the job finishes: the merged answers the file was actually built
    # with, so the panel can show what was detected.
    prep_subject: str = ""
    prep_title: str = ""
    prep_author: str = ""
    prep_book: dict = field(default_factory=dict)
    # Promo only: whether the generated copy still needs a human's sign-off
    # before it ships. "" on review and prep; on promo, "auto" ships with no
    # gate, "pending" waits in the panel for a person, "approved" once one has
    # okayed it. The generation itself doesn't read this — the delivery step does.
    approval: str = ""                 # "" | auto | pending | approved
    # Promo only: the human override for a book over the single-pass token limit.
    # False keeps the size guard; True was set by a person who saw the size at
    # drop time and chose to send the whole book in one call anyway. Older
    # records have no such field and were never overridden.
    allow_oversize: bool = False
    # Promo, plan mode only: this promo job writes a marketing plan — promo's
    # third deliverable — instead of the teaser and posts. The plan is its own
    # model call, taking author/book metadata the copy call never sees; on a
    # manual panel run the operator types those, and they ride along here. All
    # empty/False on a teaser+posts run and on review/prep. Every field but the
    # book is optional: the prompt degrades to a thinner but valid plan, so a
    # bare manuscript still produces one. See docproof/promo.prepare_plan.
    plan_only: bool = False
    plan_author: str = ""              # display / pen name, printed on the plan
    plan_blurbs: str = ""              # back-cover synopsis + endorsement blurbs
    plan_city: str = ""                # for the local-opportunities section
    plan_keywords: str = ""            # the press's positioning keywords
    # The author's own answers to the press's publicity questionnaire (the PNQ):
    # free-text Q&A grounding the plan the way blurbs do. On a manual run the
    # operator may paste it; on the watched-folder run the automated stage reads
    # it from the author's Drive folder. Empty degrades the prompt cleanly.
    plan_questionnaire: str = ""
    # Why a job failed, as a machine-readable tag when the reason needs handling
    # beyond the message string. Currently "oversize" for a promo book past the
    # single-pass limit, so the watcher can email a person about that case
    # specifically. "" on success and on ordinary failures.
    error_kind: str = ""
    # Which door this job came in by: somebody dropping a file on the window,
    # or the watcher finding one in a Drive folder. Two job stores adding up to
    # one bill, and a spending figure that cannot say which is a figure nobody
    # trusts. Older records have no `source` and were all the app.
    source: str = "app"                # app | watch
    # Whose review this is, in the web build. The id of the signed-in user who
    # created it; the desktop build has no users, so its records carry the
    # single local owner (or, from before this field existed, an empty string —
    # which the web build never writes and its ownership checks never match).
    owner_id: str = ""
    tagged: int | None = None          # paragraphs given a style
    flags: int | None = None           # things prep wants a human to decide
    verified: bool | None = None       # the author's words came through intact
    # Corrections only: how many changes the after file carries that the list
    # did not ask for — the verifier's collateral-damage count. `applied` is the
    # count that landed and `flags` the count refused for a human, reusing the
    # same fields prep does; this one has no prep analogue. None on other kinds.
    discrepancies: int | None = None
    # Corrections from a PDF: how many reviewer comments came in, how many landed,
    # and how many a person still owns (flagged, or never turned into an edit).
    # These three reconcile — applied + no-change + unresolved == total, where
    # no-change is the remainder — so the card can add up to the total the reviewer
    # marked. `applied` above is an *edit* count and deliberately does not: a
    # comment can make several edits or none. `unresolved` is the count that used
    # to vanish silently. None on other kinds and on a typed list (no ledger).
    total_comments: int | None = None
    applied_comments: int | None = None
    unresolved: int | None = None
    # Promo only: how many capitalised terms in the copy appear nowhere in the
    # manuscript — the grounding check's count, surfaced so a card can flag it.
    unverified: int | None = None
    words: int | None = None
    # What this job actually cost, recorded when it finishes so the dashboard
    # doesn't have to re-read every results folder to add it up.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    api_calls: int = 0
    cost: float | None = None
    # The Sapling pass's share of `cost` (0 when it didn't run), kept alongside
    # so the completion email can break it out — `cost` above is the grand total,
    # model plus Sapling.
    sapling_cost: float = 0.0
    # Set when a review failed the reject-all audit specifically (as opposed to
    # a provider or ingest error), so the results card can offer "download
    # anyway" only where it applies.
    audit_failed: bool = False
    # Set when the user chose "download anyway" on such a review: the file was
    # written with the audit downgraded to a warning, so it is clear the
    # integrity check did not pass.
    audit_overridden: bool = False
    # How many times we have started collecting this batch's results. A collect
    # that RAISES becomes a visible "failed"; this counts the other way it can
    # end — the process dying mid-collect (an OOM under the LanguageTool JVM, a
    # machine restart) — which leaves the job stuck in "collecting" for the
    # ticker to retry forever. Counted before each attempt so a crash still
    # counts, and capped at MAX_COLLECT_ATTEMPTS so a repeating crash becomes a
    # failure the user can see and recover, not "almost done" with no end.
    collect_attempts: int = 0
    # The Drive output archive: whether this job's produced files have been
    # pushed to the durable off-box record, and where. "" is the default and
    # means "not looked at yet" — an install with the archive off leaves every
    # record here, inert; the moment it is switched on, the ticker's sweep finds
    # them by this very emptiness and backfills. "pending" is an attempt that hit
    # a Drive hiccup and will be retried with backoff; "done" is safely archived;
    # "failed" is given up on (Drive kept refusing, or the results were already
    # gone). See app/watch/archive.py.
    archive: str = ""                  # "" | pending | done | failed
    archive_error: str = ""
    archive_attempts: int = 0
    # This job's own folder in the archive, and the Drive id of every file put
    # there (artifact name -> id), so a resumed or repeated archive uploads only
    # what is missing rather than a second copy of it. Empty until first tried.
    drive_folder_id: str = ""
    drive_files: dict[str, str] = field(default_factory=dict)

    @property
    def is_prep(self) -> bool:
        return self.kind == "prep"

    @property
    def is_corrections(self) -> bool:
        return self.kind == "corrections"

    @property
    def is_promo(self) -> bool:
        return self.kind == "promo"

    @property
    def is_plan(self) -> bool:
        """A promo job that writes the marketing plan rather than the copy. Still
        a promo job — it lives in the same store and panel — but a different
        deliverable, so the runner and the panel branch on it."""
        return self.kind == "promo" and self.plan_only

    def plain_state(self) -> str:
        extra = (PREP_STATE if self.is_prep
                 else PROMO_STATE if self.is_promo
                 else CORR_STATE if self.is_corrections else {})
        states = {**PLAIN_STATE, **extra}
        # Corrections names its own steps, so its ids win over the review's
        # where they share a name ("writing" writes one file, not a document).
        stage_states = ({**STAGE_STATE, **CORR_STAGE} if self.is_corrections
                        else STAGE_STATE)
        # A running review names the actual step it is on, so the card doesn't
        # read "reviewing" while the rewrite pass retypes the book. Only reviews
        # set a stage; prep and promo never do, so they keep their own messages.
        #
        # A multi-round review starts its section count over every round, so the
        # count alone would read as the bar jumping backwards. Name the round —
        # and add the within-round count only once it exists: a round still
        # being ingested, or riding a vendor batch, has none to show. A batch
        # round has none for hours, so it says "processing overnight" rather than
        # a bare "reviewing" that reads as a stuck synchronous pass.
        if (self.state in ("queued", "running", "collecting")
                and self.stage == "reviewing" and self.total_rounds > 1):
            head = f"Round {max(self.review_round, 1)} of {self.total_rounds}"
            if self.total:
                return f"{head} — reviewing ({self.done} of {self.total} sections)"
            if self.mode == "batch":
                return f"{head} — processing overnight"
            return f"{head} — reviewing"
        if (self.state in ("queued", "running", "collecting")
                and self.stage in stage_states):
            template = stage_states[self.stage]
        else:
            template = states.get(self.state, self.state)
        return template.format(done=self.done, total=self.total,
                               when=self.schedule_at or "later")

    def to_api(self) -> dict:
        d = asdict(self)
        d["plain_state"] = self.plain_state()
        d["ready"] = self.state == "done"
        d["is_prep"] = self.is_prep
        d["is_promo"] = self.is_promo
        d["is_plan"] = self.is_plan
        d["is_corrections"] = self.is_corrections
        # Whether the proof carried page texts, which is what decides whether the
        # run takes the page-matching step at all. A boolean, so the card can
        # promise that step (or not) without the texts themselves riding on
        # every poll.
        d["corrections_paged"] = bool(self.corrections_pages)
        # The stored reviewer-comment list and page texts are backend input for
        # the run, not card data, and on a big proof they are large — keep them off
        # every job payload.
        d.pop("corrections_comments", None)
        d.pop("corrections_pages", None)
        # Which application the reviewed file opens in, so the results card can
        # say where the changes are instead of assuming Word.
        try:
            d["format"] = get_format(self.filename).to_api()
        except IngestError:
            d["format"] = None            # a record from before formats existed
        # Two reviews of one document are now two entries that look alike, so
        # each says which folder its results went to.
        d["results_name"] = (Path(self.results_dir).name
                             if self.results_dir else None)
        # Whether this review wrote a change log — a config choice, so the card
        # offers the download only when the file is actually there. One stat on
        # a local disk, only for finished reviews.
        d["has_change_log"] = bool(
            d["format"] and self.kind == "review" and self.state == "done"
            and self.results_dir
            and (Path(self.results_dir)
                 / get_format(self.filename).change_log_name(self.filename)
                 ).is_file())
        # A click-through to this job's folder in the Drive archive, once it has
        # one. The card shows "In Drive" when archived, so the deliverable is one
        # link away even after the local copy is recycled on a redeploy.
        d["drive_link"] = (
            f"https://drive.google.com/drive/folders/{self.drive_folder_id}"
            if self.drive_folder_id else None)
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tallies(outputs) -> dict:
    """What a finished review counted, as `store.update` keywords.

    Six paths finish a review — inline, batch collect, multi-round, re-judge,
    and the two "download anyway" rebuilds — and every one of them has to
    record the same numbers or the results card will disagree with itself
    depending on how the document was produced. Collected here so that a run
    counts a new thing in one place rather than six."""
    return {"applied": outputs.applied, "queried": outputs.queried,
            "judge_held": outputs.judge_held,
            # Non-fatal degradation (a paid pass that fell open and produced
            # nothing — e.g. a dead judge/continuity key) so a "done" card can
            # say so instead of reading as a clean run. Empty on a clean one.
            "warnings": list(getattr(outputs, "warnings", []) or [])}


def read_usage(results_dir: Path | str) -> tuple[dict, float | None] | None:
    """The token counts a finished job left behind, whichever pipeline wrote
    them. Shared with the dashboard, which uses it to fill in jobs that
    finished before job records carried their own usage."""
    folder = Path(results_dir)
    for name in ("findings.json", "prep.json"):
        path = folder / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable usage in %s: %s", path, e)
            return None
        return (data.get("usage") or {}), data.get("cost")
    return None


class JobStore:
    """Job records on disk, one directory each, shared with the batch
    manifest so a job is a single folder you can inspect or delete."""

    def __init__(self, paths: Paths):
        self.paths = paths.ensure()
        self._lock = threading.RLock()

    def dir(self, job_id: str) -> Path:
        return self.paths.jobs / job_id

    def save(self, job: Job) -> Job:
        with self._lock:
            d = self.dir(job.id)
            d.mkdir(parents=True, exist_ok=True)
            job.updated_at = _now()
            # The lock serializes writers only: get() reads this file from the
            # API, ticker, and worker threads without taking it, so the write
            # must land atomically or a reader can catch it half-written and
            # see a live job as unreadable.
            write_atomic(d / APP_MANIFEST, json.dumps(asdict(job), indent=2))
        return job

    def get(self, job_id: str) -> Job | None:
        path = self.dir(job_id) / APP_MANIFEST
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable job %s: %s", job_id, e)
            return None
        known = {f for f in Job.__dataclass_fields__}
        return Job(**{k: v for k, v in data.items() if k in known})

    def all(self, owner_id: str | None = None) -> list[Job]:
        """Every job, newest first. Pass owner_id to get only one user's — the
        web build does, so one person's list never shows another's work; the
        desktop build passes nothing and sees the single owner's lot."""
        jobs = [j for d in self.paths.jobs.glob("*")
                if (j := self.get(d.name)) is not None
                and (owner_id is None or j.owner_id == owner_id)]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return None
            self._stamp_stage(job, fields)
            for k, v in fields.items():
                setattr(job, k, v)
            return self.save(job)

    @staticmethod
    def _stamp_stage(job: Job, fields: dict) -> None:
        """Reset the stage clock the moment the stage actually changes, so a
        card can show how long the current step has run. A caller that sets
        `stage_since` itself wins (nothing does today); a no-op stage write
        does not restart the clock."""
        if ("stage" in fields and fields["stage"] != job.stage
                and "stage_since" not in fields):
            job.stage_since = _now()

    def delete(self, job_id: str) -> bool:
        """Remove a job's record folder — the manifest, checkpoint, and any
        batch bookkeeping. The produced documents live elsewhere (results_dir);
        the runner clears those before calling this. Returns whether anything
        was there to remove."""
        with self._lock:
            d = self.dir(job_id)
            if not d.is_dir():
                return False
            shutil.rmtree(d, ignore_errors=True)
            return True

    def update_if(self, job_id: str, *, expect: str, **fields) -> Job | None:
        """`update`, but only if the job is still in the state the caller last
        saw it in. Cancelling reads a job's state and then, a moment later,
        writes a new one — without this, the worker thread submitting a batch
        or the ticker promoting a scheduled job could land in that gap and the
        cancellation would be silently lost. Returns None, changing nothing, if
        the state moved on first."""
        with self._lock:
            job = self.get(job_id)
            if job is None or job.state != expect:
                return None
            self._stamp_stage(job, fields)
            for k, v in fields.items():
                setattr(job, k, v)
            return self.save(job)


class JobRunner:
    """Worker thread for immediate reviews + a ticker for everything on a
    clock: scheduled submissions and batch polling."""

    def __init__(self, store: JobStore, settings: Settings, *,
                 config_path: str | Path, poll_seconds: int = POLL_SECONDS,
                 notify_home: str | Path | None = None):
        self.store = store
        self.settings = settings
        self.ledger = SpendingLedger(store.paths.spending_db)
        self.config_path = Path(config_path)
        # Where DocWatch keeps its email settings and Google sign-in. When set, a
        # finished job emails the completion log through that account — see
        # `_notify_done`. None (a runner told nothing about it) sends nothing.
        self.notify_home = Path(notify_home) if notify_home else None
        self.error_dir = self.config_path.parent / "error_types"
        self.poll_seconds = poll_seconds
        self.queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._tick_mutex = threading.Lock()
        # Serialises Drive archiving across the two threads that trigger it — an
        # inline attempt on the worker when a job finishes, and the ticker's
        # sweep — so the same job can never have two attempts creating two
        # folders for it at once. Best-effort work, so a brief wait is fine.
        self._archive_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        # Ids the user has asked to abort mid-run. The worker polls this as it
        # folds each call in; a set + lock rather than per-job Events so the
        # request can land before the worker has even picked the job up.
        self._cancel_lock = threading.Lock()
        self._cancel_requested: set[str] = set()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._work, name="docproof-worker",
                             daemon=True),
            threading.Thread(target=self._tick, name="docproof-ticker",
                             daemon=True),
        ]
        for t in self._threads:
            t.start()
        self.resume_interrupted()

    def stop(self, join: float | None = None) -> None:
        """Ask both threads to finish. Setting the flag is all a shutting-down
        app needs: the threads are daemons, and a review already in flight is
        left to end on its own rather than holding the process open.

        `join` waits that many seconds for them to actually exit — a job the
        worker had already picked up runs to completion first. Tests pass it so
        that no worker thread survives the fixture that set its stubs up; a
        leaked one goes on calling whatever the stubs were restored to, which
        means the live vendor."""
        self._stop.set()
        if join is None:
            return
        deadline = time.monotonic() + join
        for t in self._threads:
            if t is threading.current_thread():
                continue
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                raise TimeoutError(f"{t.name} did not stop within {join}s")
        self._threads = []

    def resume_interrupted(self) -> None:
        """A sync job that was mid-flight when the app closed is re-queued.

        It is not started over: the run left a checkpoint of every call it
        completed, and the re-run replays that and pays only for the rest.
        `done` is deliberately left alone — the resumed run races back through
        the cached part in moments, and resetting the bar to zero used to be
        the visible face of resetting the *spend* to zero, which is the thing
        that no longer happens. A batch review still *waiting* on the vendor
        needs nothing: the ticker finds it by its manifest. One caught mid-
        *collect* is flipped back to "waiting" so the ticker re-polls and hands
        it off again, because collect no longer runs on the ticker — see below."""
        for job in self.store.all():
            # Prep and promo are included at "collecting" too: neither has
            # vendor-side state, and both claim their results folder before
            # writing, so starting again lands in the same place rather than
            # orphaning it. A re-run promo pays for its one call again — it keeps
            # no checkpoint — which is a rare price for a crash mid-write.
            # A multi-round review owns its whole run on the worker (both modes),
            # so a batch one interrupted mid-run is re-queued here too — it is not
            # ticker-owned and would otherwise sit "running" forever. It restarts
            # from round 1 (the driver is not round-level resumable); any vendor
            # batches it had in flight are abandoned.
            # A re-judge is the one running job the worker must not be handed.
            # It runs inline in the request thread and has no queue entry to
            # resume; put its id on the queue and the worker would read an
            # ordinary review record and run the whole detector pipeline — a full
            # paid re-review nobody asked for. There is nothing to resume (the
            # gates keep no checkpoint), so it is failed with something a person
            # can act on: press the button again.
            if job.state == "running" and job.stage == "judging":
                log.info("Re-judge %s was interrupted by a restart; failing it "
                         "rather than re-running the review.", job.id)
                self.store.update(
                    job.id, state="failed", stage="",
                    error="This re-judge was interrupted by a restart. Nothing "
                          "was written — run it again from the finished review.")
                continue
            multiround = job.rounds > 1 and job.state == "running"
            interrupted = (job.state == "running" and job.mode == "now") or (
                (job.is_prep or job.is_promo) and job.state == "collecting"
                ) or multiround
            if interrupted:
                log.info("Re-queueing %s, interrupted by a restart", job.id)
                self.store.update(job.id, state="queued")
                self.queue.put(job.id)
            elif (job.mode == "batch" and job.state == "collecting"
                  and not (job.is_prep or job.is_promo)):
                # An interrupted collect of a batch review: the results are
                # still paid-for at the vendor. Back to "waiting" rather than
                # straight onto the queue — the ticker re-polls, spends an
                # attempt against MAX_COLLECT_ATTEMPTS exactly as a first try
                # would, and does the one legal enqueue itself. (The ticker used
                # to find these by re-entering "collecting"; it no longer looks
                # there, so nothing else would pick this up.)
                log.info("Collect of %s was interrupted by a restart; "
                         "returning it to the ticker.", job.id)
                self.store.update(job.id, state="waiting")
            elif job.state == "queued":
                self.queue.put(job.id)

    # -- cancellation ---------------------------------------------------------

    def request_cancel(self, job_id: str) -> None:
        """Ask a running job to stop. The worker notices between calls and
        raises out of the run; harmless if the job isn't actually running,
        since the flag is cleared when the worker next finishes with this id."""
        with self._cancel_lock:
            self._cancel_requested.add(job_id)

    def _cancel_pending(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel_requested

    def _clear_cancel(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancel_requested.discard(job_id)

    def _abort(self, job_id: str) -> None:
        """Finalize an aborted run. The checkpoint goes the way a queued
        cancel's does — a cancelled job never runs again, so the calls it did
        pay for before the abort are unreusable clutter — and any results
        folder claimed but never written is handed back."""
        self._release_results_dir(job_id)
        self.discard_checkpoint(job_id)
        job = self.store.get(job_id)
        if job is not None and job.state not in ("done", "failed"):
            self.store.update(job_id, state="cancelled", error=None)
        log.info("Job %s aborted by request", job_id)

    # -- submission -----------------------------------------------------------

    def enqueue(self, job: Job) -> Job:
        """Hand a job to the worker. Batch submission is queued rather than
        run inline: talking to the vendor can take seconds, and the HTTP
        request that created the job should not wait for it."""
        self.store.save(job)
        if job.mode == "batch" and job.schedule_at:
            return self.store.update(job.id, state="scheduled") or job
        self.queue.put(job.id)
        return job

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until the worker has drained. Used by tests and by shutdown;
        the UI polls instead."""
        deadline = time.monotonic() + timeout
        while not self.queue.empty() or self._busy.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError("worker did not settle")
            time.sleep(0.01)

    # -- config ---------------------------------------------------------------

    def config_for(self, job: Job) -> Config:
        cfg = load_config(self.config_path)
        cfg.api.model = job.model
        cfg.api.effort = job.effort
        cfg.min_confidence = job.min_confidence
        # Which English to hold the book to. Empty keeps the config's own
        # variant for older records and the watcher; a pick is vetted at submit
        # (routes/jobs.py), and Config's Literal re-checks it on assignment.
        if job.variant:
            cfg.variant = job.variant
        # The glossary pass runs its own (usually stronger) model. "off" skips
        # it; None leaves the shipped config default for older job records.
        if job.glossary_model == "off":
            cfg.glossary.enabled = False
        elif job.glossary_model:
            cfg.glossary.model = job.glossary_model
        # Multi-round review. count 1 is the ordinary single review; an empty
        # judge_prompt is the engine's "use the built-in default" sentinel, so
        # it passes through verbatim.
        cfg.rounds.count = job.rounds
        cfg.rounds.judge_prompt = job.judge_prompt
        # An empty judge_model keeps the config default (default.yaml); a panel
        # pick overrides it. The pick is vetted at submit (routes/jobs.py) — this
        # nested assignment doesn't re-run RoundsConfig's validator.
        if job.judge_model:
            cfg.rounds.judge_model = job.judge_model
        cfg.comments = self.settings.comments
        cfg.report_explanations = self.settings.explanations
        # Per-run feature switches land last, so a toggle the user set on the
        # submission panel wins over both the shipped config and the two
        # settings-backed defaults above (comments, explanations).
        features.apply_features(cfg, job.features)
        # Per-run per-category knobs (passes / chunk size), layered after the
        # feature toggles and before the continuity-only reset below, so a
        # continuity-only run (which clears error_types) still discards them.
        cfg.apply_category_knobs(job.category_knobs)
        # The continuity read's editable prompt (empty = built-in default),
        # applied like the round judge's — the sentinel passes through verbatim.
        cfg.continuity.prompt = job.continuity_prompt
        # The whole-book continuity model. Empty keeps the config default; a panel
        # pick overrides it. Vetted at submit (routes/jobs.py), like the glossary's.
        if job.continuity_model:
            cfg.continuity.model = job.continuity_model
        # The chapter-continuity reader's editable prompt, applied the same way.
        cfg.chapter_continuity.prompt = job.chapter_continuity_prompt
        # One model pick drives both the reader and its judge; the 1–5 sensitivity
        # dial sets the judge's posture and the confidence floor. Empty/None keeps
        # the config default. Applied after apply_features, which owns on/off.
        if job.chapter_continuity_model:
            cfg.chapter_continuity.model = job.chapter_continuity_model
            cfg.chapter_continuity.judge_model = job.chapter_continuity_model
        if job.chapter_continuity_sensitivity is not None:
            cfg.chapter_continuity.sensitivity = job.chapter_continuity_sensitivity
        # The meaning gate's judge. Applied AFTER apply_features, which owns the
        # gate's on/off: the picker only says which model reads the changes, and
        # an empty pick keeps the config default. Vetted at submit
        # (routes/jobs.py), like the round judge's.
        cfg.meaning_check.prompt = job.meaning_prompt
        if job.meaning_model:
            cfg.meaning_check.model = job.meaning_model
        cfg.fix_check.prompt = job.fix_prompt
        if job.fix_model:
            cfg.fix_check.model = job.fix_model
        # The smoothing pass's two dials, applied after apply_features (which owns
        # the pass's on/off): these only say HOW it behaves when it is on — how
        # much the proposer surfaces, and how hard the judge culls. Vetted at
        # submit, like the other per-run picks.
        cfg.smoothing.proposer_restraint = job.proposer_restraint
        cfg.smoothing.judge_harshness = job.judge_harshness
        # "Continuity only" strips the run to the continuity reads: no detector
        # passes, no sweeps, none of the other whole-book passes — just the
        # whole-book contradiction check, the chapter-scoped in-scene read, and
        # their margin queries. Both continuity switches are forced on regardless
        # of the feature toggle, because they ARE the run now.
        if job.continuity_only:
            cfg.error_types = []
            cfg.sweeps = []
            for _pass in ("glossary", "adjudicate", "rewrite", "languagetool",
                          "sapling", "smoothing", "consistency", "spellcheck",
                          "meaning_check", "fix_check"):
                getattr(cfg, _pass).enabled = False
            cfg.continuity.enabled = True
            cfg.chapter_continuity.enabled = True
        # Prompts the user has edited win over the shipped ones, per key.
        cfg.error_type_override_dir = str(self.store.paths.prompts)
        if job.is_prep:
            cfg.prep.outputs = PREP_OUTPUTS.get(job.prep_output, ["book"])
            # The book output is a plain reading copy by default — Times New
            # Roman, 12pt, US Letter, no ornaments — for manual runs and the
            # watched folder alike. The watched folder keeps its own design
            # field so it stays plain even if the app's default is later pointed
            # at the Atmosphere paperback sketch; same "book" writer either way.
            if job.source == "watch":
                cfg.prep.book_design = cfg.prep.watch_book_design
        return cfg

    def _checkpoint(self, job: Job, cfg: Config, prepared) -> "Checkpoint":
        """The record of what this job has already paid for.

        Fingerprinted the way the batch manifest is — document text, full
        config, the exact prompts — because cached answers are only reusable
        while all of those are unchanged. `load()` wipes a stale one itself."""
        if job.is_prep:
            fingerprint = {
                "kind": "prep",
                "content_hash": content_hash(prepared.structure),
                "config": cfg.model_dump(mode="json"),
                "prompts": {"tagging":
                            prepared.prompt.render(prepared.sheet)},
                "selection": None,
            }
        else:
            fingerprint = {
                "kind": "review",
                "content_hash": prepared.content_hash,
                "config": cfg.model_dump(mode="json"),
                "prompts": pass_prompts(cfg, prepared),
                "selection": job.selection,
            }
        checkpoint = Checkpoint(self.store.dir(job.id) / "checkpoint.json",
                                fingerprint=fingerprint)
        checkpoint.load()
        return checkpoint

    def discard_checkpoint(self, job_id: str) -> None:
        (self.store.dir(job_id) / "checkpoint.json").unlink(missing_ok=True)

    def download_anyway(self, job_id: str) -> Job | None:
        """Write the reviewed document for a review that failed the reject-all
        audit, with the audit downgraded to a warning.

        Costs nothing: every model call is already in the checkpoint, so the
        findings replay without touching the provider. The checkpoint is
        fingerprinted on the *original* config, so it is rebuilt and replayed
        under that config first; only then is the audit downgraded, for the
        write itself. The audit failure stays recorded (audit_overridden=True)
        so nothing pretends the check passed."""
        job = self.store.get(job_id)
        if job is None or job.is_prep or job.state != "failed":
            return None
        if job.rounds > 1:
            # A multi-round run has no checkpoint to replay; it rebuilds from
            # the composed snapshot its driver left beside the working files.
            return self._download_anyway_rounds(job)

        cfg = self.config_for(job)                      # original: audit strict
        prepared = prepare(cfg, job.source_path, self.error_dir,
                           selection=job.selection)
        checkpoint = self._checkpoint(job, cfg, prepared)
        # _ReplayOnly guarantees this makes no API calls: if the checkpoint is
        # somehow incomplete, it raises rather than silently charging for a
        # re-review. The factory too: with the ensemble on, run_sync ignores
        # `provider` and builds one client per detector from the factory, so
        # without it the guarantee would hold for every run except an ensemble's.
        coverage = CoverageLedger()
        findings, usage = run_sync(cfg, prepared, _ReplayOnly(),
                                   checkpoint=checkpoint, coverage=coverage,
                                   provider_factory=lambda _cfg: _ReplayOnly())

        cfg.audit = "warn"                              # now let the write pass
        # Every pass that spends inside finish() comes off — each builds its own
        # provider in there, so _ReplayOnly cannot stop them, and leaving any on
        # would put a paid pass inside the one operation that promises to charge
        # nothing. The judge gates' verdicts are NOT in these findings — the
        # checkpoint only carries raw detector output — so they are read back
        # from what the original run recorded and applied without a judge.
        # Skipping that step would rebuild the file with every held-back change
        # applied, which is the opposite of what the first summary told the
        # author.
        _free_finish(cfg)
        out = (Path(job.results_dir) if job.results_dir
               else self._claim_results_dir(job))
        outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                         source_path=job.source_path, coverage=coverage,
                         judge_held=read_meaning_held(out))
        checkpoint.delete()
        updated = self.store.update(job_id, state="done", audit_overridden=True,
                                    **_tallies(outputs),
                                    results_dir=str(out), error=job.error)
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._finish(job_id)
        return updated

    def _download_anyway_rounds(self, job: Job) -> Job | None:
        """`download_anyway` for a multi-round review.

        Costs nothing: the rounds driver snapshots its composed, original-
        coordinate findings (rounds/composed.json) before the finish that can
        fail the audit, so this rebuilds the deliverable exactly the way
        _finalize does — a fresh package off the normalized base, the audit
        downgraded to a warning for the write — without touching a provider.
        Returns None (the route's 409) for a run from before the snapshot
        existed, or one whose working files were cleaned away."""
        from docproof.checkpoint import finding_from_dict
        from docproof.utils.xml_helpers import DocxPackage

        out = Path(job.results_dir) if job.results_dir else None
        composed = out / "rounds" / "composed.json" if out else None
        base = out / "rounds" / "base.docx" if out else None
        if not (composed and composed.is_file() and base.is_file()):
            return None
        try:
            data = json.loads(composed.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable rounds snapshot for %s: %s", job.id, e)
            return None
        findings = [finding_from_dict(d) for d in data["findings"]]
        usage = Usage(**data.get("usage", {}))

        cfg = self.config_for(job)
        # The story sheet is a paid whole-book read inside prepare(), and it
        # only ever feeds detector prompts — no detector runs here, so it comes
        # off before the ingest. (The single-review path cannot do the same: its
        # checkpoint is fingerprinted on the original config and prompts, so the
        # sheet is rebuilt there from its whole-book cache instead.)
        cfg.storysheet.enabled = False
        # The same deterministic ingest the run used; base.docx was saved from
        # it, so paragraph ids line up. No provider is built or called.
        prepared0 = prepare(cfg, job.source_path, self.error_dir)
        cfg.audit = "warn"                              # let the write pass
        # ...and every pass that spends inside finish() off, for the same reason
        # as the single-review path above: each builds its own provider in there
        # and would turn "costs nothing" into a bill. The meaning gate's original
        # verdicts are replayed from the record instead, so the rebuilt file
        # holds back what the first one did. See download_anyway.
        _free_finish(cfg)
        prepared_final = replace(prepared0, pkg=DocxPackage(base),
                                 sweep_findings=[], consistency_findings=[])
        outputs = finish(prepared_final, findings, usage, cfg, out_dir=out,
                         source_path=job.source_path,
                         judge_held=read_meaning_held(out))
        updated = self.store.update(job.id, state="done", audit_overridden=True,
                                    **_tallies(outputs),
                                    results_dir=str(out), error=job.error)
        self._record_usage(job.id, out, cfg.api.model,
                           batch=job.mode == "batch")
        self._finish(job.id)
        return updated

    def recover(self, job_id: str) -> Job | None:
        """Re-collect a batch review that failed AFTER its vendor batch landed.

        A collect-time failure — a post-step raising (the LanguageTool pass,
        say), a transient write error — leaves the overnight batch complete and
        untouched at the vendor: it is billed, and its results are still there
        to download. So recovery must NOT resubmit (which is what `retry`, the
        state="queued" path, does — paying for the same batch twice). Instead we
        send the job back through the ticker's collect path against the SAME
        batch by returning it to `waiting`; the next `_advance_batch` re-polls
        (finds it ready) and re-collects, reusing the paid results. Only the
        synchronous post-steps re-run and re-bill, which is small.

        Returns the updated job, or None when there is nothing to recover this
        way: a job that is not failed, a "now" run (or one that never reached
        submit) with no batch at the vendor, or one that failed the reject-all
        audit — that last has its own `download_anyway` path and would only
        fail the same audit again, so it is refused here rather than re-collected
        at cost."""
        job = self.store.get(job_id)
        if job is None or not self.can_recover(job):
            return None
        # A fresh collect budget: the user is asking to try again, and the last
        # run may have exhausted the cap (often why it is failed here at all).
        return self.store.update_if(job_id, expect="failed", state="waiting",
                                    error=None, error_kind="", collect_attempts=0)

    def can_rejudge(self, job: Job) -> bool:
        """Whether `rejudge` has something to work from — used to decide the
        results card's "Re-judge" affordance. A finished review that kept its
        findings record and whose manuscript is still where it was; prep and
        promo have no corrections to rule on."""
        if job.kind != "review" or job.state != "done" or not job.results_dir:
            return False
        return (Path(job.results_dir, "findings.json").is_file()
                and Path(job.source_path).is_file())

    def rejudge(self, job_id: str, *, gates: dict, models: dict | None = None,
                prompts: dict | None = None) -> Job | None:
        """Put a finished review's corrections to the judge gates, and nothing
        else. No detector call is made — the corrections come off the record the
        original run left — so a book proofread before these gates existed can
        be gated now for the price of the gates alone.

        The result is written beside the original as its own review, so the
        first deliverable is never overwritten: an editor comparing the two is
        the point of running this at all."""
        from docproof.rejudge import RejudgeError, rejudge as run_rejudge

        job = self.store.get(job_id)
        if job is None or not self.can_rejudge(job):
            return None
        cfg = self.config_for(job)
        # The gates are the whole run here, so the panel's switches win outright
        # rather than riding on whatever the original job recorded.
        for name in ("meaning_check", "fix_check"):
            gate = getattr(cfg, name)
            gate.enabled = bool(gates.get(name))
            picked = (models or {}).get(name)
            if picked:
                gate.model = picked
            gate.prompt = (prompts or {}).get(name, "")
        if not (cfg.meaning_check.enabled or cfg.fix_check.enabled):
            return None

        # A new record, not a copy of the old run. Everything the finished review
        # recorded about ITS run — how far it got, when its step began, what it
        # cost, where its files went in the archive — would be read as this run's,
        # and `save` does not stamp the stage clock the way `update` does. The
        # card read the original's section count against a clock last set when
        # that review finished, so a re-judge started now announced itself as
        # "Reviewing (412 of 412 sections) · 33h 0m". The archive fields are worse
        # than cosmetic: inheriting `drive_files` makes _archive_job skip every
        # upload as already placed and mark this run archived at the original's
        # folder, so the re-judged deliverable never reaches Drive at all.
        #
        # `mode`/`schedule_at` go with them: a re-judge runs inline, here, and a
        # record claiming to be an overnight batch is a record the ticker and the
        # restart sweep both have to reason about. See _resume.
        source = self.store.save(replace(
            job, id=batchlib.new_job_id(job.filename), state="running",
            stage="judging", stage_since=_now(), results_dir="", error="",
            error_kind="", applied=0, queried=0, judge_held=0, done=0, total=0,
            review_round=0, total_rounds=0, mode="now", schedule_at=None,
            collect_attempts=0, audit_failed=False, audit_overridden=False,
            verified=None, words=None,
            input_tokens=0, output_tokens=0, cache_read_tokens=0,
            cache_write_tokens=0, api_calls=0, cost=None, sapling_cost=0.0,
            archive="", archive_error="", archive_attempts=0,
            drive_folder_id="", drive_files={},
            created_at=datetime.now(timezone.utc).isoformat(),
            meaning_model=(models or {}).get("meaning_check", ""),
            fix_model=(models or {}).get("fix_check", ""),
            features={**(job.features or {}),
                      "meaning_check": cfg.meaning_check.enabled,
                      "fix_check": cfg.fix_check.enabled}))
        out = self.results_dir(source)
        usage = Usage()
        try:
            outputs = run_rejudge(cfg, job.results_dir, out_dir=out,
                                  error_dir=self.error_dir,
                                  source=job.source_path, usage=usage)
        except (RejudgeError, ProviderError, IngestError, ValueError) as e:
            log.warning("Re-judge of %s failed: %s", job_id, e)
            updated = self.store.update(source.id, state="failed", error=str(e),
                                        stage="")
            self._finish(source.id)
            return updated
        updated = self.store.update(source.id, state="done",
                                    **_tallies(outputs),
                                    results_dir=str(out), stage="")
        self._record_usage(source.id, out, cfg.meaning_check.model, batch=False)
        self._finish(source.id)
        return updated

    def can_recover(self, job: Job) -> bool:
        """Whether `recover` has something to do — used to decide the results
        card's "Finish collecting" affordance. A failed batch review whose
        vendor batch actually landed (so a re-collect reuses paid results), and
        not a reject-all audit failure (that has its own "Download anyway")."""
        return (job.state == "failed" and not job.audit_failed
                and self._batch_job_id(job) is not None)

    def _provider(self, cfg: Config):
        name = provider_for(cfg.api.model, cfg.api.provider)
        return build_provider(cfg, api_key=get_api_key(name))

    def results_dir(self, job: Job) -> Path:
        """Where this job's finished files go, claimed as it is chosen.

        Two reviews of one document must not share a folder. The second would
        overwrite the first, and — worse — the first review's download button
        would quietly start serving the second review's document. A name
        already taken gets a numbered suffix, the way a browser handles
        downloading the same file twice.

        The folder is created here rather than merely picked: the worker
        thread and the ticker can be finishing two jobs at the same moment,
        and looking before creating would let both settle on the same name."""
        if job.results_dir:
            return Path(job.results_dir)   # already claimed; a retry reuses it
        base = Path(self.settings.output_dir).expanduser()
        stem = Path(job.filename).stem or "document"
        n = 1
        while True:
            candidate = base / (stem if n == 1 else f"{stem} ({n})")
            try:
                candidate.mkdir(parents=True)
                return candidate
            except FileExistsError:
                n += 1

    def _claim_results_dir(self, job: Job) -> Path:
        """Claim the folder and record it, so a job interrupted between here
        and its last write comes back to the same place instead of claiming a
        second one and orphaning the first."""
        out = self.results_dir(job)
        self.store.update(job.id, results_dir=str(out))
        return out

    def _release_results_dir(self, job_id: str) -> None:
        """Give an unused claim back after a failure, so a run that never
        wrote anything doesn't leave an empty folder — or push the next
        review's name to (2)."""
        job = self.store.get(job_id)
        if job is None or not job.results_dir:
            return
        try:
            Path(job.results_dir).rmdir()      # refuses if anything is in it
        except OSError:
            return                             # it has results, or is gone
        self.store.update(job_id, results_dir=None)

    def delete_job(self, job_id: str) -> bool:
        """Remove a finished job for good: its produced documents and its
        record, so it leaves the results list and reclaims its disk.

        The results folder is deleted only when it sits inside the configured
        output directory — a guard so a malformed results_dir can never turn
        this into an rmtree of somewhere it shouldn't be. The abort flag is
        cleared too, in case a delete races a run that just finished."""
        job = self.store.get(job_id)
        if job is None:
            return False
        self._record_spending(job)
        if job.results_dir:
            results = Path(job.results_dir)
            base = Path(self.settings.output_dir).expanduser().resolve()
            try:
                inside = results.resolve().is_relative_to(base)
            except (OSError, ValueError):
                inside = False
            if inside and results.is_dir():
                shutil.rmtree(results, ignore_errors=True)
            elif not inside:
                log.warning("Leaving results dir outside the output base in "
                            "place: %s", job.results_dir)
        self._clear_cancel(job_id)
        return self.store.delete(job_id)

    def _record_spending(self, job: Job) -> None:
        """Snapshot a job's cost to the ledger before its folder is removed, so
        clearing jobs frees disk without erasing the bill. Only jobs that
        actually spent are kept — a cancelled job that never called the API
        leaves nothing worth a row — and the snapshot is taken while
        `results_dir` is still there, so its cost no longer depends on it."""
        numbers = _totals_for(job, read_usage)
        if not (numbers["api_calls"] or numbers["input_tokens"]):
            return
        try:
            self.ledger.record(LedgerEntry.from_job(job, numbers))
        except OSError as e:
            # A ledger write that fails must not block the delete the user
            # asked for; losing one job's snapshot is better than a job that
            # won't clear.
            log.warning("Could not record spending for %s: %s", job.id, e)

    # -- worker ---------------------------------------------------------------

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._busy.set()
            try:
                self.run_one(job_id)
            except Exception as e:            # noqa: BLE001 - never kill the worker
                log.exception("Job %s failed", job_id)
                self.store.update(job_id, state="failed", error=str(e))
            finally:
                # Whatever happened to the job, this id's abort request is spent:
                # clear it so it can't touch a later run that reuses nothing but
                # the same worker.
                self._clear_cancel(job_id)
                self._busy.clear()
                self.queue.task_done()

    def run_one(self, job_id: str) -> None:
        """Dispatch by what the job is, then by when. Public so a test can
        drive the worker's body without a thread."""
        job = self.store.get(job_id)
        if job is None:
            return
        if job.is_prep:
            self._run_prep(job_id)
        elif job.is_corrections:
            self._run_corrections(job_id)
        elif job.is_promo:
            self._run_promo(job_id)
        elif job.rounds > 1:
            # Multi-round review owns its whole run on the worker thread (the
            # rounds are sequential — round k+1 reads round k's corrections — so
            # a batch run cannot be spread across ticker passes the way a single
            # batch is). It runs to completion here, sync or batch by mode.
            self._run_rounds(job_id)
        elif job.mode == "batch":
            # Two legs on the worker now, told apart by state: a "collecting"
            # job is a ready batch the ticker just handed off to be written; any
            # other state (queued, retried) is one still to be submitted. Both
            # do a heavy whole-book prepare, so serialising them on this single
            # thread is what keeps them from overlapping and deadlocking.
            if job.state == "collecting":
                self._collect_batch(job_id)
            else:
                self._submit_batch(job_id)
        else:
            self._run_now(job_id)

    def _run_rounds(self, job_id: str) -> None:
        """Run a multi-round review to completion on the worker thread.

        The rounds driver (docproof.rounds) does the whole pipeline per round and
        assembles the deliverable with finish, so this is thinner than _run_now:
        build the two providers (the detector model, and the judge model/effort
        from rounds config), then block in the driver. Sync or batch by mode; a
        batch run polls its own rounds rather than yielding to the ticker."""
        from docproof.rounds import run_batch_rounds, run_sync_rounds

        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        self.store.update(job_id, stage="preparing")
        try:
            review_provider = self._provider(cfg)
            jcfg = cfg.model_copy(deep=True)
            jcfg.api.model = cfg.rounds.judge_model
            jcfg.api.effort = cfg.rounds.judge_effort
            judge_provider = self._provider(jcfg)
        except (ProviderError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return

        # review_round starts at 1, not 0: the driver's first callback only
        # lands once round 1's review begins, and the whole-book ingest before
        # it would otherwise leave the card reading "Round 0".
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=0, review_round=1,
                                total_rounds=job.rounds) is None:
            return
        # "preparing", not "reviewing": the driver's first act is the whole-book
        # ingest (spell scan, story sheet), and the rounds cycle the stages from
        # there — each round re-emits "preparing" while its working document is
        # rebuilt, walks the review stages, then "round_judge" as the judge
        # takes that round's corrections.
        self.store.update(job_id, stage="preparing")

        def on_progress(rnd: int, total_rounds: int, done: int,
                        total: int) -> None:
            # The card's whole story for a multi-round run: which round, and how
            # far through it. The section count starts over each round; the
            # round number is what keeps that legible. See Job.plain_state.
            self.store.update(job_id, review_round=rnd,
                              total_rounds=total_rounds, done=done, total=total)

        def on_phase(name: str) -> None:
            # Which step the current round is on — same callback _run_now hands
            # run_sync and finish, so a rounds card names its steps too.
            self.store.update(job_id, stage=name)

        out = self._claim_results_dir(job)
        should_cancel = lambda: self._cancel_pending(job_id)  # noqa: E731
        try:
            if job.mode == "batch":
                outputs = run_batch_rounds(
                    cfg, job.source_path, self.error_dir,
                    str(out / "rounds-ws"), out_dir=out,
                    review_provider=review_provider,
                    judge_provider=judge_provider, on_progress=on_progress,
                    should_cancel=should_cancel, on_phase=on_phase)
            else:
                outputs = run_sync_rounds(
                    cfg, job.source_path, self.error_dir, out_dir=out,
                    review_provider=review_provider,
                    judge_provider=judge_provider, on_progress=on_progress,
                    should_cancel=should_cancel, on_phase=on_phase)
        except JobCancelled:
            # An abort mid-run: stop cleanly (cancelled, not failed), releasing
            # the results dir and discarding the checkpoint. The calls already
            # paid for before the abort are billed; the ones not yet started are
            # not — that is the point of the cap.
            self._abort(job_id)
            return
        except AuditError as e:
            log.error("Multi-round review for %s failed its reject-all audit: "
                      "%s", job.id, e)
            self.store.update(job_id, state="failed", error=str(e),
                              results_dir=str(out), audit_failed=True)
            self._record_usage(job_id, out, cfg.api.model,
                               batch=job.mode == "batch")
            self._archive_done(job_id)        # a failed review still has notes
            return
        except (ProviderError, IngestError, batchlib.BatchError,
                FileNotFoundError, ValueError) as e:
            self._release_results_dir(job_id)
            self.store.update_if(job_id, expect="running", state="failed",
                                 error=str(e))
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise
        self.store.update(job_id, state="done", **_tallies(outputs),
                          results_dir=str(out), error=None, stage="")
        self._record_usage(job_id, out, cfg.api.model, batch=job.mode == "batch")
        self._finish(job_id)

    def _run_now(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        # Ingesting and the whole-book reads (spell scan, story sheet) happen
        # here, before the run flips to "running", so the card reads "Reading
        # your manuscript" rather than a stale "Waiting to start" meanwhile.
        self.store.update(job_id, stage="preparing")
        try:
            provider = self._provider(cfg)
            prepared = prepare(cfg, job.source_path, self.error_dir,
                               selection=job.selection)
        except (ProviderError, IngestError, FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return

        # Compare-and-swap, not a plain write: building the provider and
        # ingesting the document took long enough for a cancel to land in the
        # meantime, and a job the user pulled back must not start paying for
        # model calls — or overwrite its own cancellation — now.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=prepared.request_count) is None:
            return

        def progress(done: int, total: int) -> None:
            self.store.update(job_id, done=done, total=total)

        def on_phase(name: str) -> None:
            # Which step the run is on. The detector loop ("reviewing") carries
            # the count; the whole-book passes after it have none, so this is all
            # the card gets to know the document has moved past the bar.
            self.store.update(job_id, stage=name)

        # The checkpoint outlives any failure below on purpose: a retry after
        # a crash or a mid-run exception resumes instead of paying again. Only
        # a finished job deletes it.
        checkpoint = self._checkpoint(job, cfg, prepared)
        coverage = CoverageLedger()
        try:
            findings, usage = run_sync(
                cfg, prepared, provider, progress=progress, on_phase=on_phase,
                checkpoint=checkpoint, coverage=coverage,
                should_cancel=lambda: self._cancel_pending(job_id))
        except JobCancelled:
            self._abort(job_id)
            return
        # finish() speaks the same on_phase callback: each of its paid passes
        # (Sapling, smoothing, the judge gates, …) names itself as it starts,
        # and "writing" is emitted only once the document is actually being
        # assembled — so the card no longer claims "writing your document"
        # through many minutes of judge and whole-book passes.
        out = self._claim_results_dir(job)
        try:
            outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                             source_path=job.source_path, coverage=coverage,
                             on_phase=on_phase)
        except AuditError as e:
            # No reviewed document was written — that is the point — but the
            # summary and findings were, and they name the paragraph that did
            # not come back. So the folder stays and the job points at it.
            #
            # The checkpoint stays too, unlike prep's: the findings did not
            # cause this. Something applied text without a revision mark
            # around it, which is a code fault, and a retry should not have to
            # pay for the review again to reproduce it.
            log.error("Review for %s failed its reject-all audit: %s",
                      job.id, e)
            self.store.update(job_id, state="failed", error=str(e),
                              results_dir=str(out), audit_failed=True)
            self._record_usage(job_id, out, cfg.api.model, batch=False)
            self._archive_done(job_id)        # a failed review still has notes
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise
        checkpoint.delete()
        self.store.update(job_id, state="done", **_tallies(outputs),
                          results_dir=str(out), error=None, stage="")
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._finish(job_id)

    # -- prep -----------------------------------------------------------------

    def _run_prep(self, job_id: str) -> None:
        """Tag a manuscript into the house style set.

        Always synchronous: the windows have to be read in order, since what a
        paragraph is depends on what came before it, and a batch API answers
        out of order by design."""
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            prepared = preplib.prepare(
                cfg, job.source_path, config_dir=self.config_path.parent,
                override_dir=self.store.paths.prep)
        except (ProviderError, IngestError, StyleSheetError, ConversionError,
                FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return

        # Same compare-and-swap as _run_now, for the same reason: preparing a
        # whole manuscript leaves plenty of room for a cancel to land first.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=prepared.request_count,
                                words=prepared.structure.word_count) is None:
            return

        # Prep reads its windows in order, one call at a time, so an abort just
        # rides the progress callback: it raises between windows, after the last
        # finished one is checkpointed and before the next is paid for.
        def progress(done: int, total: int) -> None:
            if self._cancel_pending(job_id):
                raise JobCancelled()
            self.store.update(job_id, done=done, total=total)

        # Kept through failures — including a failed verification — so a retry
        # replays the windows already paid for rather than re-tagging the book.
        checkpoint = self._checkpoint(job, cfg, prepared)
        try:
            tags, usage = preplib.run(cfg, prepared, provider,
                                      checkpoint=checkpoint, progress=progress)
        except JobCancelled:
            self._abort(job_id)
            return
        # The book output's three facts: whatever the operator typed wins,
        # field by field; the detector fills the rest with one small call. A
        # detection failure is a default face and a file-name title, never a
        # failed job. The answer rides the same checkpoint as the windows, so
        # a retry (or a re-considered manuscript) replays it instead of
        # paying for it again.
        meta = None
        # A plain design (DocWatch's) reads no subject face and hangs no
        # running heads, so it needs none of the three facts — skip the call
        # and its cost. The app's paperback design needs all three.
        if ("book" in cfg.prep.outputs and prepared.design is not None
                and prepared.design.needs_meta):
            from docproof.checkpoint import add_usage, snapshot, usage_delta

            detected = preplib.BookMeta()
            if not (job.prep_subject and job.prep_title and job.prep_author):
                cached = checkpoint.get("book_meta")
                if cached is not None and cached.items:
                    detected = preplib.BookMeta(**cached.items[0])
                    add_usage(usage, cached.usage)
                else:
                    before = snapshot(usage)
                    try:
                        detected = preplib.detect_meta(cfg, prepared, provider,
                                                       usage=usage)
                    except Exception as e:          # noqa: BLE001
                        log.warning("Subject detection failed for %s: %s",
                                    job.id, e)
                    checkpoint.put("book_meta", items=[asdict(detected)],
                                   usage=usage_delta(before, usage), ok=True)
            meta = preplib.merge_meta(detected, subject=job.prep_subject,
                                      title=job.prep_title,
                                      author=job.prep_author)
            self.store.update(job_id, prep_book=asdict(meta))

        self.store.update(job_id, state="collecting")
        out = self._claim_results_dir(job)
        try:
            outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                     source_path=job.source_path,
                                     outputs=cfg.prep.outputs, meta=meta)
        except VerificationFailed as e:
            # The notes were still written, and they are the most useful thing
            # here: they say what prep intended and where the text diverged. So
            # the folder stays and the job points at it.
            #
            # The checkpoint, though, goes: it holds the exact tags that just
            # produced the failing file, and a retry that replays them is a
            # retry that deterministically fails the same way. Verification
            # failure is the one failure where the cache is the problem.
            checkpoint.delete()
            log.error("Prep for %s failed verification: %s", job.id, e)
            self.store.update(job_id, state="failed", error=str(e),
                              results_dir=str(out), verified=False)
            self._record_usage(job_id, out, cfg.api.model, batch=False)
            self._archive_done(job_id)        # the notes are worth keeping
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        checkpoint.delete()
        self.store.update(job_id, state="done", results_dir=str(out),
                          error=None, tagged=outputs.tagged,
                          applied=outputs.tagged, flags=outputs.flags,
                          verified=all(c.ok for c in outputs.verifications),
                          words=outputs.words)
        self._record_usage(job_id, out, cfg.api.model, batch=False)
        self._finish(job_id)

    # -- corrections ----------------------------------------------------------

    def _run_corrections(self, job_id: str) -> None:
        """Apply a corrections list to the designer's exported IDML.

        Deterministic and quick: the edit list is anchored to the exact text it
        names and each span replaced, then the result is verified against the
        list, all in pure Python (see docproof.corrections). No model, no
        vendor, no batch — so, unlike a review, there is no provider to build and
        nothing to bill; it runs to completion here on the worker thread like
        prep, but leaner."""
        from docproof.corrections.run import apply_corrections

        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        # Nothing paid-for happens below (unless the sanity gate is on), but keep
        # the compare-and-swap the other runners use: a cancel that landed while
        # the job sat in the queue is honoured, and the card reads "applying"
        # instead of a stale "waiting".
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=0, stage="reading") is None:
            return
        if self._cancel_pending(job_id):
            self._abort(job_id)
            return

        # The reviewer comments (from a marked-up PDF), so every one is accounted
        # for in the change log. Absent for a typed list; a bad blob degrades to
        # the edit-only ledger rather than failing the run.
        comments = None
        if job.corrections_comments:
            try:
                loaded = json.loads(job.corrections_comments)
                comments = loaded if isinstance(loaded, list) else None
            except (json.JSONDecodeError, ValueError):
                comments = None

        # The proof's page texts, so each mark narrows to the text its own page
        # set. A bad blob degrades to no page narrowing — more flags, never a wrong
        # edit — rather than failing the run.
        page_texts = None
        if job.corrections_pages:
            try:
                loaded = json.loads(job.corrections_pages)
                if isinstance(loaded, list):
                    page_texts = [str(t or "") for t in loaded]
            except (json.JSONDecodeError, ValueError):
                page_texts = None

        # The opt-in model passes. Building a provider is the only place this run
        # touches a model; when both are off (the default) the run stays free and
        # deterministic. A missing key turns a pass off rather than failing.
        sanity = self._corrections_sanity(job) if job.corrections_sanity else None
        second = (self._corrections_second_look(job)
                  if job.corrections_second_look else None)
        # The last tier has its own flag now (the request defaults it to follow the
        # second look, so nothing that did not send it changed), so an operator can
        # take the cheap second look without also buying the frontier reads.
        escalate = (self._corrections_escalate(job)
                    if job.corrections_escalate else None)

        # Which step the apply is on, straight onto the card. The run holds no
        # lock and writes nothing else while it works, so without this a proof
        # that takes the model passes shows one motionless line for minutes.
        # A store that refuses the write must not sink an otherwise-good run.
        def on_progress(stage: str, done: int, total: int) -> None:
            try:
                self.store.update(job_id, stage=stage, done=done, total=total)
            except Exception:             # noqa: BLE001 - progress is not the job
                log.debug("Could not record corrections progress", exc_info=True)

        out = self._claim_results_dir(job)
        try:
            outputs = apply_corrections(job.source_path, job.corrections, out,
                                        comments=comments, sanity=sanity,
                                        second_look=second, escalate=escalate,
                                        page_texts=page_texts,
                                        progress=on_progress)
        except (ValueError, OSError) as e:
            # A corrections list the parser refuses whole (malformed JSON), or a
            # source that will not read — fail with the sentence, and give the
            # empty results folder back so the next run names cleanly.
            self._release_results_dir(job_id)
            self.store.update_if(job_id, expect="running", state="failed",
                                 error=str(e), stage="")
            return
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        # The opt-in passes' small model spend, when any ran, recorded like any
        # other so the dashboard is honest; the deterministic path stays 0.0.
        cost = 0.0
        if outputs.usage is not None:
            try:
                fallback = next((p[1] for p in (sanity, second, escalate)
                                 if p), "")
                cost = cost_of_usage(outputs.usage, fallback_model=fallback,
                                     batch=False) or 0.0
            except Exception:                 # noqa: BLE001 - spend logging is not the job
                log.warning("Could not price the corrections model passes",
                            exc_info=True)

        # `applied`/`flags` reuse the fields prep and review already fill so the
        # dashboard and card counters need no new plumbing; `discrepancies`,
        # `total_comments` and `unresolved` are the figures corrections adds, and
        # `verified` carries the clean flag.
        self.store.update(
            job_id, state="done", results_dir=str(out), error=None, stage="",
            applied=outputs.applied, flags=outputs.flagged,
            discrepancies=outputs.discrepancies,
            total_comments=outputs.total_comments,
            applied_comments=outputs.applied_comments,
            unresolved=outputs.unresolved,
            verified=outputs.clean, cost=cost)
        self._finish(job_id)

    def _corrections_sanity(self, job):
        """The `(provider, model)` for the opt-in edit-sanity gate, or None when no
        key is set — in which case the gate is quietly skipped rather than failing
        an otherwise-deterministic run. Reuses the corrections extraction model."""
        from app.routes.jobs import CORRECTIONS_EXTRACT_MODEL
        try:
            cfg = self.config_for(job)
            cfg.api.model = CORRECTIONS_EXTRACT_MODEL
            info = lookup(CORRECTIONS_EXTRACT_MODEL)
            key = get_api_key(info.provider) if info else None
            if not key:
                log.warning("Corrections sanity gate requested but no key for %s; "
                            "skipping the gate", CORRECTIONS_EXTRACT_MODEL)
                return None
            return self._provider(cfg), CORRECTIONS_EXTRACT_MODEL
        except Exception:                     # noqa: BLE001 - a missing gate must not sink the run
            log.warning("Could not build the corrections sanity gate; skipping",
                        exc_info=True)
            return None

    def _corrections_second_look(self, job):
        """The `(provider, model)` for the opt-in second look over the extractor's
        queries, or None when no key is set — the pass is then quietly skipped and
        every query stays a human's, exactly as with the gate off. Uses the strong
        reader (see CORRECTIONS_SECOND_LOOK_MODEL): the whole point of the pass is
        an editorial call the cheap extractor refused to make."""
        from app.routes.jobs import CORRECTIONS_SECOND_LOOK_MODEL as model
        try:
            cfg = self.config_for(job)
            cfg.api.model = model
            info = lookup(model)
            key = get_api_key(info.provider) if info else None
            if not key:
                log.warning("Corrections second look requested but no key for "
                            "%s; skipping the pass", model)
                return None
            return self._provider(cfg), model
        except Exception:                     # noqa: BLE001 - a missing pass must not sink the run
            log.warning("Could not build the corrections second look; skipping",
                        exc_info=True)
            return None

    def _corrections_escalate(self, job):
        """The `(provider, model)` for the last tier over the queries the second
        look declined, or None when neither model can be keyed.

        Prefers the frontier reader (CORRECTIONS_ESCALATE_MODEL) and falls back to
        the second look's own model when its vendor has no key here — the tier is
        worth more on the weaker model than not at all, since most of what it adds
        is the evidence it is given rather than the model it is given to. The
        fallback is logged, not silent: a run whose last tier quietly dropped to a
        cheaper model should be readable from the log."""
        from app.routes.jobs import (CORRECTIONS_ESCALATE_MODEL,
                                     CORRECTIONS_SECOND_LOOK_MODEL)
        try:
            cfg = self.config_for(job)
            for model, why in ((CORRECTIONS_ESCALATE_MODEL, ""),
                               (CORRECTIONS_SECOND_LOOK_MODEL, "fallback")):
                info = lookup(model)
                if info and get_api_key(info.provider):
                    if why:
                        log.warning("No key for %s; running the corrections last "
                                    "tier on %s instead",
                                    CORRECTIONS_ESCALATE_MODEL, model)
                    cfg.api.model = model
                    return self._provider(cfg), model
            log.warning("Corrections last tier requested but no key for %s or "
                        "%s; skipping the tier", CORRECTIONS_ESCALATE_MODEL,
                        CORRECTIONS_SECOND_LOOK_MODEL)
            return None
        except Exception:                     # noqa: BLE001 - a missing tier must not sink the run
            log.warning("Could not build the corrections last tier; skipping",
                        exc_info=True)
            return None

    # -- promo ----------------------------------------------------------------

    def _run_promo(self, job_id: str) -> None:
        """Write a teaser and a set of social posts from a finished manuscript.

        Synchronous like prep, but for a simpler reason: it is a single call
        with the whole book in front of the model, so there is nothing to run in
        parallel and no batch to wait on. What it produces — promo.json (the
        editable copy) and the two .docx — lands in the results folder. Whether
        that then ships to Drive or waits in the panel for a human is the
        approval field's business, settled by the caller above this run, not
        here: this method only generates."""
        job = self.store.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return
        if job.plan_only:
            # Same store, same panel, different deliverable: the marketing plan
            # is its own call with its own metadata, so it runs on its own path.
            self._run_plan(job_id, job)
            return
        cfg = self.config_for(job)
        try:
            provider = self._provider(cfg)
            prepared = promolib.prepare(
                cfg, job.source_path, config_dir=self.config_path.parent,
                override_dir=self.store.paths.promo,
                allow_oversize=job.allow_oversize)
        except (ProviderError, IngestError, PromoError,
                FileNotFoundError, ValueError) as e:
            # An oversize book carries its numbers so the watcher can email a
            # person the size and how to run it by hand, rather than just logging
            # a generic failure.
            extra = ({"error_kind": "oversize", "words": e.words,
                      "input_tokens": e.tokens}
                     if isinstance(e, PromoTooLarge) else {})
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e), **extra)
            return

        # Same compare-and-swap as the other pipelines: reading a whole novel
        # leaves room for a cancel to land before the paid call goes out.
        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=1,
                                words=prepared.manuscript.word_count) is None:
            return

        # One call, so cancellation is a single check right before it: past this
        # point there is no safe seam to interrupt a request already in flight.
        if self._cancel_pending(job_id):
            self._abort(job_id)
            return
        try:
            result, usage = promolib.run(cfg, prepared, provider)
        except PromoError as e:
            # A refusal, or an answer that did not fit the schema. Nothing was
            # written and no results folder was claimed; fail with the sentence.
            self.store.update(job_id, state="failed", error=str(e))
            return

        # The opt-in second pass: a model check that each claim follows from the
        # book. It never fails the run — a grounding check that broke must not
        # sink the copy it was checking — and its tokens fold into `usage`.
        claims = ()
        if cfg.promo.verify_claims:
            claims = tuple(promolib.verify_claims(
                cfg, prepared, result, provider, usage,
                config_dir=self.config_path.parent,
                override_dir=self.store.paths.promo))

        self.store.update(job_id, state="collecting")
        out = self._claim_results_dir(job)
        try:
            outputs = promolib.finish(prepared, result, usage, cfg, out_dir=out,
                                      source_path=job.source_path, claims=claims)
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        self.store.update(
            job_id, state="done", results_dir=str(out), error=None,
            words=outputs.words, unverified=outputs.flag_count,
            # Reuse the prep card's clean/flagged light: grounding-clean copy
            # reads as "verified", flagged copy invites a look before it ships.
            verified=outputs.flag_count == 0)
        self._record_usage_inline(job_id, usage, cfg.api.model)
        self._finish(job_id)

    def _run_plan(self, job_id: str, job: Job) -> None:
        """Write the marketing plan — promo's third deliverable — for a book.

        The plan sibling of `_run_promo`, and the same synchronous single-call
        shape: prepare (read the whole book, render the plan prompt with the
        job's metadata), one call, finish (write the .docx and the JSON). It
        shares every seam — the oversize guard, the cancel check right before
        the paid call, the inline usage record — differing only in the engine
        functions it drives and that no grounding pass runs (the plan's
        comparable titles are external to the book by design)."""
        cfg = self.config_for(job)
        meta = promolib.PlanMeta(
            title="", author_name=job.plan_author, keywords=job.plan_keywords,
            back_cover=job.plan_blurbs, author_city=job.plan_city,
            questionnaire=job.plan_questionnaire)
        try:
            provider = self._provider(cfg)
            prepared = promolib.prepare_plan(
                cfg, job.source_path, meta, config_dir=self.config_path.parent,
                override_dir=self.store.paths.promo,
                allow_oversize=job.allow_oversize)
        except (ProviderError, IngestError, PromoError,
                FileNotFoundError, ValueError) as e:
            extra = ({"error_kind": "oversize", "words": e.words,
                      "input_tokens": e.tokens}
                     if isinstance(e, PromoTooLarge) else {})
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e), **extra)
            return

        if self.store.update_if(job_id, expect=job.state, state="running",
                                done=0, total=1,
                                words=prepared.manuscript.word_count) is None:
            return
        if self._cancel_pending(job_id):
            self._abort(job_id)
            return
        try:
            plan, usage = promolib.run_plan(cfg, prepared, provider)
        except PromoError as e:
            self.store.update(job_id, state="failed", error=str(e))
            return

        self.store.update(job_id, state="collecting")
        out = self._claim_results_dir(job)
        try:
            outputs = promolib.finish_plan(prepared, plan, cfg, out_dir=out,
                                           source_path=job.source_path)
        except Exception:                     # noqa: BLE001 - re-raised below
            self._release_results_dir(job_id)
            raise

        self.store.update(job_id, state="done", results_dir=str(out),
                          error=None, words=outputs.words,
                          # The plan runs no grounding check, so it always reads
                          # clean — nothing for a person to reconcile before it
                          # ships, the way flagged copy invites.
                          unverified=0, verified=True)
        self._record_usage_inline(job_id, usage, cfg.api.model)
        self._finish(job_id)

    def _record_usage_inline(self, job_id: str, usage, model: str) -> None:
        """Copy token counts straight onto the record from the run in hand.

        Review and prep re-read what the engine wrote to disk; promo has the
        counts in memory (one call, no checkpoint to reconcile), so it records
        them directly. `_totals_for` reads the record before the folder, so the
        dashboard and the ledger see these with no re-read."""
        cost = cost_of_usage(usage, fallback_model=model, batch=False)
        self.store.update(
            job_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            api_calls=usage.api_calls, cost=cost)

    # -- finishing: archive, then tell a person -------------------------------

    def _finish(self, job_id: str) -> None:
        """Everything a job does once it has reached a terminal state with its
        files on disk: push them to the Drive archive, then email the completion
        log — in that order, so the email can carry the archive link when the
        first attempt lands. Both are best-effort and never raise back into the
        run; a job that did its work must not fail over an upload or a mail.

        Used at every terminal transition, success or a failure that still left
        artifacts (an audit or verification failure keeps its notes). The email
        step no-ops on anything but a finished job, so a failed-with-artifacts
        job is archived here without being announced as done."""
        self._archive_done(job_id)
        self._notify_done(job_id)

    def archive_job(self, job_id: str) -> None:
        """Archive one job now, through the same locked, best-effort path a
        completing job takes. The "Retry archive" route's entry point."""
        self._archive_done(job_id)

    def _archive_done(self, job_id: str) -> None:
        """Push a finished job's outputs to the Drive archive, if it is switched
        on. One inline attempt; a Drive hiccup leaves the job "pending" for the
        ticker's sweep to retry. Best-effort and silent when off: an install
        that never configured an archive pays nothing here. See
        app/watch/archive.py."""
        if self.notify_home is None:
            return
        try:
            from .watch import archive
            with self._archive_lock:
                archive.archive_done(self.notify_home, self.store, job_id)
        except Exception:                     # noqa: BLE001 - never over a job
            log.exception("Archiving %s failed", job_id)

    def _archive_sweep(self) -> None:
        """The ticker's Drive-archive pass: retry inline attempts that hit a
        hiccup, and backfill everything that finished before the archive was
        switched on. Bounded per tick (see archive.PER_TICK) so a large backfill
        drains over several passes rather than holding the ticker. Best-effort;
        a failure here never derails the batch work the tick also does."""
        if self.notify_home is None:
            return
        try:
            from .watch import archive
            with self._archive_lock:
                archive.sweep_once(self.notify_home, self.store)
        except Exception:                     # noqa: BLE001 - never over a tick
            log.exception("Archive sweep failed")

    def _notify_done(self, job_id: str) -> None:
        """Email the completion log for a job that just finished.

        One address and one switch for every pipeline, reused from DocWatch —
        see `notify.send_job_completion`. Watched *format* jobs are the one
        exception: the tick emails those itself, with the routing and Drive links
        a watched book carries and this record does not, so they are left to it
        and not emailed twice. Everything else — every app job, and watched promo
        — goes through here. Best-effort: a mail that will not send is logged,
        never raised, so it can never undo a finished job."""
        if self.notify_home is None:
            return
        job = self.store.get(job_id)
        if job is None or job.state != "done":
            return
        if job.source == "watch" and job.is_prep:
            return                            # the tick emails these, richer
        try:
            from .watch import notify
            notify.send_job_completion(self.notify_home, job)
        except Exception:                     # noqa: BLE001 - never over a job
            log.exception("Completion email for %s failed", job_id)

    # -- what it cost ---------------------------------------------------------

    def _record_usage(self, job_id: str, out: Path, model: str, *,
                      batch: bool) -> None:
        """Copy the token counts onto the job record.

        They are already in findings.json / prep.json, but the dashboard adds
        up every job the user has ever run, and re-reading a folder per job to
        do that gets slower every week."""
        totals = read_usage(out)
        if totals is None:
            return
        usage, cost = totals
        if cost is None:
            # Summed at each model's own rate from the per-model breakdown the
            # run recorded (falls back to `model` for records that predate it),
            # so a mixed OpenAI/Anthropic review is not priced at the detector's.
            cost = cost_of_usage(usage, fallback_model=model, batch=batch)
        # Sapling is billed per character and never in the model estimate, so
        # fold its charge into the recorded total — this is what the email, the
        # spending ledger and the dashboard all read.
        sapling_cost = usage.get("sapling_cost", 0.0) or 0.0
        if sapling_cost:
            cost = (cost or 0.0) + sapling_cost
        self.store.update(
            job_id,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            api_calls=usage.get("api_calls", 0), cost=cost,
            sapling_cost=sapling_cost)

    # -- batch ----------------------------------------------------------------

    def _submit_batch(self, job_id: str) -> None:
        job = self.store.get(job_id)
        # Same guard _run_now and _run_prep already have: a job cancelled while
        # its id was still sitting in the queue must not be submitted the
        # instant the worker gets to it.
        if job is None or job.state not in ("queued", "running"):
            return
        cfg = self.config_for(job)
        # Submission ingests the document and makes the whole-book reads (spell
        # scan, story sheet) while building the requests — minutes on a big
        # book, which read as a stuck "Waiting to start" without this. Cleared
        # on the flip to waiting: the overnight card has its own message, and
        # collect starts the stage story over from "preparing".
        self.store.update(job_id, stage="preparing")
        try:
            provider = self._provider(cfg)
            with self._stall_alarm(job_id, "Submitting"):
                batch_job = batchlib.submit(cfg, job.source_path,
                                            self.error_dir, provider,
                                            self.store.paths.jobs,
                                            selection=job.selection)
        except (ProviderError, IngestError, batchlib.BatchError,
                FileNotFoundError, ValueError) as e:
            self.store.update_if(job_id, expect=job.state, state="failed",
                                 error=str(e))
            return
        # batchlib picked its own folder; record the link and adopt its total.
        # The id file goes down before the state does: `waiting` is the
        # ticker's cue to go looking for the id, and writing them the other
        # way round gave it a moment where a submitted, billable batch looked
        # lost and the job was failed for it.
        (self.store.dir(job_id) / "batch_job_id").write_text(batch_job.job_id,
                                                             encoding="utf-8")
        if self.store.update_if(job_id, expect=job.state, state="waiting",
                                total=batch_job.request_count,
                                error=None, stage="") is None:
            # Cancelled while the batch was on its way to the vendor. It can't
            # be unsubmitted, so say what it cost: the id file names it.
            log.warning("Job %s was cancelled during submission; batch %s is "
                        "at the vendor and will not be collected.", job_id,
                        batch_job.job_id)

    def _batch_job_id(self, job: Job) -> str | None:
        path = self.store.dir(job.id) / "batch_job_id"
        return path.read_text("utf-8").strip() if path.is_file() else None

    def _tick(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick_once()
            except Exception:                 # noqa: BLE001
                log.exception("Ticker pass failed")

    def tick_once(self) -> None:
        """One pass: submit anything due, advance anything waiting. Public so
        tests can drive it without waiting on a timer.

        One pass at a time, and a second caller is turned away rather than
        queued: the ticker thread and `POST /api/tick` both come through here,
        and two passes over the same ready batch would each try to hand it off.
        Two handoffs can't both fire — the CAS waiting→collecting in
        `_advance_batch` lets exactly one through — but turning the second
        caller away keeps the pass cheap. The heavy collect no longer runs
        here: it is queued to the worker, so this mutex is now held only for
        cheap polls and the archive sweep, and stays free during a collect."""
        if not self._tick_mutex.acquire(blocking=False):
            return
        try:
            for job in self.store.all():
                if job.state == "scheduled" and self._due(job):
                    # Guards against a cancellation landing between this read
                    # and the write below: only queue it if it was still
                    # scheduled at the moment of the update, not just at the
                    # moment of this read.
                    if self.store.update_if(job.id, expect="scheduled",
                                            state="queued"):
                        self.queue.put(job.id)
                # Only `waiting` here now. `collecting` is no longer ticker-
                # owned: once a batch is ready, `_advance_batch` hands it to the
                # worker and the worker writes it. A collect interrupted by a
                # restart is flipped back to `waiting` by `resume_interrupted`,
                # so it re-enters through this same door. Prep is never at a
                # vendor — the worker owns it start to finish — so the ticker
                # leaves it alone rather than looking for a batch that isn't
                # there.
                elif job.state == "waiting" and not job.is_prep:
                    self._advance_batch(job)
            # After the batch work, the Drive archive's retry-and-backfill pass.
            # Held inside the same mutex as the loop above, so an inline archive
            # attempt from the worker thread and this sweep can never race to
            # upload one job twice. Bounded per pass; a large backfill drains
            # over several ticks.
            self._archive_sweep()
        finally:
            self._tick_mutex.release()

    def _due(self, job: Job) -> bool:
        target = self._scheduled_for(job)
        return target is None or datetime.now().astimezone() >= target

    def _scheduled_for(self, job: Job) -> datetime | None:
        """When a scheduled job should actually go, or None for "right now".

        The time is the first HH:MM on or after the moment the job was made,
        which is the difference between "tonight at 2 AM" and "2 AM already
        happened today, go immediately"."""
        if not job.schedule_at:
            return None
        try:
            hh, mm = (int(p) for p in job.schedule_at.split(":", 1))
            created = datetime.fromisoformat(job.created_at).astimezone()
            target = created.replace(hour=hh, minute=mm, second=0,
                                     microsecond=0)
        except (AttributeError, TypeError, ValueError):
            return None                       # unparseable: don't strand it
        return target if target >= created else target + timedelta(days=1)

    def _advance_batch(self, job: Job) -> None:
        batch_id = self._batch_job_id(job)
        if batch_id is None:
            self.store.update(job.id, state="failed",
                              error="Lost track of this review; start it again.")
            return
        cfg = self.config_for(job)
        try:
            batch_job = batchlib.load(self.store.paths.jobs, batch_id)
            provider = self._provider(cfg)
            status = batchlib.poll(batch_job, provider, self.store.paths.jobs)
        except (batchlib.BatchError, ProviderError) as e:
            self.store.update(job.id, state="failed", error=str(e))
            return

        self.store.update(job.id, done=status.succeeded + status.errored,
                          total=status.total or job.total)
        if batch_job.state == "failed":
            self.store.update(job.id, state="failed", error=batch_job.error)
            return
        if batch_job.state != "ready":
            return

        # Give up rather than retry forever when collecting keeps killing the
        # process before it can record a failure (an OOM under the LanguageTool
        # JVM, a machine restart mid-write). The count is written with the
        # collecting transition below — before the attempt — so a crash still
        # spends one; once they run out, the job becomes a visible, recoverable
        # failure instead of "almost done" with no end.
        attempt = job.collect_attempts + 1
        if attempt > MAX_COLLECT_ATTEMPTS:
            self.store.update(
                job.id, state="failed",
                error="Writing the reviewed document didn't finish after "
                      "several tries — the results are still here to collect. "
                      "Use “Finish collecting” to try again.")
            return

        # CAS for the same reason the scheduled→queued promotion has one: the
        # job was read at the top of the pass, and its state may have moved on.
        # It is also the enqueue guard — only the caller that flips
        # waiting→collecting may put the id on the queue, so one ready batch can
        # never be queued twice across ticks. `expect="waiting"` because that is
        # the only state the ticker calls this in now (recover() sends a failed
        # job back to "waiting" first, so it too comes through here). The heavy
        # collect then runs on the worker thread, where it serialises with
        # submit instead of racing it — they share the LanguageTool JVM cache,
        # and the overlap was the deadlock.
        if self.store.update_if(job.id, expect="waiting", state="collecting",
                                collect_attempts=attempt) is None:
            return
        self.queue.put(job.id)

    def _collect_batch(self, job_id: str) -> None:
        """Worker-side second half of a batch review: re-ingest, fold the
        vendor's findings, run the whole-book post-passes, and write the
        document. The ticker hands a ready batch here (state "collecting") so
        this heavy prepare can never overlap `_submit_batch`'s equally heavy
        one — a single worker thread runs both, one at a time.

        Own error handling, mirroring the old `_advance_batch` tail, so the
        watcher's hand-driven `_drain` (which calls `run_one` on its own thread)
        behaves exactly as the live worker does."""
        job = self.store.get(job_id)
        if job is None or job.state != "collecting":
            # Deleted, or failed/cancelled while its id sat in the queue.
            return
        batch_id = self._batch_job_id(job)
        if batch_id is None:
            self.store.update(job_id, state="failed",
                              error="Lost track of this review; start it again.")
            return
        cfg = self.config_for(job)
        try:
            batch_job = batchlib.load(self.store.paths.jobs, batch_id)
            provider = self._provider(cfg)
        except (batchlib.BatchError, ProviderError) as e:
            self.store.update(job_id, state="failed", error=str(e))
            return
        if batch_job.state != "ready":
            # Shouldn't happen — the ticker only hands off a batch it polled
            # ready — but a stale manifest goes back to the ticker to re-poll
            # rather than raising inside collect. The spent attempt stays spent.
            self.store.update_if(job_id, expect="collecting", state="waiting")
            return
        out = self._claim_results_dir(job)

        def on_phase(name: str) -> None:
            # Collect re-walks the pipeline's steps (re-ingest, fold, the
            # whole-book post-passes, finish), and this is how the card and its
            # step tracker follow along rather than sitting on "almost done"
            # for the entire stretch. Same callback _run_now hands run_sync.
            self.store.update(job_id, stage=name)

        try:
            with self._stall_alarm(job_id, "Collecting"):
                outputs = batchlib.collect(batch_job, provider, self.error_dir,
                                           self.store.paths.jobs, out_dir=out,
                                           on_phase=on_phase)
        except Exception as e:                # noqa: BLE001
            # Anything uncaught here would otherwise leave the job in
            # `collecting`, which nothing would pick up — so a permanent
            # failure has to become a state the user can see and recover.
            log.exception("Collecting %s failed", job_id)
            self._release_results_dir(job_id)
            self.store.update(job_id, state="failed", error=str(e))
            return
        self.store.update(job_id, state="done", **_tallies(outputs),
                          results_dir=str(out), error=None, stage="")
        self._record_usage(job_id, out, cfg.api.model, batch=True)
        self._notify_done(job_id)

    @contextmanager
    def _stall_alarm(self, job_id: str, what: str,
                     timeout: float = ENGINE_STALL_SECONDS):
        """Around a heavy engine call: if it runs past `timeout`, log loudly and
        dump every thread's stack to stderr (which is what reaches `fly logs`),
        repeating each interval until the call returns. Diagnosis, not
        intervention — Python threads can't be killed, and abandoning one leaves
        it racing its replacement, so the job still ends only through its own
        success / failure / restart paths. The alarm just names the hang."""
        finished = threading.Event()

        def alarm() -> None:
            while not finished.wait(timeout):
                log.error("%s for job %s has been running over %d minutes; "
                          "dumping all thread stacks.", what, job_id,
                          int(timeout // 60))
                try:
                    faulthandler.dump_traceback(all_threads=True)
                except Exception:             # noqa: BLE001 - never break the run
                    pass

        watcher = threading.Thread(target=alarm, daemon=True,
                                   name=f"docproof-stall-{job_id}")
        watcher.start()
        try:
            yield
        finally:
            finished.set()
