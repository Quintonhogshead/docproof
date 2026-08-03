# DocProof, the app

Drop in a stack of manuscripts, pick a reviewer, get back tracked-changes Word
files. No terminal, no config files, no environment variables.

## Running it

```bash
.venv/bin/docproof-app
```

That starts a local server on a free port and opens your browser. Nothing is
reachable from the network — it binds to `127.0.0.1` only. `Ctrl+C` stops it.

Useful flags: `--port 8765` to pin the port, `--no-browser` to skip opening a
tab, `--home <dir>` to put jobs and settings somewhere other than
`~/Library/Application Support/DocProof`.

**First run:** go to Settings and paste an API key. DocProof sends documents to
Claude or ChatGPT, and those services bill you directly. Keys go into the macOS
Keychain — never into a file, never returned to the browser, never logged. The
**Test** button makes one cheap real call so a bad key fails there rather than
at 11pm. If `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` are already in your
environment, they're picked up automatically and take precedence.

Finished documents land in `~/Documents/DocProof/<document name>/`.

## Now vs. overnight

**Batch pricing is a flat 50% discount at any hour** — both vendors, all day.
"Overnight" buys you a workflow, not a cheaper rate:

| | Cost | Ready in |
|---|---|---|
| Right now | full price | minutes |
| Overnight | **half** | usually under an hour; 24h at the outside |

Overnight submits immediately by default. The optional "hold until 11:00 PM" is
for deferring the *submission*, and it carries a real caveat the UI states
plainly: **your Mac must be awake with DocProof open at that time.** If you just
want the discount, leave the hold unchecked — you get it either way.

A batch review survives quitting the app. Everything needed to finish sits in
`~/Library/Application Support/DocProof/jobs/<id>/`, so reopening DocProof picks
up where it left off. A sync ("right now") review interrupted mid-flight is
re-queued from the start instead, because there's no vendor-side state to
reconnect to.

## Choosing a reviewer

Six models, three tiers each from Anthropic and OpenAI. The dropdown shows a
live cost estimate for the exact files you dropped, both now and at batch
rates. Prices live in `docproof/providers/catalog.py` — **re-verify them when
adding a model**, since that table is what the user sees.

For manuscript work, Sonnet 5 or Terra is the sensible default. Haiku and Luna
are good for a spelling-and-typos pass. Opus and Sol earn their price on the
judgment-call error types (tense shifts, pronoun agreement).

## Working from the terminal instead

The CLI does everything the app does, plus batch commands:

```bash
docproof inventory draft.docx      # what a run would cost, no API calls
docproof review draft.docx         # review now
docproof submit draft.docx         # queue at batch prices → prints a job id
docproof status                    # what's queued and how far along
docproof collect <job-id>          # finish it
```

## How it fits together

```
app/            FastAPI + static frontend. Job queue, scheduler, settings.
  run.py          entry point: picks a port, opens a browser
  jobs.py         worker thread (sync reviews) + ticker (scheduled, batch)
  main.py         HTTP routes
  static/         the three screens — no build step, no framework
docproof/       the pipeline. Knows nothing about the app.
  pipeline.py     prepare → run → finish, shared by CLI, batch, and app
  batch.py        submit/poll/collect + job manifests
  providers/      the only code that imports a vendor SDK
    base.py         Provider protocol, strict schema normalization
    catalog.py      models, prices, capabilities
```

The pipeline is provider-agnostic: `providers/` is the single seam where a
vendor SDK appears. Adding a third vendor means one new file implementing four
methods, plus catalog rows.

## Testing

```bash
pytest -q
```

49 tests, none of which touch a network. Every vendor call goes through the
`Provider` protocol, so `tests/fakes.py` covers both the synchronous and batch
paths — including a scripted batch provider that reports in-progress polls so
restart and resume behaviour is exercised without sleeping.

## Not built yet

**A double-clickable `DocProof.app`.** Everything runs from
`.venv/bin/docproof-app` today, which still needs a terminal to start. Packaging
it with PyInstaller (plus a `rumps` menu-bar item so closing the browser tab
doesn't kill an in-flight review, and a Gatekeeper note for the unsigned first
launch) is the remaining step. Nothing in the code needs to change for it —
`app/run.py` is already the entry point a bundle would call.
