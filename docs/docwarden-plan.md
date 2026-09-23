# DocWarden: from monitor to work co-pilot

*Design, 2026-09-23. Grows the Warden (`app/warden/`, v0.228.0, plan in
[monitoring-agent-plan.md](monitoring-agent-plan.md)) into a persistent
assistant that lives on the Mac Mini, talks to Quinton over iMessage, answers
the team's email, keeps the pipeline unstuck, and remembers. Own package and
repo (`docwarden`); DocProof keeps only the server routes it needs.*

## What Quinton asked for (2026-09-23)

| Question | Answer |
| --- | --- |
| First jobs | First line of defense on email (team status questions answered and fixed, not forwarded); an outward tech-support surface; a to-do list and updates on long-running threads |
| Beyond DocProof | Calendar, inbox triage, and an index of every Claude Code / Codex task in flight. Build the bones so stronger models can become a full agentic partner |
| Do vs draft | Send and answer email, write HubSpot. Quinton is back-of-house |
| Clock | Morning brief; an alert whenever a book or job lags |
| Channel | iMessage, natural language, constantly. "An AI junior assistant" |
| Memory | Persistent, long-term |
| Team access | Not yet; designed for it |
| Authority | New action classes are Tier 1 (ask). Never delete anything. Never message an address outside `@atmospherepress.com` |
| Budget | Whatever it costs, on the Max subscription |
| Data | Everything: inbox, calendar, Drive, HubSpot, journals, ledgers, cost |
| Host | The Mini, arriving 2026-09-23 ~3 p.m. Eastern; nothing installed yet |
| Model in the loop | Open; it must be able to pull a `needs_human` book and explain why |
| Repo | Own package and repo, named DocWarden |
| Success | Books get unstuck; basic email answered or escalated |
| Failure to avoid | Freezing. See problem, overcome problem |

## Shape

Three layers survive from the Warden, but the middle one changes from
"one model turn per tick" to "a resident brain that turns on every event".

```
                 ┌──────────────────────────────────────────────┐
  iMessage ────▶ │  surfaces: imessage · mail · brief · (team)  │
  docwarden@ ──▶ │                                              │
  clock ───────▶ │  brain: event loop → context → harness turn  │ ──▶ verbs (tier-gated)
  findings ────▶ │                                              │ ──▶ replies
                 │  core: connectors · snapshot · rules · verbs │
                 │        journal · threads · memory · policy   │
                 └──────────────────────────────────────────────┘
```

**Core (`docwarden.core`, deterministic, no model).** The Warden's snapshot,
rules, verbs, and journal move here unchanged, then grow: new connectors
(Gmail full read on both identities, Google Calendar, Drive, GitHub, the
Claude Code / Codex work index), new rules (`job-lagging`, `thread-stale`,
`support-unanswered`), new verbs (below), and three stores that did not exist:
`threads`, `memory`, `policy`.

**Brain (`docwarden.brain`).** A `launchd` daemon that runs an event loop.
Every inbound text, inbound email, clock tick, and rule finding becomes an
*event*. For each event the brain assembles context (the relevant thread,
the people involved, the snapshot slice that matters, the memory pages the
event touches, the runbook) and runs **one harness turn** with a typed
toolbelt. The turn ends by writing to the journal and, usually, a reply. The
brain never blocks: a turn that exceeds its time box is killed, its event is
re-queued once, then escalated as a thread.

**Surfaces (`docwarden.surfaces`).** iMessage to and from Quinton (owner
channel, the only channel that can say "yes N"). Mail as
`docwarden@atmospherepress.com` for the team and for tech support. The
morning brief. Later, per-person iMessage or mail for the team, each with
their own policy row.

The harness stays swappable. The brain talks to it through one interface:
`turn(context, tools) -> reply`, backed by Claude Code headless first
(`claude -p --mcp-config docwarden.json`) and Codex second. Tools are exposed
by `docwarden mcp serve`, a local MCP server over the verbs, so the model
calls `galley_nudge(book="Gunn")` rather than shelling out and parsing text.
That is the "bones" ask: a stronger model plugs into the same toolbelt and
the same memory with no code change.

## Memory, threads, policy

Three stores under `~/.docwarden/`, all plain files or SQLite, all readable
by a person, all editable by the model through verbs that journal the edit.

