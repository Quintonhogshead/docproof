# Running the practitioner

The practitioner is a headless Claude Code session whose working directory is
a **per-book workspace** seeded from this directory. DocProof is its tool; the
session drives the `docproof` CLI and (optionally) `galley.orchestrator` as
instruments.

## Workspace layout

```
workspace/<book-slug>/
  CLAUDE.md          <- copied from galley/practitioner/CLAUDE.md
  KNOBS.md           <- copied from galley/practitioner/KNOBS.md
  .claude/skills/    <- copied from galley/practitioner/skills/
  .claude/settings.local.json   (seeded once: Bash allowlist for docproof +
                                 grep/head/tail/wc/ls/cat runs/*; never
                                 overwritten)
  source/<book>.docx
  profile.json       (written by /profile)
  PLAN.md            (written by /draft-plan, human-approved at the gate)
  approval.json      (written by `docproof galley approve` from the plan)
  state.json         (the run state machine — `galley state`, advanced with
                      BOTH --source and --config at every stage)
  runs/              (each docproof run's --out dir; *.log slices)
  runs/driver/       (the driver's per-phase logs + driver.json ledger)
  runs/outcome.json  (written by the driver when a run stops: needs_human + why)
  deliverable/       (final docx + letter.md + style-sheet.md + outcome.json)
  handoff/           (the four files DocWatch picks up; see below)
```

Flush the workspace at intake of a new book. Persistent memory — the
`galley_memory.db` store (house rulings, precedents, authors) and the
calibration file — lives OUTSIDE the workspace and carries across books.

## Launch

For a whole book unattended, use `docproof galley drive` (below). `launch.sh`
starts the intake phase only, with a person watching:

```bash
BOOK=/path/to/Book.docx SLUG=book-slug ./launch.sh
```

`launch.sh` builds the workspace, copies the manual, KNOBS, and skills, seeds
the permission allowlist, drops the source in place, and starts a headless
session with the intake prompt. The session then follows the loop in
CLAUDE.md: profile → plan → **gate (it stops and waits for a human reply)** →
execute → audit → adjudicate → verify → **settle** → certify → deliver, ending
at one of two stopping points recorded in `outcome.json`: `done`, or
`needs_human` with the reason (for DocWatch to flip the HubSpot toggle).

### Key isolation (both launchers)

The brain runs on the Claude Max **subscription** (`claude setup-token` once,
then `export CLAUDE_CODE_OAUTH_TOKEN=<token>`); the launcher refuses to start
without that token, because the fallback is the API key — real dollars for
every brain turn. The API keys reach only the docproof sifter processes: the
launcher strips `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from the brain's
environment (`env -u`) and puts `~/galley-bin/docproof` first on PATH — a
wrapper that sources `~/.docproof-eval.env` and execs the venv's `docproof`
against the main checkout. The brain never sees a key; a sifter never runs
without one (it fails loudly instead). `DOCPROOF_CANDIDATE_APPLY=1` is
exported for this deployment only (candidate screening may apply; production
keeps the shadow floor).

## Nobody at the keyboard: `docproof galley agent`

`galley drive` runs one book. The **agent** is the long-running process that
decides which book, and when — so the whole proofing loop runs with nobody
watching.

DocWatch (on Fly, `proof_runner = external`) finds `<surname> - Book 1.docx` at
"Ready for Proofing", marks the manuscript `awaiting` and emails. It cannot run
Galley itself: the brain is a Claude Max subscription, which lives on one
machine of ours. The agent is what closes that gap.

```
every --poll-interval (default 5 min):
    GET <app>/api/watch/awaiting          (bearer token, read-only)
    for the first book this machine has not already claimed:
        download the Book 1 with the watcher's own Google sign-in
        run galley.driver over it — --approve auto, $10, mechanical only
        the driver's hand-off uploads the Book 2 set to the author's folder
