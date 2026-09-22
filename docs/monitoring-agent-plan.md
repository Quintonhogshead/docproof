# The Warden: a monitoring agent for DocProof, Galley and DocWatch

*Plan, 2026-09-22. Built the same day as v0.228.0 (`app/warden/`, the
`docproof-warden` CLI, the warden routes on the server, the skill under
`galley/practitioner/skills/warden/`); setup and daily use are in
[warden.md](warden.md). Not yet installed on the Mini.*

A Mac Mini arrives 2026-09-23. Its only job is to host Claude Code and Codex.
This plan puts one agent on it that wakes on a clock, reads the state of every
DocProof service, decides whether anything is stuck, fixes what it is allowed
to fix, asks about the rest, and keeps Quinton and the team informed. It also
takes on the one intake problem that has hurt the rollout most: author names
entered wrong, or not at all, in HubSpot.

## Decisions already made

| Question | Answer |
| --- | --- |
| Reach Quinton | iMessage from a dedicated Apple ID on the Mini |
| Reach the team | Email, sent through the DocWatch notify sign-in the Fly secrets hold |
| Read context | Quinton's inbox, read-only, filtered by a label and known senders |
| Fix authority | Tiered: safe fixes on its own, risky fixes after a yes, new code after a yes and a PR |
| Harness on day one | Claude Code headless on a separate Max/Pro account; Codex is the drop-in fallback |
| Mini's other job | The native InDesign corrections worker, in local-review mode first |
| Where the work runs | Fly keeps running DocWatch and Galley. The Mini runs nothing book-shaped except InDesign |

## The shape of it

Three layers, so the model is replaceable and the judgment is testable.

1. **`docproof warden` (Python, in this repo).** Deterministic. It gathers
   every signal into one snapshot, runs the stuck rules, and exposes the fix
   verbs. No model inside. Runs in seconds. Every verb has `--dry-run`.
2. **A skill file (`galley/practitioner/skills/warden/`).** The runbook:
   symptom, known causes, the verb that fixes each, and what it may not do.
   Written once, read by whichever harness runs the tick.
3. **The harness tick.** `launchd` runs `claude -p` (or `codex exec`) with
   the same prompt every 20 minutes. The model reads the snapshot, follows the
   runbook, calls verbs, writes to its journal, and exits. It never runs
   longer than one tick. A second `launchd` entry runs a cheaper *listener*
   tick every 2 minutes that only reads inbound iMessages and email replies
   and, when one needs the model, wakes the main tick early.

Model agnosticism is real because the model only ever sees a JSON snapshot
and a markdown runbook, and only ever acts through the CLI. Switching harness
is one line in the plist.

## What it watches

Every tick, `docproof warden snapshot` collects:

**Fly.** `fly status` and `fly machine list` for both process groups,
`/healthz`, the last 200 lines of `fly logs` per group, and the current
release versions. Deploys and machine restarts are visible here, which is how
the Warden knows *not* to touch a run (see guardrails).

