"""The unattended driver — one book, start to hand-off, no human in the loop.

Until now a person ran ONE phase per invocation (``PHASE=ladder galley-run.sh``),
answered the plan gate in chat, and carried the deliverable somewhere by hand.
This module sequences the phases, decides the gate under a stated policy, stops
the moment anything goes wrong, and puts the finished files where DocWatch can
see them.

Three things it is deliberately NOT:

* **A second brain.** Each phase is still its own lean headless session with the
  same prompt the shell launcher used — those prompts now live HERE, in the
  repo, as the single source of truth (``galley/practitioner/galley-run.sh`` is
  a thin wrapper over ``docproof galley drive``). The driver decides *which*
  session runs next and *whether* the run may continue; it never edits the
  book.
* **A new ledger.** ``state.json`` (galley/state_machine.py) remains the record
  of where a run is. The driver reads it to prove a phase did what its prompt
  said, and ``--from PHASE`` restarts against it. ``runs/driver.json`` is a log
  of the driver's own decisions, not a source of truth.
* **A gate that can be talked past.** ``--approve auto`` approves only a plan
  that is BOTH inside the budget and mechanical-only; anything else escalates
  (``email``) or stops (``manual``). A phase that exits nonzero stops the run
  and writes ``runs/outcome.json`` as ``needs_human`` naming the phase and the
  last lines of its log — nothing is retried, nothing is worked around.

**Scope (owner, 2026-09-03): go-live Galley is MECHANICAL PROOFREADING ONLY.**
``flights`` (the copy-edit deck), the merge desk, and the gated wave-2
``reread`` are tabled; the driver refuses to run them in mechanical mode, the
phase prompts say so, and ``galley approve --mechanical-only`` + ``certify``
enforce it on the artifacts.

Session-spawning is one injectable function (``spawn_claude``) so the whole
sequence — gate policy, failure handling, state advancement, hand-off naming —
is testable with a fake spawner against a temp workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# --- the phase order ---------------------------------------------------------

#: Every phase the practitioner loop knows, in order.
ALL_PHASES: tuple[str, ...] = (
    "profile", "approve", "sweeps", "ladder", "flights", "audit", "reread",
    "verify", "settle", "certify", "deliver",
)
#: The go-live order: mechanical proofreading only.
MECHANICAL_PHASES: tuple[str, ...] = (
    "profile", "approve", "sweeps", "ladder", "audit", "verify", "settle",
    "certify", "deliver",
)
#: Phases that belong to the copy-edit scope, tabled for go-live.
COPYEDIT_PHASES: tuple[str, ...] = ("flights", "reread")

#: The state each phase must have reached by the time it exits 0. A phase whose
#: session returned success without advancing the ledger did not do its job —
#: the driver stops rather than build the next phase on an unproven one.
#: ``sweeps`` and ``verify`` have no state of their own in RUN_STATES.
REQUIRED_STATE: dict[str, str] = {
    "profile": "intake",
    "approve": "plan_approved",
    "ladder": "mechanical_complete",
    "audit": "audited",
    "settle": "settled",
    "certify": "certified",
    "deliver": "delivered",
}

#: The API ceiling for one book (Quinton, 2026-09-03). It is not advice: the
#: driver passes it to `galley approve`, so `approval.json` carries it as
#: `max_spend_usd` and every paid verb REFUSES (exit 5) past it — which stops
#: the driver as needs_human rather than overspending quietly.
DEFAULT_BUDGET_USD = 10.0
DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_PERMISSION_MODE = "acceptEdits"
DEFAULT_WORKSPACE_ROOT = "~/galley-workspaces"
DEFAULT_WRAPBIN = "~/galley-bin"
#: The stage token the proofread deliverable carries in the hand-off. The house
#: series is Book Original -> book 0 (formatting) -> Book 1 (the developmental
#: edit) -> Book 2 (this); app/watch/naming.py owns it, and this is a re-export
#: so the two sides of the hand-off cannot drift. The numbered stages are
#: written either way — a `Book One` source hands back a `Book Two` — which
#: `naming.stage_base` decides, not this constant.
HANDOFF_STAGE = "Book 2"
#: Where the driver leaves its own log and ledger inside the workspace.
DRIVER_DIR = "driver"
#: The decision log Galley ships beside the manuscript (galley/journal.py).
DECISION_LOG_NAME = "DECISION_LOG.md"

#: Runaway protection on the subscription side. A headless session that loops
#: spends no API dollars but burns the Max subscription and the wall clock all
#: the same, so every phase carries BOTH a turn cap (`claude --max-turns`) and
#: a wall-clock timeout on the subprocess. Hitting either ends the run as
#: needs_human naming the phase and the cap — never silently.
DEFAULT_MAX_TURNS = 100
PHASE_MAX_TURNS: dict[str, int] = {
    "profile": 80,      # scans + PLAN.md, no spend
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
#: Wall-clock seconds per phase. Two hours is the floor for a whole-book
#: command; the ladder runs a full review, and verify/settle re-read the book.
DEFAULT_PHASE_TIMEOUT_S = 2 * 3600.0
PHASE_TIMEOUT_S: dict[str, float] = {
    "ladder": 3 * 3600.0,
    "verify": 4 * 3600.0,
    "settle": 4 * 3600.0,
}
#: What a session says when it stopped because the turn cap was reached. Read
#: off the phase log, because an exit code alone does not distinguish "hit the
#: cap" from "the command failed".
_TURN_CAP_RE = re.compile(r"max(?:imum)?[ _-]?turns?\b|turn limit",
                          re.IGNORECASE)
#: The conventional exit code for "killed by a timeout".
TIMEOUT_RC = 124

#: The settle policy (Quinton, 2026-09-03). The until-clean sweep runs at most
#: SETTLE_ROUNDS rounds; a round raising SETTLE_QUIET_FLOOR new items or FEWER
#: is quiet and the book is done. `--quiet-floor` is inclusive in
#: galley/settle.py (`new_items <= quiet_floor`), so "fewer than five" is a
#: floor of 4. The percentage rule is switched off (`--quiet-share 0` makes
#: `new_items <= 0` the only share that counts) so the absolute number decides
#: alone. If the last permitted round is still noisy, the book needs a human.
SETTLE_ROUNDS = 3
SETTLE_QUIET_FLOOR = 4
SETTLE_QUIET_SHARE = 0.0
#: How many trailing log lines a failure reason carries.
TAIL_LINES = 20

# The keys the brain must never see: the whole point of the launcher's
# isolation is that the Claude session runs on the Max subscription and only
# the docproof sifter subprocesses (re-keyed by the ~/galley-bin wrapper) spend
# API dollars.
STRIPPED_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


class DriverError(RuntimeError):
    """A setup problem the driver refuses to start on. The message is the fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- the phase prompts (the single source of truth) --------------------------