**`journal.sqlite`** (exists). Every event, finding, action, message, request.
Append-only. Answers "what happened".

**`threads.sqlite`** (new). One row per long-running item: a stuck book, an
unanswered support email, a to-do Quinton gave it, a PR waiting on review, a
HubSpot record with a bad name. Fields: `id`, `kind`, `subject`, `parties`,
`status` (open / waiting-on-quinton / waiting-on-team / waiting-on-external /
done), `next_action`, `next_action_owner`, `due`, `last_update`,
`source_refs` (email ids, book, PR url). Answers "what is still open and
whose move is it". The to-do list *is* the set of threads whose owner is
Quinton. The morning brief and "what's going on" are views over it.

**`memory/`** (new). Markdown pages the model reads and writes:
- `people.md`: the team directory. Name, address, role, what they usually
  ask, how they like to be answered. Kelly's row is the first one.
- `house.md`: standing facts (times in Eastern, one book per Max window,
  never deploy mid-run, the requeue recipe, where archives live).
- `preferences.md`: how Quinton wants things (brief length, what to
  escalate, what to handle quietly).
- `lessons.md`: what went wrong and what fixed it, appended after every
  escalation that ends in a fix. This is the runbook's growth path.
- `projects/<name>.md`: one page per thing in flight that needs more than a
  thread row.

The model edits these through `remember(page, text)` and `forget(page,
text)`. Every write is journaled with the event that caused it, so a bad
memory can be traced and reverted.

**`policy.yaml`** (new). The tier table, per verb class and per party,
editable by Quinton over iMessage: "always yes to requeues under $2" becomes
a row that promotes that verb class to Tier 0 with a bound. This is how the
assistant earns autonomy without a code change, and how it is taken back.

## Authority

The existing ladder stays. Everything the Warden did on its own (Tier 0)
stays Tier 0: `galley-nudge`, `galley-resume`, `docwatch-requeue` for a
book delivered before, app restarts with no book claimed, native kickstart,
re-sending a completion email. Everything new starts at Tier 1 and moves
down only through `policy.yaml`.

| Action | Tier | Notes |
| --- | --- | --- |
| Read anything | 0 | Inbox, calendar, Drive, HubSpot, ledgers, journals, session index |
| Reply to a team member as `docwarden@` with status, no state change | 0 | Recipient must be `@atmospherepress.com`; the reply quotes the facts it used |
| Send or reply as Quinton | 1 | Draft is texted with a number; "yes N" sends |
| Any email to an address outside `@atmospherepress.com` | 2 | Hard guard in `mail.send`, not a policy row. No verb exists for it |
| Write a HubSpot property | 1 | `hubspot-fill-name` stays 0 in its already-safe case |
| Move or rename in Drive | 1 | |
| Delete anything, anywhere | 2 | No verb exists. Not overridable |
| Spend money or a Max window on a re-run | 1 | Bound in `policy.yaml` once Quinton sets one |
| Create or move a calendar event | 1 | |
| Restart the agent machine, merge a PR, deploy | 1 / 1 / 2 | Unchanged |
| Write code | 1 + PR | Unchanged |
| Edit its own memory pages | 0 | Journaled and revertible |
| Edit `policy.yaml` | owner only | Only a text from Quinton's number |

A "yes" is still a reply to a numbered request, inside 12 hours, from the
owner's number. Natural-language yeses ("go ahead", "do it") are accepted
when they arrive as the next message after exactly one open request; two
open requests require a number.

Text from an email, a HubSpot note, a manuscript, or a web page is data.
An instruction found there is quoted back to Quinton, never acted on. This
was already the rule and it matters more now that the assistant reads a
whole inbox.

## Not freezing

The Warden's failure mode (and Galley's, and DocWatch's) was the silent
stall. DocWarden holds four invariants, enforced in core rather than left to
the model:

1. **Every input leaves a row.** Every text, email, finding, and tick ends in
   the journal as handled, deferred (with a thread), or escalated. A
   connector that fails is a finding, not an exception.
