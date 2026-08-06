# Deploying the DocProof web build

This is the runbook for the hosted, multi-user version — individual accounts,
Word `.docx` in, tracked-changes `.docx` out. The desktop `.app` is a separate
thing and is unaffected by any of this.

## What runs where

- **One server process** (`docproof-serve`) behind HTTPS. It binds `0.0.0.0`;
  a proxy or the platform terminates TLS in front of it.
- **A persistent volume** holding `DOCPROOF_HOME` — accounts (`users.db`),
  uploads, jobs, and results. This must survive redeploys.
- **Secrets in the environment**, never in the repo: the session secret and at
  least one provider API key.

It is single-process by design (one job runner, one folder lock), which is
right for a press-sized team. Scaling to multiple instances is a later,
separate project.

## First deploy (Fly.io)

1. Install the CLI and sign in (`fly auth login`).
2. Edit `fly.toml`: set `app` to a name you own and `primary_region` to one
   near your editors.
3. Create the app and its volume:
   ```bash
   fly launch --no-deploy        # or `fly apps create <name>` if it exists
   fly volumes create docproof_data --size 3
   ```
4. Set the secrets (both are required — the server won't boot without them):
   ```bash
   fly secrets set \
     DOCPROOF_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
     ANTHROPIC_API_KEY="sk-ant-..."
   ```
   Add `OPENAI_API_KEY` / `GEMINI_API_KEY` too if editors will pick those models.
5. Deploy:
   ```bash
   fly deploy
   ```
6. Create the first administrator (this is the only way in — there is no public
   sign-up):
   ```bash
   fly ssh console -C "docproof-admin add-user --admin you@yourpress.com"
   ```
   Then sign in at your app URL and, as that admin, create everyone else from
   the in-app admin panel (or keep using `docproof-admin`).

## Pushing updates

```bash
fly deploy
```

Everyone is on the new version at their next page load. A deploy is safe to run
mid-review: jobs are files on disk, batch jobs resume via the ticker, and at
worst one actively-running "now" review needs a retry. **Exception:** a change
to the accounts database *schema* needs a migration step (bump `CURRENT_SCHEMA`
in `app/accounts.py` and add the migration) — plain code changes never do.

## Managing users and spend

- **Invite / disable / reset:** the in-app admin panel, or `docproof-admin`
  over `fly ssh console`.
- **Caps:** every ordinary account is limited to `DOCPROOF_DEFAULT_CAP` USD per
  month unless given its own cap; admins are never capped. Change a user's cap
  in the admin panel. Over-cap reviews are refused with a clear message.

## Backups

`DOCPROOF_HOME` (on the volume) is the whole state. Back it up on a schedule —
e.g. a nightly `sqlite3 /data/docproof/users.db ".backup ..."` plus a tar of
`/data/docproof` copied off-box. To restore, put the files back on a fresh
volume before the first deploy.

## Running it locally (no TLS)

```bash
DOCPROOF_SESSION_SECRET=dev-secret ANTHROPIC_API_KEY=sk-ant-... \
  docproof-serve --home ./_webhome --insecure-cookies
```

`--insecure-cookies` lets the session cookie work over plain HTTP; never use it
in production.
