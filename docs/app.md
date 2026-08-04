# DocProof, the app

Drop in a stack of manuscripts, pick a reviewer, get back tracked-changes Word
files. No terminal, no config files, no environment variables.

## Running it

Double-click **DocProof.app**. It opens in its own window — no browser, no
terminal. To build it:

```bash
.venv/bin/pyinstaller DocProof.spec
```

That produces `dist/DocProof.app` (~43 MB). It is **unsigned**, so the very
first launch needs right-click → **Open** once; double-clicking works from
then on. Drag it to `/Applications` if you like.

Launching a second copy doesn't start a second server — it opens another
window onto the one already running, because two copies sharing a job folder
would poll the same batches and race over the same files.

There's still a browser mode if you prefer it:

```bash
.venv/bin/docproof-app
```

Either way the server binds to `127.0.0.1` only; nothing is reachable from the
network. Useful flags: `--port 8765` to pin the port, `--home <dir>` to put
jobs and settings somewhere other than `~/Library/Application Support/DocProof`
(`docproof-app` also takes `--no-browser`).

**First run:** go to Settings and paste an API key. DocProof sends documents to
Claude, ChatGPT, or Gemini, and those services bill you directly. Keys go into
the macOS Keychain — never into a file, never returned to the browser, never
logged. The **Test** button makes one cheap real call so a bad key fails there
rather than at 11pm. If `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
`GEMINI_API_KEY` are already in your environment, they're picked up
automatically and take precedence.

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

Ten models from Anthropic, OpenAI, and Google. The dropdown shows a live cost
estimate for the exact files you dropped, both now and at batch rates. Prices
live in `docproof/providers/catalog.py` — **re-verify them when adding a
model**, since that table is what the user sees.

For manuscript work, Sonnet 5, Terra, or Gemini 3.6 Flash is the sensible
default. Haiku, Luna, and Flash Lite are good for a spelling-and-typos pass.
Opus, Sol, and Gemini 3.1 Pro earn their price on the judgment-call error types
(tense shifts, pronoun agreement).

Claude Fable 5 is listed but is not a recommendation: at $10/$50 per million
tokens it costs twice Opus for a task that is precise and well-specified, where
the ceiling is mostly reached already. It also needs an account with 30-day
data retention — zero-retention organizations get an error on every request.
Reach for it only when the judgment-call types are still coming back wrong on
Opus.

All three vendors discount batch work by exactly 50%.

## Reviewing part of a document

Drop a manuscript and DocProof splits it into sections. If there's more than
one, **Choose which parts to review** lists them by their opening lines, all
ticked. Untick what you don't want; the cost estimate follows along. Untick
everything in a file and that file sits out the run.

The choice is recorded with the job, which matters for overnight reviews: when
results are collected hours later the document is re-read from disk, and
without a record the run would quietly expand to the whole thing.

From the terminal:

```bash
docproof inventory draft.docx              # lists section ids
docproof review draft.docx --only chunk-003,chunk-007
docproof submit draft.docx --only chunk-003
```

## Seeing what changed

A finished review has **See what changed** next to it. That's a reading view of
the same findings that are in the `.docx`: grouped by kind of mistake,
commonest first, each one shown as before → after with only the altered words
marked. Confidence is a phrase rather than a score, and anything the model
found but didn't apply is kept in a separate panel rather than dropped.

## Changing what it looks for

**What it looks for** shows the instructions sent with your document, one
editable section per kind of mistake, plus the complete assembled message
under *See exactly what gets sent*.

Edits are written to `~/Library/Application Support/DocProof/error_types/` and
shadow the shipped prompts key by key. The originals are never modified, so
**Reset** is just deleting one small file, and error types you haven't touched
keep getting upstream improvements. Each batch job records the prompts it
actually sent, so a review collected days later can still say what it asked.

## Spending less

Two levers, in order of how much they save:

1. **Turn off "Ask for a reason with every change"** (Settings). Explanations
   are most of what the model writes back, and output bills at roughly 5× input.
   The field leaves the schema entirely — asking and discarding would cost the
   same. Your corrections are unaffected; only the margin comments go.
2. **Review fewer sections**, as above. Cost is close to linear in sections.

Grouping error types into fewer passes is the biggest lever of all — each pass
re-sends the whole document — but it trades detection focus, so it's a config
decision rather than a switch. See `docs/error-types.md`.

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
DocProof.spec     PyInstaller recipe for the Mac app
docproof_desktop.py  the packaged app's entry script (see its docstring)
app/            FastAPI + static frontend. Job queue, scheduler, settings.
  desktop.py      native window + uvicorn on a thread; single-instance guard
  run.py          browser entry point: picks a port, opens a tab
  jobs.py         worker thread (sync reviews) + ticker (scheduled, batch)
  main.py         HTTP routes
  prompts.py      reading and editing the shipped prompts
  report.py       findings.json → the reading view
  static/         the five screens — no build step, no framework
docproof/       the pipeline. Knows nothing about the app.
  pipeline.py     prepare → run → finish, shared by CLI, batch, and app
  batch.py        submit/poll/collect + job manifests
  providers/      the only code that imports a vendor SDK
    base.py         Provider protocol, schema normalization (strict + inlined)
    catalog.py      models, prices, capabilities
```

The pipeline is provider-agnostic: `providers/` is the single seam where a
vendor SDK appears. Adding a vendor means one new file implementing four
methods, plus catalog rows — that's exactly what the Gemini provider is.
Gemini needs a narrower schema dialect than the other two, which is what
`inlined_json_schema` in `base.py` produces.

## Testing

```bash
pytest -q
```

91 tests, none of which touch a network. Every vendor call goes through the
`Provider` protocol, so `tests/fakes.py` covers both the synchronous and batch
paths — including a scripted batch provider that reports in-progress polls so
restart and resume behaviour is exercised without sleeping. Response parsing
for OpenAI and Gemini is tested against canned payloads built from the vendors'
own SDK types.

## Not built yet

- **Accept/reject in the app.** The reading view shows what changed but can't
  change it — that still happens in Word's Review tab. A screen that lets you
  drop individual findings and re-write the `.docx` is the obvious next step.
- **A custom app icon.** The bundle ships with the default one.
- **Re-review reuse.** Re-running a document pays for every section again, even
  ones whose text and prompt haven't moved since the last run.