DocWatch's next tick reads the outcome.json and moves HubSpot on.
```

**One book at a time, once each, always with an answer.** A local ledger
(`~/galley-workspaces/.agent-state.json`) records every Drive file id claimed,
finished or failed, and the claim is written *before* the work — so a crash
resumes the same workspace from the phase its `state.json` reached rather than
paying for a whole novel twice, and a book that failed is never retried. A
download that fails, a driver that crashes and a run that stops all write a
`needs_human` outcome.json naming what went wrong and upload it (with the
decision log) to the author's folder, so a book is never left silent at "Ready
for Proofing".

### One-time setup

1. **Give the brain a subscription token** — the agent never spends API dollars
   on the practitioner's own thinking.

   ```bash
   claude setup-token          # prints a token; copy it
   ```

2. **Set the server's half of the shared secret.** On Fly:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"   # keep this
   fly secrets set DOCPROOF_AGENT_TOKEN=<that value> -a atmosphere-docproof
   ```

   Without it the server answers *nobody* on `/api/watch/awaiting`; a value
   shorter than 24 characters is refused as not a secret.

3. **Write the machine's credentials file** — never the shell, because a
   service manager passes almost no environment and a token in a shell profile
   is a token in every process.

   ```bash
   mkdir -p ~/.galley && touch ~/.galley/agent.env && chmod 600 ~/.galley/agent.env
   ```

   ```ini
   CLAUDE_CODE_OAUTH_TOKEN=<from step 1>
   GALLEY_APP_URL=https://atmosphere-docproof.fly.dev
   GALLEY_AGENT_TOKEN=<the same value as step 2>
   ```

   The agent refuses to start if that file is readable by any other account.
   Anything else you put in it rides through to the driver's environment.

4. **Sign in to Google**, so the agent can download the Book 1 and upload the
   Book 2 set:

   ```bash
   docproof-watch auth
   ```

   On macOS that stores the refresh token in the Keychain and nothing more is
   needed. **On Linux there is usually no keyring backend**, so `get_api_key`
   falls back to the environment: add the refresh token to the same
   credentials file instead —

   ```ini
   GOOGLE_REFRESH_TOKEN=<the refresh token>
   ```

   — and the agent puts it into its own environment at startup, which is where
   `app/watch/drive.py` looks first on every platform.

5. **Install the service.**

   ```bash
   docproof galley agent --install
   ```

   A launchd LaunchAgent (`~/Library/LaunchAgents/com.atmosphere.galley-agent.plist`)
   on macOS; a systemd user unit (`~/.config/systemd/user/galley-agent.service`,
   enabled with lingering so it survives logout) on Linux. Same command line,
   same credentials file, same log: `~/galley-workspaces/agent.log`. It also
   refreshes `~/galley-bin/galley-run.sh` from this repo's copy, keeping the old
   one beside it as `.bak`.

   If `loginctl enable-linger` is refused on Linux (it needs root), the unit
   still runs for the session — finish it with
   `sudo loginctl enable-linger $USER`.

### Day to day

| Command | What it does |
|---|---|
| `docproof galley agent --once` | one poll, then exit — the way to test the setup |
| `docproof galley agent --status` | what this machine has claimed, finished and failed |
| `docproof galley agent --uninstall` | stop and remove the service |
| `docproof galley agent --drive-folder-id ID` | deliver every hand-off to one folder instead of the author's — a rehearsal |

`--budget` overrides the $10 per-book API ceiling; `--poll-interval` the five
minutes. The log and the ledger both live under `--workspace-root` (default
`~/galley-workspaces`), beside the per-book workspaces themselves.

## Unattended: `docproof galley drive`

The whole loop with nobody watching, one book at a time — what the agent calls.

```bash
docproof galley drive --book "/path/Book.docx" --slug book-slug \
    --approve auto --budget 20 --drive-folder-id <folder>
```

