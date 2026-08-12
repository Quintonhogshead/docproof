# Deploying the DocProof web build — a full guide

This is the complete, follow-along guide to putting DocProof on the internet so
several people at the press can sign in and use it. It assumes no prior
server experience — every command is written out. Work through it top to
bottom the first time; after that, the **Everyday operations** section is all
you'll need.

The desktop `.app` is a separate thing (built with PyInstaller from
`DocProof.spec`) and is unaffected by anything here.

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
- **Not on the web build:** the InDesign "prepare for layout" step (it needs a
  Mac with InDesign), the Google Drive watcher, and the desktop self-updater.
  These are hidden automatically when running as the web build.

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
site on every push to `main` — so **merging a PR updates the live site**,
usually within a couple of minutes. It needs one one-time secret:

1. Create a Fly deploy token:
   ```bash
   fly tokens create deploy
   ```
2. In GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**, name it `FLY_API_TOKEN`, and paste the token.

After that, nothing to do — merge and it ships. Watch a run under the repo's
**Actions** tab.

**By hand** (any time, no token needed on the server — you deploy from your
Mac):

```bash
git checkout main && git pull
fly deploy
```

Everyone is on the new version at their next page load — nobody installs
anything. **A deploy is safe to run mid-review:** jobs are files on disk,
overnight (batch) jobs resume on their own, and at worst one review that was
actively running needs a one-click retry.

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
