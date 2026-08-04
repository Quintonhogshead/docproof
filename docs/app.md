# DocProof, the app

Drop in a stack of manuscripts and pick what to do with them. No terminal, no
config files, no environment variables.

- **Review for errors** — get the files back with native tracked changes, Word
  `.docx` and InDesign `.idml` alike. See [indesign.md](indesign.md) for what
  IDML support does and does not cover.
- **Prepare for layout** — tag every paragraph into a house InDesign style set
  and get back a file that maps on Place, a tracked-changes version, or both.
  See [prep.md](prep.md), including how to drop in your own style guide.

The Spending tab adds up what either has cost.

## Running it

Double-click **DocProof.app**. It opens in its own window — no browser, no
terminal. To build it:

```bash
.venv/bin/pyinstaller DocProof.spec
```

The Dock icon comes from `app/DocProof.icns`, which is checked in so a build
needs nothing but PyInstaller. To change it, edit the values in
`tools/make_icon.py` and run it — it draws every size with Core Graphics and
packs them with `iconutil`, carrying the wordmark at large sizes and a `DP`
monogram below 64px where a wordmark would just be a smudge.

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

## What are you starting with?

Above the drop zone: **Word documents**, **InDesign layouts**, or **Both**
(the default). It narrows the file picker and the drop zone to the suffixes
that answer, names anything dropped that doesn't match rather than silently
ignoring it, and — because prep is the step that gets a manuscript *into*
InDesign — choosing layouts also switches the job to "review for errors".

The buttons are built from `GET /api/formats`, including the `.doc`/`.rtf`/
`.odt`/`.txt`-and-friends list that LibreOffice converts at drop time, so
adding a format server-side never leaves the front door describing the old
one. The client filter is a convenience, not a gate: every file is still
preflighted on the server when it lands.

## Now vs. overnight

**Batch pricing is a flat 50% discount at any hour** — both vendors, all day.
"Overnight" buys you a workflow, not a cheaper rate:

| | Cost | Ready in |
|---|---|---|
| Right now | full price | minutes |
| Overnight | **half** | usually under an hour; 24h at the outside |

Either kind of document can go either way: nothing between submit and collect
knows whether it is carrying a Word manuscript or an InDesign layout, so an
`.idml` review takes the overnight discount exactly as a `.docx` does. (Prep is
the exception — see below.)

Overnight submits immediately by default. The optional "hold until 11:00 PM" is
for deferring the *submission*, and it carries a real caveat the UI states
plainly: **your Mac must be awake with DocProof open at that time.** If you just
want the discount, leave the hold unchecked — you get it either way.

**Preparing for layout has no overnight form.** Prep reads its windows in
order — what a paragraph is depends on what came before it, and each window is
sent the previous window's answers — so it cannot be split into a pile of
independent requests answered out of order. The app hides the choice for prep
jobs and the server pins them to "right now" regardless of what was asked for.

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

## Opening what came out

A finished job has **Open in Word** (or **Open in InDesign**, or for prep
**Open the file for InDesign**) next to it, plus a quieter **Show in Finder**.

Those buttons hand the file to the application on this Mac rather than
downloading a second copy of it: the window DocProof runs in is a WebView,
which cannot display a `.docx` or an `.idml` and refuses to download one, so
a link to the file would do nothing at all. `POST /api/jobs/{id}/open/{which}`
runs `open` (or `open -R` to reveal) on the file already sitting in your
output folder. Run in an ordinary browser instead — `python -m app.run` — the
route answers `501` and the page falls back to downloading the file.

If the file has been moved or renamed since the run, the button says which
file it went looking for rather than failing silently.

A finished prep job also offers **Place into the InDesign template**, once
Settings knows where your template is —
[indesign.md](indesign.md#placing-it-for-you).

## Seeing what changed

A finished review has **See what changed** next to it. That's a reading view of
the same findings that are in the reviewed file: grouped by kind of mistake,
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
docproof prep draft.docx --output both   # tag it for the house template
```

## How it fits together

```
DocProof.spec     PyInstaller recipe for the Mac app
docproof_desktop.py  the packaged app's entry script (see its docstring)
app/            FastAPI + static frontend. Job queue, scheduler, settings.
  desktop.py      native window + uvicorn on a thread; single-instance guard
  run.py          browser entry point: picks a port, opens a tab
  jobs.py         worker thread (sync reviews + prep) + ticker (scheduled, batch)
  main.py         HTTP routes
  prompts.py      reading and editing the shipped prompts
  report.py       findings.json → the reading view
  usage.py        every job's tokens and cost, added up for the Spending tab
  static/         the six screens — no build step, no framework
docproof/       the pipeline. Knows nothing about the app.
  pipeline.py     prepare → run → finish, shared by CLI, batch, and app
  batch.py        submit/poll/collect + job manifests
  prep/           manuscript prep: the second pipeline (see prep.md)
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

224 tests, none of which touch a network, and none of which start InDesign.
Every vendor call goes through the
`Provider` protocol, so `tests/fakes.py` covers both the synchronous and batch
paths — including a scripted batch provider that reports in-progress polls so
restart and resume behaviour is exercised without sleeping. Response parsing
for OpenAI and Gemini is tested against canned payloads built from the vendors'
own SDK types.

## Not built yet

- **Accept/reject in the app.** The reading view shows what changed but can't
  change it — that still happens in Word's Review tab, or InDesign's Track
  Changes panel. A screen that lets you drop individual findings and re-write
  the reviewed file is the obvious next step.
- **A layout built from nothing.** Prep's deliverable is the tagged `.docx`.
  Point Settings at your template and **Place into the InDesign template** now
  produces a real `.indd` from it — see
  [indesign.md](indesign.md#placing-it-for-you) — but by flowing the manuscript
  into a copy of *your* template, not by designing one. It also places into
  page 1's first text frame; a template whose story starts elsewhere needs a
  hand.
- **Re-review reuse.** Re-running a document pays for every section again, even
  ones whose text and prompt haven't moved since the last run.