# Copied verbatim from ~/galley-bin/galley-run.sh, which is now a thin wrapper
# over `docproof galley drive`. `{book}` is the source basename inside the
# workspace. Each prompt reads its inputs from workspace files and stays lean:
# no session carries the whole book's context, because cache-read scales with
# context x turns.
_PROMPTS: dict[str, str] = {
    "profile": (
        "Intake: manuscript at source/{book}. Read CLAUDE.md's Context "
        "discipline first. Open the state machine (`docproof galley state . "
        "--advance intake --source source/{book} --config CONFIG` — always "
        "BOTH flags). Run /profile then /draft-plan (both $0). Write "
        "profile.json and PLAN.md. STOP at the plan gate — do not spend past "
        "it."),
    "approve": (
        "Phase: freeze the approved plan. Read PLAN.md (the plan has been "
        "approved — the approval line is at the bottom of the file). "
        "Materialize the run config it names (`docproof galley genre-pack … "
        "--out runs/mech.yaml` if not already written), print the egress "
        "report (`docproof galley routes source/{book} --config runs/mech.yaml "
        "> runs/routes.txt`; read it back), then write the immutable manifest: "
        "`docproof galley approve source/{book} --config runs/mech.yaml "
        "--budget {budget} --out approval.json`. The cap is ${budget} of API "
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
        "Phase: mechanical ladder. Write the run config from PLAN.md + "
        "KNOBS.md (config REPLACES default.yaml — restate every section). Run "
        "`docproof review … --approval approval.json` to runs/ladder/ with "
        "output redirected to runs/ladder.log; read only the summary + counts "
        "+ the dollar line. Confirm findings.checkpoint.json exists before "
        "finish(). Advance the state machine (--source and --config). Report "
        "applied/query counts and spend."),
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
        "accepted text as the author will read it. Read only the summary line "
        "and the WARNING/ERROR lines of runs/<final>/run.log; if "
        "finished_walk.json lists unread_paragraphs, re-run with --paragraphs "
        "on them (@FILE) to a side dir and say so. Do NOT hand-fix anything it "
        "raises — that is the settle phase. Advance the state machine "
        "(--source and --config). Report residual/problem counts."),
    "settle": (
        "Phase: residual settlement — zero open candidates. Follow /settle. "
        "Run `docproof galley settle runs/<final> --source source/{book} "
        "--config <run config> --engine subagent {settle_flags} > "
        "runs/settle.log 2>&1` (the config is the $0 replay config the final "
        "build used; run from the workspace root so a relative "
        "intent_zones_file resolves). Those flags are REQUIRED and exact: "
        "sweep until a round comes back quiet, at most {settle_rounds} "
        "round(s); a round raising fewer than {settle_noisy} new item(s) is "
        "quiet and the book is done. If the last round is still noisy the book "
        "needs a human proofreader — report that, do not sweep again. Read the "
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
        "artifact scan, change-verify, finished-walk). A FAIL blocks delivery: "
        "fix the failing check, never ship around it. Advance the state "
        "machine to certified (--source and --config). Report the "
        "certificate."),
    "deliver": (
        "Phase: deliver the CERTIFIED build. PRECONDITION: runs/certify.txt "
        "shows PASSED and runs/<final>/settlement.json has open: [] — read "
        "both; if either is missing or failing, STOP and say which. Do NOT "
        "rebuild anything after certify: copy the certified tracked-changes "
        "docx from runs/<final> to deliverable/, render the editor's letter + "
        "style sheet (`docproof galley letter`) to deliverable/ as letter.md "
        "and style-sheet.md, and copy outcome.json beside them (its outcome — "
        "done or needs_human — and reason go in the letter's closing "
        "paragraph). Advance the state machine to delivered (--source and "
        "--config). Report final spend, change/comment counts, and the "
        "outcome."),
}

