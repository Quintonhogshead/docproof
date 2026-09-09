"""Run Galley phases with plan approval, subprocess limits, state checks, and
artifact handoff.

Each phase runs in a separate session. Mechanical mode excludes copyediting
phases; failures produce a needs_human outcome.
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.watch.naming import CLEAN_SUFFIX, PROOF_STAGE as HANDOFF_STAGE
from docproof import agent_lane
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
    "certify": "certified",
    "deliver": "delivered",
}

# API spending ceiling recorded in approval.json.
DEFAULT_BUDGET_USD = 10.0
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
        "Intake: manuscript at source/{book}. Read CLAUDE.md's Context "
        "discipline first. Open the state machine (`docproof galley state . "
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
        "KNOBS.md for the sweep contract. Author all sweep files in one batch, "
        "dry-run each (`docproof sweep IN --rule F > runs/sweep_<key>.txt`; "
        "read only the match summary), and apply only the sweeps PLAN.md "
        "approved — a sweep whose blast radius exceeds the plan's figure is an "
        "escalation (`docproof galley ask`), not a judgment call. Do NOT read "
        "sweeps.py or the manuscript whole."),
    "ladder": (
        "Phase: mechanical ladder. The six-window chapter sweep is wave 1 "
        "line 1 — `chapter_sweep` on Luna in the run config (the "
        "mechanical-wave stage enables it) PLUS the six-window Sonnet $0 "
        "session-subagent sweep, imported before the ladder's own findings. "
        "Write the run config from PLAN.md + "
        "KNOBS.md (config REPLACES default.yaml — restate every section). Run "
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
        "Run `docproof galley settle runs/<final> --source source/{book} "
        "--config <run config> --engine subagent --approval approval.json "
        "{settle_flags} > "
        "runs/settle.log 2>&1` (the config is the $0 replay config the final "
        "build used; run from the workspace root so a relative "
        "intent_zones_file resolves). Those flags are REQUIRED and exact: "
        "sweep until a round comes back quiet, at most {settle_rounds} "
        "round(s); a round raising fewer than {settle_noisy} new item(s) is "
        "quiet and the book is done. If the last round is still noisy the book "
        "needs a human proofreader — report that, do not sweep again. A "
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
    return prompt + _UNATTENDED_NOTE


# Appended to every phase prompt. The manual says it too (directive 6), but a
# brain that has just read a 65k-word intake reaches for `galley ask` the way
# a person would reach for a colleague, and unattended there is no colleague.
_UNATTENDED_NOTE = (
    " UNATTENDED RUN: nobody is on the other end of `docproof galley ask` — "
    "the driver sees a new QUESTIONS.md entry and ends the run as "
    "needs_human with your question as the reason. So do not ask; decide, "
    "and record the decision in the decision log. Escalate only what "
    "genuinely blocks the book: an unreadable source, a tool that fails, a "
    "cap you would exceed. Anything a proofreader would put to the author "
    "goes in the deliverable as a margin query, not to `galley ask`.")


def phases_for(mechanical_only: bool = True) -> tuple[str, ...]:
    return MECHANICAL_PHASES if mechanical_only else ALL_PHASES


def select_phases(*, mechanical_only: bool = True, start: str | None = None,
                  only: Sequence[str] | None = None) -> list[str]:
    """The phases this invocation will run, in order.

    ``only`` names them explicitly (still ordered, still scope-checked);
    ``start`` slices the default order from that phase onward."""
    order = phases_for(mechanical_only)
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
    #: | "credentials" (never signed in — the token, not the book).
    limit: str | None = None
    # Structured CLI result, including subtype and turn count.
    subtype: str = ""
    num_turns: int | None = None

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
    """Detect max_turns from the structured result, falling back to the
    transcript when no result exists.
    """
    if result is not None and result.get("is_error"):
        blob = " ".join(str(result.get(k) or "")
                        for k in ("result", "error", "message", "subtype"))
        if detect_credential_failure(blob):
            return "credentials"
    if detect_credential_failure(tail):
        return "credentials"
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
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path = spec.log_path.with_suffix(".stream.jsonl")
    with open(spec.log_path, "w", encoding="utf-8") as fh:
        fh.write(f"{DRIVER_LINE_PREFIX}: phase {spec.phase} at {_now()} "
                 f"(max-turns {spec.max_turns}, timeout "
                 f"{spec.timeout_s / 3600:.1f}h)\n")
        fh.flush()
        timed_out = False
        with open(stream_path, "w", encoding="utf-8") as raw:
            try:
                proc = subprocess.run(spec.argv, cwd=str(spec.workspace),
                                      env=spec.env, stdout=raw,
                                      stderr=subprocess.STDOUT, text=True,
                                      timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
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
    return PhaseResult(spec.phase, proc.returncode, spec.log_path, tail,
                       limit=limit,
                       subtype=str((result or {}).get("subtype") or ""),
                       num_turns=(result or {}).get("num_turns"))


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
    outcome: str = "done"                 # "done" | "needs_human"
    reason: str = ""
    stopped_at: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    handoff: list[Path] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.outcome == "done" else 7

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
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    #: Told about every step (phase start/end, the gate, the stop, the finish)
    #: as a dict with an "event" key — the agent turns these into heartbeats.
    #: A reporter that raises never sinks the run.
    progress: Callable[[dict[str, Any]], None] | None = None
    #: Set when settlement ran out of rounds while still finding errors. The
    #: book is needs_human, but the run keeps going: the verdict rides out
    #: with the finished hand-off rather than in place of it.
    unconverged: str = field(default="", init=False, repr=False)


    @property
    def workspace(self) -> Path:
        return Path(str(self.workspace_root)).expanduser() / self.slug

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

    def turns_for(self, phase: str) -> int:
        if phase in self.max_turns_by_phase:
            return int(self.max_turns_by_phase[phase])
        if self.max_turns is not None:
            return int(self.max_turns)
        base = PHASE_MAX_TURNS.get(phase, DEFAULT_MAX_TURNS)
        return int(round(base * self.length_factor_for(phase)))

    def model_for(self, phase: str) -> str:
        if phase in self.model_by_phase:
            return str(self.model_by_phase[phase])
        if self.model:
            return str(self.model)
        return PHASE_MODEL.get(phase, DEFAULT_MODEL)

    def effort_for(self, phase: str) -> str | None:
        """The session's --effort, or None to leave Claude Code's default."""
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
        return base * self.length_factor_for(phase)

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
        argv += [
                # Preserve the structured completion beside the readable
                # log.
                "--output-format", "stream-json", "--verbose"]
        # The phase rides in the environment so any verb the brain runs can
        # say who ran it. `galley outcome --set` reads it: an overrule made
        # by the deliver-phase brain is recorded as exactly that, not as
        # "human" (the first Fly delivery's outcome.json, decision log and
        # HubSpot value all claimed a person overruled settle; nobody had).
        return PhaseSpec(phase=phase, prompt=prompt, workspace=self.workspace,
                         log_path=self._driver_dir() / f"{phase}.log",
                         argv=argv, env={**env, BRAIN_PHASE_ENV: phase},
                         max_turns=turns, timeout_s=self.timeout_for(phase))

    def _questions_text(self) -> str:
        try:
            return (self.workspace / "QUESTIONS.md").read_text(encoding="utf-8")
        except OSError:
            return ""

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
        result.outcome = "needs_human"
        result.reason = reason
        result.stopped_at = phase
        self._write_outcome(reason)
        self._write_ledger(result)
        # Preserve the decision log for a stopped run, and hand over the book
        # itself if the run got far enough to edit one.
        self.salvage_deliverable()
        self.write_decision_log()
        # Return a stopped run's outcome even when no manuscript was
        # produced.
        self._stopped_handoff(result)
        self.log(f"STOPPED at {phase or 'the plan gate'}: {reason}")
        self._progress("stopped", phase=phase, outcome="needs_human",
                       reason=reason[:600])
        return result

    def _stopped_handoff(self, result: DriveResult) -> None:
        """Build and upload available artifacts after a stopped run. Log
        handoff failures without replacing the original error.
        """
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

    def salvage_deliverable(self) -> None:
        """Fill deliverable/ from the run's own artifacts when a stop cut the
        deliver phase short.

        A run that stopped after the book was edited still edited the book.
        Handing back an outcome and a decision log leaves the manuscript —
        the thing the next person actually works from — stranded in the
        workspace, so copy the built tracked-changes file over and render the
        letters that explain it. Every failure here is logged and swallowed:
        the verdict ships either way.
        """
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
                    self.log(f"salvaged the edited manuscript from "
                             f"runs/{run.name} for the stopped run")
                except OSError as e:                        # noqa: PERF203
                    self.log(f"could not salvage the manuscript ({e})")
        self.salvage_letters(run, out)

    def salvage_letters(self, run: Path, out: Path) -> None:
        """Render the letter, style sheet, verification report and author
        letter for a run that never reached deliver. Anything already there
        was rendered by the run itself and is left alone."""
        want = ((_LETTER_NAMES, "letter"), (_STYLE_NAMES, "style sheet"),
                (_VERIFICATION_NAMES, "verification report"),
                (_AUTHOR_LETTER_NAMES, "author letter"))
        if all(_first_existing(out, names) for names, _label in want):
            return
        try:
            from galley.casefile_synth import casefile_from_run, workspace_waves
            from galley.letter import (render_all, render_author_letter,
                                       render_verification_report, run_evidence)
            cf = casefile_from_run(run)
            # The real spend is the workspace's, not this one run's.
            waves = workspace_waves(self.workspace)
            if waves:
                cf.waves = list(waves)
                cf.budget.charges = []
                for wave in waves:
                    cf.budget.charge(f"run {wave.index}", wave.spend_usd,
                                     wave=wave.index)
            evidence = run_evidence(run, self.workspace)
            title = handoff_base(self.book.name)
            render_all(cf, out, evidence=evidence, title=title)
            render_verification_report(evidence, out, cf=cf, title=title)
            render_author_letter(cf, out, evidence=evidence, title=title)
            self.log(f"rendered the letters for the stopped run from "
                     f"runs/{run.name}")
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
        """Write the driver's verdict and its reason to runs/outcome.json, or
        to `where` — the deliverable, when the driver overrules a verdict the
        run wrote for itself and the hand-off must carry the correction."""
        from galley.outcome import Outcome, hubspot_fields
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
        stream = self.workspace / "runs" / DRIVER_DIR / f"{phase}.stream.jsonl"
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
        path.write_text(json.dumps(result.to_json(), indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        return path


    def run_gate(self, result: DriveResult) -> bool:
        """Decide the plan gate. Returns True to continue into `approve`."""
        plan_path = self.workspace / "PLAN.md"
        if self.approve == "manual":
            result.gate = {"policy": "manual", "approved": False}
            self._stop(result, "approve",
                       "plan gate: --approve manual — a human must approve "
                       f"{plan_path} and then resume with `--from approve`")
            return False
        plan = read_plan(plan_path)
        config_path = self.workspace / "runs" / "mech.yaml"
        approved, reason = gate_decision(
            plan, self.budget_usd,
            config_path=config_path if config_path.is_file() else None)
        result.gate = {"policy": self.approve, "approved": approved,
                       "reason": reason,
                       "total_usd": plan.total_usd,
                       "copyedit_lines": list(plan.copyedit_lines),
                       "config_lanes": config_copyedit_lanes(config_path)
                       if config_path.is_file() else []}
        if approved:
            record_approval(plan_path, reason, by="galley drive (auto)")
            self.log(f"plan gate: APPROVED — {reason}")
            self._progress("gate", approved=True, reason=reason[:300])
            return True
        if self.approve == "auto":
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


    def run(self) -> DriveResult:
        # Invalid setup raises without writing an outcome for the
        # manuscript.
        phases = select_phases(mechanical_only=self.mechanical_only,
                               start=self.start_phase, only=self.only_phases)
        ws = seed_workspace(self.book, self.slug,
                            workspace_root=self.workspace_root,
                            source_id=self.source_id,
                            on_source_change=self.on_source_change)
        env = build_env(self.env, wrapbin=self.wrapbin)
        result = DriveResult(workspace=ws)

        gate_due = "approve" in phases and not (
            self.approve == "manual" and "profile" not in phases)
        for phase in phases:
            if phase == "approve" and gate_due:
                if not self.run_gate(result):
                    return result
            effort = self.effort_for(phase)
            factor = self.length_factor_for(phase)
            scaled = (f", x{factor:.1f} for {self.book_words():,} words"
                      if factor > 1.0 else "")
            self.log(f"--- phase {phase} ({self.model_for(phase)}"
                     f"{', effort ' + effort if effort else ''}{scaled}) ---")
            spec = self._spec(phase, env)
            self._progress("phase_start", phase=phase,
                           model=self.model_for(phase), effort=effort,
                           max_turns=spec.max_turns, timeout_s=spec.timeout_s,
                           log_path=str(spec.log_path))
            asked_before = self._questions_text()
            outcome = self._spawner()(spec)
            result.phases.append(outcome)
            self._progress("phase_end", phase=phase, ok=outcome.ok,
                           returncode=outcome.returncode, limit=outcome.limit,
                           num_turns=outcome.num_turns)
            if outcome.limit == "timeout":
                return self._stop(
                    result, phase,
                    f"phase {phase} hit its wall-clock cap of "
                    f"{spec.timeout_s / 3600:.1f}h and was killed — a session "
                    f"that long is looping, not working; last lines of "
                    f"{outcome.log_path}:\n{outcome.tail}")
            if outcome.limit == "max_turns":
                return self._stop(
                    result, phase,
                    f"phase {phase} hit its turn cap of {spec.max_turns} "
                    f"(claude --max-turns) — last lines of "
                    f"{outcome.log_path}:\n{outcome.tail}")
            if outcome.limit == "credentials":
                # Not a verdict on the book: nothing ran, the ledger did not
                # move, and the same phase resumes once the token is fixed.
                self._write_ledger(result)
                self._progress("credentials", phase=phase,
                               tail=outcome.tail[-600:])
                self.log(f"HALTED at {phase}: the brain could not sign in")
                raise CredentialsError(
                    f"phase {phase} could not sign in to Claude Code — the "
                    f"subscription token (CLAUDE_CODE_OAUTH_TOKEN) is expired "
                    f"or revoked; last lines of {outcome.log_path}:\n"
                    f"{outcome.tail}")
            if not outcome.ok:
                return self._stop(
                    result, phase,
                    f"phase {phase} exited {outcome.returncode}; last lines of "
                    f"{outcome.log_path}:\n{outcome.tail}")
            asked = self._new_question(asked_before)
            if asked and self.question_gate:
                # galley ask exits zero; an unanswered escalation must still
                # stop unattended work.
                return self._stop(
                    result, phase,
                    f"phase {phase} escalated a question and there is nobody "
                    f"to answer it:\n{asked[:1200]}")
            if phase == "settle":
                unconverged = self.settle_verdict()
                if unconverged:
                    # A book that will not converge is needs_human — but the
                    # human who picks it up needs the edited manuscript, not a
                    # log of one. Record the verdict and keep going: certify
                    # and deliver still run, and the hand-off ships whole.
                    self.unconverged = unconverged
                    self.log(f"NOT CONVERGED at settle: {unconverged}")
                    self._progress("unconverged", phase=phase,
                                   reason=unconverged[:600])
            need = REQUIRED_STATE.get(phase) if self.state_gate else None
            if need and not self._state_reached(need):
                # Name the cause we have actually seen, because "did not
                # advance the ledger" describes the symptom and cost four
                # rounds of log archaeology to trace the first time.
                backgrounded = self.backgrounded_work(phase)
                extra = (f" The session left work running in the background "
                         f"({backgrounded}) and ended anyway, which kills that "
                         f"work mid-flight: a long read must run in the "
                         f"foreground."
                         if backgrounded else "")
                return self._stop(
                    result, phase,
                    f"phase {phase} exited 0 but the run state machine is at "
                    f"{self._current_state() or 'nothing'!r}, not {need!r} — "
                    f"the session did not advance the ledger, so the next "
                    f"phase would build on an unproven one.{extra}")
            self._write_ledger(result)

        result.outcome, result.reason = self._final_verdict(result)
        if "deliver" in phases:
            try:
                result.handoff = self.run_handoff()
            except DriverError as e:
                return self._stop(result, "deliver", f"hand-off failed: {e}")
            if self.drive_folder_id:
                uploader = self.upload or _default_upload
                try:
                    result.uploaded = uploader(result.handoff,
                                               self.drive_folder_id)
                except Exception as e:                      # noqa: BLE001
                    return self._stop(
                        result, "deliver",
                        f"the hand-off files are in "
                        f"{self.handoff_dir or (ws / 'handoff')} but the Drive "
                        f"upload failed ({e}). Sign in with `docproof-watch "
                        f"auth`, then re-run `docproof galley drive … --from "
                        f"deliver`, or put the files in folder "
                        f"{self.drive_folder_id} by hand.")
        self._write_ledger(result)
        self._progress("finished", outcome=result.outcome,
                       reason=result.reason[:600])
        self.log(f"{result.outcome}: {result.reason}")
        return result

    def _final_verdict(self, result: DriveResult) -> tuple[str, str]:
        """Read the delivered outcome.json, preserving any needs_human verdict.

        A settlement that never converged is needs_human whatever the run
        wrote for itself: the driver counted the rounds."""
        from galley.outcome import Outcome
        for candidate in self._outcome_sources():
            payload = Outcome.load(candidate.parent)
            if payload is not None and payload.outcome:
                if self.unconverged and payload.outcome != "needs_human":
                    self._write_outcome(self.unconverged)
                    self._write_outcome(self.unconverged,
                                        self.workspace / "deliverable")
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
        # Render the log from the artifacts being handed off.
        self.write_decision_log()
        return build_handoff(self.workspace, self.book.name, out,
                             outcome_sources=self._outcome_sources())


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