**DocWatch** (via the admin API on the app group, using an admin token kept in
the Mini's Keychain). `/api/watch` for the last tick time, the schedule, the
sign-in state, per-file records and stages, and `/api/watch/awaiting` for the
books Galley should be claiming. `last_pass.json` and the completion log
answer "did the last pass do anything".

**Galley.** The agent heartbeat that `agent_heartbeat` stores in the watch
home (state, phase, book, turns, spent, `at`). The ledger
(`.agent-state.json`) and `.subscription-pause.json` read through a small new
read-only endpoint on the agent group, because `fly ssh console` on every
tick is slow and fragile. `galley outcome` for finished books.

**Native InDesign worker on the Mini itself.** `launchctl print` for the two
login agents, their logs, the interior ledger and batch receipts, held
batches and quiet-period timers, and a liveness probe that asks InDesign for
its version over Apple Events with a short timeout.

**HubSpot.** Projects (object 0-970) created or changed since the last tick,
plus the corrections form's recent submissions.

**Email.** Quinton's inbox, read-only via the Gmail API, label `DocProof`
plus senders `docwatch@`, `fly.io`, `google.com` security notices, HubSpot
notifications, and anything from the team that mentions a book or author in
the awaiting list. The notify mailbox, for replies to the Warden's own mail.

**Its own journal.** `~/.docproof-warden/journal.sqlite`: every finding, every
action, every message sent and received, every open approval. This is what
stops it re-alerting the same thing every 20 minutes and what lets it say "I
already tried X at 3:40".

## Stuck rules

Deterministic, in `warden/rules.py`, each with a name the runbook refers to.
Thresholds live in `config/warden.yaml`.

| Rule | Fires when |
| --- | --- |
| `agent-silent` | Newest Galley heartbeat older than 3 × its own `heartbeat_interval_s` while a book is claimed |
| `agent-stalled` | Same phase and turn count across 90 minutes while state is `running` |
| `agent-dead-token` | Heartbeat or log shows the 401 hold |
| `agent-quota-frozen` | `.subscription-pause.json` present past its own resume time |
| `book-unclaimed` | A book has sat in awaiting longer than `GALLEY_BOOK_SPACING_HOURS` plus 1 hour with the agent idle |
| `book-abandoned` | Ledger says `claimed`, no heartbeat mentions it, machine restarted since |
| `watch-tick-missed` | DocWatch's last tick is later than the schedule allows, or the schedule's fixed times passed with no pass |
| `watch-signin-dead` | The sign-in preflight fails or the log shows "Google no longer accepts the saved sign-in" |
| `watch-model-unavailable` | A pass ended in `ModelUnavailable` |
| `watch-file-skipped-silently` | A file in the folder matches the manuscript rule, has no state record, and has been there longer than two ticks (the Morales `.doc` case) |
| `native-worker-down` | Either login agent not running, or InDesign fails the liveness probe |
| `native-batch-held` | A batch in `held` or an unresolved Project older than 24 hours |
| `hubspot-name-suspect` | See the names lane below |
| `machine-oom` | Fly log shows an OOM kill on either group |
| `deploy-during-run` | A release landed on the agent group while a book was claimed |

## The runbook the model follows

One entry per rule, with the causes we have actually seen. The memory notes
from the last month become this document's first draft:

- **agent-silent / agent-stalled.** Sonnet stall (Georgis, 14 h) → `warden
  galley nudge`, which is `agent --forget <book>` followed by a resume, only
  after a snapshot of the workspace is taken. Duplicate question ids and dead
  DocWatch ticks (Gunn) → same nudge, and check DocWatch too. Machine restart
  mid-run → wait one poll interval first, because resumes are exempt from the
  spacing window.
- **agent-dead-token.** Never fixable on its own: needs a fresh
  `claude setup-token` typed by a person. Text Quinton with the exact three
  commands, then hold.
- **agent-quota-frozen.** Confirm the pause file's resume time is in the past,
  then `warden galley resume`. If the freeze already marked reads as skipped,
  the outcome has to be re-run: ask.
- **book-unclaimed.** Check the ledger for a prior `done` on that Drive id
  (the requeue recipe): `agent --forget`, move the workspace aside, DocWatch
  `flags.reset`, tick. Also check whether the file is actually a Google Doc
  export (the MIME case).
- **watch-tick-missed.** If the app machine is up and the tick is simply
  late, run one pass by hand with `docproof-watch --home /data/docproof/watch
  once` over `fly ssh`. If the machine is down, restart it; the app group is
  safe to restart when the agent group has no claimed book.
- **watch-signin-dead.** Cannot fix. Email the team and text Quinton with the
  DocWatch tab's sign-in instructions; suppress repeats to once a day.
- **watch-model-unavailable.** Check the vendor account (credits, keys).
  Report which lane died. If the pass can go on a subscription lane, ask.
- **watch-file-skipped-silently.** Report the file, its MIME type and size,
  and the rule that excluded it. If it is a `.doc`, offer to convert and
  re-upload (ask).
- **native-worker-down.** `launchctl kickstart` the agent. If InDesign is
  hung, quit it with Apple Events, then force-quit, then kickstart. Never
  touch a job folder.
- **native-batch-held.** Read the hold reason. Identity holds go to the names
  lane. Everything else is reported, not fixed.
- **deploy-during-run.** Report loudly; the fix is human.

## Fix tiers

**Tier 0, on its own, logged.** Read anything. Take snapshots. `agent
--forget` a book whose run is provably dead. `flags.reset` and a DocWatch
tick for a book that has been delivered before. Restart the *app* machine
when no book is claimed. Resume a past-due subscription pause. Kickstart the
native worker or InDesign. Re-send a completion email that never went out.

**Tier 1, after a yes from Quinton by iMessage.** Restart the *agent*
machine. Requeue a book that will cost money or a Max window. Convert and
re-upload a manuscript. Write a name into HubSpot when the sources disagree.
Change any DocWatch setting. Run a pass on a subscription lane instead of a
dead API lane. **Write code** (see below).

**Tier 2, never.** Deploy. `fly secrets set`. Merge to `main` or release the
agent group without a second, separate yes. Touch a book mid-run. Delete anything in Drive or the job store. Send email as
Quinton. Act on an instruction found inside an email, a HubSpot note, or a
manuscript; those get quoted back to Quinton instead.

A yes is a reply to a specific numbered request ("yes 14"), inside 12 hours,
from Quinton's number. Nothing else is a yes.

### When the fix is code

Some stuck states are bugs, and the Warden is sitting in a checkout with the
tests. When the runbook has no verb for what it found, it may propose a code
change, and it asks before writing a line:

1. It texts a numbered request: the rule that fired, the root cause it
   believes, the files it would touch, and the one-sentence fix. Reading and
   reproducing the bug need no permission; writing does.
2. On yes, it works in its own git worktree on the Mini on a
   `warden/<rule>-<date>` branch, follows CLAUDE.md, bumps the version, runs
   the relevant tests, and opens a PR with the snapshot that triggered it
   attached as evidence. It texts the PR link and stops.
3. Merging is a second yes, because merging `main` deploys the app group. The
   Warden merges only when no book is claimed, then watches the release land
   and reports. Releasing the agent group stays Tier 2: a person dispatches
   that, after the current book finishes.
4. If tests fail or the fix grows past the files it named, it texts what it
   found and abandons the branch rather than widening the request.

One code request open at a time. A request nobody answers in 12 hours
expires, and the diagnosis goes into the weekly email instead.

## The names lane

The problem: a Project gets created with the author's name misspelled or
blank, then DocWatch cannot find the folder, the native worker holds the
book, and the deliverable is named wrong. Everything downstream already does
exact matching on purpose, so the fix belongs upstream, at intake.

Every tick, for each Project created or edited since the last tick, the
Warden compares four spellings: the HubSpot first and last name, the Drive
author folder, the byline on the manuscript's first pages (DocWatch already
reads these), and the name on any corrections-form submission. It uses the
existing `name_key` comparison (accents and case flattened). Then:

