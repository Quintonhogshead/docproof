"""Run Galley phases with plan approval, subprocess limits, state checks, and
artifact handoff.

Each phase runs in a separate session. Mechanical mode excludes copyediting
phases; unattended sessions recover within caps without waiting for replies.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.watch.naming import CLEAN_SUFFIX, PROOF_STAGE as HANDOFF_STAGE
from docproof import agent_lane
from docproof.subscription_limits import UsageLimitError, is_usage_limited
from galley.journal import JOURNAL_NAME as DECISION_LOG_NAME
from galley.phases import ALL_PHASES, COPYEDIT_PHASES, MECHANICAL_PHASES

# Minimum state required after each successful phase. Sweeps and verify have
# no separate run state.
REQUIRED_STATE: dict[str, str] = {
    "profile": "intake",
    "approve": "plan_approved",
    "ladder": "mechanical_complete",
    "audit": "audited",
    "settle": "settled",
    "astra_review": "astra_reviewed",
    "certify": "certified",
    "deliver": "delivered",
}

# API spending ceiling recorded in approval.json.
DEFAULT_BUDGET_USD = 10.0
DEFAULT_ASTRA_BUDGET_USD = 25.0
DEFAULT_ASTRA_MAX_OUTPUT_TOKENS = 32768
DEFAULT_ASTRA_CHUNK_BYTES = 180000
DEFAULT_MODEL = "claude-fable-5-1"
#: The cheaper, faster brain for the phases that follow a script.
MECHANICAL_MODEL = "claude-opus-5"
# Which brain drives each phase. Judgment phases (the plan gate, the ladder's
# reading of its own results, audit, settle's adjudication, the copy-edit
# flights) stay on Fable; the phases that run a fixed set of commands and read
# their output go to Opus 5 (owner, 2026-09-06). A phase absent here runs on
# DEFAULT_MODEL.
PHASE_MODEL: dict[str, str] = {
    "profile": MECHANICAL_MODEL,
    "sweeps": MECHANICAL_MODEL,
    "verify": MECHANICAL_MODEL,
    "certify": MECHANICAL_MODEL,
    "deliver": MECHANICAL_MODEL,
}
#: Fable phases run at high effort (owner, 2026-09-06); a phase absent here
#: leaves the session at Claude Code's default effort.
DEFAULT_EFFORT = "high"
PHASE_EFFORT: dict[str, str] = {
    phase: DEFAULT_EFFORT for phase in
    ("approve", "ladder", "flights", "audit", "reread", "settle")
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_PERMISSION_MODE = "acceptEdits"
#: Set in every phase session's environment to the phase name, so verbs
#: that record a decision can attribute it to the brain that made it.
BRAIN_PHASE_ENV = "GALLEY_BRAIN_PHASE"
DEFAULT_WORKSPACE_ROOT = "~/galley-workspaces"
DEFAULT_WRAPBIN = "~/galley-bin"
#: Where the driver leaves its own log and ledger inside the workspace.
DRIVER_DIR = "driver"

# Bound each session by both turns and elapsed time.
DEFAULT_MAX_TURNS = 100
PHASE_MAX_TURNS: dict[str, int] = {
    # Raised from 80 on 2026-09-07. A from-scratch profile on a 65k-word book
    # spent all 80 and never reached PLAN.md: the number audit, paragraph map
    # and text extraction, then the genre pack, egress report and dry-run — and
    # then a second pricing pass, because the naive dry-run price came back
    # over the budget cap and a plan priced above it cannot pass the gate.
    # Every profile that fit in 80 was resuming a workspace that already had
    # its scans. None of this work spends; the cap is turns, not dollars.
    "profile": 160,     # scans + PLAN.md, no spend
    "approve": 60,      # genre-pack, routes, approve — three commands
    "sweeps": 120,      # a dry-run and an apply per bespoke sweep
    "ladder": 100,      # one long review command, plus log reads
    "flights": 150,
    "audit": 100,
    "reread": 150,
    "verify": 250,      # re-reads every applied edit; may re-run per paragraph
    "settle": 400,      # the until-clean sweep, round after round
    "certify": 60,
    "deliver": 80,
}
# Per-phase timeouts in seconds.
DEFAULT_PHASE_TIMEOUT_S = 2 * 3600.0
PHASE_TIMEOUT_S: dict[str, float] = {
    "ladder": 3 * 3600.0,
    "verify": 4 * 3600.0,
    "settle": 4 * 3600.0,
}
# The phases whose work grows with the book — ladder reads per chunk, verify
# re-reads per applied edit, settle runs rounds per residual — carry caps sized
# for a novel of LENGTH_BASELINE_WORDS. A longer book gets them scaled up in
# proportion, never down (the table is the floor), and never past
# LENGTH_SCALE_MAX: a 235k-word epic gets 4x, not 4.7x, because the wall clock
# still has to catch a session that is looping rather than working. Every
# other phase is fixed overhead: profile took 17 minutes on a 65k-word novel
# and 17 minutes on a 3.6k-word story (2026-09-07). The word count comes from
# the workspace's profile.json, so nothing scales until profile has run — and
# profile itself never scales.
LENGTH_SCALED_PHASES = ("ladder", "verify", "settle")
LENGTH_BASELINE_WORDS = 50_000
LENGTH_SCALE_MAX = 4.0
# Settle's work tracks the residual count, not the word count: a dense short
# book (the seeded 3.6k-word test spent 108 turns) gets no relief from length
# scaling. The verify outputs the final run holds — change_verify.json's
# problems plus finished_walk.json's residuals — are known before settle
# starts, so its caps stretch with them too, under the same ceiling.
ITEM_SCALED_PHASES = ("settle",)
SETTLE_ITEMS_BASELINE = 40

# A session that hits its turn cap or wall clock while measurably advancing
# the book is unfinished work, not a failure. Until 2026-09-12 a capped
# session was charged its whole cap, the durable execution budget then had
# nothing left, so the "recovery" attempt never ran for exactly the two
# limits it names — and every later agent poll re-blocked the book on
# "execution budget exhausted" until a new deploy. Now the driver grants a
# bounded continuation (a share of the phase's base cap, at most
# RECOVERY_MAX_GRANTS times, durably recorded) when the session left new
# evidence behind, and resumes the same Claude Code conversation when its
# transcript is still on disk. A session that changed nothing earns nothing.
RECOVERY_GRANT_SHARE = 0.5
RECOVERY_MAX_GRANTS = 2
# A recovery launched with a handful of turns cannot finish anything; it
# only re-reads the references and hits the cap again. Below the floor
# (absolute, or a quarter of the phase's cap when that is smaller) the driver
# stops with evidence instead.
RECOVERY_FLOOR_TURNS = 40
RECOVERY_FLOOR_S = 15 * 60.0
#: Phases whose sessions run under the edit-guard hook (galley/edit_guard.py).
EDIT_GUARDED_PHASES = ("verify", "settle")


def length_factor(words: int | float | None) -> float:
    """How much to stretch a length-scaled phase's caps for a book this long:
    1.0 at or under the baseline, proportional above it, capped."""
    try:
        w = float(words or 0)
    except (TypeError, ValueError):
        return 1.0
    if w <= LENGTH_BASELINE_WORDS:
        return 1.0
    return min(LENGTH_SCALE_MAX, w / LENGTH_BASELINE_WORDS)


def items_factor(items: int | float | None) -> float:
    """How much to stretch an item-scaled phase's caps for this many open
    items: 1.0 at or under the baseline, proportional above it, capped."""
    try:
        n = float(items or 0)
    except (TypeError, ValueError):
        return 1.0
    if n <= SETTLE_ITEMS_BASELINE:
        return 1.0
    return min(LENGTH_SCALE_MAX, n / SETTLE_ITEMS_BASELINE)


def edit_guard_settings() -> dict[str, Any]:
    """The `claude --settings` payload that installs the edit-guard hook."""
    import shlex
    import sys
    command = f"{shlex.quote(sys.executable)} -m galley.edit_guard"
    return {"hooks": {"PreToolUse": [{
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": command, "timeout": 10}]}]}}


def session_transcript(workspace: Path, session_id: str,
                       home: str | None = None) -> Path | None:
    """Where Claude Code keeps the conversation a phase session ran in, if
    it is still on disk — the precondition for `claude --resume`."""
    if not session_id:
        return None
    base = home or os.environ.get("HOME") or str(Path.home())
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(Path(workspace).resolve()))
    path = Path(base) / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
    return path if path.is_file() else None


# Fallback turn-cap detection for sessions without a structured result.
_TURN_CAP_RE = re.compile(r"max(?:imum)?[ _-]?turns?\b|turn limit",
                          re.IGNORECASE)
#: The conventional exit code for "killed by a timeout".
TIMEOUT_RC = 124

# A session that never got past sign-in. Claude Code prints one of these and
# exits non-zero on turn one when the subscription token has expired or been
# revoked — a machine problem, not a book problem, so the driver must not
# write a needs_human verdict for it.
_CREDENTIALS_RE = re.compile(
    r"Failed to authenticate|OAuth access token is invalid"
    r"|OAuth token (?:has )?(?:expired|been revoked)|authentication_error"
    r"|API Error: 401\b|Invalid API key|Not logged in|Please run /login",
    re.IGNORECASE)

# Quiet means <= 4 new items; disable the percentage threshold. Escalate if
# three rounds remain noisy.
SETTLE_ROUNDS = 3
SETTLE_QUIET_FLOOR = 4
SETTLE_QUIET_SHARE = 0.0
#: How many trailing log lines a failure reason carries.
TAIL_LINES = 20

# Keep session authentication on the subscription; the wrapper supplies
# detector API keys.
STRIPPED_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


log = logging.getLogger("galley.driver")

class DriverError(RuntimeError):
    """Invalid driver configuration."""


def astra_review_settings(run_dir, *, transport=None, max_chunk_bytes=None,
                          persist=False) -> dict[str, Any]:
    """Resolve the saved transport before dispatch; never change an owned job.

    An older receipt without a transport field is an API request. An omitted
    option resumes that request; an explicitly conflicting option is an error.
    """
    from docproof.utils.files import write_atomic
    from galley.outcome import ASTRA_REQUIRED_NAME
    run = Path(run_dir)

    def read(path):
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise DriverError(f"Unreadable Astra routing evidence: {path.name}") from e
        if not isinstance(value, dict):
            raise DriverError(f"Invalid Astra routing evidence: {path.name}")
        return value

    marker = read(run / ASTRA_REQUIRED_NAME)
    inherited = read(run.parent.parent / ASTRA_REQUIRED_NAME) if run.parent.name == "runs" else {}
    receipt_path = run / "astra-review.json"
    receipt = read(receipt_path)
    owned = receipt_path.exists()
    saved = (receipt.get("transport") or "api") if owned else marker.get("transport") or inherited.get("transport")
    if transport not in {None, "api", "codex"} or saved not in {None, "api", "codex"}:
        raise DriverError("Astra transport must be codex or api.")
    if owned and (transport is not None and transport != saved):
        raise DriverError("A submitted Astra review already owns another transport; resume its saved transport.")
    if owned and marker.get("transport") not in {None, saved}:
        raise DriverError("Astra receipt and enrolled transport disagree; operator recovery is required.")
    selected = transport or saved or "codex"
    stored_chunk_bytes = receipt.get("max_chunk_bytes", marker.get("max_chunk_bytes",
        inherited.get("max_chunk_bytes", DEFAULT_ASTRA_CHUNK_BYTES)))
    if owned and selected == "codex" and marker.get("max_chunk_bytes") not in {None, stored_chunk_bytes}:
        raise DriverError("Astra receipt and enrolled chunk size disagree; operator recovery is required.")
    chunk_bytes = stored_chunk_bytes if max_chunk_bytes is None else max_chunk_bytes
    if type(chunk_bytes) is not int or chunk_bytes <= 0:
        raise DriverError("Astra chunk bytes must be a positive integer.")
    if owned and selected == "codex" and max_chunk_bytes is not None and chunk_bytes != stored_chunk_bytes:
        raise DriverError("A submitted Astra review has a frozen chunk size; resume without changing it.")
    settings = {**marker, "schema_version": 1, "model": "gpt-6-astra",
                "reasoning_effort": "high", "transport": selected,
                "max_chunk_bytes": chunk_bytes}
    if persist:
        write_atomic(run / ASTRA_REQUIRED_NAME, json.dumps(settings, indent=2))
    return settings


class CredentialsError(DriverError):
    """The brain's session could not sign in: the subscription token behind
    CLAUDE_CODE_OAUTH_TOKEN is expired, revoked or missing. The manuscript is
    untouched and the run can resume from the same phase once the token is
    replaced, so no verdict is written for the book.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



