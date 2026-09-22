# The Warden — setup and day-to-day operation

The design is `docs/monitoring-agent-plan.md`. This page is the checklist
for getting it running on the Mac Mini, and the vocabulary for talking to it
once it is. See also `galley/practitioner/skills/warden/` — the runbook the
harness itself reads.

## One-time setup on the Mini

1. **Check out this repo** and install it into a venv, the same way any
   other DocProof install does: `python -m venv .venv && .venv/bin/pip
   install -e .`. `docproof-warden` is a console script this install
   registers (`pyproject.toml`'s `[project.scripts]`).

2. **Grant Full Disk Access to the venv's Python binary** (System Settings →
   Privacy & Security → Full Disk Access → add `.venv/bin/python3`). Without
   this, `app/warden/messaging/imessage.py::read_since` cannot open
   `~/Library/Messages/chat.db` and inbound texts are silently invisible —
   the Warden still runs, it just never hears "yes 3".

3. **Sign Messages.app into the Warden's own Apple ID** (not Quinton's, not
   the Mini's primary account — a dedicated one, `docproof@` once that
   mailbox exists). Send yourself a test iMessage from Quinton's number to
   that Apple ID once, by hand, so the conversation exists in `chat.db`
   before the Warden ever tries to read it.

4. **Store secrets in the Mini's Keychain**, one at a time, value on stdin
   (never as an argv — an argv is visible in `ps` and shell history):

   ```
   echo -n "$TOKEN" | docproof-warden secret set warden_token
   echo -n "$TOKEN" | docproof-warden secret set hubspot
   echo -n "$ID"    | docproof-warden secret set google_client_id
   echo -n "$SECRET" | docproof-warden secret set google_client_secret
   echo -n "$TOKEN" | docproof-warden secret set google_notify_refresh
   echo -n "$TOKEN" | docproof-warden secret set google_inbox_refresh
   ```

   Or set the matching environment variable instead (see the table below) —
   the environment always wins over the Keychain, which is what makes a
   `launchd` plist or a one-off `docproof-warden tick` run under a different
   secret possible without touching Keychain at all.

   | secret | env override | what it's for |
   | --- | --- | --- |
   | `warden_token` | `DOCPROOF_WARDEN_TOKEN` | bearer for the server's `/api/watch/warden*` routes |
   | `hubspot` | `HUBSPOT_TOKEN` | reading/writing the Projects object |
   | `google_client_id` / `google_client_secret` | `DOCPROOF_GOOGLE_CLIENT_ID` / `_SECRET` | OAuth client for both Google identities below |
   | `google_notify_refresh` | `DOCPROOF_GOOGLE_NOTIFY_REFRESH` | the DocWatch notify mailbox — sends team email, reads its own replies |
   | `google_inbox_refresh` | `DOCPROOF_GOOGLE_INBOX_REFRESH` | Quinton's inbox, read-only |
   | `fly_token` | `FLY_API_TOKEN` | optional — the `fly` CLI may already be logged in |

5. **`DOCPROOF_WARDEN_TOKEN` must also be set as a Fly secret on the `app`
   process group** — the server side of the same bearer:

   ```
   fly secrets set DOCPROOF_WARDEN_TOKEN="$TOKEN" -a atmosphere-docproof
   ```

   Use the same value the Mini's Keychain (or environment) holds under
   `warden_token`. This does not trigger the agent-group release Galley
   worries about — `fly secrets set` on the `app` group alone redeploys only
   `app`, per `fly-deploy-app-vs-agent.md`.

6. **`docproof-warden init`** — writes `~/.docproof-warden/warden.yaml` with
   defaults if one is not already there, and prints which of the secrets
   above are still missing (never their values). Edit `warden.yaml` by hand
   afterward for `owner_handle` (Quinton's iMessage handle), `team_emails`,
   and any threshold that needs tuning — it is plain YAML, meant to be
   hand-edited between ticks.

7. **`docproof-warden snapshot` then `docproof-warden check`** — confirm the
   Warden can actually reach Fly, DocWatch, HubSpot, and (if installed) the
   native worker, and that nothing is on fire before turning on the clock.

8. **`docproof-warden install`** — writes and loads two `launchd` agents
   under `~/Library/LaunchAgents`:
   - `com.docproof.warden-tick` — a full tick every `tick_interval_min`
     minutes (default 20; override with `--interval`).
   - `com.docproof.warden-listen` — a cheap read of inbound iMessages/email
     every `listen_interval_min` minutes (default 2; override with
     `--listen-interval`).

   Logs land in `~/.docproof-warden/logs/{tick,listen}.log`.
   `docproof-warden uninstall` unloads and removes both.

## The command vocabulary

Everything below also works from the CLI directly (`docproof-warden ...`),
which is how the harness itself acts, and how a person debugging by hand
does the same thing it would.

**Texted to the Warden** (only from `owner_handle`'s number — nothing else
is ever treated as an instruction):

| text | does |
| --- | --- |
| `status` | one-line state: Galley's phase, open findings, open requests |
| `yes N` / `no N` | answer numbered request `N` — runs its verb on yes |
| `pause` | stop acting on findings; keeps watching and reporting |
| `resume` | start acting again |
| `quiet 3h` | suppress non-high texts for 3 hours (or `Nm` for minutes) |
| `forget <surname>` | `galley-nudge` that book, if a stall finding is open for it |
| `why <surname>` | recent findings/actions/file record mentioning that name |
| anything else | passed to the harness as a question, answered by text |

**What it texts, unprompted:** one message per new high/medium finding
(high bypasses quiet hours; medium waits them out), one combined message
per tick for whatever just resolved, and one numbered request per new
Tier-1 finding with a verb (`"#N <what>. Reply 'yes N' or 'no N'."`).
Never more than `max_texts_per_hour` in an hour.

**Email:** the team gets one `[Warden]`-tagged message per Tier-1 send-team
call and code-request notice; replies land back in the notify mailbox and
the next `listen` tick reads them, but only a text from Quinton's own
number can say yes to anything — an email reply gets a status answer, never
an approval.

## Pausing, quieting, and stopping

- **`pause` / `docproof-warden request` won't run new fixes**, but the
  clock keeps collecting snapshots, evaluating rules, and notifying — this
  is "stop touching things", not "stop watching".
- **`quiet Nh`** only suppresses non-high texts; a Tier-0 fix or a
  high-severity finding still happens and still texts.
- **`docproof-warden uninstall`** is the real stop: it unloads both
  `launchd` agents. Nothing runs again until `install` is run again.

## Verbs, tiers, and what a Tier-0 fix will and won't touch

See `galley/practitioner/skills/warden/references/runbook.md` for the exact
verb per rule. The short version: every verb that could touch a running
book refuses outright while Galley's heartbeat shows one claimed
(`needs_idle_agent`, enforced in `app/warden/verbs.py`, not by convention).
Nothing here ever deploys, sets a Fly secret, merges to `main`, or deletes
anything in Drive or the job store — those have no verb at all, on purpose;
`docproof-warden verb <anything-not-in-the-registry>` simply refuses.
