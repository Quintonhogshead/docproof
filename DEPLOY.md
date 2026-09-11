# Deploying the DocProof web build — a full guide

This is the complete, follow-along guide to putting DocProof on the internet so
several people at the press can sign in and use it. It assumes no prior
server experience — every command is written out. Work through it top to
bottom the first time; after that, the **Everyday operations** section is all
you'll need.

Fly is the supported production deployment. The old desktop `.app` is sunset
and is not part of this guide or the release path.

---

## 1. What you're building

```
   your editors ──HTTPS──▶ [ Fly.io ]──▶ one DocProof process ──▶ Claude / OpenAI / Gemini
                                            │
                                            └──▶ a persistent disk (/data)
                                                   ├─ users.db   (accounts)
                                                   └─ jobs/…      (uploads + finished documents)
```

- **One server process.** DocProof runs as a single process (`docproof-serve`).
  That is deliberate — it has one job runner and one folder lock — and it's the
  right size for a press-sized team. It serves many people at once; it just
  isn't spread across multiple machines. (Scaling to several machines is a
  separate, later project.)
- **A persistent disk** holds everything that must survive a redeploy: the
  accounts database and every job. On Fly.io this is a "volume."
- **Secrets live in the environment,** never in the code or the repo: a session
  secret (which signs the login cookies) and at least one AI provider API key.
- **HTTPS** is handled for you by the host.

### What it does and doesn't do

- **In:** Word `.docx` uploads. **Out:** a tracked-changes `.docx` you download.
- **Accounts** are individual and **created only by an administrator** — there
  is no public sign-up page.
- **Manuscript prep** produces the InDesign-ready file as an **IDML** the
  designer opens directly (InDesign turns it into an INDD on open) — generated
  server-side, no Adobe software on the box. After opening, the designer runs
  the one-time **reflow script** (downloadable from the results screen) to flow
  the book across pages, since InDesign only reflows on edit, not on open.
- **Not on the web build:** the Google Drive watcher and the desktop
  self-updater. These are hidden automatically when running as the web build.
  (Opening the IDML and running the reflow script happen on the designer's own
  Mac, not the server.)

---

## 2. Before you start — what you'll need

1. **A Fly.io account** — sign up at <https://fly.io> (this guide uses Fly
   because a deploy is a single command; the same app runs on any host that can
   run a container with a disk — see *Other hosts* at the end).
2. **The Fly CLI** installed on your Mac:
   ```bash
   brew install flyctl
   ```
   Then sign in:
   ```bash
   fly auth login
   ```
3. **An AI provider API key** — at least one of:
   - Claude (Anthropic): <https://console.anthropic.com> → API keys
   - OpenAI: <https://platform.openai.com/api-keys>
   - Gemini (Google): <https://aistudio.google.com/apikey>

   The server holds this one key and every review runs on it. (This is the same
   spend you already have on the desktop app — just centralized. The per-user
   monthly caps below keep it predictable.)
4. **The DocProof code** on your Mac — this repository, on the branch you want
   to deploy.
5. *(Optional)* **A domain name** if you want `docproof.atmospherepress.com`
   instead of a free `something.fly.dev` address. Covered in §7.

---

## 3. First deploy, step by step

All commands are run from the repository folder on your Mac.

### 3.1 Point the config at your app

`fly.toml` already sets `app = "atmosphere-docproof"` and
`primary_region = "iad"`. Change
them if you like:

- **`app`** must be globally unique on Fly, and lowercase (it becomes the URL
  subdomain). If `fly apps create` (next step) reports the name is taken, pick
  another (e.g. `atmosphere-docproof-2`) and put it here.
- **`primary_region`** should be near your editors. Codes are at
  <https://fly.io/docs/reference/regions/> — e.g. `iad` (Virginia), `lax` (Los
  Angeles), `lhr` (London).

### 3.2 Create the app and its disk