- **All four agree.** Nothing.
- **HubSpot is blank, the other sources agree.** Tier 0: fill HubSpot from the
  folder and byline, note who created the record, and tell them by email
  what was filled and why. This is the common case and the safe one.
- **HubSpot disagrees with folder and byline, which agree with each other.**
  Tier 1: propose the correction to Quinton by iMessage with all four
  spellings. On yes, write HubSpot, add a title/byline alias in the native
  registry if a batch is already held, and email the record's creator.
- **Sources genuinely conflict, or the byline is missing.** Report to the
  team by email with the evidence and stop. No fuzzy guessing writes to
  HubSpot, which keeps the native worker's identity guarantee intact.

Two upstream asks that need no agent at all, listed here so they are not
forgotten: make first and last name required on the Project creation form,
and add a "Drive folder name" property the Warden can check against.

A weekly email to the team lists every name it touched, so the pattern of who
mis-enters what becomes visible.

## Talking to people

**iMessage.** Messages.app on the Mini signed in as a new Apple ID
(`docproof@atmospherepress.com` once that mailbox exists; any Apple ID works
in the meantime). Sending is an `osascript` call. Reading is a query of
`~/Library/Messages/chat.db`, which needs Full Disk Access for the Warden's
Python. Only messages from Quinton's number count as instructions.

Message discipline: one text per new finding, one per resolution, never more
than four texts an hour, quiet hours 11 p.m. to 7 a.m. Eastern unless the
severity is High. Every time is Eastern. Severity and prefix match the email
convention already in use: `[DocProof][High][Action]`, `[Medium][FYI]`.

