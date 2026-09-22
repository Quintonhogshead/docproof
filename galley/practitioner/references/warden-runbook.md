# The Warden's runbook

One entry per rule in `app/warden/rules.py` (plus the names lane in
`app/warden/names.py`). "Verb" is exact — the name `docproof-warden verb`
takes. "None" means Tier 2: report only, and only a person fixes it.

## agent-silent / agent-stalled

**Symptom.** Galley's heartbeat is more than `agent_silent_factor` times its
own interval old while a book is claimed (`agent-silent`), or the same
phase and turn count has held for `agent_stalled_min` minutes while the
state is `running` (`agent-stalled`).

**Causes seen.** A long model stall (Sonnet stuck 14h on Georgis); duplicate
question ids and a dead DocWatch tick leaving the agent waiting on nothing
(Gunn); a Fly machine restart mid-run, which looks identical to a stall for
one poll interval — that case clears itself next tick without a nudge, so
do not fire on the very first sighting if the machine's `updated_at` is
within the last poll interval.

**Verb.** `galley-nudge book=<the book>` — already refuses unless an open
`agent-silent`, `agent-stalled`, or `book-abandoned` finding names that book,
so it is already Tier 0 and safe to run whenever the rule fired. Also check
DocWatch's own last tick; if it is also late, that is `watch-tick-missed`,
handled separately, not a reason to skip the nudge.

## agent-dead-token

**Symptom.** The heartbeat's `credentials_error` is set, or a 401 with
"token"/"credential" appears in the agent group's Fly logs.

**Verb.** None. This needs a person to type `claude setup-token` on their
own machine and push the result. `say` Quinton the exact three commands:
`claude setup-token > token.txt`, verify it locally, then
`fly secrets set CLAUDE_CODE_OAUTH_TOKEN="$(cat token.txt)" -a
atmosphere-docproof --stage` followed by the deploy that applies it. Then
hold — do not retry the nudge verb; a dead token does not un-stick itself.

## agent-quota-frozen

**Symptom.** `.subscription-pause.json`'s `resume_after` has already
passed.

**Verb.** `galley-resume` (Tier 0; it re-checks `resume_after` itself and
refuses if the window has not actually elapsed, so calling it early is
harmless — it just declines). If the freeze already caused DocWatch reads to
be marked skipped mid-run (see the 2026-09-16 quota-skip incident), resuming
alone is not enough — that book needs a re-run via `docwatch-requeue`, which
is Tier 1 judgement: `say` Quinton rather than firing it yourself.

## book-unclaimed

**Symptom.** A book has sat in `awaiting` longer than the claim spacing plus
`unclaimed_extra_h`, with the agent idle.

**Causes seen.** The requeue recipe applies when the ledger already shows a
prior `done` for that Drive id — the file was delivered once and DocWatch's
flags need resetting before the agent will look at it again. Also check
whether the file is actually a Google Doc export under a non-`.docx`
extension (the MIME case) — `docwatch-requeue` does not fix that; report it
instead if the file record's `mime_type` says `application/vnd.google-apps.*`.

**Verb.** `docwatch-requeue file_id=<id>` (Tier 0: resets the proof flag,
then runs a pass).

## book-abandoned

**Symptom.** The ledger says `claimed`, the current heartbeat no longer
mentions that book, and the agent machine has restarted since the claim.

**Verb.** `galley-nudge book=<name>` (same guard and same verb as
agent-silent/stalled — this rule exists so a restart-while-claimed case
still gets a nudge even once the heartbeat has moved on and stopped
mentioning the old book at all).

## watch-tick-missed