> **Do not use `fly launch`.** Its source scanner tries to guess a framework
> and errors out *("Could not detect runtime or Dockerfile")* even though the
> Dockerfile is right there. Create the app directly instead — `fly deploy`
> (step 3.4) builds straight from our `Dockerfile` via `fly.toml`, with no
> scanning.

```bash
fly apps create atmosphere-docproof
```

(Use the same name as `app` in `fly.toml`. If it's taken, choose another and
update `fly.toml` to match.) Then create the persistent disk in the same region
as `primary_region` (3 GB is plenty to start; you can grow it later):

```bash
fly volumes create docproof_data --size 3 --region iad
```

### 3.3 Set the secrets

Two are required — the server refuses to start without them:

```bash
fly secrets set \
  DOCPROOF_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  ANTHROPIC_API_KEY="sk-ant-...your key..."
```

- `DOCPROOF_SESSION_SECRET` signs the login cookies. The command above
  generates a strong random value; you never need to see or reuse it. If you
  ever change it, everyone is simply logged out and signs in again.
- Set `OPENAI_API_KEY` and/or `GEMINI_API_KEY` too if editors will pick those
  models.
- `GOOGLE_REFRESH_TOKEN` and `HUBSPOT_TOKEN` are needed only if you run the
  Drive watcher on the server, and `HUBSPOT_TOKEN` only if it gates on HubSpot
  ([docs/watch.md](docs/watch.md#gating-on-hubspot-optional)). Both are plain
  `fly secrets set` values — `fly secrets set HUBSPOT_TOKEN=…` — read
  environment-first, so setting them here is all the wiring there is. Neither is
  a review-provider key, so neither appears in the admin portal's key screen.

To confirm what's set (values are hidden):

```bash
fly secrets list
```

### 3.4 Deploy

```bash
fly deploy
```

This builds the container and starts it. When it finishes, `fly open` opens the
app in your browser — you'll see the sign-in screen. You can't sign in yet;
that's the next step.

### 3.5 Create the first administrator

There's no public sign-up, so the first account is made from the server's
command line:

```bash
fly ssh console -C "docproof-admin add-user --admin you@atmospherepress.com"
```

It prompts for a password (typed, not echoed). That account is an
administrator — it can create everyone else from inside the app.

Now reload the app, sign in with that email and password, and you're in.

---

## 4. Everyday operations

### Adding and managing people

Two ways, both equivalent:

- **In the app (easiest):** sign in as an admin → **Admin** tab → *Add someone*.
  Give them an email and a temporary password to share, optionally a monthly
  limit, and whether they're an admin. The **Everyone** table lets you change
  anyone's limit, disable an account, or promote someone.
- **From the command line** (for scripts or if you're locked out of the UI):
  ```bash
  fly ssh console -C "docproof-admin list-users"
  fly ssh console -C "docproof-admin add-user editor@atmospherepress.com"
  fly ssh console -C "docproof-admin reset-password editor@atmospherepress.com"
  fly ssh console -C "docproof-admin disable-user editor@atmospherepress.com"
  fly ssh console -C "docproof-admin set-cap editor@atmospherepress.com 25"
  ```

### Spend limits

- Every ordinary account is limited to `DOCPROOF_DEFAULT_CAP` dollars per month
  (set to `20` in `fly.toml`; change it there and redeploy, or remove it to
  leave ordinary users uncapped by default).
- Give any user their own limit in the Admin tab (blank = use the default).
- **Administrators are never capped** — that's what God Mode means.
- A review that would put someone over their limit is refused with a clear
  message telling them to ask an admin. Nothing is billed for a refused review.

### Pushing an update

**Automatic (set up in this repo).** `.github/workflows/deploy.yml` deploys the
web `app` process on every push to `main` — so **merging a PR updates the live
site**, usually within a couple of minutes. It leaves Galley workers running
their current image and command so a long book is not interrupted by a merge.
It needs one one-time secret:

1. Create a Fly deploy token:
   ```bash
   fly tokens create deploy
   ```
2. In GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**, name it `FLY_API_TOKEN`, and paste the token.

After that, merge and the web app ships. Watch a run under the repo's
**Actions** tab. To release Galley workers too, run **Deploy to Fly.io** manually
with **Also release Galley workers** enabled, after the workers are idle or
checkpoint recovery has been arranged. Deployments are serialized; a later
push does not cancel an in-progress release.

**By hand** (any time, no token needed on the server — you deploy from your
Mac):

```bash
git checkout main && git pull
fly deploy --process-groups app
```

Everyone is on the new version at their next page load — nobody installs
anything. Web jobs are files on disk; overnight jobs resume, and a web review
that was actively running may need a retry. This process filter leaves the
separate Galley worker running. An unfiltered `fly deploy` releases both groups
and can interrupt a Galley phase.

### Examination-graph emergency rollback

The phase-one examination graph is shadow instrumentation: it writes coverage
artifacts but cannot feed edits into a manuscript. To disable it across the
live Fly site immediately, without reverting or redeploying code:

```bash
fly secrets set DOCPROOF_EXAMINATION_GRAPH=0 -a atmosphere-docproof
```

Fly restarts the app with the kill switch set. It overrides the YAML default
and any old job's per-run feature choice. To re-enable the shipped setting:

```bash
fly secrets unset DOCPROOF_EXAMINATION_GRAPH -a atmosphere-docproof
```

The paid Phase 1B judge has its own, narrower kill switch. Use this first when
you want to stop all independent model calls and spend while leaving the free
coverage ledger running:

```bash
fly secrets set DOCPROOF_EXAMINATION_JUDGMENT=0 -a atmosphere-docproof
```

To remove that override later (the shipped setting remains off, so this does
not turn the experiment on globally):

```bash
fly secrets unset DOCPROOF_EXAMINATION_JUDGMENT -a atmosphere-docproof
```

Phase 2's production-receipt prompt has a separate narrow brake too. It leaves
the phase-one ledger and optional Phase 1B judge available, but restores the
finding-only production prompt and leaves its broad obligations pending:

```bash
fly secrets set DOCPROOF_EXAMINATION_PRODUCTION_VERDICTS=0 -a atmosphere-docproof
```

Remove the override to restore the shipped Phase 2 setting:

```bash
fly secrets unset DOCPROOF_EXAMINATION_PRODUCTION_VERDICTS -a atmosphere-docproof
```

Phase 1B is visible only to administrators and can run only as one **Right now**
review round. Its UI shows the $2.00 per-manuscript ceiling, and the server
enforces the role and timing constraints even for direct API requests. The
verdicts are evaluation-only and cannot create findings or alter the document.

For one job only, turn off **Examination coverage ledger — shadow** in the
submission panel's Safety section. See [the shadow rollout and artifact
contract](docs/examination-graph.md) for the full rollback ladder.

**The one exception** is a change to the *accounts database schema* (a new
column, say). Those need a migration: bump `CURRENT_SCHEMA` in
`app/accounts.py` and add the migration step before deploying. Ordinary
code changes never need this.

### Watching spend

- Each user sees their own spend on the **Spending** tab.
- An admin sees everyone's month-to-date in the **Admin** tab, and the raw
  per-user totals at `GET /api/admin/usage`.

### Enabling the LanguageTool pass

LanguageTool is an optional local mechanical-floor pass — a Java rules checker
that proposes commas / missing words / hyphenation the model misses, routed
through the same confirm valve. It ships **off** and stays off until a paired
Johnson compare proves it earns its keep (see `docs/measuring-recall.md`).

The image and machine are already prepared: the Docker image carries a headless
JRE and the `[languagetool]` extra, the machine is sized at 2 GB for the JVM, and
`LTP_JAR_DIR_PATH` points the ~260 MB jar at the `/data` volume. To turn it on:

1. Set `languagetool.enabled: true` in the config the server loads, and redeploy.
2. The **first review after enabling** downloads the jar to `/data/languagetool`
   (one-time, ~30–60 s, then cached and surviving redeploys).

Everything above the config flag is done; do **not** flip it until the measurement
says so.

---

### The Galley agent on Fly

Galley — the unattended proofreader — needs a machine that holds a Claude Max
subscription token, and since 2026-09-06 that machine can be a second Fly
process group, `agent`, beside the web app (`fly.toml` `[processes]`). It runs
`docproof galley agent`: every five minutes it asks the web app which Book 1
files DocWatch has marked awaiting, downloads one with the watcher's Google
sign-in, runs the driver (one Claude Code session per phase on the
subscription, the docproof sifters on the API keys, LanguageTool locally) and
uploads the Book 2 set. Nothing about it runs in the web process.

One-time setup:

1. **Its disk.** The agent's workspaces, claim ledger and LanguageTool jar live
   on a volume of their own, mounted at `/data` on the agent machine only:

   ```bash
   fly volumes create galley_data -a atmosphere-docproof -r iad -s 10
   ```

2. **Its secrets.** Secrets are app-wide, so the subscription token is named
   `GALLEY_OAUTH_TOKEN` — nothing in the web app reads that name — and only
   becomes `CLAUDE_CODE_OAUTH_TOKEN` inside the agent's mode-600 credentials
   file, which the entrypoint (`galley/practitioner/fly/galley-agent`) writes
   from the environment at boot. The Google values are the watcher's sign-in:
   the refresh token from the Mac keychain (or `docproof-watch auth`), the
   client id and secret from the watcher's `watch.json`.

   ```bash
   fly secrets set -a atmosphere-docproof \
     GALLEY_OAUTH_TOKEN=<from `claude setup-token`> \
     GOOGLE_REFRESH_TOKEN=<the watcher's refresh token> \
     GOOGLE_CLIENT_ID=<the watcher's client id> \
     GOOGLE_CLIENT_SECRET=<the watcher's client secret>
   ```

   `DOCPROOF_AGENT_TOKEN`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are the
   web app's existing secrets and are reused as-is: the poller presents the
   agent token to `/api/watch/awaiting`, and the sifters get the API keys
   through the brain's `docproof` wrapper (`galley/practitioner/galley-bin/
   docproof`), never the brain itself.

3. **Deploy the worker explicitly.** Use `fly deploy --process-groups agent`
   or manually run the deployment workflow with **Also release Galley workers**
   enabled. Pushes to `main` release only the web app. `fly.toml` sizes the worker at
   `shared-cpu-4x` / 8 GB; raise it there if `fly logs` for the `agent`
   process shows OOM kills.

4. **Turn proofing on** under Admin → Automations → Proofread, runner
   `external`. Until then the agent polls and finds nothing.

   Optionally `GALLEY_ALERT_EMAIL=<you>` too (a plain `fly secrets set`
   value): the agent emails that address when it starts, when its polling
   breaks and again when it recovers, and when a hand-off delivery is given
   up on. Without it the agent uses the watcher's notify address.

**When the token dies.** A `claude setup-token` token expires or is revoked
eventually, and Claude Code then prints `Failed to authenticate. API Error: 401
OAuth access token is invalid` and exits on turn one. Since v0.193.12 the
agent treats that as the machine's problem, not the book's: the driver raises
instead of writing a `needs_human` verdict, the claimed book stays claimed and
untouched, the agent stops claiming, emails once, and the practitioner panel
shows **Halted · subscription token rejected**. It also checks the token at
boot, before any book. To recover, mint a token on a Mac signed in to the
Max subscription — but **do not copy it off the screen.** `claude setup-token`
draws the token inside a box as 35-character rows with screen redraws between
them, so a mouse selection picks up the wraps, `pbpaste | tr -d '[:space:]'`
glues the surrounding prose onto the token, and a grep over the boxed output
stops at the first row. Every one of those yields a well-formed token that the
API rejects with `401 OAuth access token is invalid`, which looks exactly like
a bad mint (the 2026-09-09 rotation took five tries that way). Redirect stdout
instead — the prompts still reach the terminal, and the token lands in the
file as one plain 108-character line:

```bash
claude setup-token > /tmp/tok.txt
```

Verify it locally before it goes anywhere near Fly (this never echoes the
token; the API key is unset so the answer can only come from the token):

```bash
env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$(grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' /tmp/tok.txt)" \
  claude -p "Reply with exactly the word: ok" --max-turns 1 --output-format json < /dev/null \
  | grep -o '"result":"[^"]*"'
```

Only when that prints `"result":"ok"`:

```bash
fly secrets set -a atmosphere-docproof \
  GALLEY_OAUTH_TOKEN="$(grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' /tmp/tok.txt)" && rm -f /tmp/tok.txt
```

The secret restarts the machines; the agent's first poll resumes the held
book from the phase it was in. To confirm on the machine itself, source the
credentials file and strip the API keys the way the agent does — a bare
`claude -p` over `fly ssh console` inherits `ANTHROPIC_API_KEY`, answers on
it, and proves nothing about the subscription:

```bash
fly ssh console -a atmosphere-docproof --process-group agent -C "/bin/sh -c 'set -a; . /root/.galley/agent.env; set +a; env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY claude -p \"Reply with exactly the word: ok\" --max-turns 1 --output-format json < /dev/null'"
```

A book that an older agent wrote off as
`needs_human` over a dead token is `failed` in the ledger and will not be
retried — drop it and mark it awaiting again:

```bash
fly ssh console -a atmosphere-docproof --process-group agent \
  -C "docproof galley agent --workspace-root /data/galley-workspaces --forget '<Surname> - Book 1.docx'"
```

then Admin → Automations → Run so DocWatch lists it as awaiting; note that
the `needs_human` outcome.json already uploaded beside it must be removed
from the author's folder first, or DocWatch moves the book on at its next pass.

**Watching it.** The agent is not a black box:

- **Admin → Automations → Proofread, "The practitioner machine"** shows the
  agent's last heartbeat: which machine, whether it is still reporting (red
  after 20 minutes of silence), which book and phase it is in, on which
  brain, for how long, how many turns the session has taken, the settle
  round, and the last error or verdict. The agent reports at every phase
  boundary and once a minute while a book runs (`POST /api/watch/agent`,
  behind the same bearer token as the awaiting list).
- **A `needs_human` hand-off carries the evidence.** Beside the outcome and
  the decision log the folder gets `<surname> - Book 2 - diagnostics.zip`:
  every phase transcript, the driver ledger, `PLAN.md`, `QUESTIONS.md`, the
  run state and settlement files, and the agent's own log. Unzip that before
  reaching for `fly ssh console`.
- **The agent emails about itself** (see `GALLEY_ALERT_EMAIL`) — a boot after
  a deploy, a poll that stopped working, a delivery it gave up on. Book
  verdicts still arrive the DocWatch way, on the next pass.
- **Taking a book back.** The agent resumes a claimed book at every boot for
  as long as DocWatch lists it as awaiting. To stop a run for good (a killed
  test, a file dropped by mistake): `fly machine stop <agent machine>`, then
  click **Release** on the book's row under "Out with the practitioner", then
  `fly machine start`. Putting the HubSpot status back at the ready value
  queues the book again.
- `fly logs -a atmosphere-docproof --process agent` is the live container log.
  The entrypoint does not create an `agent.log` file; use an explicit log sink
  when preserving an isolated run's stdout. Per-phase transcripts are under
  `/data/galley-workspaces/<slug>/runs/driver/` (`fly ssh console --process agent`).

An explicit deployment that includes the `agent` group restarts its machines
and restores the normal `galley-agent` polling command. Stop or quiesce the
worker first and arrange recovery for any interrupted operation; do not use it
to update a running isolated pilot. Checkpoint and budget receipts survive,
but an interrupted operation may need reconciliation before it can resume.
Automatic web-only releases leave the worker image and command intact. The
Max token is a personal seat: books proofread here share its quota with
whatever the same account is doing in Claude Code.

## 5. Backups and restore

Everything that matters lives on the volume under `/data/docproof` — the
accounts database and every job. Back it up on a schedule.

**A manual backup to your Mac:**

```bash
# copy the whole state folder down
fly ssh sftp get /data/docproof/users.db ./backup-users.db
```

For a full snapshot, `fly ssh console` in and `tar` the folder, or rely on
Fly's own volume snapshots (enabled by default, retained for several days —
see <https://fly.io/docs/volumes/snapshots/>).

**To restore:** put the files back under `/data/docproof` on a fresh volume
before the app starts serving.

> Fly volume snapshots are a safety net, not a strategy — for anything you'd
> hate to lose, also keep an off-Fly copy (the `users.db` is tiny).

---

## 6. Running it locally first (optional but recommended)

Before deploying, you can run the exact web build on your own Mac:

```bash
pip install -e ".[app]"

DOCPROOF_SESSION_SECRET=dev-secret ANTHROPIC_API_KEY=sk-ant-... \
  docproof-serve --home ./_webhome --insecure-cookies --port 8000
```

Then create a local admin and open it:

```bash
docproof-admin --home ./_webhome add-user --admin you@atmospherepress.com
open http://localhost:8000
```

`--insecure-cookies` lets the login cookie work over plain HTTP; it exists only
for local testing and must never be used in production (where HTTPS is on).

---

## 7. A custom domain (optional)

To use `docproof.atmospherepress.com`:

```bash
fly certs add docproof.atmospherepress.com
```

Fly prints the DNS records to add at your domain registrar (an A/AAAA or a
CNAME). Add them; the certificate issues automatically within a few minutes.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Server won't start; log says *"needs a session secret"* | `DOCPROOF_SESSION_SECRET` isn't set. `fly secrets set DOCPROOF_SESSION_SECRET=...` (see §3.3). |
| Server won't start; log says *"No API key is set"* | No provider key. `fly secrets set ANTHROPIC_API_KEY=...`. |
| Sign-in says *"Wrong email or password"* for a real account | Reset it: `fly ssh console -C "docproof-admin reset-password them@atmospherepress.com"`. |
| *"Too many attempts. Wait a minute"* | The login throttle after 5 wrong tries. Wait 60 seconds. |
| A review is refused with *"monthly limit"* | The user hit their cap. Raise it in the Admin tab, or wait for the new month. Admins are never capped. |
| Everyone got logged out after a deploy | The session secret changed. Harmless — they just sign in again. Don't rotate it casually. |
| Logs | `fly logs` (live) or `fly logs -n` (recent). |
| Open a shell on the server | `fly ssh console`. |

---

## 9. What it costs

- **Fly.io hosting:** roughly **$5–15/month** for the small always-on machine
  (`shared-cpu-1x`, 1 GB) plus the 3 GB volume. Confirm current pricing at
  <https://fly.io/docs/about/pricing/>.
- **AI usage:** variable, and the same per-document cost you already pay — now
  on one central key, bounded by the per-user caps.
- **Domain:** ~$12/year if you want one. HTTPS is free.
- **No per-seat fees:** adding a user costs nothing; they just draw on the
  shared (capped) AI budget.

---

## 10. Security notes

- The whole `/api` surface is closed until you sign in — a route added later is
  private by default.
- One person can never see or reach another's documents; a foreign job id
  returns "not found," not a permission error.
- Passwords are stored only as scrypt hashes; the session cookie is signed with
  your secret.
- Keep `min_machines_running = 1` in `fly.toml`: the overnight-review ticker
  has to stay awake to collect batch results. Don't enable auto-stop.
- Never commit secrets. They belong in `fly secrets`, not `fly.toml` or the
  code.

---

## Other hosts

Nothing here is Fly-specific except the CLI commands. Any host that runs a
container with a persistent disk works — Render, Railway, a plain VM with
Docker. The contract is always the same: run `docproof-serve`, give it a
persistent `DOCPROOF_HOME`, set `DOCPROOF_SESSION_SECRET` and an API key in the
environment, put HTTPS in front, and keep exactly one instance running.