It seeds the workspace exactly as `launch.sh` does, then runs `profile →
approve → sweeps → ladder → audit → verify → settle → certify → deliver`, each
as its own lean headless session with the phase prompts below — which now live
in `galley/driver.py`, the single source of truth (`--print-prompt PHASE`
prints one). **Go-live scope is mechanical proofreading only**, so `flights`
and `reread` are not run; `--copyedit` re-opens them for an experiment.

| Flag | What it does |
|---|---|
| `--approve auto` | approve the plan when its `TOTAL` is inside `--budget` (default **$10**, the API ceiling) and no line puts a copy-edit lane in scope; otherwise stop |
| `--approve email` | send the plan through `galley ask` and poll `QUESTIONS.md` for `APPROVED`/`DECLINED` below the marker, until `--reply-timeout` (6h) |
| `--approve manual` | today's behaviour: stop at the gate for a person |
| `--from PHASE` / `--phases …` | restart from, or run only, these phases (`state.json` is the ledger) |
| `--handoff DIR` | where the DocWatch hand-off is written (default `<workspace>/handoff/`) |
| `--drive-folder-id ID` | also upload the hand-off there, using the watcher's own Google sign-in (`docproof-watch auth`) |
| `--no-state-gate` | don't require each phase to have advanced `state.json` |
| `--no-question-gate` | don't stop when a phase appends an escalation to `QUESTIONS.md` |
| `--max-turns N` / `--phase-max-turns PHASE=N` | turn cap per session (default: settle 400, verify 250, ladder 100, 60-150 elsewhere) |
| `--timeout HOURS` / `--phase-timeout PHASE=HOURS` | wall-clock cap per session (default 2h; ladder 3h, verify/settle 4h) |
| `--settle-rounds N` / `--settle-quiet-floor N` / `--settle-quiet-share S` | the settle policy (default 3 / 4 / 0) |

**Stopping.** A phase that exits nonzero stops the driver, writes
`runs/outcome.json` as `needs_human` naming the phase and its last log lines,
and leaves the workspace alone — nothing is retried or worked around. The same
happens when the gate refuses, when an emailed gate gets no reply, when a
phase hits its turn cap or wall-clock timeout, when the settle sweep is still
noisy at its round ceiling, when a phase exits 0 without advancing the state
machine, and when a phase appends an escalation to `QUESTIONS.md` (`galley ask` exits 0, so the file is what says a
session asked something nobody is there to answer). Exit codes: **0** finished,
**7** stopped needing a human (read `runs/outcome.json`), **2** a setup error.
Per-phase logs and the driver's own ledger are in `runs/driver/`.

**The DocWatch hand-off.** After `deliver`, five files land in the hand-off
directory under the house series. That series is `Book Original` (what the
author sends) -> `book 0` (formatting) -> `Book 1` (the developmental edit) ->
`Book 2` (this), so a proofread reads `<surname> - Book 1.docx` and hands back
`<surname> - Book 2.*`; the surname is read by `app/watch/naming.py`, the same
transform DocWatch looks for them with.

```
<surname> - Book 2.docx                the tracked-changes proofread manuscript
<surname> - Book 2 - letter.md         the editor's letter
<surname> - Book 2 - style-sheet.md    the style sheet
<surname> - Book 2 - decision-log.md   every action taken, and why
<surname> - Book 2 - outcome.json      done | needs_human, with the reason
```

Both outcomes move the book on in HubSpot — `done` to `Proofing Complete`,
`needs_human` to `Needs Human PR` — so nothing is left sitting at
`Ready for Proofing` for nobody to notice. DocWatch owns that write.

**The decision log** (`galley/journal.py`) is rendered from the run's own
artifacts: driver events and the gate decision, the plan's G-items and how they
were resolved, each sweep and its count, every applied edit with the
explanation its detector recorded, every withheld row with its status, every
author query, the verify verdicts, the settle records round by round, the
certify checks, and the outcome with its evidence. Grouped by phase, then by
paragraph; a phase that never ran is a one-line "not run" section. It is
deterministic and reads no clock, so `docproof galley journal RUN --workspace
WS --out FILE` regenerates it byte for byte at any time. The driver writes it
to `deliverable/DECISION_LOG.md` at hand-off — and also when a run STOPS, so a
workspace someone picks up always says what was decided and why it stopped.