# Appended to the phases where the scope decision changes what the session may
# do. Kept as one sentence per phase: the manual (CLAUDE.md) carries the
# doctrine, the prompt only has to make the boundary unmissable in a session
# that never reads a human's chat message.
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
    """The prompt for one phase's headless session.

    ``book`` is the manuscript's basename inside ``source/``; ``budget_usd`` is
    the API ceiling the `approve` phase freezes into ``approval.json``. In
    mechanical mode a phase the scope forbids has no prompt at all — asking for
    one is a programming error, not a runtime choice."""
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
    return prompt


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


# --- the workspace -----------------------------------------------------------

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


def seed_workspace(book: str | Path, slug: str, *,
                   workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
                   source_dir: Path | None = None) -> Path:
    """Build (or refresh) the per-book workspace, exactly as launch.sh does.

    Idempotent: the manual, KNOBS and skills are re-copied so a workspace never
    runs a stale manual, while ``runs/``, ``settings.local.json`` and anything
    the session wrote are preserved — a workspace's own edits win."""
    src = Path(book).expanduser()
    if not src.is_file():
        raise DriverError(f"no manuscript at {src}")
    root = Path(str(workspace_root)).expanduser()
    ws = root / slug
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
    return ws


def build_env(base: dict[str, str] | None = None, *,
              wrapbin: str | Path = DEFAULT_WRAPBIN) -> dict[str, str]:
    """The brain's environment: subscription token in, API keys OUT, the
    docproof wrapper first on PATH.

    Refuses without ``CLAUDE_CODE_OAUTH_TOKEN`` for the reason the shell
    launcher does — the fallback is the API key, which is real dollars for
    every brain turn."""
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
    # Candidate screening may APPLY in this deployment only; production keeps
    # the shadow floor (see launch.sh).
    env["DOCPROOF_CANDIDATE_APPLY"] = "1"
    return env