# Each phase reads inputs from workspace files; {book} is the source
# basename.
_PROMPTS: dict[str, str] = {
    "profile": (
        "Intake: manuscript at source/{book}. Follow the common Context "
        "discipline. Open the state machine (`docproof galley state . "
        "--advance intake --source source/{book} --config CONFIG` — always "
        "BOTH flags). Run /profile then /draft-plan (both $0). The profile "
        "includes the NUMBER AUDIT extraction: every numeral and spelled "
        "number with context, to runs/numbers.txt, reviewed for house style, "
        "consistency, and arithmetic (contradictions become author queries). "
        "Write profile.json and PLAN.md. STOP at the plan gate — do not "
        "spend past it."),
    "approve": (
        "Phase: freeze the approved plan. Read PLAN.md (the plan has been "
        "approved — the approval line is at the bottom of the file). "
        "Materialize the run config it names (`docproof galley genre-pack … "
        "--out runs/mech.yaml` if not already written), print the egress "
        "report (`docproof galley routes source/{book} --config runs/mech.yaml "
        "> runs/routes.txt`; read it back), then write the immutable manifest: "
        "`docproof galley approve source/{book} --config runs/mech.yaml "
        "--budget {budget} --comment-budget <profile.json comment_budget> "
        "--out approval.json`. The comment budget is a CEILING certify "
        "enforces on the delivered document (about 1 per 1,000 words). The "
        "cap is ${budget} of API "
        "spend for the whole book — that exact figure, not the plan's total: "
        "it is the ceiling every paid verb refuses past, and PLAN.md's total "
        "must already fit under it. Advance the state machine (--source and "
        "--config). Every later paid command runs with --approval "
        "approval.json. Report the allowed models/providers and the cap; do "
        "NOT spend."),
    "sweeps": (
        "Phase: bespoke sweeps. Read PLAN.md for the approved sweep list and "
        "references/sweeps.md for the sweep contract. Author all sweep files in one batch, "
        "dry-run each (`docproof sweep IN --rule F > runs/sweep_<key>.txt`; "
        "read only the match summary), and apply only the sweeps PLAN.md "
        "approved. If a sweep exceeds the plan's match count, narrow it to "
        "the approved cases and route remaining candidates through the "
        "existing mechanical judgment/verification lanes; record the decision "
        "and preserve required coverage without asking for permission. Do NOT read "
        "sweeps.py or the manuscript whole. Treat each proposed repair as independent: "
        "after two unsuccessful supported attempts on the same repair, leave its "
        "wording unchanged, record the exact anchor, attempted operation, failure "
        "evidence and remaining work in DECISION_LOG.md, and continue other repairs. "
        "Do not spend this phase discovering unsupported paragraph-join or sweep "
        "formats. Record affected plan lines as deferred with evidence, never ran. "
        "Pass unresolved candidates to the remaining mechanical readers; do not "
        "claim they were fixed or exempt them from review. Technical limitations "
        "are production flags, not author questions. Reserve time to write the "
        "handoff even when individual repairs remain unresolved."),
    "ladder": (
        "Phase: mechanical ladder. The six-window chapter sweep is wave 1 "
        "line 1 — `chapter_sweep` on Luna in the run config (the "
        "mechanical-wave stage enables it) PLUS the six-window Sonnet $0 "
        "session-subagent sweep, imported before the ladder's own findings. "
        "Reuse the exact approved config unchanged from PLAN.md and "
        "approval.json; do not rewrite or regenerate it after approval. Run "
        "`docproof review … --approval approval.json` to runs/ladder/ with "
        "output redirected to runs/ladder.log. Run it in the FOREGROUND and "
        "WAIT for it to exit — never background it, never end your turn "
        "while it runs. Redirecting output is not backgrounding: the redirect keeps the log out of your context, and you still block on the "
        "command. A session that ends with the read still in flight kills "
        "it, wastes every paid call it had not checkpointed, and fails the "
        "phase. Then read only the summary + counts "
        "+ the dollar line. Confirm findings.checkpoint.json exists before "
        "finish(). Advance the state machine (--source and --config). Then "
        "keep the PLAN LEDGER: for EVERY numbered line of PLAN.md run "
        "`docproof galley plan-line LABEL --status ran --evidence PATH` (or "
        "`--status skipped --reason WHY`, or `--status deferred --evidence "
        "WHERE`). A $0 subagent lane the plan lists is a line like any other: "
        "run it and record it, or record why not — certify FAILS on a line it "
        "cannot account for, and the letter tells the author what was "
        "promised and not done. Report applied/query counts and spend."),
    "flights": (
        "Phase: copy-edit flights on the PROOFREAD text (never raw). Follow "
        "/flight-deck; every `galley flights` call carries --approval "
        "approval.json and --budget from PLAN.md. Send flights + judge output "
        "to runs/flights/ and read a summary slice only. Emit copyedit "
        "edit-channel findings for the merge desk."),
    "audit": (
        "Phase: audit the latest wave. Follow /audit (`docproof galley audit "
        "RESULTS --source source/{book} --config C --approval approval.json`). "
        "Build the density table to runs/audit.txt, read the quiet chapters' "
        "real pages (sampled, not whole), write hypotheses to runs/audit.json. "
        "Advance the state machine (--source and --config). Report the "
        "hypotheses and what they would cost; do NOT start a paid re-read."),
    "reread": (
        "Phase: targeted wave-2 re-read. GATED: read runs/audit.txt and "
        "runs/audit.json — proceed ONLY if PLAN.md (or QUESTIONS.md's reply) "
        "records approval of the wave-2 line and its cost; otherwise STOP and "
        "say so. Turn each approved hypothesis into a single-pass re-read "
        "scoped to chapter x error classes (`docproof review … --only <chunks> "
        "--error-types <classes> --approval approval.json`, or a $0 Opus "
        "subagent read imported via import-findings), priced against the "
        "marginal-cost ceiling in PLAN.md. Send output to runs/wave2/; read "
        "only summaries + the dollar line. Stop when the marginal cost per "
        "finding crosses the ceiling. Advance the state machine (--source and "
        "--config). Report findings added and spend."),
    "verify": (
        "Phase: verify the finished text for SENSE on the $0 subscription "
        "lane. Run `docproof galley verify runs/<final> --config <run config> "
        "--engine subagent > runs/verify.log 2>&1` (add --context <notes file> "
        "if the workspace has voice notes; --dry-run first if you want the "
        "read count). It re-reads every applied edit and proofreads the "
        "accepted text as the author will read it. ROTATE the readers: a "
        "subagent that wrote or imported edits for a window never verifies "
        "that window (assign windows offset from the ladder/fleet split), and "
        "each walk window is read TWICE — mechanics first, then a slow "
        "type-and-compare pass for omissions, duplicated passages, and sense. "
        "Read only the summary line "
        "and the WARNING/ERROR lines of runs/<final>/run.log; if "
        "finished_walk.json lists unread_paragraphs, re-run with --paragraphs "
        "on them (@FILE) to a side dir and say so. Do NOT hand-fix anything it "
        "raises — that is the settle phase. Advance the state machine "
        "(--source and --config). Report residual/problem counts."),
    "settle": (
        "Phase: residual settlement — zero open candidates. Follow /settle. "
        "All separate verification outputs are registered with their source run "
        "and enter settlement. If a proven author-knowledge query remains "
        "no_suggestion, use --queries with the exact current build and residual "
        "evidence as /settle documents; do not ask for approval to use this "
        "existing mechanical-scope query channel. "
        "Run `docproof galley settle runs/<final> --source source/{book} "
        "--config <run config> --engine subagent --approval approval.json "
        "{settle_flags} > "
        "runs/settle.log 2>&1` (the config is the $0 replay config the final "
        "build used; run from the workspace root so a relative "
        "intent_zones_file resolves). Those flags are REQUIRED and exact: "
        "sweep until a round comes back quiet, at most {settle_rounds} "
        "round(s); a round raising fewer than {settle_noisy} new item(s) is "
        "quiet under the absolute rule; a nonzero --quiet-share adds the "
        "specified percentage rule. If the last round is still noisy, "
        "preserve that evidence and do not sweep again. In an enrolled "
        "astra-review-required.json workspace leave the editorial verdict "
        "to Astra; otherwise the legacy driver records nonconvergence. "
        "Internal repairs stay open and block certification, never becoming "
        "author questions because a round limit was reached. A "
        "residual closes as an EDIT or a DROP whenever the book itself "
        "answers it (a verbatim repeat, a dictionary compound, a pronoun the "
        "sentence disambiguates, a comma splice); it closes as a QUERY only "
        "for author knowledge — a fact, an intent, an identity. The comment "
        "budget in approval.json is a ceiling certify enforces: read "
        "settlement.json's query count against it, and collapse same-rule "
        "families to one comment before certify rather than after. Read the "
        "summary lines and settlement.json's counts; open must be []. Then "
        "`docproof galley state . --advance settled --results runs/<final> "
        "--source source/{book} --config <run config>` (it refuses, exit 7, "
        "while anything is open). Never hand-patch an owning row's "
        "replacement. Report the counts, the outcome in outcome.json, and "
        "spend ($0 on the subagent lane)."),
    "certify": (
        "Phase: the delivery gate. Run `docproof galley certify runs/<final> "
        "--approval approval.json --source source/{book} --config <run "
        "config>` to runs/certify.txt and read it back. Every check must PASS "
        "(hashes, approved routes, checkpoint, zero-cost anomaly, budget, "
        "artifact scan, change-verify, finished-walk, comment budget). A FAIL "
        "blocks delivery: fix the failing check, never ship around it — an "
        "over-budget comment count means collapsing families and deciding "
        "what the book answers, not raising the number. Advance the state "
        "machine to certified (--source and --config). Report the "
        "certificate."),
    "deliver": (
        "Phase: deliver the CERTIFIED build. PRECONDITION: runs/certify.txt "
        "shows PASSED and runs/<final>/settlement.json has open: [] — read "
        "both; if either is missing or failing, STOP and say which. Do NOT "
        "rebuild anything after certify: copy the certified tracked-changes "
        "docx from runs/<final> to deliverable/, then render the letter, the "
        "style sheet, and the verification report with `docproof galley "
        "letter runs/<final> --workspace . --source source/{book} --out "
        "deliverable/` (letter.md, style-sheet.md, verification.md, and "
        "author-letter.docx — the AUTHOR-facing letter that ships beside the "
        "manuscript; the author sees nothing else — "
        "--workspace is what makes the letter report the REAL spend across "
        "every run, not the $0 replay build's; the verification report "
        "carries the delivered file's SHA-256, the certificate table with its "
        "skipped checks named, the untracked preparation counts, and the "
        "honest residual statement). Read all three back: a style sheet that "
        "says no rulings were recorded, or a letter that says $0 when the "
        "ladder billed, is a defect to fix before hand-off. Copy outcome.json "
        "beside them (its outcome — done or needs_human — and reason go in "
        "the letter's closing paragraph). Advance the state machine to "
        "delivered (--source and --config). Report final spend, "
        "change/comment counts, and the outcome. If certify's plan-ledger or "
        "comment-premises check failed, deliver NOTHING: account for the "
        "plan line, or drop the stale query and rebuild, then certify again."),
}

# Small, explicit readings for each session. The common manual is automatic;
# operational detail and historical evidence are not loaded for unrelated phases.
_PHASE_REFERENCES: dict[str, tuple[str, ...]] = {
    "profile": ("intake.md", "config.md"),
    "approve": ("config.md",),
    "sweeps": ("house-rules.md", "sweeps.md", "findings.md"),
    "ladder": ("house-rules.md", "lanes.md", "findings.md", "judgment.md"),
    "flights": ("legacy-copyedit.md", "house-rules.md", "findings.md"),
    "audit": ("house-rules.md",),
    "reread": ("legacy-copyedit.md", "house-rules.md"),
    "verify": ("house-rules.md", "verification.md"),
    "settle": ("house-rules.md", "comment-reconciliation.md"),
    "certify": ("delivery.md",),
    "deliver": ("delivery.md",),
}

# Scope restrictions appended to the relevant phase prompts.
_MECHANICAL_NOTE: dict[str, str] = {
    "profile": (
        " MECHANICAL PROOFREADING ONLY (go-live scope): the plan you draft "
        "must contain NO copy-edit flights, NO merge desk, and NO wave-2 "
        "re-read line — mechanical lanes and $0 lanes only, under "
        "`--stage mechanical-wave`. A line the scope forbids is not a "
        "recommendation to make; leave it out."),
    "approve": (
        " MECHANICAL PROOFREADING ONLY: compose the config with `--stage "
        "mechanical-wave` and pass `--mechanical-only` to `docproof galley "
        "approve`, so approval.json records the copy-edit lanes as shut and "
        "certify fails if one appears."),
    "audit": (
        " MECHANICAL PROOFREADING ONLY: a wave-2 re-read is out of scope for "
        "this run — report the hypotheses for the record and stop there."),
    "certify": (
        " MECHANICAL PROOFREADING ONLY: the mechanical-only check must PASS "
        "too; a copy-edit finding in this run is a scope failure, not "
        "something to certify around."),
    "deliver": (
        " MECHANICAL PROOFREADING ONLY: one tracked-change author (the "
        "proofreader); no copy-edit lane ships."),
}


def settle_flags(rounds: int = SETTLE_ROUNDS,
                 quiet_floor: int = SETTLE_QUIET_FLOOR,
                 quiet_share: float = SETTLE_QUIET_SHARE) -> str:
    """The exact `galley settle` flags the settle phase must use."""
    return (f"--until-clean --rounds {int(rounds)} "
            f"--quiet-floor {int(quiet_floor)} --quiet-share {quiet_share:g}")


def phase_prompt(phase: str, book: str, *, mechanical_only: bool = True,
                 budget_usd: float = DEFAULT_BUDGET_USD,
                 settle_rounds: int = SETTLE_ROUNDS,
                 settle_quiet_floor: int = SETTLE_QUIET_FLOOR,
                 settle_quiet_share: float = SETTLE_QUIET_SHARE) -> str:
    """Build a phase prompt using the source basename and API budget. Reject
    phases excluded by mechanical mode.
    """
    if phase == "astra_review":
        return ("Final editorial review: gpt-6-astra at "
                "high reasoning over the complete edited manuscript, revisions, "
                "comments, and verification issues. This phase runs directly; "
                "it does not start a Claude session.")
    if phase not in _PROMPTS:
        raise DriverError(
            f"unknown phase {phase!r} — expected one of {', '.join(ALL_PHASES)}")
    if mechanical_only and phase in COPYEDIT_PHASES:
        raise DriverError(
            f"phase {phase!r} is copy-edit scope; this run is mechanical "
            f"proofreading only (drop --mechanical-only to run it)")
    prompt = _PROMPTS[phase].format(
        book=book, budget=f"{budget_usd:.2f}",
        settle_flags=settle_flags(settle_rounds, settle_quiet_floor,
                                  settle_quiet_share),
        settle_rounds=settle_rounds, settle_noisy=settle_quiet_floor + 1)
    if mechanical_only:
        prompt += _MECHANICAL_NOTE.get(phase, "")
    readings = ", ".join(f"references/{name}"
                         for name in _PHASE_REFERENCES[phase])
    return (f"Read only these phase references once (workspace-relative): "
            f"{readings}. Skills may name an additional needed contract; "
            f"do not load the full reference directory or historical manuals. "
            + prompt + _UNATTENDED_NOTE)


# Appended to every phase prompt. The common manual says it too, but a
# brain that has just read a 65k-word intake reaches for `galley ask` the way
# a person would reach for a colleague, and unattended there is no colleague.
_UNATTENDED_NOTE = (
    " UNATTENDED RUN: no human checks your work or answers questions until "
    "the final handoff. Make evidence-supported decisions within the approved "
    "scope and record the reasons. Preserve uncertain author wording; missing "
    "author knowledge becomes a justified margin query in the final deliverable "
    "and never pauses the book. `galley ask` only records a local note here; "
    "do not wait for a reply or use QUESTIONS.md as a stop signal. Recover "
    "routine tool/anchor/artifact failures through supported commands, resume "
    "checkpoints, and verify the result. Never bypass a failing gate, change "
    "a frozen approval/config, exceed a cap, or drop required coverage. If a "
    "required operation still cannot succeed, preserve evidence and report "
    "the concrete operational failure, not a request for instructions. "
    "In an Astra-enrolled workspace a technical blocker preserves the "
    "editorial verdict; it never becomes an author question or a manual "
    "needs_human overrule.")


def phases_for(mechanical_only: bool = True) -> tuple[str, ...]:
    return MECHANICAL_PHASES if mechanical_only else ALL_PHASES


def select_phases(*, mechanical_only: bool = True, start: str | None = None,
                  only: Sequence[str] | None = None,
                  astra_review: bool = True) -> list[str]:
    """The phases this invocation will run, in order.

    ``only`` names them explicitly (still ordered, still scope-checked);
    ``start`` slices the default order from that phase onward."""
    order = phases_for(mechanical_only)
    if not astra_review:
        order = tuple(p for p in order if p != "astra_review")
    if only:
        unknown = [p for p in only if p not in ALL_PHASES]
        if unknown:
            raise DriverError(
                f"unknown phase(s) {', '.join(unknown)} — expected from "
                f"{', '.join(ALL_PHASES)}")
        forbidden = [p for p in only if mechanical_only and p in COPYEDIT_PHASES]
        if forbidden:
            raise DriverError(
                f"phase(s) {', '.join(forbidden)} are copy-edit scope; this "
                f"run is mechanical proofreading only")
        return [p for p in order if p in set(only)]
    if start:
        if start not in order:
            raise DriverError(
                f"cannot start at {start!r} — expected one of "
                f"{', '.join(order)}")
        return list(order[order.index(start):])
    return list(order)



_SETTINGS_SEED = {
    "permissions": {
        "allow": [
            "Bash(docproof:*)",
            "Bash(grep:*)",
            "Bash(head:*)",
            "Bash(tail:*)",
            "Bash(wc:*)",
            "Bash(ls:*)",
            "Bash(cat runs/:*)",
        ]
    }
}


def practitioner_dir() -> Path:
    """Where CLAUDE.md / KNOBS.md / skills/ live in this checkout."""
    here = Path(__file__).resolve().parent / "practitioner"
    if not (here / "CLAUDE.md").is_file():
        raise DriverError(
            f"no practitioner manual at {here} — run the driver from a "
            f"DocProof checkout (the manual and skills are not packaged into "
            f"the wheel)")
    return here


class SourceChanged(DriverError):
    """The workspace was seeded with a different manuscript than the one
    offered now, and the caller did not ask for a revision transition."""


def _record_source(ws: Path, src: Path, source_id: str, sha: str,
                   revision: int) -> None:
    """Write the source identity into state.json — creating an empty state
    machine when the workspace has none yet — so every later stage sees it."""
    from galley.state_machine import RunStateMachine
    path = ws / "state.json"
    machine = RunStateMachine.load(path) if path.is_file() else RunStateMachine()
    machine.source_sha256 = sha
    machine.source_id = source_id or machine.source_id
    machine.source_name = src.name
    machine.revision = revision
    machine.save(path)