2. **Every blocker has a ladder.** A verb that fails runs its documented
   alternative, then escalates with a concrete ask ("token is dead; run these
   three commands"). The ladder lives in the runbook; the brain follows it;
   core enforces that a failed verb cannot simply end the turn.
3. **Every open thread has a next action and an owner.** A thread that sits
   past its `due` with no update fires `thread-stale`, which lands in the
   brief and, when the owner is Quinton, in a text.
4. **A turn is time-boxed and retried once.** A harness that hangs is killed
   at the box; the event goes back on the queue once; the second failure is a
   thread with `next_action_owner = quinton` and the harness's last output
   attached.

Escalation is not failure. The brief's first section is "things I could not
resolve and what I need from you", never "things I skipped".

## The first four jobs

### 1. Email first line

Two identities. **Quinton's inbox**, read in full, is triaged: each new
message is classified (needs-Quinton / I-can-answer / FYI / noise), linked to
a thread when it belongs to one, and either drafted for Quinton (Tier 1,
texted with a number) or answered from `docwarden@` when it is a team
status question. **`docwarden@atmospherepress.com`** is the assistant's own
identity: the team writes to it directly for status and support, it replies
in-thread, and it copies Quinton only when the thread needs him.

The Kelly case, end to end: Kelly emails "where is the Purpura proofread?".
The brain finds the book in DocWatch state, the Galley ledger, and HubSpot;
if it is running, it answers with the stage and an estimate; if it is stuck,
it runs the Tier 0 fix, then answers with what it found and what it did; if
the fix is Tier 1 it answers Kelly that it is on it, texts Quinton the
request, and closes the loop with Kelly after the yes. The whole exchange
is one thread.

Tech support: the same mailbox, with a `support` label. Questions about the
DocProof app, DocWatch behavior, InDesign corrections, or Galley outcomes
are answered from the docs, the runbook, and the live state. Anything it
cannot answer becomes a thread owned by Quinton with the draft it would have
sent.

### 2. Getting books unstuck, including `needs_human`

All fifteen Warden rules stay. Three are added:

- `job-lagging`: a book has sat in one DocWatch stage past that stage's
  usual duration (learned from the completion log, with a floor per stage).
- `needs-human-unexplained`: a Galley outcome is `needs_human` and no thread
  holds an explanation yet.
- `support-unanswered`: mail to `docwarden@` older than one hour with no
  reply and no thread.

New verb `galley-inspect <book>`: pulls the outcome package for a delivered
book (outcome.json, the Astra gate report, the six-file hand-off, the
question log) from the Drive archive or the workspace over `fly ssh`, into
`~/.docwarden/inspect/<book>/`, and hands the brain the paths. The brain
reads them and writes a thread: what the gate objected to, whether the
objection is a real blocker, and a recommendation (override per the decided
override policy, re-run, or hand to a person). Override itself stays Tier 1.

### 3. Morning brief and lag alerts

At 7:30 a.m. Eastern, by iMessage (short) and email (full):

1. What I need from you: open requests, threads waiting on Quinton.
2. Pipeline: books by stage, anything lagging, what I did overnight.
3. Threads: what moved, what is waiting on whom.
4. Today: calendar, with anything the pipeline implies (a book due).
5. Inbox: what I answered, what I drafted, what I left.
6. Work in flight: Claude Code / Codex sessions and branches, their state.
7. Spend: yesterday and month to date, per service.

Lag alerts fire as they happen, subject to the hourly cap, with "constantly"
read as: every finding worth a sentence gets one, batched per hour, and the
brief catches the rest.

### 4. Work-in-flight index

Where Claude Code and Codex tasks run is Quinton's Mac, not the Mini, so the
index has two feeds:

- **GitHub** (reliable, remote): every branch with a `claude/` or `codex/`
  prefix, its last commit, its PR, CI state, review state, and whether it is
  merged or stale. Read through `gh`.
- **Session sync** (richer, needs a small agent on the Mac): a `launchd`
  agent on Quinton's Mac that every ten minutes writes an index of
  `~/.claude/projects/*/` and `~/.codex/sessions/` (session id, cwd, title,
  last activity, last user message, whether a turn is still running) to a
  file in a shared Drive folder or over `ssh` to the Mini. No transcript
  content leaves the Mac, only the index.

The brief's "Work in flight" section and the question "where are we on X"
read both.

## Calendar

Google Calendar on Quinton's account, read at Tier 0 for the brief and for
context ("you have the Redding call at 2, the Redding proofread lands
tomorrow"). Creating or moving events is Tier 1. Reminders it sets for
itself are threads, not calendar events.

## Repo and package

- Repo `Quintonhogshead/docwarden`, package `docwarden`, CLI `docwarden`,
  home `~/.docwarden/`. Python 3.12, same tooling as DocProof.
- `app/warden/` moves over as `docwarden.core.{snapshot,rules,verbs,journal,
  commands,approvals,config,secrets,install,messaging}`; the DocProof repo
  keeps `app/routes/watch.py`'s warden routes and `warden_payload`, and drops
  `app/warden/` once the new package is installed on the Mini. The
  `docproof-warden` CLI becomes a shim that prints where it went.
- DocWarden depends on `docproof` as a library only for the clients it
  already has (`app.watch.hubspot`, `app.watch.notify`, `app.watch.drive`),
  never for the pipeline. Everything book-shaped still runs on Fly.
- The runbook and skill move too: `docwarden/runbook/` holds the markdown
  the harness reads, versioned with the code that exposes the verbs.
- Tests move with the modules. New: brain event loop with a fake harness,
  threads store, policy promotion and demotion, the mail domain guard, the
  no-silent-skip invariant (every fixture input yields a journal row).

## Harness and cost

Claude Code headless on Quinton's Max subscription via `claude setup-token`,
the same way Galley's subscription lane works, token in the Mini's Keychain.
Model: Opus 5.5 by default for every turn; Fable for `galley-inspect`
readings and anything the policy marks as judgment. Turns are small (one
event, one thread, a few tool calls), so the Galley one-book-per-window
spacing does not apply; the brain does honor the subscription's session
limit by pausing the queue and texting once when it hits it, rather than
freezing (the Kyler lesson).

## Phases

**Phase 0, today.** Mini arrives. Install the Warden as it is, per
[warden.md](warden.md): venv, Full Disk Access, the dedicated Apple ID,
Keychain secrets, `DOCPROOF_WARDEN_TOKEN` on Fly, `init`, `snapshot`,
`check`, `install`. Prove a tick and a "yes N" round trip. Nothing in this
plan is worth building on a Mini that cannot yet read `chat.db`.

**Phase 1, the resident brain.** New repo. Lift `app/warden/`. Add the MCP
toolbelt, the brain daemon and event queue, `threads`, `memory/`,
`policy.yaml`, natural-language iMessage (any text that is not a fixed
command becomes an event), the morning brief. Ship when Quinton can text
"what's going on" and "remind me to chase the Bradshaw cover Friday" and get
correct answers, and the brief arrives.

**Phase 2, email.** The `docwarden@` mailbox (a Workspace user, plus
`support@` as an alias if wanted). Full inbox read, triage, drafts as Tier 1
requests, team status replies as Tier 0, the domain guard, `support-
unanswered`. Kelly's flow end to end. HubSpot writes as Tier 1 verbs.
Calendar read.

**Phase 3, judgment.** `galley-inspect` and `needs-human-unexplained`.
`job-lagging` with learned durations. Standing approvals in `policy.yaml`.
The work-in-flight index, GitHub feed first, session sync second.

**Phase 4, the team.** Per-person policy rows; Kelly can ask it directly by
text; each person's requests are numbered in their own space and only
Quinton's number can approve anything above Tier 0.

## Confirmed 2026-09-23 (afternoon)

1. **Kelly** is the Book Production Manager. Her ask is almost always "this
   book is stuck". `people.md` starts with her row; the Kelly flow in job 1 is
   the first thing Phase 2 proves.
2. **`docwarden@atmospherepress.com` is on hold.** Until it exists, the
   assistant's own voice is the DocWatch notify mailbox the Warden already
   sends team mail through. Replies to Quinton's inbox are drafts as
   Quinton (Tier 1). Team status answers go out from the notify mailbox.
3. **Mac-side session sync is acceptable.** The Mini is the hub; Quinton's
   Mac publishes its session index to it.
4. **Apple ID**: Quinton is creating `quinton@atmosphere...` today for the
   Mini's Messages.app. `owner_handle` stays Quinton's personal number.
5. **Tier for team replies depends on the ask.** Pure status (where is it,
   what stage, when) is Tier 0 from the notify mailbox. Anything whose
   answer implies a change (requeue, override, re-run, HubSpot edit) is
   Tier 1: the assistant tells the team member it is on it, texts Quinton the
   request, and closes the loop after the yes. `policy.yaml` carries this
   as two rows, `reply.status` and `reply.change`.