# --- spawning ----------------------------------------------------------------

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
    #: Which runaway cap ended this session, if either: "timeout" | "max_turns".
    limit: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.limit


def detect_turn_cap(text: str) -> bool:
    """Whether a phase log says the session stopped at its turn cap."""
    return bool(_TURN_CAP_RE.search(text or ""))


def spawn_claude(spec: PhaseSpec) -> PhaseResult:
    """Run one phase as its own headless `claude -p` session, teeing its output
    to the phase log, under the phase's turn and wall-clock caps. The default
    spawner; injected over in tests."""
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(spec.log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# galley driver: phase {spec.phase} at {_now()} "
                 f"(max-turns {spec.max_turns}, timeout "
                 f"{spec.timeout_s / 3600:.1f}h)\n")
        fh.flush()
        try:
            proc = subprocess.run(spec.argv, cwd=str(spec.workspace),
                                  env=spec.env, stdout=fh,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            fh.write(f"\n# TIMEOUT: killed after {spec.timeout_s / 3600:.1f}h\n")
            return PhaseResult(spec.phase, TIMEOUT_RC, spec.log_path,
                               tail_of(spec.log_path), limit="timeout")
    tail = tail_of(spec.log_path)
    limit = "max_turns" if detect_turn_cap(tail) else None
    return PhaseResult(spec.phase, proc.returncode, spec.log_path, tail,
                       limit=limit)


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


# --- the plan gate -----------------------------------------------------------

# The plan's bottom line: "TOTAL est. $2.80 · CAP $10 · stop: …". The FIRST
# dollar figure after the word TOTAL is the total; a later "CAP $10" on the
# same line is the cap, not the spend.
_TOTAL_RE = re.compile(r"^[^\n]*\bTOTAL\b[^$\n]*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                       re.IGNORECASE | re.MULTILINE)
# A plan line that puts a copy-edit lane, the merge desk, or a wave-2 re-read
# in scope. Matched per line so the offending line can be quoted back.
_COPYEDIT_RE = re.compile(
    r"\b(copy[- ]?edit(?:ing|s)?|flight[- ]?deck|flights?|merge[- ]?desk|"
    r"smoothing|rewrite lane|wave[- ]?2|re-?read)\b", re.IGNORECASE)
# …unless the line is saying it is NOT happening. A mechanical plan is allowed
# to name what it excludes ("no copy-edit flights — mechanical only").
_NEGATED_RE = re.compile(
    r"\b(no|none|not|never|off|omitted|omit|excluded|exclude|skipped?|skip|"
    r"locked|out of scope|tabled|n/?a|zero)\b", re.IGNORECASE)
# The token the driver stamps into QUESTIONS.md so a reply is unambiguously a
# reply to THIS gate and not to an older question in the same file.
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


def read_plan(path: str | Path) -> PlanSummary:
    """Parse a drafted PLAN.md for the two facts the gate turns on: the priced
    total, and whether any line puts a copy-edit lane in scope."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise DriverError(f"no plan to gate on at {path}: {e}") from e
    totals = _TOTAL_RE.findall(text)
    total = float(totals[-1].replace(",", "")) if totals else None
    offenders = [ln.strip() for ln in text.splitlines()
                 if _COPYEDIT_RE.search(ln) and not _NEGATED_RE.search(ln)]
    return PlanSummary(total, offenders, text)


def gate_decision(plan: PlanSummary, budget_usd: float
                  ) -> tuple[bool, str]:
    """``--approve auto``'s rule: approve a plan that is priced, inside the
    budget, and mechanical-only. Returns (approved, reason)."""
    if plan.total_usd is None:
        return False, ("PLAN.md has no parseable TOTAL line — a plan the "
                       "driver cannot price is a plan it cannot approve")
    if plan.total_usd > budget_usd:
        return False, (f"the plan totals ${plan.total_usd:.2f}, over the "
                       f"${budget_usd:.2f} budget")
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
    """Log the gate question in the workspace's reply file, marker LAST.

    Everything below the marker is the human's answer, so the driver's own
    instructions — which necessarily spell APPROVED and DECLINED — can never
    be read back as a reply."""
    with open(questions_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n\n## {subject}\n\n{body}\n\n{token}\n"
                 f"(write APPROVED or DECLINED on the next line)\n")


def reply_after(text: str, token: str) -> str | None:
    """``"approved"`` / ``"declined"`` from the part of the reply file that
    follows this gate's marker, or None while nothing has been written."""
    idx = text.rfind(token)
    if idx < 0:
        return None
    # Everything after the marker is the reply; the driver writes its own
    # instructions ABOVE it precisely so they cannot be mistaken for one.
    after = text[idx + len(token):]
    approved = _APPROVED_RE.search(after)
    declined = _DECLINED_RE.search(after)
    if approved and (not declined or approved.start() < declined.start()):
        return "approved"
    if declined:
        return "declined"
    return None


# --- the run -----------------------------------------------------------------

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
    """Sequence the phases of one book, unattended.

    Every side channel is injectable: ``spawn`` runs a phase, ``ask`` sends the
    gate escalation, ``sleep``/``clock`` drive the reply poll, ``upload`` puts
    the hand-off on Drive. The defaults are the real ones."""

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
    model: str = DEFAULT_MODEL
    permission_mode: str = DEFAULT_PERMISSION_MODE
    wrapbin: Path = Path(DEFAULT_WRAPBIN)
    reply_timeout_s: float = 6 * 3600.0
    poll_interval_s: float = 30.0
    state_gate: bool = True
    question_gate: bool = True
    #: Runaway caps. `max_turns`/`timeout_s` override every phase; the two
    #: `*_by_phase` maps override one, and win over the flat override.
    max_turns: int | None = None
    max_turns_by_phase: dict[str, int] = field(default_factory=dict)
    timeout_s: float | None = None
    timeout_by_phase: dict[str, float] = field(default_factory=dict)
    #: The settle policy: at most `settle_rounds` until-clean rounds, a round
    #: raising `settle_quiet_floor` new items or fewer is quiet.
    settle_rounds: int = SETTLE_ROUNDS
    settle_quiet_floor: int = SETTLE_QUIET_FLOOR
    settle_quiet_share: float = SETTLE_QUIET_SHARE
    env: dict[str, str] | None = None
    # Resolved at call time (see `_spawner`), never bound as a dataclass
    # default: a default captured here would survive a monkeypatch of
    # `spawn_claude` and a test would spawn a REAL headless session.
    spawn: Callable[[PhaseSpec], PhaseResult] | None = None
    ask: Callable[[str, str, str], str] | None = None
    upload: Callable[[list[Path], str], list[str]] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print

    # -- helpers --

    @property
    def workspace(self) -> Path:
        return Path(str(self.workspace_root)).expanduser() / self.slug

    def _spawner(self) -> Callable[[PhaseSpec], PhaseResult]:
        return self.spawn or spawn_claude

    def _driver_dir(self) -> Path:
        d = self.workspace / "runs" / DRIVER_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def turns_for(self, phase: str) -> int:
        if phase in self.max_turns_by_phase:
            return int(self.max_turns_by_phase[phase])
        if self.max_turns is not None:
            return int(self.max_turns)
        return PHASE_MAX_TURNS.get(phase, DEFAULT_MAX_TURNS)

    def timeout_for(self, phase: str) -> float:
        if phase in self.timeout_by_phase:
            return float(self.timeout_by_phase[phase])
        if self.timeout_s is not None:
            return float(self.timeout_s)
        return PHASE_TIMEOUT_S.get(phase, DEFAULT_PHASE_TIMEOUT_S)

    def _spec(self, phase: str, env: dict[str, str]) -> PhaseSpec:
        prompt = phase_prompt(phase, self.book.name,
                              mechanical_only=self.mechanical_only,
                              budget_usd=self.budget_usd,
                              settle_rounds=self.settle_rounds,
                              settle_quiet_floor=self.settle_quiet_floor,
                              settle_quiet_share=self.settle_quiet_share)
        turns = self.turns_for(phase)
        argv = ["claude", "-p", prompt, "--model", self.model,
                "--permission-mode", self.permission_mode,
                "--max-turns", str(turns)]
        return PhaseSpec(phase=phase, prompt=prompt, workspace=self.workspace,
                         log_path=self._driver_dir() / f"{phase}.log",
                         argv=argv, env=env, max_turns=turns,
                         timeout_s=self.timeout_for(phase))

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
        """Why this book still needs a human after the settle sweep, or "".

        The policy is the sweep's own arithmetic, read back off
        ``settlement.json``: the loop is allowed `settle_rounds` rounds, and a
        round raising more than `settle_quiet_floor` new items is noisy. A
        sweep that ran out of rounds while still noisy has not converged on
        clean, and no amount of further mechanical work will fix that — it is
        a human proofreader's book."""
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
        # A stopped run still gets its decision log: what a person picking the
        # workspace up needs most is the record of what was decided before it
        # stopped, and why it stopped.
        self.write_decision_log()
        # …and it still HANDS OFF. A book that stops has an answer —
        # needs_human, with the reason — and DocWatch's contract makes
        # outcome.json the one required file precisely so that answer can come
        # back from a run that produced nothing else. Without this the stopped
        # book sits silent in the author's folder until a person notices.
        self._stopped_handoff(result)
        self.log(f"STOPPED at {phase or 'the plan gate'}: {reason}")
        return result

    def _stopped_handoff(self, result: DriveResult) -> None:
        """Best-effort partial hand-off for a run that stopped.

        Everything here is defensive: this runs on the failure path, and an
        exception raised while reporting a failure would replace a legible
        `needs_human` with a traceback. A hand-off that cannot be built or
        uploaded is logged and left on disk for a person."""
        try:
            out = Path(self.handoff_dir) if self.handoff_dir \
                else self.workspace / "handoff"
            # The driver's OWN verdict leads the list. A run that stopped at
            # certify may have a settle-written `done` beside its findings, and
            # handing that back would tell DocWatch the book is finished when
            # the run says it is not.
            result.handoff = build_handoff(
                self.workspace, self.book.name, out,
                outcome_sources=[self.workspace / "runs" / "outcome.json",
                                 *self._outcome_sources()],
                partial=True)
        except Exception as e:                              # noqa: BLE001
            self.log(f"no hand-off for the stopped run ({e})")
            return
        if not self.drive_folder_id:
            return
        uploader = self.upload or _default_upload
        try:
            result.uploaded = uploader(result.handoff, self.drive_folder_id)
        except Exception as e:                              # noqa: BLE001
            self.log(f"the stopped run's hand-off is in {out} but the Drive "
                     f"upload failed ({e}) — put those files in folder "
                     f"{self.drive_folder_id} by hand, or re-run the deliver "
                     f"phase once Google sign-in is working")

    def write_decision_log(self) -> Path | None:
        """Render `deliverable/DECISION_LOG.md` from whatever artifacts exist.

        Best-effort by design: a decision log that cannot render must never be
        the thing that sinks a finished run, so a failure is logged and the
        run carries on (the hand-off, which requires the file, will say so)."""
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

    def _write_outcome(self, reason: str) -> Path:
        """The driver's own verdict, in the place DocWatch reads: `runs/`.

        A phase's own settle-written outcome.json lives beside its findings;
        this one says the RUN could not finish, and why."""
        from galley.outcome import Outcome, hubspot_fields
        runs = self.workspace / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        return Outcome(outcome="needs_human", reason=reason,
                       evidence={"driver": True, "slug": self.slug},
                       hubspot=hubspot_fields("needs_human"),
                       set_by="galley drive").save(runs)

    def _write_ledger(self, result: DriveResult) -> Path:
        path = self._driver_dir() / "driver.json"
        path.write_text(json.dumps(result.to_json(), indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        return path

    # -- the gate --

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
        approved, reason = gate_decision(plan, self.budget_usd)
        result.gate = {"policy": self.approve, "approved": approved,
                       "reason": reason,
                       "total_usd": plan.total_usd,
                       "copyedit_lines": list(plan.copyedit_lines)}
        if approved:
            record_approval(plan_path, reason, by="galley drive (auto)")
            self.log(f"plan gate: APPROVED — {reason}")
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

    # -- the sequence --

    def run(self) -> DriveResult:
        # Setup problems (no token, an unknown or out-of-scope phase) RAISE:
        # they are a caller's mistake, not a verdict on the book, and must not
        # write a needs_human outcome DocWatch would act on.
        phases = select_phases(mechanical_only=self.mechanical_only,
                               start=self.start_phase, only=self.only_phases)
        ws = seed_workspace(self.book, self.slug,
                            workspace_root=self.workspace_root)
        env = build_env(self.env, wrapbin=self.wrapbin)
        result = DriveResult(workspace=ws)

        gate_due = "approve" in phases and not (
            self.approve == "manual" and "profile" not in phases)
        for phase in phases:
            if phase == "approve" and gate_due:
                if not self.run_gate(result):
                    return result
            self.log(f"--- phase {phase} ---")
            spec = self._spec(phase, env)
            asked_before = self._questions_text()
            outcome = self._spawner()(spec)
            result.phases.append(outcome)
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
            if not outcome.ok:
                return self._stop(
                    result, phase,
                    f"phase {phase} exited {outcome.returncode}; last lines of "
                    f"{outcome.log_path}:\n{outcome.tail}")
            asked = self._new_question(asked_before)
            if asked and self.question_gate:
                # CLAUDE.md's escalation contract is "log it in QUESTIONS.md,
                # push it, and STOP the blocked thread" — but `galley ask`
                # exits 0, so a session that escalated and carried on looks
                # exactly like one that had nothing to ask. Unattended, an
                # unanswered question must stop the run, not ride along.
                return self._stop(
                    result, phase,
                    f"phase {phase} escalated a question and there is nobody "
                    f"to answer it:\n{asked[:1200]}")
            if phase == "settle":
                unconverged = self.settle_verdict()
                if unconverged:
                    return self._stop(result, phase, unconverged)
            need = REQUIRED_STATE.get(phase) if self.state_gate else None
            if need and not self._state_reached(need):
                return self._stop(
                    result, phase,
                    f"phase {phase} exited 0 but the run state machine is at "
                    f"{self._current_state() or 'nothing'!r}, not {need!r} — "
                    f"the session did not advance the ledger, so the next "
                    f"phase would build on an unproven one")
            self._write_ledger(result)

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
        result.outcome, result.reason = self._final_verdict(result)
        self._write_ledger(result)
        self.log(f"{result.outcome}: {result.reason}")
        return result

    def _final_verdict(self, result: DriveResult) -> tuple[str, str]:
        """The run's own verdict, taken from the delivered outcome.json rather
        than invented: `galley settle` already made this call from the run's
        numbers, and the driver must not paper over a `needs_human`."""
        from galley.outcome import Outcome
        for candidate in self._outcome_sources():
            payload = Outcome.load(candidate.parent)
            if payload is not None and payload.outcome:
                return payload.outcome, payload.reason
        return "done", (f"{len(result.phases)} phase(s) completed; no "
                        f"outcome.json was written by the run")

    def _outcome_sources(self) -> list[Path]:
        """Where the run's own outcome.json may be, newest first."""
        ws = self.workspace
        found = [p for p in (ws / "deliverable" / "outcome.json",) if p.is_file()]
        runs = sorted((ws / "runs").glob("*/outcome.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return found + runs

    # -- hand-off --

    def run_handoff(self) -> list[Path]:
        out = Path(self.handoff_dir) if self.handoff_dir \
            else self.workspace / "handoff"
        # The decision log is rendered from the run's artifacts at hand-off
        # time, so it always describes the build that actually ships.
        self.write_decision_log()
        return build_handoff(self.workspace, self.book.name, out,
                             outcome_sources=self._outcome_sources())


def _default_ask(subject: str, body: str, book: str) -> str:
    from app.watch.notify import send_question
    from app.watch.settings import default_watch_home
    return send_question(default_watch_home(), subject, body, book=book)


# --- hand-off ----------------------------------------------------------------

def handoff_base(source_name: str, stage: str = HANDOFF_STAGE) -> str:
    """``"Redding - Book 1.docx"`` -> ``"Redding - Book 2"``, and
    ``"Redding - Book One.docx"`` -> ``"Redding - Book Two"``.

    Straight through ``app/watch/naming.stage_base``, so the surname is read
    exactly as the watcher reads it — dash-, case- and spacing-tolerant, and
    tolerant of whichever stage token the source happens to carry — the number's
    spelling is mirrored the same way, and the two sides of the hand-off cannot
    disagree about what a file is called."""
    from app.watch import naming
    return naming.stage_base(Path(source_name).stem, stage)


#: The hand-off contract DocWatch reads (sibling agent owns the other side):
#: the deliverable's file within the workspace, and the suffix it lands under.
_LETTER_NAMES = ("letter.md", "EDITORS_LETTER.md")
_STYLE_NAMES = ("style-sheet.md", "STYLE_SHEET.md")
_JOURNAL_NAMES = (DECISION_LOG_NAME, "decision-log.md")


def build_handoff(workspace: str | Path, source_name: str,
                  handoff_dir: str | Path, *,
                  outcome_sources: Iterable[Path] = (),
                  partial: bool = False) -> list[Path]:
    """Copy the five hand-off files into ``handoff_dir`` under house names.

    ``<surname> - Book 2.docx`` (the tracked-changes proofread manuscript),
    ``… - letter.md``, ``… - style-sheet.md``, ``… - decision-log.md``,
    ``… - outcome.json``. On a FINISHED run every one is required: a hand-off
    missing a piece is not a hand-off, so a missing file raises rather than
    shipping a partial folder.

    ``partial=True`` is the STOPPED run's hand-off. A run that died at the
    ladder has no deliverable and no letter, but it does have a verdict and a
    decision log — and DocWatch's contract makes only ``outcome.json``
    required, precisely so a stopped book still comes back with its answer
    instead of sitting silent in the folder. So a partial hand-off ships
    whatever exists, and raises only when there is no outcome at all."""
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
    outcome = next((p for p in outcome_sources if Path(p).is_file()), None)
    if outcome is None:
        raise DriverError(
            f"no outcome.json for {ws} — `docproof galley settle` writes it "
            f"beside the run's findings; deliver copies it to deliverable/")

    pairs = ((docx, f"{base}.docx"),
             (letter, f"{base} - letter.md"),
             (style, f"{base} - style-sheet.md"),
             (journal, f"{base} - decision-log.md"),
             (Path(outcome), f"{base} - outcome.json"))
    written: list[Path] = []
    for src, name in pairs:
        if src is None:
            continue
        dest = out / name
        shutil.copy2(src, dest)
        written.append(dest)
    return written


def _first_existing(folder: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        p = folder / name
        if p.is_file():
            return p
    return None


_MIME = {".docx": ("application/vnd.openxmlformats-officedocument"
                   ".wordprocessingml.document"),
         ".md": "text/markdown",
         ".json": "application/json"}


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
    """Put the hand-off in a Drive folder with the watcher's own sign-in.

    Reuses app/watch/drive.py and the CLI's stored Google refresh token — the
    same credentials `docproof-watch` runs on, no server needed. A setup gap
    raises with the command that fixes it; the caller reports the manual
    fallback (drag the files in the hand-off folder into the Drive folder)."""
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
    "DEFAULT_MAX_TURNS", "DEFAULT_MODEL", "DEFAULT_PERMISSION_MODE",
    "DEFAULT_PHASE_TIMEOUT_S", "DEFAULT_WORKSPACE_ROOT", "DEFAULT_WRAPBIN",
    "HANDOFF_STAGE", "MECHANICAL_PHASES", "PHASE_MAX_TURNS", "PHASE_TIMEOUT_S",
    "REQUIRED_STATE", "SETTLE_QUIET_FLOOR", "SETTLE_QUIET_SHARE",
    "SETTLE_ROUNDS", "TIMEOUT_RC", "DriveResult", "Driver", "DriverError",
    "PhaseResult", "PhaseSpec", "PlanSummary", "build_env", "build_handoff",
    "detect_turn_cap", "drive_token", "gate_decision", "gate_question",
    "handoff_base",
    "phase_prompt", "phases_for", "read_plan", "record_approval",
    "reply_after", "seed_workspace", "select_phases", "settle_flags",
    "spawn_claude", "tail_of",
]