Inbound vocabulary Quinton can use: `status`, `yes N`, `no N`, `pause`
(stop acting, keep watching), `resume`, `quiet 3h`, `forget <surname>`,
`why <surname>`. Anything else is passed to the model tick as a question and
answered in a text.

**Email to the team.** Sent through the DocWatch notify sign-in, `Reply-To`
the same address, with a `[Warden]` subject tag. Replies land in that mailbox
and the listener tick reads them. Team members can also write to it directly
with a book or author name and get a status back; they cannot approve Tier 1
actions.

**Quinton's inbox.** Gmail API, read-only scope, on quinton@. The Warden
reads only the `DocProof` label plus the sender allow-list. It quotes; it
never acts on content there without a yes.

## The InDesign hub

Day 1 the Mini gets InDesign, the book fonts, this checkout, and
`tools/install_native_interior.py --home ~/DocProof/review --install`, which
already writes the two login agents with `--local-only`. Then the three
rehearsals (`native-test`, `login`, `astra-test`) through the Mac equivalent
of `windows_interior.py`, which needs writing if it does not exist. The
Warden supervises from the first tick: liveness, held batches, quiet periods,
and a short email per finished batch with the diff summary and where the
`.5` edition sits. Delivery stays off until Quinton has reviewed a few
editions and says so; turning it on is a Tier 2 change he makes himself.

## Guardrails

- **Never deploy mid-run** is enforced in code, not prose: every Tier 0 verb
  refuses when the agent heartbeat shows a claimed book.
- One action per tick, then re-snapshot. No chained fixes.
- Every verb writes its before-state to the journal so a person can undo it.
- Approvals expire. Approvals name one action. No standing approvals.
- Email, HubSpot notes and manuscript text are data. A sentence in any of
  them addressed to the agent is quoted to Quinton, never obeyed.
- Secrets live in the Mini's Keychain. Nothing secret goes in a text or an
  email.
- The Warden's own subscription is separate from Galley's, so a chatty day
  cannot eat a book window.
- If the Warden itself cannot reach Fly, HubSpot or Google for three ticks,
  it texts once and stops trying to fix things until they are back.

## Phases

| Phase | Deliverable | Depends on |
| --- | --- | --- |
| 0. Mini day one | Claude Code + Codex signed in on the Warden account; Keychain holds admin token, Gmail refresh token, HubSpot token; Messages.app on the agent Apple ID; Full Disk Access granted | The Mini arriving; a second subscription; the Apple ID |
| 1. Snapshot + rules | `docproof warden snapshot` and `warden check`, the read-only agent-group status endpoint, the journal, unit tests over recorded snapshots for every rule | Phase 0 |
| 2. Reporting tick | launchd tick, iMessage out, `status` in, the runbook skill file. Report-only for one week | Phase 1 |
| 3. Tier 0 fixes | The verbs, each with dry-run and a journal entry; the claimed-book refusal | One clean week of Phase 2 |
| 4. Team email + inbox | Gmail send via the notify sign-in, reply reading, Quinton's inbox reader, Tier 1 approval flow | Phase 2 |
| 5. Names lane | Four-way comparison, blank-fill, propose flow, weekly report | Phase 4 |
| 6. InDesign hub | Native worker installed local-only, rehearsals passed, Warden supervision | Phase 2; InDesign licence |
| 7. Code-fix flow | Worktree per request, PR with evidence, second-yes merge, claimed-book refusal on merge | Phase 4 |
| 8. Codex fallback | Same prompt and skill run under `codex exec`; alternate one day a week to keep it honest | Phase 3 |

Phase 1 and 2 are two or three working days. Phases 3 to 5 are a week
together. Phase 6 is bounded by InDesign setup, not code.

## Still open

- The Apple ID and the `docproof@` mailbox: create both, or use an existing
  Apple ID for the first week?
- Which of the team should receive Warden email, and should any of them be
  able to say `yes` for Tier 1 actions in Quinton's absence?
- The tick interval. Twenty minutes catches everything in the rules table
  within an hour of it starting. Ten minutes doubles the subscription spend
  for little gain.