**Symptom.** DocWatch's last tick is older than `tick_every_minutes ×
tick_late_factor`, or a scheduled fixed time has passed with nothing since.

**Verb.** `docwatch-run` first (Tier 0 — runs one pass by hand, the same as
the admin panel's button). If the app machine itself looks down in the Fly
section of the snapshot, `fly-restart-app` instead — safe only when the
agent group has no claimed book, which the verb itself enforces
(`needs_idle_agent`).

## watch-signin-dead

**Symptom.** The sign-in preflight failed, or DocWatch reports no working
Google sign-in.

**Verb.** None. Email the team and `say` Quinton the DocWatch tab's sign-in
instructions (the redirect URI Google needs, if the error was
`redirect_uri_mismatch`). Suppress repeats to once a day — check
`docproof-warden status` for whether this was already reported before
sending another.

## watch-model-unavailable

**Symptom.** The last DocWatch pass ended in `ModelUnavailable`.

**Verb.** None automatic. Report which lane died (the note names it) and
check the vendor account — credits, keys. If the pass could go on the
subscription lane instead of the dead API lane, `say` Quinton to ask, since
changing which lane a pass runs on is a DocWatch setting (Tier 1).

## watch-file-skipped-silently

**Symptom.** A file record has an error, no `job_id`/`marked`/`done`, and
has sat that way for more than `skipped_ticks` ticks — the Morales `.doc`
case, where a format was refused and then nothing retried it.

**Verb.** None automatic. Report the file, its `mime_type`, and the error.
If it is a `.doc`, offer to convert and re-upload — that is a Tier-1 ask,
not something to do unprompted.

## native-worker-down

**Symptom.** Either login agent (`com.docproof.interior-review-worker`,
`com.docproof.interior-review-ui`) is not `running`, or the InDesign
liveness probe failed.

**Verb.** `native-kickstart label=<agent label>` for a dead login agent
(Tier 0). For InDesign itself not answering (the finding's key is
`indesign`, not a launchd label), use `indesign-restart` instead — it quits
InDesign over Apple Events, force-quits if that alone does not take, then
kickstarts the worker so it picks the relaunched app back up. Never touch a
job folder while doing either.

## native-batch-held

**Symptom.** A batch is in state `held`.

**Verb.** None. Read the hold reason. If it names an identity mismatch,
that is the names lane's problem (below) — check whether HubSpot's name for
that Project is a `propose`/`conflict` case and report accordingly.
Everything else is reported to the team, not fixed here.

## machine-oom

**Symptom.** A Fly group's log shows an out-of-memory kill.

**Verb.** `fly-restart-app` or `fly-restart-agent` depending on which group
(Tier 1 — this always goes through a numbered request, never fires on its
own even though the app-group case would otherwise be safe, because an OOM
is worth a person's attention before restarting into the same condition).

## deploy-during-run

**Symptom.** A release landed on the agent group while a book was claimed.

**Verb.** None. This should not happen — the guardrail is enforced in code
on every Tier-0 verb — so when it fires anyway it means someone deployed by
hand. Report it loudly; do not restart anything yourself.

## source-unreachable

**Symptom.** One snapshot section (`fly`, `docwatch`, `hubspot`, `native`,
`email`) has carried an `error` for three consecutive snapshots.

**Verb.** None. Text once, then stop trying to fix anything until the
source comes back — per the plan's own guardrail, three-ticks-dead means
"you cannot trust what you're seeing", not "diagnose harder".

## hubspot-name-suspect (the names lane)

**Symptom.** `app/warden/names.py` compared HubSpot's author name against
the Drive folder, the manuscript byline, and any corrections-form
submission, and they disagree.

- **`fill`** — HubSpot is blank, the other sources agree with each other.
  **Verb:** `hubspot-fill-name project_id=<id> first=<F> last=<L>` (Tier 0
  — it recomputes the verdict itself before writing, so a stale call is
  refused rather than overwriting something that has since changed).
- **`propose`** — HubSpot disagrees, but the folder and byline agree with
  each other. **Verb:** `hubspot-set-name project_id=<id> first=<F>
  last=<L>` (Tier 1 — only after a yes).
- **`conflict`** — genuine disagreement, or nothing to compare HubSpot
  against. **No verb.** Report to the team by email with all four spellings
  and stop; no fuzzy guessing ever writes to HubSpot.