With `--drive-folder-id` they are also uploaded with the watcher's own Drive
credentials. If sign-in is missing the driver stops and says so; the files are
already in the hand-off directory, so the manual fallback is to run
`docproof-watch auth` and re-run `docproof galley drive … --from deliver`, or
to put those four files in the Drive folder by hand.

## The phase loop (`galley-run.sh`)

For a person driving one phase at a time. `galley/practitioner/galley-run.sh`
(install it over `~/galley-bin/galley-run.sh`) is now a thin wrapper over
`docproof galley drive --phases "$PHASE" --approve manual`.

One phase per lean `-p` session, so no session carries the whole book's
context (cache-read scales with context × turns). Each phase reads its inputs
from workspace files and advances the state machine.

| `PHASE=` | What the session does | Spend |
|---|---|---|
| `profile` | Intake: open the state machine, `/profile` + `/draft-plan`, write `profile.json` + `PLAN.md`, STOP at the plan gate | $0 |
| `approve` | From the human-approved `PLAN.md`: materialize the run config, print `galley routes`, write `approval.json` (`galley approve --budget`). Every later paid verb runs with `--approval approval.json` | $0 |
| `sweeps` | Author the bespoke sweeps, dry-run, gate on blast radius, `--apply` | $0 |
| `ladder` | The mechanical wave: `docproof review … --approval` to `runs/ladder/` | paid |
| `flights` | **TABLED for go-live (copy-edit scope).** Copy-edit flights on the proofread text (`/flight-deck`, `--approval --budget`) | paid judge |
| `audit` | `/audit`: density table, sampled pages, hypotheses to `runs/audit.json`. On a mechanical run the hypotheses are recorded and no paid re-read follows | one call |
| `reread` | **TABLED for go-live (copy-edit scope).** Gated wave-2: targeted chapter × error-class re-reads from the audit's hypotheses, under the marginal-cost ceiling; refuses without recorded approval | paid |
| `verify` | `galley verify --context BRIEF --approval` (`--dry-run` first for the call count; `--engine subagent` for the $0 lane): re-reads every applied edit + proofreads the accepted text; nonzero on a flagged edit or a high-severity residual | paid / $0 |
| `settle` | `/settle`: `galley settle RUN --source --config --engine auto` closes every item verify raised (absorb / add / drop / query), rebuilding at $0 and re-verifying the touched paragraphs until nothing is open; writes `settlement.json` + `outcome.json`; `galley state --advance settled --results RUN` | $0 / judge |
| `certify` | `galley certify --approval --source --config`: the delivery gate; every check must PASS (now including terminal states, residual settlement, outcome) | $0 |
| `deliver` | Requires `runs/certify.txt` PASSED, else stops. The CERTIFIED build is what ships — deliver copies it, renders letter + style sheet to `deliverable/`, and records the outcome; it never rebuilds after certify (adjudicate + merge-desk run BEFORE verify) | $0 |

`PROMPT="…"` overrides a phase's prompt; `MODEL`/`PERM` override the brain
model and permission mode.

## Two integration depths

1. **Session-as-brain (this directory).** The session plans and calls CLI
   verbs; the orchestrator's Python hooks are unused. Maximum judgment,
   human-gated. This is the mode the skills document.
2. **Hooks-as-brain (`galley/brain.py`).** `make_auditor`/`make_planner` give
   `run_galley` a mechanical audit→plan loop with no session at all — the app
   path uses this for T2+ jobs. Cheaper, bounded, no taste. The session may
   still be layered on top for the gates and the adjudication.