def seed_workspace(book: str | Path, slug: str, *,
                   workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
                   source_dir: Path | None = None, source_id: str = "",
                   on_source_change: str = "refuse") -> Path:
    """Create or refresh a workspace while preserving run output and local
    settings.

    Record the source hash and optional Drive id in state.json. Unchanged
    input preserves progress. Changed input raises SourceChanged unless
    on_source_change="revise", which archives the previous run artifacts and
    starts a new revision.
    """
    from galley.manifest import sha256_file
    src = Path(book).expanduser()
    if not src.is_file():
        raise DriverError(f"no manuscript at {src}")
    root = Path(str(workspace_root)).expanduser()
    ws = root / slug
    sha = sha256_file(src)
    state_path = ws / "state.json"
    revision = 1
    if state_path.is_file():
        from galley.state_machine import RunStateMachine
        try:
            machine = RunStateMachine.load(state_path)
        except (OSError, ValueError) as e:
            raise DriverError(f"{state_path} is unreadable ({e})") from e
        recorded = machine.source_sha256
        revision = max(1, int(machine.revision or 1))
        if recorded and recorded != sha:
            if on_source_change != "revise":
                raise SourceChanged(
                    f"workspace {ws} was seeded with a different manuscript "
                    f"(recorded {recorded[:12]}…, offered {sha[:12]}…, "
                    f"revision {revision}); its runs and state belong to "
                    f"that revision. Re-run with the revision transition "
                    f"(`--revise`, or on_source_change='revise') to set the "
                    f"previous results aside and start revision "
                    f"{revision + 1}.")
            stamp = f"rev{revision}"
            for sub in ("runs", "deliverable", "handoff"):
                d = ws / sub
                if d.exists() and any(d.iterdir()):
                    target = ws / f"{sub}.{stamp}"
                    n = 1
                    while target.exists():
                        n += 1
                        target = ws / f"{sub}.{stamp}-{n}"
                    d.rename(target)
            state_path.rename(ws / f"state.{stamp}.json")
            revision += 1
            log.warning("workspace %s: the source changed; revision %d "
                        "starts fresh, the previous results are in *.%s",
                        ws, revision, stamp)
    for sub in ("source", "runs", "deliverable", ".claude"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    manual = source_dir or practitioner_dir()
    shutil.copy2(manual / "CLAUDE.md", ws / "CLAUDE.md")
    shutil.copy2(manual / "KNOBS.md", ws / "KNOBS.md")
    if (manual / "references").is_dir():
        # Refresh shipped references without deleting a workspace's own files.
        # Older custom manuals may not supply this optional directory.
        shutil.copytree(manual / "references", ws / "references",
                        dirs_exist_ok=True)
    skills = ws / ".claude" / "skills"
    if skills.exists():
        shutil.rmtree(skills)
    shutil.copytree(manual / "skills", skills)
    if not (ws / "source" / src.name).exists() or \
            (ws / "source" / src.name).read_bytes() != src.read_bytes():
        shutil.copy2(src, ws / "source" / src.name)
    settings = ws / ".claude" / "settings.local.json"
    if not settings.exists():
        settings.write_text(json.dumps(_SETTINGS_SEED, indent=2) + "\n",
                            encoding="utf-8")
    _record_source(ws, src, source_id, sha, revision)
    return ws


def workspace_slug(name: str, author_last: str = "", file_id: str = "") -> str:
    """Build a workspace name from the surname and Drive id suffix, or the
    filename stem when no id is supplied.
    """
    strip = re.compile(r"[^a-z0-9]+")
    base = strip.sub("-", (author_last or "").lower()).strip("-") or \
        strip.sub("-", Path(name).stem.lower()).strip("-") or "untitled"
    suffix = strip.sub("-", (file_id or "").lower()).strip("-")[-8:]
    return f"{base}-{suffix}" if suffix else base


def build_env(base: dict[str, str] | None = None, *,
              wrapbin: str | Path = DEFAULT_WRAPBIN) -> dict[str, str]:
    """Require the subscription token, strip API keys, and prepend the docproof
    wrapper to PATH.

    The token set here reaches the BRAIN only: Claude Code does not pass its
    own OAuth token down to the Bash children the brain spawns, so a sifter
    running `api.claude_lane: subagent` finds its subscription in
    `~/.galley/agent.env` instead (docproof.agent_lane). Warn when that file
    holds no token — otherwise the ladder gets all the way to its first Claude
    call before failing closed, which is what the 2026-09-06 run did.
    """
    env = dict(os.environ if base is None else base)
    token = "".join((env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").split())
    if not token:
        raise DriverError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set — the brain would fall back to "
            "the ANTHROPIC_API_KEY (API dollars) or fail as 'not logged in'. "
            "Fix: run `claude setup-token` once, then export "
            "CLAUDE_CODE_OAUTH_TOKEN=<token>.")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    for key in STRIPPED_KEYS:
        env.pop(key, None)
    wrap = str(Path(str(wrapbin)).expanduser())
    env["PATH"] = f"{wrap}{os.pathsep}{env.get('PATH', '')}"
    # Enable applying screened candidates only in this deployment; see
    # launch.sh.
    env["DOCPROOF_CANDIDATE_APPLY"] = "1"
    if not agent_lane.file_token():
        log.warning(
            "%s holds no %s — the docproof sifters cannot inherit this "
            "session's token, so a phase running api.claude_lane: subagent "
            "will refuse rather than bill the API. Put the `claude "
            "setup-token` value in that file (chmod 600).",
            agent_lane.credentials_path(), agent_lane.OAUTH_TOKEN_KEY)
    return env



@dataclass(frozen=True)
class PhaseSpec:
    """Everything one headless phase session needs — the single seam a test
    replaces."""
    phase: str
    prompt: str
    workspace: Path
    log_path: Path
    argv: list[str]
    env: dict[str, str]
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_s: float = DEFAULT_PHASE_TIMEOUT_S


@dataclass
class PhaseResult:
    phase: str
    returncode: int
    log_path: Path | None = None
    tail: str = ""
    #: What ended this session early, if anything: "timeout" | "max_turns"
    #: | "credentials" (never signed in) | "usage" (shared subscription limit).
    limit: str | None = None
    # Structured CLI result, including subtype and turn count.
    subtype: str = ""
    num_turns: int | None = None
    #: The Claude Code conversation id, for a continuation with --resume.
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.limit


# Exclude driver-written log headers: their configured max-turns value is
# not evidence of exhaustion.
DRIVER_LINE_PREFIX = "# galley driver"


def transcript_tail(text: str) -> str:
    """The session's own last lines — the driver's metadata lines removed."""
    return "\n".join(ln for ln in (text or "").splitlines()
                     if not ln.startswith(DRIVER_LINE_PREFIX)
                     and not ln.startswith("# TIMEOUT")
                     and not ln.startswith("# result:"))


def detect_credential_failure(text: str) -> bool:
    """Whether the session's output says it never signed in."""
    return bool(_CREDENTIALS_RE.search(text or ""))


def detect_turn_cap(text: str) -> bool:
    """Detect turn exhaustion in session output, excluding the driver header
    that lists configured caps.
    """
    return bool(_TURN_CAP_RE.search(transcript_tail(text)))


# The stream-json completion event provides the explicit error_max_turns
# signal.
RESULT_SUBTYPE_MAX_TURNS = "error_max_turns"


def parse_session_result(text: str) -> dict[str, Any] | None:
    """Return the last structured result event in a stream-json log, or None if
    absent.
    """
    found = None
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def session_limit(result: dict[str, Any] | None, tail: str) -> str | None:
    """Classify auth, subscription exhaustion, or the session's turn ceiling."""
    if result is not None and result.get("is_error"):
        blob = " ".join(str(result.get(k) or "")
                        for k in ("result", "error", "message", "subtype"))
        if detect_credential_failure(blob):
            return "credentials"
        if is_usage_limited(blob) or is_usage_limited(tail):
            return "usage"
    if detect_credential_failure(tail):
        return "credentials"
    if result is None and is_usage_limited(tail):
        return "usage"
    if result is not None:
        subtype = str(result.get("subtype") or "")
        if subtype == RESULT_SUBTYPE_MAX_TURNS:
            return "max_turns"
        if subtype.startswith("success") or subtype == "":
            return None
        # Other structured errors are represented by the return code.
        return None
    return "max_turns" if detect_turn_cap(tail) else None


def _render_stream_line(raw: str) -> str:
    """One stream-json event as a readable transcript line, or "" for events
    with nothing a person would read."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(obj, dict):
        return raw
    kind = obj.get("type")
    if kind == "assistant":
        msg = obj.get("message") or {}
        parts = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]).rstrip())
            elif block.get("type") == "tool_use":
                parts.append(f"[tool: {block.get('name', '?')}]")
        return "\n".join(parts)
    if kind == "result":
        return (f"# result: subtype={obj.get('subtype')} "
                f"turns={obj.get('num_turns')} "
                f"is_error={obj.get('is_error')}")
    return ""


def spawn_claude(spec: PhaseSpec) -> PhaseResult:
    """Run a headless phase with turn and wall-clock limits.

    Save raw stream-json and a readable transcript. Use the structured
    result to classify termination; fall back to transcript detection when
    it is absent.
    """
    if os.name == "nt":
        # Check containment support before starting a child. Import failure
        # inside Popen's context would otherwise wait for an unbounded phase.
        try:
            __import__("win32api")
            __import__("win32job")
        except ImportError as exc:
            raise DriverError("Windows phase containment requires pywin32; "
                              "install docproof[galley].") from exc
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path = spec.log_path.with_suffix(".stream.jsonl")
    with open(spec.log_path, "w", encoding="utf-8") as fh:
        fh.write(f"{DRIVER_LINE_PREFIX}: phase {spec.phase} at {_now()} "
                 f"(max-turns {spec.max_turns}, timeout "
                 f"{spec.timeout_s / 3600:.1f}h)\n")
        fh.flush()
        timed_out = False
        with open(stream_path, "w", encoding="utf-8") as raw:
            from docproof.platform_io import process_job, terminate_process_tree
            containment = ({"creationflags": subprocess.CREATE_NO_WINDOW |
                            subprocess.CREATE_NEW_PROCESS_GROUP}
                           if os.name == "nt" else {"start_new_session": True})
            with subprocess.Popen(spec.argv, cwd=str(spec.workspace),
                                  env=spec.env, stdout=raw,
                                  stderr=subprocess.STDOUT, text=True,
                                  **containment) as proc:
                with process_job(proc):
                    try:
                        proc.wait(timeout=spec.timeout_s)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        terminate_process_tree(proc)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    finally:
                        # A shell/tool child may outlive the supervisor, even
                        # when that supervisor exits promptly on SIGTERM.
                        # A phase owns all of its work: no children may keep
                        # rebuilding after timeout, interruption, or completion.
                        terminate_process_tree(proc, force=True)
                        proc.wait(timeout=5)
        # the raw stream is closed now; render it into the readable log
        _render_stream(stream_path, fh)
        if timed_out:
            fh.write(f"\n# TIMEOUT: killed after {spec.timeout_s / 3600:.1f}h\n")
            fh.flush()
            return PhaseResult(spec.phase, TIMEOUT_RC, spec.log_path,
                               transcript_tail(tail_of(spec.log_path)),
                               limit="timeout")
    result = parse_session_result(stream_path.read_text(encoding="utf-8",
                                                        errors="replace"))
    tail = transcript_tail(tail_of(spec.log_path))
    limit = session_limit(result, tail)
    if limit == "usage" and result:
        detail = str(result.get("result") or result.get("error") or "")
        if detail and is_usage_limited(detail):
            tail = detail
    return PhaseResult(spec.phase, proc.returncode, spec.log_path, tail,
                       limit=limit,
                       subtype=str((result or {}).get("subtype") or ""),
                       num_turns=(result or {}).get("num_turns"),
                       session_id=str((result or {}).get("session_id") or ""))


def _render_stream(stream_path: Path, fh) -> None:
    """Append the readable transcript of a stream-json file to the log."""
    try:
        raw = stream_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for ln in raw.splitlines():
        rendered = _render_stream_line(ln)
        if rendered:
            fh.write(rendered + "\n")
    fh.flush()


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_of(path: str | Path, lines: int = TAIL_LINES) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])



# Use the first dollar amount after TOTAL, excluding a later CAP amount.
_TOTAL_RE = re.compile(r"^[^\n]*\bTOTAL\b[^$\n]*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                       re.IGNORECASE | re.MULTILINE)
# Match copyediting scope per line so a refusal can quote the offending
# text.
_COPYEDIT_RE = re.compile(
    r"\b(copy[- ]?edit(?:ing|s)?|flight[- ]?deck|flights?|merge[- ]?desk|"
    r"smoothing|rewrite lane|wave[- ]?2|re-?read)\b", re.IGNORECASE)
# "reread" names two unrelated things: the copy-edit `reread` PHASE, which
# go-live tables, and the mechanical rotated two-pass reread that verify and
# settle have run since v0.187.0. A line that reaches for the second while
# naming only mechanical phases is not a copy-edit line, and refusing it
# blocked every book whose plan described verify that way.
_MECHANICAL_CONTEXT_RE = re.compile(
    r"\b(verify|settle|certify|sweeps?|ladder|audit|chapter sweep)\b",
    re.IGNORECASE)


def _is_copyedit_line(line: str) -> bool:
    """Whether a plan line puts a copy-edit lane in scope."""
    hits = {m.group(0).lower() for m in _COPYEDIT_RE.finditer(line)}
    if not hits or _NEGATED_RE.search(line):
        return False
    # Only a bare re-read claim is ambiguous; every other term is unmistakably
    # the copy-edit lane, so a mechanical word nearby must not excuse it.
    if all(h.replace("-", "") == "reread" for h in hits):
        return not _MECHANICAL_CONTEXT_RE.search(line)
    return True
# Allow a plan to mention excluded copyediting work.
_NEGATED_RE = re.compile(
    r"\b(no|none|not|never|off|omitted|omit|excluded|exclude|skipped?|skip|"
    # Every inflection of "lock": a plan that says the stage LOCKS
    # smoothing (not "locked") is reporting the lane shut, not opening
    # it — and only the past tense was excused, so it was refused.
    r"lock(?:s|ed|ing)?|out of scope|tabled|n/?a|zero)\b", re.IGNORECASE)
# Identify the current plan gate in QUESTIONS.md.
GATE_TOKEN_PREFIX = "GALLEY-GATE"
_APPROVED_RE = re.compile(r"^\s*(?:[-*>#\s]*)?(APPROVED|APPROVE|YES)\b",
                          re.IGNORECASE | re.MULTILINE)
_DECLINED_RE = re.compile(
    r"^\s*(?:[-*>#\s]*)?(DECLINED|DECLINE|REJECTED|REJECT|NO)\b",
    re.IGNORECASE | re.MULTILINE)


@dataclass
class PlanSummary:
    """What the driver can read off a drafted PLAN.md without a model."""
    total_usd: float | None
    copyedit_lines: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def mechanical_only(self) -> bool:
        return not self.copyedit_lines


# A plan's promises are its priced line items: a line that opens with an item
# marker (`2.`, `4b.`, `-`, a table pipe) and carries a dollar amount. The rest
# of PLAN.md is the practitioner explaining the plan — and a plan for a
# mechanical wave explains, at length, that the copy-edit lanes are shut. Read
# as scope, that explanation refused a clean plan four lines over on
# 2026-09-07 ("the copy-edit-scope lines are absent from this plan", "certify
# FAILS the delivery if any copy-edit finding appears", a caveats item whose
# "No copy-edit lane" fell on the next line). Each refusal cost a 17-minute
# profile session. The gate scans the promises; the config gate below is the
# authority on what the run will actually do.
_PLAN_ITEM_RE = re.compile(
    r"^\s*(?:\d+[a-z]?[.)]|[-*•]|\|)\s*\S.*\$\s*\d")


def _is_plan_item(line: str) -> bool:
    """Whether a PLAN.md line is a priced line item — a promise, not prose."""
    return bool(_PLAN_ITEM_RE.match(line))


def read_plan(path: str | Path) -> PlanSummary:
    """Parse a drafted PLAN.md for the two facts the gate turns on: the priced
    total, and whether any priced line item puts a copy-edit lane in scope."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise DriverError(f"no plan to gate on at {path}: {e}") from e
    totals = _TOTAL_RE.findall(text)
    total = float(totals[-1].replace(",", "")) if totals else None
    offenders = [ln.strip() for ln in text.splitlines()
                 if _is_plan_item(ln) and _is_copyedit_line(ln)]
    return PlanSummary(total, offenders, text)


# Config keys that ARE the copy-edit lanes. The stage locks all three shut;
# a run config is the structural truth about what a wave will do, where
# PLAN.md is only the prose describing it.
_COPYEDIT_CONFIG_KEYS = (("smoothing", "enabled"), ("smoothing", "edits"),
                         ("rewrite", "enabled"), ("flights", "enabled"))


def config_copyedit_lanes(path: str | Path) -> list[str]:
    """Which copy-edit lanes a run config actually turns on, as dotted keys.

    The prose gate reads what the practitioner WROTE; this reads what the run
    will DO. Config wins where they disagree, because the config is what
    `docproof review` executes. A missing or unreadable config returns [] —
    the caller decides whether that is fatal, since a config is not written
    until the plan is drafted."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    on = []
    for section, key in _COPYEDIT_CONFIG_KEYS:
        block = data.get(section)
        if isinstance(block, dict) and block.get(key) is True:
            on.append(f"{section}.{key}")
    return on


def gate_decision(plan: PlanSummary, budget_usd: float, *,
                  config_path: str | Path | None = None
                  ) -> tuple[bool, str]:
    """``--approve auto``'s rule: approve a plan that is priced, inside the
    budget, and mechanical-only. Returns (approved, reason).

    When a run config exists it is the AUTHORITY on which lanes are open —
    it is what `docproof review` executes, while PLAN.md is prose about it.
    The prose scan then only has to catch a plan that promises copy-edit work
    the config has not been written for yet."""
    if plan.total_usd is None:
        return False, ("PLAN.md has no parseable TOTAL line — a plan the "
                       "driver cannot price is a plan it cannot approve")
    if plan.total_usd > budget_usd:
        return False, (f"the plan totals ${plan.total_usd:.2f}, over the "
                       f"${budget_usd:.2f} budget")
    lanes = config_copyedit_lanes(config_path) if config_path else []
    if lanes:
        return False, (f"the run config opens {len(lanes)} copy-edit lane(s), "
                       f"which go-live does not do: {', '.join(lanes)}")
    if not plan.mechanical_only:
        quoted = "; ".join(ln[:110] for ln in plan.copyedit_lines[:3])
        return False, (f"{len(plan.copyedit_lines)} plan line(s) put a "
                       f"copy-edit lane in scope, which go-live does not do: "
                       f"{quoted}")
    return True, (f"plan totals ${plan.total_usd:.2f} within the "
                  f"${budget_usd:.2f} budget; mechanical lines only")


def record_approval(plan_path: str | Path, reason: str, *, by: str,
                    at: str | None = None) -> str:
    """Append the approval line the `approve` phase reads back off PLAN.md."""
    line = (f"\nApproved by {by} at {at or _now()}: {reason}. "
            f"Mechanical proofreading only.\n")
    with open(plan_path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return line


def gate_question(slug: str, plan: PlanSummary, budget_usd: float,
                  reason: str, token: str) -> tuple[str, str]:
    """The subject and body of the plan-gate escalation. Says what is blocked,
    what the driver would have done, and exactly how to answer."""
    subject = f"Plan gate — {slug}"
    total = ("unpriced" if plan.total_usd is None
             else f"${plan.total_usd:.2f}")
    body = (
        f"Galley drafted a plan for {slug} and cannot approve it "
        f"unattended.\n\n"
        f"Why: {reason}\n"
        f"Plan total: {total}   Budget: ${budget_usd:.2f}\n"
        f"Scope: mechanical proofreading only (no copy-edit flights, no merge "
        f"desk, no wave-2 re-read).\n\n"
        f"To answer, edit QUESTIONS.md in the book's workspace and write a "
        f"line reading APPROVED (or DECLINED) below the marker line at the "
        f"bottom of that entry. The marker reads:\n    {token}\n"
        f"(the driver reads only what follows the LAST copy of it, so this "
        f"quoted one is harmless).\n\n"
        f"Nothing has been spent. The run is paused at the plan gate and "
        f"stops as needs_human if no reply arrives.\n\n"
        f"--- PLAN.md ---\n{plan.text}")
    return subject, body


def stamp_gate_question(questions_path: str | Path, token: str, subject: str,
                        body: str) -> None:
    """Append a gate question with its reply marker last, so instructions above
    it cannot count as an answer.
    """
    with open(questions_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n\n## {subject}\n\n{body}\n\n{token}\n"
                 f"(write APPROVED or DECLINED on the next line)\n")


def reply_after(text: str, token: str) -> str | None:
    """``"approved"`` / ``"declined"`` from the part of the reply file that
    follows this gate's marker, or None while nothing has been written."""
    idx = text.rfind(token)
    if idx < 0:
        return None
    # Only text after the reply marker can answer the gate.
    after = text[idx + len(token):]
    approved = _APPROVED_RE.search(after)
    declined = _DECLINED_RE.search(after)
    if approved and (not declined or approved.start() < declined.start()):
        return "approved"
    if declined:
        return "declined"
    return None



@dataclass
class DriveResult:
    """What the driver did, and where it stopped."""
    workspace: Path
    phases: list[PhaseResult] = field(default_factory=list)
    outcome: str = "done"                 # "done" | "needs_human" | "blocked"
    reason: str = ""
    stopped_at: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    handoff: list[Path] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    # Legacy interactive question stop; unattended notes never set this.
    asked: bool = False
    recovery_exhausted: bool = False
    recovery: list[dict[str, Any]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.outcome in {"done", "phases_complete"} else (8 if self.outcome == "blocked" else 7)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "workspace": str(self.workspace),
            "outcome": self.outcome,
            "reason": self.reason,
            "stopped_at": self.stopped_at,
            "gate": dict(self.gate),
            "phases": [{"phase": p.phase, "returncode": p.returncode,
                        "limit": p.limit or "",
                        "log": str(p.log_path) if p.log_path else ""}
                       for p in self.phases],
            "handoff": [str(p) for p in self.handoff],
            "uploaded": list(self.uploaded),
            "asked": self.asked,
            "recovery_exhausted": self.recovery_exhausted,
            "recovery": list(self.recovery),
        }


@dataclass
class Driver:
    """Sequence one book through the configured phases with injectable session,
    notification, timing, and upload functions.
    """

    book: Path
    slug: str
    workspace_root: Path = Path(DEFAULT_WORKSPACE_ROOT)
    budget_usd: float = DEFAULT_BUDGET_USD
    # A separate, explicit ceiling for the one final API review. It does not
    # consume or silently enlarge the legacy detector approval's budget.
    astra_review: bool = True
    astra_budget_usd: float = DEFAULT_ASTRA_BUDGET_USD
    astra_max_output_tokens: int = DEFAULT_ASTRA_MAX_OUTPUT_TOKENS
    astra_transport: str | None = None  # None resumes saved routing, else codex.
    astra_chunk_bytes: int | None = None
    astra_client: Any = None
    # The legacy mode remains explicit for controlled comparison. Production
    # mechanical jobs choose code orchestration through the CLI/agent.
    execution_mode: str | None = "session"
    review_rounds: int = 2
    review_calls: int = 400
    review_output_tokens: int = 2_000_000
    command_spawn: Callable[[PhaseSpec], PhaseResult] | None = None
    approve: str = "auto"                       # auto | email | manual
    mechanical_only: bool = True
    start_phase: str | None = None
    only_phases: Sequence[str] | None = None
    handoff_dir: Path | None = None
    drive_folder_id: str = ""
    # None = the per-phase table (PHASE_MODEL / PHASE_EFFORT); a value here
    # overrides it for every phase; the by-phase maps win over both.
    model: str | None = None
    model_by_phase: dict[str, str] = field(default_factory=dict)
    effort: str | None = None
    effort_by_phase: dict[str, str] = field(default_factory=dict)
    permission_mode: str = DEFAULT_PERMISSION_MODE
    wrapbin: Path = Path(DEFAULT_WRAPBIN)
    reply_timeout_s: float = 6 * 3600.0
    poll_interval_s: float = 30.0
    state_gate: bool = True
    question_gate: bool = True
    #: Install the edit-guard hook on EDIT_GUARDED_PHASES sessions.
    edit_guard: bool = True
    # Per-phase caps take precedence over global overrides. Either override
    # is taken exactly as given; only the table defaults scale with length.
    max_turns: int | None = None
    max_turns_by_phase: dict[str, int] = field(default_factory=dict)
    timeout_s: float | None = None
    timeout_by_phase: dict[str, float] = field(default_factory=dict)
    # The book's length for scaling (LENGTH_SCALED_PHASES). None reads it off
    # the workspace's profile.json once profile has written one.
    words: int | None = None
    # Source identity and policy for changed content: refuse or archive and
    # revise.
    source_id: str = ""
    on_source_change: str = "refuse"
    # A round is quiet when new items <= settle_quiet_floor.
    settle_rounds: int = SETTLE_ROUNDS
    settle_quiet_floor: int = SETTLE_QUIET_FLOOR
    settle_quiet_share: float = SETTLE_QUIET_SHARE
    env: dict[str, str] | None = None
    # Resolve spawn_claude at call time so injected test spawners replace
    # it.
    spawn: Callable[[PhaseSpec], PhaseResult] | None = None
    ask: Callable[[str, str, str], str] | None = None
    upload: Callable[[list[Path], str], list[str]] | None = None
    verify_upload: Callable[[Path, str, str], bool] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    #: Told about every step (phase start/end, the gate, the stop, the finish)
    #: as a dict with an "event" key — the agent turns these into heartbeats.
    #: A reporter that raises never sinks the run.
    progress: Callable[[dict[str, Any]], None] | None = None
    # Legacy runs still hand over their edited book when settlement is noisy.
    # Enrolled runs delegate the editorial verdict to the final Astra receipt.
    unconverged: str = field(default="", init=False, repr=False)
    _phase_usage: dict[str, tuple[int, float]] = field(default_factory=dict,
                                                     init=False, repr=False)
    _phase_limits: dict[str, tuple[int, float]] = field(default_factory=dict,
                                                      init=False, repr=False)


    @property
    def workspace(self) -> Path:
        return Path(str(self.workspace_root)).expanduser() / self.slug

    def resolve_execution_mode(self):
        if self.execution_mode is not None:
            return self.execution_mode
        saved_path = self.workspace / "runs" / DRIVER_DIR / "driver.json"
        saved = json.loads(saved_path.read_text("utf-8")) if saved_path.is_file() else {}
        prior = saved.get("execution_mode")
        if prior in {"code", "session"}:
            self.execution_mode = prior
        elif not self.mechanical_only or not self.astra_review or self._final_run() is not None:
            # An existing unversioned manuscript does not have the new initial
            # full-read receipts. Preserve its workflow during an upgrade.
            self.execution_mode = "session"
        else:
            self.execution_mode = "code"
        return self.execution_mode

    def _spawner(self) -> Callable[[PhaseSpec], PhaseResult]:
        return self.spawn or spawn_claude

    def _progress(self, event: str, **fields: Any) -> None:
        if self.progress is None:
            return
        payload = {"event": event, "slug": self.slug, "book": self.book.name,
                   "at": _now(), **fields}
        try:
            self.progress(payload)
        except Exception:                                   # noqa: BLE001
            log.warning("progress reporter failed on %s", event, exc_info=True)

    def _driver_dir(self) -> Path:
        d = self.workspace / "runs" / DRIVER_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def book_words(self) -> int | None:
        """The manuscript's word count: an explicit `words`, else the
        workspace profile's, else None (profile has not run)."""
        if self.words:
            return int(self.words)
        try:
            prof = json.loads((self.workspace / "profile.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(prof, dict):
            return None
        raw = prof.get("word_count") or prof.get("words")
        try:
            return int(raw) if raw and float(raw) > 0 else None
        except (TypeError, ValueError):
            return None

    def length_factor_for(self, phase: str) -> float:
        if phase not in LENGTH_SCALED_PHASES:
            return 1.0
        return length_factor(self.book_words())

    def open_items(self) -> int | None:
        """What verify left for settle in the final run: change problems plus
        walk residuals. None until verify has written either file."""
        run = self._final_run()
        if run is None:
            return None
        total, seen = 0, False
        for name, key in (("change_verify.json", "problems"),
                          ("finished_walk.json", "residuals")):
            try:
                data = json.loads((run / name).read_text("utf-8"))
            except (OSError, ValueError):
                continue
            rows = data.get(key) if isinstance(data, dict) else None
            if isinstance(rows, list):
                total += len(rows)
                seen = True
        return total if seen else None

    def items_factor_for(self, phase: str) -> float:
        if phase not in ITEM_SCALED_PHASES:
            return 1.0
        return items_factor(self.open_items())

    def scale_for(self, phase: str) -> float:
        """The larger of the length and open-item stretches, never below 1."""
        return max(self.length_factor_for(phase), self.items_factor_for(phase))

    def turns_for(self, phase: str) -> int:
        if phase in self.max_turns_by_phase:
            return int(self.max_turns_by_phase[phase])
        if self.max_turns is not None:
            return int(self.max_turns)
        base = PHASE_MAX_TURNS.get(phase, DEFAULT_MAX_TURNS)
        return int(round(base * self.scale_for(phase)))

    def model_for(self, phase: str) -> str:
        if self.execution_mode == "code" and phase in {"approve", "audit", "verify", "settle"}:
            return "code"
        if phase == "astra_review":
            return "gpt-6-astra"
        if self.astra_review and phase in ("certify", "deliver"):
            return "deterministic"
        if phase in self.model_by_phase:
            return str(self.model_by_phase[phase])
        if self.model:
            return str(self.model)
        return PHASE_MODEL.get(phase, DEFAULT_MODEL)

    def effort_for(self, phase: str) -> str | None:
        """The session's --effort, or None to leave Claude Code's default."""
        if self.execution_mode == "code" and phase in {"approve", "audit", "verify", "settle"}:
            return None
        if phase == "astra_review":
            return "high"
        if self.astra_review and phase in ("certify", "deliver"):
            return None
        if phase in self.effort_by_phase:
            level = self.effort_by_phase[phase]
        elif self.effort:
            level = self.effort
        else:
            level = PHASE_EFFORT.get(phase)
        if level is None:
            return None
        level = str(level).strip().lower()
        if level not in EFFORT_LEVELS:
            raise DriverError(
                f"effort {level!r} for {phase} — expected one of "
                f"{', '.join(EFFORT_LEVELS)}")
        return level

    def timeout_for(self, phase: str) -> float:
        if phase in self.timeout_by_phase:
            return float(self.timeout_by_phase[phase])
        if self.timeout_s is not None:
            return float(self.timeout_s)
        base = PHASE_TIMEOUT_S.get(phase, DEFAULT_PHASE_TIMEOUT_S)
        return base * self.scale_for(phase)

    def _resource_env(self, phase: str, env: dict[str, str]) -> dict[str, str]:
        import hashlib
        from docproof.resource_ledger import context_env
        from galley.manifest import sha256_file
        from galley.unattended import UNATTENDED_ENV, WORKSPACE_ENV
        config = self.workspace / "runs" / "mech.yaml"
        digest = sha256_file(config) if config.is_file() else hashlib.sha256(
            b"configuration-not-yet-profiled").hexdigest()
        values = {**env, **context_env(self._driver_dir() / "resources.jsonl",
            sha256_file(self.book), digest), BRAIN_PHASE_ENV: phase,
            UNATTENDED_ENV: "1" if self.approve == "auto" else "0",
            WORKSPACE_ENV: str(self.workspace.resolve())}
        if self.execution_mode == "code" and phase in {"verify", "settle"}:
            values.update(DOCPROOF_RESOURCE_GROUP="review",
                DOCPROOF_RESOURCE_MAX_CALLS=str(self.review_calls),
                DOCPROOF_RESOURCE_MAX_OUTPUT_TOKENS=str(self.review_output_tokens))
        return values

    def _execution_budget(self):
        from galley.execution_budget import ExecutionBudget
        from galley.manifest import sha256_file
        return ExecutionBudget(self._driver_dir() / "execution-budget.json",
                               sha256_file(self.book))

    def _spec(self, phase: str, env: dict[str, str]) -> PhaseSpec:
        prompt = phase_prompt(phase, self.book.name,
                              mechanical_only=self.mechanical_only,
                              budget_usd=self.budget_usd,
                              settle_rounds=self.settle_rounds,
                              settle_quiet_floor=self.settle_quiet_floor,
                              settle_quiet_share=self.settle_quiet_share)
        turns = self.turns_for(phase)
        argv = ["claude", "-p", prompt, "--model", self.model_for(phase),
                "--permission-mode", self.permission_mode,
                "--max-turns", str(turns)]
        effort = self.effort_for(phase)
        if effort:
            argv += ["--effort", effort]
        if self.edit_guard and phase in EDIT_GUARDED_PHASES:
            argv += ["--settings", json.dumps(edit_guard_settings())]
        argv += [
                # Preserve the structured completion beside the readable
                # log.
                "--output-format", "stream-json", "--verbose"]
        # The phase rides in the environment so any verb the brain runs can
        # say who ran it. `galley outcome --set` reads it: an overrule made
        # by the deliver-phase brain is recorded as exactly that, not as
        # "human" (the first Fly delivery's outcome.json, decision log and
        # HubSpot value all claimed a person overruled settle; nobody had).
        from galley.unattended import UNATTENDED_ENV, WORKSPACE_ENV
        spec = PhaseSpec(phase=phase, prompt=prompt, workspace=self.workspace,
                         log_path=self._driver_dir() / f"{phase}.log",
                         argv=argv, env=self._resource_env(phase, env),
                         max_turns=turns, timeout_s=self.timeout_for(phase))
        return self._apply_caps(spec, turns, self.timeout_for(phase))

    @staticmethod
    def _apply_caps(spec: PhaseSpec, turns: int, seconds: float) -> PhaseSpec:
        """Bound one session by turns and wall clock everywhere it matters.

        The Bash tool inside the session is capped at ten minutes by default
        (BASH_MAX_TIMEOUT_MS), while verify and settle on a novel run for an
        hour or more. The manual tells the brain to run them in the FOREGROUND;
        without these variables it cannot, so it backgrounds the command and
        polls — one turn per poll — or the tool kills it and the brain starts
        the read again. The session's Bash ceiling is the phase's own.
        """
        argv = list(spec.argv)
        argv[argv.index("--max-turns") + 1] = str(turns)
        millis = str(max(1, int(seconds * 1000)))
        env = {**spec.env, "BASH_DEFAULT_TIMEOUT_MS": millis,
               "BASH_MAX_TIMEOUT_MS": millis}
        return replace(spec, argv=argv, env=env, max_turns=turns,
                       timeout_s=seconds)

    def _progress_marker(self) -> tuple[str, int, int]:
        """A cheap fingerprint of the workspace's evidence: the run state
        plus the count and newest mtime of every file outside the driver's
        own directory. A session that changes it did work; one that leaves
        it alone did not — and only the first earns a continuation."""
        newest, count = 0, 0
        skip = (self.workspace / "runs" / DRIVER_DIR).resolve()
        for root, dirs, files in os.walk(self.workspace):
            if Path(root).resolve() == skip:
                dirs[:] = []
                continue
            for name in files:
                try:
                    st = os.stat(os.path.join(root, name))
                except OSError:
                    continue
                count += 1
                newest = max(newest, st.st_mtime_ns)
        return (self._current_state(), count, newest)

    def _below_floor(self, phase: str, turns: int, seconds: float) -> bool:
        floor_turns = min(RECOVERY_FLOOR_TURNS, max(1, self.turns_for(phase) // 4))
        floor_s = min(RECOVERY_FLOOR_S, self.timeout_for(phase) / 4)
        return turns < floor_turns or seconds < floor_s

    def _grant_continuation(self, phase: str, execution, reason: str) -> int:
        """Extend a capped-but-progressing phase; 0 when no grant is left."""
        from galley.execution_budget import ExecutionBudgetError
        turns = max(1, int(round(self.turns_for(phase) * RECOVERY_GRANT_SHARE)))
        seconds = self.timeout_for(phase) * RECOVERY_GRANT_SHARE
        try:
            number = execution.extend(phase, turns, seconds, reason=reason,
                                      max_grants=RECOVERY_MAX_GRANTS)
        except ExecutionBudgetError as exc:
            self.log(f"phase {phase}: no continuation — {exc}")
            return 0
        self.log(f"--- phase {phase}: continuation {number} of "
                 f"{RECOVERY_MAX_GRANTS} granted (+{turns} turns, "
                 f"+{seconds / 60:.0f} min) — the session was cut off while "
                 f"advancing the book ---")
        self._progress("continuation", phase=phase, grant=number,
                       turns=turns, timeout_s=seconds)
        return number

    def _questions_text(self) -> str:
        from galley.unattended import NOTES_NAME
        parts = []
        for path in (self.workspace / "QUESTIONS.md", self._driver_dir() / NOTES_NAME):
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
        return "\n".join(parts)

    def _new_question(self, before: str) -> str:
        """What a phase appended to QUESTIONS.md, or ""."""
        after = self._questions_text()
        if not after or after == before:
            return ""
        return after[len(before):].strip() if after.startswith(before) \
            else after.strip()

    def settle_verdict(self) -> str:
        """Return a reason for human review if settlement exhausted its rounds
        without meeting the quiet threshold; otherwise return an empty
        string.
        """
        run = self._final_run()
        if run is None:
            return ""
        payload = _read_json(run / "settlement.json")
        if not isinstance(payload, dict):
            return ""
        conv = payload.get("convergence") or {}
        if not conv or conv.get("stopped") in ("clean", "quiet"):
            return ""
        if conv.get("quiet") is True:
            return ""
        rounds = int(conv.get("rounds", 0) or 0)
        new_items = conv.get("last_new_items", "?")
        return (f"still finding errors after {rounds} round(s): {new_items} in "
                f"the last round (the sweep stopped on "
                f"{conv.get('stopped', 'the round cap')}, and a round is quiet "
                f"only under {self.settle_quiet_floor + 1} new items) — the "
                f"book is not converging on clean and needs a human "
                f"proofreader")

    def _final_run(self) -> Path | None:
        """The run directory the build ended in: the newest `runs/*` holding a
        findings envelope."""
        pin = self.workspace / "runs" / DRIVER_DIR / "final-run.json"
        if pin.is_file():
            try:
                rel = json.loads(pin.read_text("utf-8"))["run"]
                run = (self.workspace / rel).resolve()
                run.relative_to(self.workspace.resolve())
                if (run / "findings.json").is_file():
                    return run
            except (OSError, ValueError, KeyError, TypeError):
                pass
        runs = sorted((self.workspace / "runs").glob("*/findings.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return runs[0].parent if runs else None

    def _current_state(self) -> str:
        from galley.state_machine import RunStateMachine
        path = self.workspace / "state.json"
        if not path.is_file():
            return ""
        try:
            return RunStateMachine.load(path).current
        except (OSError, ValueError):
            return ""

    def _state_reached(self, state: str) -> bool:
        from galley.state_machine import RunStateMachine
        path = self.workspace / "state.json"
        if not path.is_file():
            return False
        try:
            return RunStateMachine.load(path).reached(state)
        except (OSError, ValueError):
            return False

    def _stop(self, result: DriveResult, phase: str | None, reason: str
              ) -> DriveResult:
        if self.astra_review:
            return self._block(result, phase, reason)
        result.outcome = "needs_human"
        result.reason = reason
        result.stopped_at = phase
        self._write_outcome(reason)
        self._write_ledger(result)
        # Preserve the decision log for a stopped run.
        self.salvage_deliverable()
        self.write_decision_log()
        # Return a stopped run's outcome even when no manuscript was
        # produced.
        self._stopped_handoff(result)
        self.log(f"STOPPED at {phase or 'the plan gate'}: {reason}")
        self._progress("stopped", phase=phase, outcome="needs_human",
                       reason=reason[:600])
        return result

    def _block(self, result: DriveResult, phase: str | None, reason: str
               ) -> DriveResult:
        """Keep an operational failure out of the manuscript's CRM verdict."""
        from docproof.utils.files import write_atomic
        result.outcome, result.reason, result.stopped_at = "blocked", reason, phase
        self._write_ledger(result)
        write_atomic(self._driver_dir() / "blocked.json", json.dumps({
            "schema_version": 1, "phase": phase, "reason": reason,
            "generated_at": _now(), "editorial_verdict_unchanged": True,
        }, indent=2))
        self.write_decision_log()
        self.log(f"BLOCKED at {phase or 'the plan gate'}: {reason}")
        self._progress("blocked", phase=phase, outcome="blocked", reason=reason[:600])
        return result

    def _review_snapshot_available(self) -> bool:
        from galley.verify import deliverable_docx
        run = self._final_run()
        return bool(run and deliverable_docx(run)
                    and all((run / name).is_file() for name in
                            ("findings.json", "change_verify.json", "finished_walk.json")))

    def _enroll_astra(self) -> Path:
        from docproof.utils.files import write_atomic
        run = self._final_run()
        if run is None:
            raise DriverError("No final findings/build is available for Astra review.")
        astra_review_settings(run, transport=self.astra_transport,
                              max_chunk_bytes=self.astra_chunk_bytes, persist=True)
        write_atomic(self._driver_dir() / "final-run.json", json.dumps({
            "run": str(run.relative_to(self.workspace)),
        }, indent=2))
        return run

    def _run_astra_review(self, result: DriveResult) -> DriveResult | None:
        """The final review; a durable receipt, not a session exit, decides."""
        from galley.outcome import astra_outcome
        from galley.state_machine import RunStateMachine
        phase = "astra_review"
        path = self._driver_dir() / f"{phase}.log"
        self._progress("phase_start", phase=phase, model="gpt-6-astra", effort="high")
        try:
            run = self._enroll_astra()
            settings = astra_review_settings(run)
            contexts = [p for p in [self.workspace / "runs" / "audit.json"]
                        if self.execution_mode == "code" and p.is_file()]
            context_kwargs = {"context_paths": contexts} if contexts else {}
            from docproof.resource_ledger import use_context
            with use_context(self._resource_env(phase, {})):
                if settings["transport"] == "api":
                    from galley.astra_review import review_run
                    receipt = review_run(run, budget_usd=self.astra_budget_usd,
                                         max_output_tokens=self.astra_max_output_tokens,
                                         client=self.astra_client, **context_kwargs)
                else:
                    from galley.astra_subscription import review_run
                    receipt = review_run(run, max_chunk_bytes=settings["max_chunk_bytes"], **context_kwargs)
            if (receipt["review"]["editorial_verdict"] == "ready"
                    and receipt.get("repair_required")):
                from galley.astra_reconcile import reconcile_run
                receipt = reconcile_run(run)
            path.write_text(json.dumps({
                "model": "gpt-6-astra", "reasoning_effort": "high",
                "transport": settings["transport"],
                "response_id": receipt.get("response_id", ""),
                "packet_sha256": receipt["packet_sha256"],
                "review": receipt["review"], "usage": receipt.get("usage", {}),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            # assess validates the receipt again; pending repairs cannot write done.
            verdict = astra_outcome(run)
            verdict.save(run)
        except Exception as e:                              # noqa: BLE001
            path.write_text(str(e) + "\n", encoding="utf-8")
            result.phases.append(PhaseResult(phase, 8, path, str(e)))
            self._progress("phase_end", phase=phase, ok=False, returncode=8)
            return self._block(result, phase, str(e))
        state_path = self.workspace / "state.json"
        machine = RunStateMachine.load(state_path)
        if not machine.reached("astra_reviewed"):
            previous = machine.history[-1] if machine.history else None
            machine.advance("astra_reviewed", by="gpt-6-astra (final editorial review)",
                            source_sha256=machine.source_sha256,
                            config_sha256=previous.config_sha256 if previous else "")
            machine.save(state_path)
        result.phases.append(PhaseResult(phase, 0, path))
        self._progress("phase_end", phase=phase, ok=True, returncode=0)
        self._write_ledger(result)
        if verdict.outcome == "needs_human":
            # This is an editorial escalation, so publish Astra's actual reason.
            result.outcome, result.reason = verdict.outcome, verdict.reason
            result.stopped_at = phase
            verdict.save(self.workspace / "runs")
            self._write_ledger(result)
            self.write_decision_log()
            self._stopped_handoff(result)
            if result.outcome == "blocked":
                # Packaging can fail before a retryable upload package exists.
                # Keep the claim resumable instead of announcing completion.
                return result
            self._progress("finished", phase=phase, outcome=verdict.outcome,
                           reason=verdict.reason[:600])
            return result
        return None

    def _run_final_phase(self, phase: str, result: DriveResult
                         ) -> DriveResult | None:
        """After Astra, certification and packaging are deterministic commands."""
        from galley.manifest import certify_run, sha256_file, write_certificate_receipt
        from galley.outcome import astra_outcome
        from galley.state_machine import RunStateMachine
        from galley.verify import build_fingerprints, deliverable_docx
        path = self._driver_dir() / f"{phase}.log"
        self._progress("phase_start", phase=phase, model=None, effort=None)
        try:
            run = self._enroll_astra()
            verdict = astra_outcome(run)
            if verdict.outcome != "done":
                # A delivery-only resume of a legitimate editorial escalation
                # reuses its receipt and diagnostic package, never reruns a book.
                return self._run_astra_review(result)
            verdict.save(run)
            if phase == "certify":
                from types import SimpleNamespace
                from docproof.__main__ import _effective_cfg
                approval = json.loads((self.workspace / "approval.json").read_text("utf-8"))
                config_path = Path(approval["config_path"])
                if not config_path.is_absolute():
                    config_path = self.workspace / config_path
                cfg = _effective_cfg(SimpleNamespace(
                    config=str(config_path), stage=approval.get("stage"),
                    genre=approval.get("genre")))
                cert = certify_run(run, manifest=approval, cfg=cfg, source=self.book)
                write_certificate_receipt(run, cert)
                lines = [f"Certificate for {run}:"]
                for check in cert.checks:
                    glyph = "PASS" if check.status == "pass" else check.status.upper()
                    lines.append(f"  [{glyph}] {check.name} — {check.detail}")
                lines.append("PASSED" if cert.passed else "FAILED")
                (self.workspace / "runs" / "certify.txt").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
                if not cert.passed:
                    reasons = [c.detail for c in cert.checks
                               if c.status == "fail" or (c.required and c.status != "pass")]
                    raise DriverError("Structural certification blocked delivery: " + "; ".join(reasons))
                machine = RunStateMachine.load(self.workspace / "state.json")
                if not machine.reached("certified"):
                    previous = machine.history[-1]
                    machine.advance("certified", by="galley certify",
                                    source_sha256=machine.source_sha256,
                                    config_sha256=previous.config_sha256)
                    machine.save(self.workspace / "state.json")
            else:
                certificate = json.loads((run / "certificate.json").read_text("utf-8"))
                fingerprints = build_fingerprints(run)
                if (not certificate.get("passed") or not fingerprints
                        or certificate.get("build_sha256") != fingerprints["build_sha256"]):
                    raise DriverError("No passing certificate covers the exact final manuscript.")
                if not self._packaged_files():
                    self._render_final_reports(run)
                final_docx = deliverable_docx(run)
                copied = deliverable_docx(self.workspace / "deliverable")
                if not final_docx or not copied or sha256_file(final_docx) != sha256_file(copied):
                    raise DriverError("The delivery copy does not match the certified manuscript.")
            path.write_text(f"{phase} completed deterministically\n", encoding="utf-8")
            result.phases.append(PhaseResult(phase, 0, path))
            self._write_ledger(result)
            self._progress("phase_end", phase=phase, ok=True, returncode=0)
            return None
        except Exception as e:                              # noqa: BLE001
            path.write_text(str(e) + "\n", encoding="utf-8")
            result.phases.append(PhaseResult(phase, 8, path, str(e)))
            self._progress("phase_end", phase=phase, ok=False, returncode=8)
            return self._block(result, phase, str(e))

    def _render_final_reports(self, run: Path) -> None:
        from galley.casefile import CaseFile
        from galley.casefile_synth import casefile_from_run, workspace_waves
        from galley.letter import (render_all, render_author_letter,
                                   render_verification_report, run_evidence)
        from galley.outcome import astra_outcome
        from galley.astra_review import validate_receipt
        from galley.verify import deliverable_docx
        out = self.workspace / "deliverable"
        out.mkdir(parents=True, exist_ok=True)
        manuscript = deliverable_docx(run)
        if manuscript is None:
            raise DriverError("No certified manuscript exists to deliver.")
        # Remove an older manuscript candidate before copying the pinned build;
        # supporting DOCX files are preserved and regenerated separately.
        existing = deliverable_docx(out)
        if existing and existing.name != manuscript.name:
            existing.unlink()
        shutil.copy2(manuscript, out / manuscript.name)
        verdict = astra_outcome(run)
        verdict.save(run)
        verdict.save(out)
        casefile = run / "casefile.json"
        cf = CaseFile.load(casefile) if casefile.exists() else casefile_from_run(run)
        waves = workspace_waves(self.workspace)
        if waves:
            cf.waves = list(waves)
            cf.budget.charges = []
            for wave in waves:
                cf.budget.charge(f"run {wave.index}", wave.spend_usd, wave=wave.index)
        receipt = validate_receipt(run)
        if receipt.get("actual_cost_usd") is not None:
            cf.budget.charge("Final Astra review (uncached list-rate estimate)",
                             float(receipt["actual_cost_usd"]))
        evidence = run_evidence(run, self.workspace)
        title = handoff_base(self.book.name)
        render_all(cf, out, evidence=evidence, title=title)
        render_verification_report(evidence, out, cf=cf, title=title)
        render_author_letter(cf, out, evidence=evidence, title=title)

    def _stopped_handoff(self, result: DriveResult) -> None:
        """Build and upload available artifacts after a stopped run. Log
        handoff failures without replacing the original error.
        """
        if self.astra_review:
            self._astra_stopped_handoff(result)
            return
        out = Path(self.handoff_dir) if self.handoff_dir \
            else self.workspace / "handoff"
        try:
            # Prefer the driver failure over an earlier settle-written done
            # verdict.
            result.handoff = build_handoff(
                self.workspace, self.book.name, out,
                outcome_sources=[self.workspace / "runs" / "outcome.json",
                                 *self._outcome_sources()],
                partial=True)
        except Exception as e:                              # noqa: BLE001
            self.log(f"no hand-off for the stopped run ({e})")
            result.handoff = []
        # The evidence travels with the verdict: every phase transcript, the
        # driver ledger and the run state, zipped beside the outcome, so a
        # needs_human never has to be diagnosed over ssh.
        bundle = build_diagnostics(self.workspace, self.book.name, out)
        if bundle is not None:
            result.handoff.append(bundle)
        if not result.handoff or not self.drive_folder_id:
            return
        uploader = self.upload or _default_upload
        try:
            result.uploaded = uploader(result.handoff, self.drive_folder_id)
        except Exception as e:                              # noqa: BLE001
            self.log(f"the stopped run's hand-off is in {out} but the Drive "
                     f"upload failed ({e}) — put those files in folder "
                     f"{self.drive_folder_id} by hand, or re-run the deliver "
                     f"phase once Google sign-in is working")

    def _astra_stopped_handoff(self, result: DriveResult) -> None:
        """Freeze editorial escalation evidence without publishing a ready book."""
        from docproof.utils.files import write_atomic
        from galley.astra_review import validate_receipt
        from galley.manifest import sha256_file
        from galley.outcome import astra_outcome
        from galley.verify import deliverable_docx
        out = Path(self.handoff_dir) if self.handoff_dir else self.workspace / "handoff"
        try:
            run = self._final_run()
            if run is None:
                raise DriverError("No pinned final review is available.")
            verdict, receipt = astra_outcome(run), validate_receipt(run)
            if verdict.outcome != "needs_human":
                raise DriverError("A diagnostic escalation requires Astra's human-review verdict.")
            files = self._packaged_files()
            if not files:
                out.mkdir(parents=True, exist_ok=True)
                base = handoff_base(self.book.name)
                pairs = [(run / "outcome.json", f"{base} - outcome.json"),
                         (run / "astra-review.json", f"{base} - astra-review.json"),
                         (run / "findings.json", f"{base} - findings.json")]
                journal = self.workspace / "deliverable" / DECISION_LOG_NAME
                if journal.is_file():
                    pairs.append((journal, f"{base} - decision-log.md"))
                for source, name in pairs:
                    dest = out / name
                    shutil.copy2(source, dest)
                    files.append(dest)
                bundle = build_diagnostics(self.workspace, self.book.name, out)
                if bundle is not None:
                    files.append(bundle)
                manuscript = deliverable_docx(run)
                if manuscript is None:
                    raise DriverError("The reviewed manuscript is missing.")
                write_atomic(self._driver_dir() / "package.json", json.dumps({
                    "schema_version": 1, "kind": "human_review",
                    "packet_sha256": receipt["packet_sha256"], "run": str(run),
                    "source_id": self.source_id or "", "build_sha256": sha256_file(manuscript),
                    "outcome": verdict.outcome, "reason": verdict.reason,
                    "artifacts": [{"path": str(path), "name": path.name,
                                   "sha256": sha256_file(path)} for path in files],
                }, indent=2, ensure_ascii=False))
            result.handoff = files
            if self.drive_folder_id:
                package = json.loads((self._driver_dir() / "package.json").read_text("utf-8"))
                result.uploaded = publish_verified_handoff(
                    package, self.drive_folder_id, self._driver_dir() / "delivery.json",
                    source_id=self.source_id or "", upload=self.upload, verify=self.verify_upload)
        except Exception as e:                              # noqa: BLE001
            if not result.handoff:
                self._block(result, "deliver",
                            "Astra's human-review verdict is recorded, but its "
                            f"diagnostic package could not be prepared ({e}); "
                            "the handoff remains pending.")
                return
            self.log(f"Astra's human-review verdict is recorded; its diagnostic "
                     f"delivery is pending ({e})")

    def salvage_deliverable(self) -> None:
        """Preserve production's edited-book recovery for explicit legacy runs."""
        if self.astra_review:
            return
        run = self._final_run()
        if run is None:
            return
        out = self.workspace / "deliverable"
        out.mkdir(parents=True, exist_ok=True)
        from galley.verify import deliverable_docx
        if deliverable_docx(out) is None:
            built = deliverable_docx(run)
            if built is not None:
                try:
                    shutil.copy2(built, out / built.name)
                    self.log(f"salvaged the edited manuscript from runs/{run.name} for the stopped run")
                except OSError as e:
                    self.log(f"could not salvage the manuscript ({e})")
        self.salvage_letters(run, out)

    def salvage_letters(self, run: Path, out: Path) -> None:
        """Recover production reports when a legacy run never reached deliver."""
        want = (_LETTER_NAMES, _STYLE_NAMES, _VERIFICATION_NAMES, _AUTHOR_LETTER_NAMES)
        if all(_first_existing(out, names) for names in want):
            return
        try:
            from galley.casefile_synth import casefile_from_run, workspace_waves
            from galley.letter import (render_all, render_author_letter,
                                       render_verification_report, run_evidence)
            cf = casefile_from_run(run)
            waves = workspace_waves(self.workspace)
            if waves:
                cf.waves = list(waves)
                cf.budget.charges = []
                for wave in waves:
                    cf.budget.charge(f"run {wave.index}", wave.spend_usd, wave=wave.index)
            evidence = run_evidence(run, self.workspace)
            title = handoff_base(self.book.name)
            render_all(cf, out, evidence=evidence, title=title)
            render_verification_report(evidence, out, cf=cf, title=title)
            render_author_letter(cf, out, evidence=evidence, title=title)
            self.log(f"rendered the letters for the stopped run from runs/{run.name}")
        except Exception as e:                              # noqa: BLE001
            self.log(f"no letters for the stopped run ({e})")

    def write_decision_log(self) -> Path | None:
        """Render deliverable/DECISION_LOG.md from available artifacts; log
        failures and continue.
        """
        run = self._final_run()
        out = self.workspace / "deliverable" / DECISION_LOG_NAME
        try:
            from galley.journal import write_journal
            return write_journal(run or self.workspace / "runs", out,
                                 workspace=self.workspace,
                                 book=self.book.name, generated_at=_now())
        except Exception as e:                              # noqa: BLE001
            self.log(f"could not render the decision log ({e})")
            return None

    def _write_outcome(self, reason: str, where: Path | None = None) -> Path:
        """Write the driver failure and its reason to runs/outcome.json."""
        from galley.outcome import Outcome, hubspot_fields
        if self.astra_review:
            raise DriverError("An enrolled Astra verdict cannot be overwritten by a driver heuristic.")
        runs = Path(where) if where is not None else self.workspace / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        return Outcome(outcome="needs_human", reason=reason,
                       evidence={"driver": True, "slug": self.slug},
                       hubspot=hubspot_fields("needs_human"),
                       set_by="galley drive").save(runs)

    def backgrounded_work(self, phase: str) -> str:
        """What the phase session left running when it ended, if anything.

        A `claude -p` session that backgrounds a long command exits 0 with the
        work unfinished, and the child dies with the session. The stream log
        records those tasks, so the driver can say so instead of reporting
        only the missing state advance.

        Returns a short description for the failure message, or "" when the
        session ended cleanly."""
        directory = self.workspace / "runs" / DRIVER_DIR
        streams = list(directory.glob(f"{phase}*.stream.jsonl"))
        stream = max(streams, key=lambda p: p.stat().st_mtime_ns) if streams else directory / f"{phase}.stream.jsonl"
        try:
            tail = stream.read_text(encoding="utf-8",
                                    errors="replace").splitlines()[-40:]
        except OSError:
            return ""
        markers = [line for line in tail
                   if '"background_tasks_changed"' in line
                   or '"task_notification"' in line]
        if not markers:
            return ""
        return f"{len(markers)} background-task event(s) in the last turns"

    def _write_ledger(self, result: DriveResult) -> Path:
        path = self._driver_dir() / "driver.json"
        from docproof.resource_ledger import summarize
        payload = result.to_json()
        resources = self._driver_dir() / "resources.jsonl"
        if resources.exists():
            payload["resources"] = summarize(resources)
        intake = Path(self.book).parent.parent / "resources.jsonl"
        if Path(self.book).parent.name == "formatted" and intake.is_file():
            payload["preparation_resources"] = summarize(intake)
        payload["execution_mode"] = self.execution_mode
        path.write_text(json.dumps(payload, indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        return path


    def run_gate(self, result: DriveResult, env: dict[str, str] | None = None,
                 *, repair: bool = True) -> bool:
        """Decide the plan gate. Returns True to continue into `approve`."""
        plan_path = self.workspace / "PLAN.md"
        if self.approve == "manual":
            result.gate = {"policy": "manual", "approved": False}
            self._stop(result, "approve",
                       "plan gate: --approve manual — a human must approve "
                       f"{plan_path} and then resume with `--from approve`")
            return False
        config_path = self.workspace / "runs" / "mech.yaml"
        import yaml
        plan, lanes = PlanSummary(None, [], ""), []
        try:
            plan = read_plan(plan_path)
            lanes = config_copyedit_lanes(config_path) if config_path.is_file() else []
            approved, reason = gate_decision(
                plan, self.budget_usd,
                config_path=config_path if config_path.is_file() else None)
        except (DriverError, ValueError, yaml.YAMLError) as exc:
            approved, reason = False, f"cannot validate the draft plan/config: {exc}"
        result.gate = {"policy": self.approve, "approved": approved,
                       "reason": reason,
                       "total_usd": plan.total_usd,
                       "copyedit_lines": list(plan.copyedit_lines),
                       "config_lanes": lanes}
        if approved:
            record_approval(plan_path, reason, by="galley drive (auto)")
            self.log(f"plan gate: APPROVED — {reason}")
            self._progress("gate", approved=True, reason=reason[:300])
            return True
        if self.approve == "auto":
            turns, seconds = self._remaining_phase_budget("profile")
            if (repair and env is not None and turns > 0 and seconds > 0
                    and not (self.workspace / "approval.json").exists()
                    and not self._state_reached("plan_approved")):
                stopped = self._run_session_phase("profile", env, result,
                    guidance=f"The automatic plan gate refused the draft: {reason}. "
                    "Correct only the plan/proposed config before approval. "
                    "Reuse the existing profile. No paid work is authorized yet. "
                    "Preserve all required coverage, approved routes, and the "
                    "existing scope and budget; do not ask to enlarge them.")
                if stopped is not None:
                    return False
                return self.run_gate(result, env, repair=False)
            result.recovery_exhausted = True
            self._stop(result, "approve", f"plan gate refused: {reason}")
            return False
        return self._escalate(result, plan, reason)

    def _escalate(self, result: DriveResult, plan: PlanSummary, reason: str
                  ) -> bool:
        token = f"{GATE_TOKEN_PREFIX} {self.slug} {_now()}"
        subject, body = gate_question(self.slug, plan, self.budget_usd, reason,
                                      token)
        questions = self.workspace / "QUESTIONS.md"
        stamp_gate_question(questions, token, subject, body)
        sender = self.ask or _default_ask
        try:
            to = sender(subject, body, self.slug)
        except Exception as e:                              # noqa: BLE001
            result.gate["sent"] = False
            self._stop(result, "approve",
                       f"plan gate refused ({reason}) and the question could "
                       f"not be sent ({e}) — it is logged in {questions}")
            return False
        result.gate["sent"] = True
        result.gate["sent_to"] = to
        result.gate["token"] = token
        self._progress("gate", approved=None, escalated_to=to)
        self.log(f"plan gate: escalated to {to}; waiting up to "
                 f"{self.reply_timeout_s / 3600:.1f}h for a reply in "
                 f"{questions}")
        verdict = self._await_reply(questions, token)
        result.gate["reply"] = verdict or "timeout"
        if verdict == "approved":
            record_approval(self.workspace / "PLAN.md",
                            f"human reply to {token}", by="human (email)")
            result.gate["approved"] = True
            self.log("plan gate: APPROVED by reply")
            return True
        if verdict == "declined":
            self._stop(result, "approve",
                       f"plan gate: declined by reply ({reason})")
            return False
        self._stop(result, "approve",
                   f"plan gate: no reply within "
                   f"{self.reply_timeout_s / 3600:.1f}h — the question is in "
                   f"{questions} ({reason})")
        return False

    def _await_reply(self, questions: Path, token: str) -> str | None:
        deadline = self.clock() + self.reply_timeout_s
        while True:
            try:
                text = questions.read_text(encoding="utf-8")
            except OSError:
                text = ""
            verdict = reply_after(text, token)
            if verdict:
                return verdict
            if self.clock() >= deadline:
                return None
            self.sleep(self.poll_interval_s)


    def _phase_problem(self, phase: str, spec: PhaseSpec, outcome: PhaseResult,
                       review_snapshot: bool) -> str:
        if outcome.limit == "usage":
            raise UsageLimitError(f"Claude subscription usage limit at {phase}: {outcome.tail}")
        if outcome.limit == "credentials":
            raise CredentialsError(
                f"phase {phase} could not sign in to Claude Code — the "
                f"subscription token (CLAUDE_CODE_OAUTH_TOKEN) is expired "
                f"or revoked; last lines of {outcome.log_path}:\n{outcome.tail}")
        if review_snapshot:
            return ""
        if outcome.limit == "timeout":
            return (f"phase {phase} hit its wall-clock cap of "
                    f"{spec.timeout_s / 3600:.1f}h and was killed; last lines "
                    f"of {outcome.log_path}:\n{outcome.tail}")
        if outcome.limit == "max_turns":
            return (f"phase {phase} hit its turn cap of {spec.max_turns} "
                    f"(claude --max-turns); last lines of "
                    f"{outcome.log_path}:\n{outcome.tail}")
        if not outcome.ok:
            return (f"phase {phase} exited {outcome.returncode}; last lines "
                    f"of {outcome.log_path}:\n{outcome.tail}")
        need = REQUIRED_STATE.get(phase) if self.state_gate else None
        if need and not self._state_reached(need):
            backgrounded = self.backgrounded_work(phase)
            extra = (f" The session left work running in the background "
                     f"({backgrounded}) and ended anyway, which kills that "
                     f"work mid-flight. Run long reads in the foreground."
                     if backgrounded else "")
            return (f"phase {phase} exited 0 but the run state machine is at "
                    f"{self._current_state() or 'nothing'!r}, not {need!r} — "
                    f"the session did not advance the ledger, so the next "
                    f"phase would build on an unproven one.{extra}")
        return ""

    def _remaining_phase_budget(self, phase: str) -> tuple[int, float]:
        return self._execution_budget().remaining(
            phase, self.turns_for(phase), self.timeout_for(phase))

    def _run_code_phase(self, phase, env, result):
        from galley.engine_phases import EnginePhases
        self._engine_env = env
        def execute(spec):
            budget = self._execution_budget()
            # Every subprocess owns its remaining wall time. Model calls have
            # a separate shared review budget in the resource ledger.
            log_path = spec.log_path.with_name(spec.log_path.stem + "-" + uuid.uuid4().hex + ".log")
            key, _, seconds = budget.reserve("code-" + spec.phase, 0,
                self.timeout_for(spec.phase), log_path=log_path)
            spec = replace(spec, timeout_s=seconds, log_path=log_path)
            self._progress("phase_start", phase=spec.phase, model="code",
                           timeout_s=seconds, log_path=str(log_path))
            started = self.clock()
            outcome = (self.command_spawn or spawn_claude)(spec)
            elapsed = max(0.0, self.clock() - started)
            if outcome.limit == "timeout":
                elapsed = max(elapsed, seconds)
            budget.finish(key, turns=0, seconds=elapsed, status="completed")
            if outcome.limit == "usage" or is_usage_limited(outcome.tail):
                # Let the engine commit the known terminal command receipt
                # before propagating the quota pause to the agent.
                outcome = replace(outcome, limit="usage")
            result.phases.append(outcome)
            self._progress("phase_end", phase=spec.phase,
                           ok=not outcome.limit and outcome.returncode in (
                               (0, 1) if spec.phase in {"verify", "settle"} else (0,)),
                           returncode=outcome.returncode, limit=outcome.limit)
            return outcome
        try:
            EnginePhases(self, execute).run(phase)
        except UsageLimitError as exc:
            self._block(result, phase, str(exc))
            raise
        except Exception as exc:
            return self._block(result, phase, str(exc))
        return None

    def _run_session_phase(self, phase: str, env: dict[str, str],
                           result: DriveResult, *, guidance: str = ""
                           ) -> DriveResult | None:
        """One session, at most one recovery within the original caps, and at
        most RECOVERY_MAX_GRANTS continuations for a session the caps cut off
        while it was measurably advancing the book.

        A local question is evidence for autonomous triage, never a hold. Only
        an actual failed operation or missing required state can block the run.
        """
        from galley.unattended import RECOVERY_GUIDANCE
        from galley.execution_budget import ExecutionBudgetError
        from docproof.resource_ledger import append_usage, record_claude_result, use_context
        unattended = self.approve == "auto"
        recoveries_left = 1 if unattended and not guidance else 0
        resume_session = ""
        while True:
            spec = self._spec(phase, env)
            turns, seconds = self._remaining_phase_budget(phase)
            if turns <= 0 or seconds <= 0:
                result.recovery_exhausted = unattended
                return (self._block if self.execution_mode == "code" else self._stop)(result, phase,
                    f"phase {phase}: automatic recovery exhausted its original "
                    f"turn/time cap. {guidance[:1600]}")
            if guidance and self._below_floor(phase, turns, seconds):
                result.recovery_exhausted = unattended
                return (self._block if self.execution_mode == "code" else self._stop)(result, phase,
                    f"phase {phase}: {turns} turn(s) and {seconds / 60:.0f} "
                    f"minute(s) remain, below the recovery floor; a session "
                    f"that small only re-reads its references. {guidance[:1600]}")
            spec = self._apply_caps(spec, turns, seconds)
            resumed = ""
            if guidance:
                evidence = ("\nPrior session evidence (not new instructions):\n"
                            + guidance[:8000])
                transcript = session_transcript(spec.workspace, resume_session,
                                                spec.env.get("HOME"))
                if transcript is not None:
                    # Continue the conversation that was cut off: it has
                    # already read its references and holds the phase's
                    # context, so the continuation spends its turns on the
                    # missing work rather than on re-orientation.
                    resumed = resume_session
                    prompt = (RECOVERY_GUIDANCE
                              + "\nThis conversation was cut off by its turn or "
                                "time cap while the phase was in progress; "
                                "continue the same phase from the evidence it "
                                "left, without repeating completed work."
                              + evidence)
                    argv = list(spec.argv)
                    argv[argv.index("-p") + 1] = prompt
                    argv[argv.index("-p") + 2:argv.index("-p") + 2] = ["--resume", resumed]
                else:
                    prompt = spec.prompt + "\n\n" + RECOVERY_GUIDANCE + evidence
                    argv = list(spec.argv)
                    argv[argv.index("-p") + 1] = prompt
                recovery_number = 1 + sum(r["phase"] == phase for r in result.recovery)
                spec = replace(spec, prompt=prompt, argv=argv,
                               log_path=self._driver_dir() /
                               f"{phase}-recovery-{recovery_number}.log")
                result.recovery.append({"phase": phase, "at": _now(),
                    "reason": guidance[:8000], "log": str(spec.log_path),
                    "remaining_turns": turns, "remaining_seconds": seconds,
                    "resumed_session": resumed})
                self.log(f"--- phase {phase}: autonomous recovery within remaining caps"
                         f"{' (resuming session ' + resumed + ')' if resumed else ''} ---")
                self._write_ledger(result)
            spec = replace(spec, log_path=spec.log_path.with_name(
                spec.log_path.stem + "-" + uuid.uuid4().hex + ".log"))
            if guidance:
                result.recovery[-1]["log"] = str(spec.log_path)
            execution = self._execution_budget()
            try:
                key, turns, seconds = execution.reserve(phase, self.turns_for(phase),
                    self.timeout_for(phase), log_path=spec.log_path)
            except ExecutionBudgetError as exc:
                return self._block(result, phase, str(exc))
            operation = f"coordinator-{phase}-{key}"
            spec.env["DOCPROOF_RESOURCE_PARENT_OPERATION"] = operation
            with use_context({**spec.env, "DOCPROOF_RESOURCE_PARENT_OPERATION": ""}):
                append_usage(receipt_id=key, operation_id=operation,
                    model=self.model_for(phase), transport="claude-code",
                    status="started", usage=None)
            self._progress("phase_start", phase=phase,
                           model=self.model_for(phase), effort=self.effort_for(phase),
                           max_turns=spec.max_turns, timeout_s=spec.timeout_s,
                           log_path=str(spec.log_path), resumed_session=resumed)
            before = self._questions_text()
            marker = self._progress_marker()
            started = self.clock()
            outcome = self._spawner()(spec)
            elapsed = max(0.0, self.clock() - started)
            progressed = self._progress_marker() != marker
            resume_session = outcome.session_id or ""
            used_turns, used_seconds = self._phase_usage.get(phase, (0, 0.0))
            # Missing usage is not permission to grant another full session.
            measured = outcome.num_turns
            consumed = measured if type(measured) is int and measured >= 0 else spec.max_turns
            if outcome.limit == "max_turns":
                consumed = max(consumed, spec.max_turns)
            if outcome.limit == "timeout":
                elapsed = max(elapsed, spec.timeout_s)
            self._phase_usage[phase] = (used_turns + consumed, used_seconds + elapsed)
            execution.finish(key, turns=consumed, seconds=elapsed, status="completed")
            stream = spec.log_path.with_suffix(".stream.jsonl")
            raw = parse_session_result(stream.read_text("utf-8", errors="replace")) if stream.is_file() else None
            with use_context({**spec.env, "DOCPROOF_RESOURCE_PARENT_OPERATION": ""}):
                record_claude_result(raw or {}, operation_id=operation, receipt_id=key,
                                     model=self.model_for(phase))
            result.phases.append(outcome)
            self._progress("phase_end", phase=phase, ok=outcome.ok,
                           returncode=outcome.returncode, limit=outcome.limit,
                           num_turns=outcome.num_turns, progressed=progressed)
            review_snapshot = (self.astra_review and phase in ("verify", "settle")
                               and self._review_snapshot_available())
            try:
                problem = self._phase_problem(phase, spec, outcome, review_snapshot)
            except UsageLimitError as exc:
                # A saved build never makes a failed reader/settler successful.
                # The agent pauses the shared subscription before more phases.
                self._block(result, phase, str(exc))
                raise
            except CredentialsError:
                self._write_ledger(result)
                self._progress("credentials", phase=phase, tail=outcome.tail[-600:])
                raise
            notes = self._new_question(before)
            if not unattended:
                if problem:
                    return self._stop(result, phase, problem)
                if notes and self.question_gate:
                    result.asked = True
                    return self._stop(result, phase,
                        f"phase {phase} escalated a question in QUESTIONS.md:\n{notes[:1200]}")
                return None
            # Give a prematurely stopped session a concrete continuation. A
            # completed recovery that leaves final notes does not ask again.
            triage = bool(notes and self.question_gate and not guidance)
            if problem or triage:
                capped = outcome.limit in ("max_turns", "timeout")
                if problem and capped and progressed and \
                        self._grant_continuation(phase, execution, problem):
                    guidance = problem
                    if notes:
                        guidance += "\nLocal notes:\n" + notes
                    continue
                remaining_turns, remaining_seconds = self._remaining_phase_budget(phase)
                if recoveries_left > 0 and remaining_turns > 0 and remaining_seconds > 0:
                    recoveries_left -= 1
                    guidance = problem or "Resolve the local notes and complete this phase."
                    if notes:
                        guidance += "\nLocal notes:\n" + notes
                    continue
            if problem:
                result.recovery_exhausted = True
                return (self._block if self.execution_mode == "code" else self._stop)(result, phase,
                    f"Automatic recovery could not complete the required work: {problem}")
            return None

    def run(self) -> DriveResult:
        # Invalid setup raises without writing an outcome for the
        # manuscript.
        if self.execution_mode not in {None, "session", "code"}:
            raise DriverError("execution_mode must be session or code")
        if self.execution_mode == "code" and (not self.mechanical_only or not self.astra_review):
            raise DriverError("Code orchestration requires mechanical proofreading and final Astra review")
        if not 1 <= self.review_rounds <= 2 or self.review_calls <= 0 or self.review_output_tokens <= 0:
            raise DriverError("Review requires 1–2 rounds and positive call/output budgets")
        phases = select_phases(mechanical_only=self.mechanical_only,
                               start=self.start_phase, only=self.only_phases,
                               astra_review=self.astra_review)
        ws = seed_workspace(self.book, self.slug,
                            workspace_root=self.workspace_root,
                            source_id=self.source_id,
                            on_source_change=self.on_source_change)
        self.resolve_execution_mode()
        if self.execution_mode == "code" and (not self.mechanical_only or not self.astra_review):
            raise DriverError("Saved code orchestration requires mechanical proofreading and final Astra review")
        if not self.astra_review:
            from galley.outcome import requires_astra_review
            prior = self._final_run()
            if prior is not None and requires_astra_review(prior):
                raise DriverError("This run is enrolled in final Astra review; "
                                  "--no-astra-review cannot bypass its evidence.")
        else:
            from docproof.utils.files import write_atomic
            from galley.outcome import ASTRA_REQUIRED_NAME
            settings = astra_review_settings(self._final_run() or ws,
                transport=self.astra_transport, max_chunk_bytes=self.astra_chunk_bytes)
            write_atomic(ws / ASTRA_REQUIRED_NAME, json.dumps(settings, indent=2))
        direct = {"astra_review", "certify", "deliver"} if self.astra_review else set()
        env = build_env(self.env, wrapbin=self.wrapbin) \
            if any(p not in direct for p in phases) else {}
        result = DriveResult(workspace=ws)
        self._execution_budget().reconcile(parse_session_result)

        gate_due = "approve" in phases and not (
            self.approve == "manual" and "profile" not in phases)
        for phase in phases:
            if phase == "astra_review":
                stopped = self._run_astra_review(result)
                if stopped is not None:
                    return stopped
                continue
            if self.astra_review and phase in ("certify", "deliver"):
                stopped = self._run_final_phase(phase, result)
                if stopped is not None:
                    return stopped
                continue
            if phase == "approve" and gate_due:
                if not self.run_gate(result, env):
                    return result
            from galley.engine_phases import PHASES as CODE_PHASES
            stopped = (self._run_code_phase(phase, env, result)
                       if self.execution_mode == "code" and phase in CODE_PHASES
                       else self._run_session_phase(phase, env, result))
            if stopped is not None:
                return stopped
            if phase == "settle" and not self.astra_review:
                unconverged = self.settle_verdict()
                if unconverged:
                    self.unconverged = unconverged
                    self.log(f"NOT CONVERGED at settle: {unconverged}")
                    self._progress("unconverged", phase=phase, reason=unconverged[:600])
            self._write_ledger(result)

        try:
            result.outcome, result.reason = self._final_verdict(result)
        except Exception as e:                               # noqa: BLE001
            return self._block(result, "deliver", str(e))
        if "deliver" in phases:
            try:
                result.handoff = self.run_handoff()
            except DriverError as e:
                return self._stop(result, "deliver", f"hand-off failed: {e}")
            if self.drive_folder_id:
                try:
                    if self.astra_review:
                        package = json.loads((self._driver_dir() / "package.json").read_text("utf-8"))
                        result.uploaded = publish_verified_handoff(
                            package, self.drive_folder_id,
                            self._driver_dir() / "delivery.json",
                            source_id=self.source_id or self.slug,
                            upload=self.upload, verify=self.verify_upload)
                    else:
                        uploader = self.upload or _default_upload
                        result.uploaded = uploader(result.handoff, self.drive_folder_id)
                except Exception as e:                      # noqa: BLE001
                    return self._stop(
                        result, "deliver",
                        f"the hand-off files are in "
                        f"{self.handoff_dir or (ws / 'handoff')} but the Drive "
                        f"upload failed ({e}). Sign in with `docproof-watch "
                        f"auth`, then re-run `docproof galley drive … --from "
                        f"deliver`, or put the files in folder "
                        f"{self.drive_folder_id} by hand.")
            if self.astra_review:
                from galley.state_machine import RunStateMachine
                machine = RunStateMachine.load(ws / "state.json")
                if not machine.reached("delivered"):
                    previous = machine.history[-1]
                    machine.advance("delivered", by="galley handoff",
                                    source_sha256=machine.source_sha256,
                                    config_sha256=previous.config_sha256)
                    machine.save(ws / "state.json")
        self._write_ledger(result)
        self._progress("finished", outcome=result.outcome,
                       reason=result.reason[:600])
        self.log(f"{result.outcome}: {result.reason}")
        return result

    def _final_verdict(self, result: DriveResult) -> tuple[str, str]:
        """Read the delivered outcome.json, preserving any needs_human verdict."""
        from galley.outcome import Outcome
        if self.astra_review and any(p.phase in ("astra_review", "certify", "deliver")
                                     for p in result.phases):
            from galley.outcome import astra_outcome
            run = self._final_run()
            if run is None:
                raise DriverError("No final Astra-reviewed run is available.")
            verdict = astra_outcome(run)
            return verdict.outcome, verdict.reason
        if self.execution_mode == "code":
            return "phases_complete", "Requested phases completed; final editorial clearance has not run."
        for candidate in self._outcome_sources():
            payload = Outcome.load(candidate.parent)
            if payload is not None and payload.outcome:
                if self.unconverged and payload.outcome != "needs_human":
                    self._write_outcome(self.unconverged)
                    self._write_outcome(self.unconverged, self.workspace / "deliverable")
                    return "needs_human", self.unconverged
                return payload.outcome, payload.reason
        if self.unconverged:
            self._write_outcome(self.unconverged)
            return "needs_human", self.unconverged
        return "done", (f"{len(result.phases)} phase(s) completed; no "
                        f"outcome.json was written by the run")

    def _outcome_sources(self) -> list[Path]:
        """Where the run's own outcome.json may be, newest first."""
        ws = self.workspace
        found = [p for p in (ws / "deliverable" / "outcome.json",) if p.is_file()]
        runs = sorted((ws / "runs").glob("*/outcome.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return found + runs


    def run_handoff(self) -> list[Path]:
        out = Path(self.handoff_dir) if self.handoff_dir \
            else self.workspace / "handoff"
        if self.astra_review:
            from galley.outcome import astra_outcome
            from galley.verify import deliverable_docx
            from galley.manifest import sha256_file
            run = self._enroll_astra()
            verdict = astra_outcome(run)
            frozen = self._packaged_files()
            if frozen:
                return frozen
            original = deliverable_docx(run)
            copied = deliverable_docx(self.workspace / "deliverable")
            if not original or not copied or sha256_file(original) != sha256_file(copied):
                raise DriverError("The handoff manuscript differs from the Astra-reviewed build.")
            verdict.save(run)
            verdict.save(self.workspace / "deliverable")
        # Render the log from the artifacts being handed off.
        self.write_decision_log()
        files = build_handoff(self.workspace, self.book.name, out,
                              outcome_sources=self._outcome_sources())
        if self.astra_review:
            from docproof.utils.files import write_atomic
            from galley.astra_review import validate_receipt
            receipt = validate_receipt(run)
            write_atomic(self._driver_dir() / "package.json", json.dumps({
                "schema_version": 1, "packet_sha256": receipt["packet_sha256"],
                "run": str(run), "source_id": self.source_id or self.slug,
                "build_sha256": sha256_file(original),
                "outcome": verdict.outcome, "reason": verdict.reason,
                "artifacts": [{"path": str(path), "name": path.name,
                               "sha256": sha256_file(path)} for path in files],
            }, indent=2, ensure_ascii=False))
        return files

    def _packaged_files(self) -> list[Path]:
        """Reuse a committed, hash-checked package on delivery-only retries."""
        path = self._driver_dir() / "package.json"
        if not path.exists():
            return []
        from galley.astra_review import validate_receipt
        from galley.manifest import sha256_file
        from galley.verify import deliverable_docx
        run = self._final_run()
        if run is None:
            raise DriverError("A packaged run has lost its final build identity.")
        receipt = validate_receipt(run)
        package = json.loads(path.read_text("utf-8"))
        manuscript = deliverable_docx(run)
        if (receipt["packet_sha256"] != package.get("packet_sha256")
                or manuscript is None
                or sha256_file(manuscript) != package.get("build_sha256")):
            raise DriverError("The pinned package belongs to a different reviewed build.")
        artifacts = package.get("artifacts") or []
        human = package.get("kind") == "human_review"
        if (human and receipt["review"]["editorial_verdict"] != "needs_human") or (
                not human and not receipt.get("delivery_ready")):
            raise DriverError("The pinned package conflicts with the final editorial verdict.")
        if len(artifacts) < (3 if human else 8):
            raise DriverError("The pinned package is incomplete.")
        files = []
        for item in artifacts:
            artifact = Path(item["path"])
            if not artifact.is_file() or sha256_file(artifact) != item["sha256"]:
                raise DriverError(f"Packaged artifact changed or is missing: {artifact.name}")
            files.append(artifact)
        return files


def _default_ask(subject: str, body: str, book: str) -> str:
    from app.watch.notify import send_question
    from app.watch.settings import default_watch_home
    return send_question(default_watch_home(), subject, body, book=book)



def handoff_base(source_name: str, stage: str = HANDOFF_STAGE) -> str:
    """Use watcher naming rules to produce Book 2 or Book Two, matching the
    source spelling.
    """
    from app.watch import naming
    return naming.stage_base(Path(source_name).stem, stage)


# Workspace filenames accepted for handoff.
_LETTER_NAMES = ("letter.md", "EDITORS_LETTER.md")
_STYLE_NAMES = ("style-sheet.md", "STYLE_SHEET.md")
_JOURNAL_NAMES = (DECISION_LOG_NAME, "decision-log.md")
_VERIFICATION_NAMES = ("verification.md", "VERIFICATION.md")
_AUTHOR_LETTER_NAMES = ("author-letter.docx",)


def build_handoff(workspace: str | Path, source_name: str,
                  handoff_dir: str | Path, *,
                  outcome_sources: Iterable[Path] = (),
                  partial: bool = False) -> list[Path]:
    """Copy the manuscript, author letter, editor's letter, style sheet,
    decision log, verification report, and outcome into the handoff directory
    under house names — and derive the clean copy (`<base> - clean.docx`:
    every change accepted, every comment removed) from the manuscript beside
    it, so the folder holds both the record and the reading text.

    A complete handoff requires all eight files. With partial=True, copy
    available files and require only outcome.json.
    """
    ws = Path(workspace)
    out = Path(handoff_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = handoff_base(source_name)
    deliverable = ws / "deliverable"

    from galley.verify import deliverable_docx
    docx = deliverable_docx(deliverable)
    if docx is None and not partial:
        raise DriverError(
            f"no tracked-changes .docx in {deliverable} — the deliver phase "
            f"must copy the certified build there before hand-off")

    letter = _first_existing(deliverable, _LETTER_NAMES)
    if letter is None and not partial:
        raise DriverError(
            f"no editor's letter in {deliverable} (looked for "
            f"{', '.join(_LETTER_NAMES)}) — render it with `docproof galley "
            f"letter`")
    style = _first_existing(deliverable, _STYLE_NAMES)
    if style is None and not partial:
        raise DriverError(
            f"no style sheet in {deliverable} (looked for "
            f"{', '.join(_STYLE_NAMES)}) — render it with `docproof galley "
            f"letter`")
    journal = _first_existing(deliverable, _JOURNAL_NAMES)
    if journal is None and not partial:
        raise DriverError(
            f"no decision log in {deliverable} (looked for "
            f"{', '.join(_JOURNAL_NAMES)}) — render it with `docproof galley "
            f"journal RUN --workspace {ws}`")
    verification = _first_existing(deliverable, _VERIFICATION_NAMES)
    if verification is None and not partial:
        raise DriverError(
            f"no verification report in {deliverable} (looked for "
            f"{', '.join(_VERIFICATION_NAMES)}) — `docproof galley letter "
            f"RUN --workspace {ws}` renders it beside the letter")
    outcome = next((p for p in outcome_sources if Path(p).is_file()), None)
    if outcome is None:
        raise DriverError(
            f"no outcome.json for {ws} — `docproof galley settle` writes it "
            f"beside the run's findings; deliver copies it to deliverable/")
    # The author reads Word and nothing else: the tracked-changes file and
    # this letter are the whole delivery from their side.
    author_letter = _first_existing(deliverable, _AUTHOR_LETTER_NAMES)
    if author_letter is None and not partial:
        raise DriverError(
            f"no author letter in {deliverable} (looked for "
            f"{', '.join(_AUTHOR_LETTER_NAMES)}) — `docproof galley letter "
            f"RUN --workspace {ws} --source …` renders it beside the letter")

    pairs = ((docx, f"{base}.docx"),
             (author_letter, f"{base} - Author Letter.docx"),
             (letter, f"{base} - letter.md"),
             (style, f"{base} - style-sheet.md"),
             (journal, f"{base} - decision-log.md"),
             (verification, f"{base} - verification.md"),
             (Path(outcome), f"{base} - outcome.json"))
    written: list[Path] = []
    for src, name in pairs:
        if src is None:
            continue
        dest = out / name
        shutil.copy2(src, dest)
        written.append(dest)
        if src is docx:
            clean = _clean_copy(dest, out / f"{base}{CLEAN_SUFFIX}.docx",
                                partial=partial)
            if clean is not None:
                written.append(clean)
    return written


def _clean_copy(tracked: Path, dest: Path, *, partial: bool) -> Path | None:
    """The accepted-changes reading copy, derived from the hand-off's own
    tracked-changes file. A complete hand-off cannot ship without it — the
    folder would be missing the file the next reader opens first — but a
    partial one (a stopped run's evidence) carries whatever it can."""
    from docproof.cleancopy import CleanCopyError, write_clean_copy
    try:
        return write_clean_copy(tracked, dest)
    except CleanCopyError as e:
        if partial:
            log.warning("no clean copy for the partial hand-off (%s)", e)
            return None
        raise DriverError(
            f"could not derive the clean copy from {tracked.name} ({e}) — "
            f"the hand-off needs both the tracked-changes file and its "
            f"accepted-changes reading copy") from e


def _first_existing(folder: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        p = folder / name
        if p.is_file():
            return p
    return None


_MIME = {".docx": ("application/vnd.openxmlformats-officedocument"
                   ".wordprocessingml.document"),
         ".md": "text/markdown",
         ".json": "application/json",
         ".zip": "application/zip"}


DIAGNOSTICS_SUFFIX = " - diagnostics.zip"
#: Files bigger than this stay out of the bundle (the raw stream-json of a
#: long session can run to hundreds of MB; its readable rendering is enough).
DIAGNOSTICS_MAX_FILE_BYTES = 25 * 1024 * 1024


def diagnostics_sources(workspace: str | Path) -> list[tuple[Path, str]]:
    """What a needs_human bundle carries: (path, name inside the zip)."""
    ws = Path(workspace)
    picks: list[tuple[Path, str]] = []

    def add(path: Path) -> None:
        if not path.is_file():
            return
        try:
            if path.stat().st_size > DIAGNOSTICS_MAX_FILE_BYTES:
                return
        except OSError:
            return
        picks.append((path, path.relative_to(ws).as_posix()))

    for name in ("PLAN.md", "QUESTIONS.md", "state.json", "profile.json",
                 "approval.json"):
        add(ws / name)
    runs = ws / "runs"
    add(runs / "outcome.json")
    driver = runs / DRIVER_DIR
    if driver.is_dir():
        for path in sorted(driver.iterdir()):
            # The readable transcripts and the ledger; not the raw stream.
            if path.suffix in (".log", ".json") and \
                    not path.name.endswith(".stream.jsonl"):
                add(path)
    for run in sorted(p for p in runs.glob("*") if p.is_dir()
                      and p.name != DRIVER_DIR):
        for name in ("outcome.json", "settlement.json", "verify.json",
                     "certificate.json", "review.log", "run.log"):
            add(run / name)
    return picks


def build_diagnostics(workspace: str | Path, source_name: str,
                      handoff_dir: str | Path, *,
                      extra: Iterable[tuple[Path, str]] = ()) -> Path | None:
    """Zip the run's evidence beside the hand-off as
    `<base> - diagnostics.zip`. Returns None when there is nothing to bundle
    or the bundle cannot be written — a diagnostics failure never replaces the
    verdict it accompanies."""
    ws = Path(workspace)
    out = Path(handoff_dir)
    picks = diagnostics_sources(ws)
    for path, name in extra:
        if Path(path).is_file():
            picks.append((Path(path), name))
    if not picks:
        return None
    dest = out / f"{handoff_base(source_name)}{DIAGNOSTICS_SUFFIX}"
    try:
        out.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, name in picks:
                zf.write(path, name)
    except Exception:                                       # noqa: BLE001
        log.warning("could not write the diagnostics bundle %s", dest,
                    exc_info=True)
        return None
    return dest


def live_progress(workspace: str | Path, phase: str | None) -> dict[str, Any]:
    """What a running phase has done so far, read off the files the session
    writes as it goes — the raw stream (turns, last activity) and the run's
    settlement.json (rounds). Cheap enough to call every minute."""
    ws = Path(workspace)
    out: dict[str, Any] = {}
    if phase:
        stream = ws / "runs" / DRIVER_DIR / f"{phase}.stream.jsonl"
        try:
            st = stream.stat()
            out["last_activity_at"] = datetime.fromtimestamp(
                st.st_mtime, timezone.utc).isoformat()
            out["stream_bytes"] = st.st_size
            turns = 0
            with open(stream, "rb") as fh:
                for line in fh:
                    if b'"type":"assistant"' in line or \
                            b'"type": "assistant"' in line:
                        turns += 1
            out["turns"] = turns
        except OSError:
            pass
    rounds = 0
    for path in (ws / "runs").glob("*/settlement.json"):
        data = _read_json(path)
        if isinstance(data, dict):
            rounds = max(rounds, int(data.get("rounds") or 0))
    if rounds:
        out["settle_rounds"] = rounds
    return out


def drive_token(*, get_key=None) -> str:
    """A Drive access token from the CLI's own Google sign-in.

    The same credentials `docproof-watch` runs on — the keystore's refresh
    token and the watcher's client id/secret — so a Mac-side process reaches
    Drive without a server and without a second sign-in. A setup gap raises
    with the command that fixes it, because the two answers ("no client" and
    "not signed in") have two different fixes."""
    from app.settings import get_api_key
    from app.watch import drive
    from app.watch.settings import GOOGLE_KEY, WatchSettings, default_watch_home

    ws = WatchSettings.load(default_watch_home())
    if not (ws.client_id and ws.client_secret):
        raise DriverError("Google sign-in is not set up — run "
                          "`docproof-watch init` then `docproof-watch auth`")
    refresh = (get_key or get_api_key)(GOOGLE_KEY)
    if not refresh:
        raise DriverError("DocProof is not signed in to Google — run "
                          "`docproof-watch auth`")
    return drive.refresh_access_token(ws.client_id, ws.client_secret, refresh)


def publish_verified_handoff(package: dict[str, Any], folder_id: str,
                             ledger_path: Path, *, source_id: str,
                             upload: Callable | None = None,
                             verify: Callable | None = None) -> list[str]:
    """Publish a frozen package once, checkpointing each verified remote file.

    The outcome is the watcher's commit marker and is uploaded last. A retry
    adopts matching remote files after an ambiguous response, then verifies
    bytes before acknowledging delivery. Tests inject both upload and verify.
    """
    import hashlib
    from app.watch import drive
    from docproof.utils.files import write_atomic
    from galley.manifest import sha256_file
    packet = str(package["packet_sha256"])
    records = list(package.get("artifacts") or [])
    if not records:
        raise DriverError("No package artifacts were recorded.")
    records.sort(key=lambda row: row["name"].endswith(" - outcome.json"))
    for row in records:
        path = Path(row["path"])
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise DriverError(f"The frozen artifact changed: {row['name']}")
    if upload is not None and verify is None:
        raise DriverError("A custom uploader must supply remote content verification.")
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text("utf-8"))
        if ledger.get("packet_sha256") != packet or ledger.get("folder_id") != folder_id:
            raise DriverError("Delivery receipts belong to a different packet or folder.")
    else:
        ledger = {"schema_version": 1, "packet_sha256": packet,
                  "folder_id": folder_id, "status": "pending", "artifacts": {}}
    ledger["status"] = "pending"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        write_atomic(ledger_path, json.dumps(ledger, indent=2, ensure_ascii=False))

    save()  # A pending delivery is durable before any network request.
    token = drive_token() if upload is None else None
    if upload is None and not token:
        raise DriverError("Google Drive sign-in is unavailable for delivery.")
    listing = drive.list_folder(token, folder_id) if token else []
    by_id = {item.id: item for item in listing}
    ids = []
    for row in records:
        path = Path(row["path"])
        old = ledger["artifacts"].get(row["name"], {})
        if old and old.get("sha256") != row["sha256"]:
            raise DriverError(f"Delivery receipt hash changed for {row['name']}")
        file_id = str(old.get("file_id") or "")
        if token and file_id and file_id not in by_id:
            raise DriverError(f"Previously uploaded file is no longer in the folder: {row['name']}")
        if not file_id and token:
            matches = [item for item in listing if item.name == row["name"]
                       and item.app_properties.get("galley_packet") == packet
                       and item.app_properties.get("galley_sha256") == row["sha256"]
                       and item.app_properties.get("galley_source") == source_id]
            if len(matches) > 1:
                raise DriverError(f"Multiple remote copies match {row['name']}; reconcile them before retry.")
            if matches:
                file_id = matches[0].id
        if not file_id:
            if token:
                file_id = drive.upload(
                    token, folder_id, path, name=row["name"],
                    mime_type=_MIME.get(path.suffix.lower(), "application/octet-stream"),
                    app_properties={"galley_packet": packet,
                                    "galley_sha256": row["sha256"],
                                    "galley_source": source_id})
            else:
                uploaded = upload([path], folder_id)
                if not uploaded or len(uploaded) != 1 or not str(uploaded[0]).strip():
                    raise DriverError(f"No upload id returned for {row['name']}")
                file_id = str(uploaded[0])
        ledger["artifacts"][row["name"]] = {
            "sha256": row["sha256"], "file_id": file_id, "verified": False}
        save()  # Retain the id even if the subsequent read-back fails.
        confirmed = (hashlib.sha256(drive.download_bytes(
            token, file_id, what="verify a handoff artifact")).hexdigest() == row["sha256"]
                     if token else bool(verify(path, file_id, folder_id)))
        if not confirmed:
            raise DriverError(f"Remote content verification failed for {row['name']}")
        ledger["artifacts"][row["name"]]["verified"] = True
        save()
        ids.append(file_id)
    ledger.update(status="delivered", acknowledged_at=_now())
    save()
    return ids


def _default_upload(files: list[Path], folder_id: str) -> list[str]:
    """Upload handoff files using the watcher's stored Google credentials.
    Raise on setup or upload failure.
    """
    from app.watch import drive

    token = drive_token()
    ids: list[str] = []
    for path in files:
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
        ids.append(drive.upload(token, folder_id, path, name=path.name,
                                mime_type=mime))
    return ids


__all__ = [
    "ALL_PHASES", "COPYEDIT_PHASES", "DECISION_LOG_NAME", "DEFAULT_BUDGET_USD",
    "DEFAULT_EFFORT", "DEFAULT_MAX_TURNS", "DEFAULT_MODEL", "DIAGNOSTICS_SUFFIX",
    "EDIT_GUARDED_PHASES", "ITEM_SCALED_PHASES", "RECOVERY_FLOOR_S",
    "RECOVERY_FLOOR_TURNS", "RECOVERY_GRANT_SHARE", "RECOVERY_MAX_GRANTS",
    "SETTLE_ITEMS_BASELINE", "edit_guard_settings", "items_factor",
    "session_transcript",
    "DEFAULT_PERMISSION_MODE", "EFFORT_LEVELS", "MECHANICAL_MODEL",
    "DEFAULT_PHASE_TIMEOUT_S", "DEFAULT_WORKSPACE_ROOT", "DEFAULT_WRAPBIN",
    "HANDOFF_STAGE", "MECHANICAL_PHASES", "PHASE_EFFORT", "PHASE_MAX_TURNS",
    "PHASE_MODEL", "PHASE_TIMEOUT_S",
    "REQUIRED_STATE", "SETTLE_QUIET_FLOOR", "SETTLE_QUIET_SHARE",
    "SETTLE_ROUNDS", "TIMEOUT_RC", "DriveResult", "Driver", "DriverError",
    "PhaseResult", "PhaseSpec", "PlanSummary", "build_env", "build_handoff",
    "config_copyedit_lanes", "detect_turn_cap", "drive_token",
    "gate_decision", "gate_question",
    "handoff_base",
    "build_diagnostics", "diagnostics_sources", "live_progress",
    "phase_prompt", "phases_for", "read_plan", "record_approval",
    "reply_after", "seed_workspace", "select_phases", "settle_flags",
    "spawn_claude", "tail_of", "SourceChanged", "workspace_slug",
    "CredentialsError", "detect_credential_failure",
    "transcript_tail", "parse_session_result", "session_limit",
    "DRIVER_LINE_PREFIX", "RESULT_SUBTYPE_MAX_TURNS",
]
