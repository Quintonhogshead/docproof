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

Or `tools/update.sh --install`, which does that plus the tests and the copy
into `/Applications` — see [below](#which-version-am-i-running-and-how-do-i-update).

The Dock icon comes from `app/DocProof.icns`, which is checked in so a build
needs nothing but PyInstaller. To change it, edit the values in
`tools/make_icon.py` and run it — it draws every size with Core Graphics and
packs them with `iconutil`, carrying the wordmark at large sizes and a `DP`
monogram below 64px where a wordmark would just be a smudge.

That produces `dist/DocProof.app` (~43 MB). It is **unsigned**, so the very
first launch needs right-click → **Open** once; double-clicking works from
then on. Drag it to `/Applications` if you like.

### One DocProof per folder

Launching a second copy of the app doesn't start a second server — it opens
another window onto the one already running, found through the port it left in
`desktop.port`.

That covers two launches of the *app*. It does not cover a copy started from a
checkout, which shares the same default home and used to start happily
alongside it. Two runners over one job folder is expensive, not untidy:
`resume_interrupted` treats any job left in `running` as one *it* abandoned in
a crash, so the second copy adopts the first's in-flight review, resets its
progress and runs it again — the same manuscript reviewed twice, billed twice,
with both writing to the same results folder. The visible symptom is a
progress bar that jumps between two numbers.

So the real claim is on the folder, in [`app/lock.py`](../app/lock.py): every
entry point that starts a runner takes an exclusive `flock` on
`owner.lock` in the app home first, and says who has it if it can't. `flock`
rather than a PID in a file because the kernel drops it when the holder dies
however it dies — a `kill -9` leaves nothing to clean up and nothing stale to
reason about. Read-only uses (`create_app(..., start_runner=False)`, which is
what the tests build) don't claim anything.

Two homes never collide, so `--home` is the escape hatch if you genuinely want
two running at once.

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

## Which version am I running, and how do I update?

**Settings → This version** prints it: version, build date, the commit it was
built from, and the branch. `docproof --version` answers from the same place
in the terminal.

The version number lives in
[`docproof/__init__.py`](../docproof/__init__.py) and nowhere else — pyproject
reads it, `DocProof.spec` stamps it into the bundle's `Info.plist`, and the app
shows it. Releasing is editing one line.

A version alone doesn't identify a build, though: `0.1.0` covers every commit
until it doesn't, and two `DocProof.app`s a month apart otherwise look
identical. So the spec also stamps the commit, the branch, the build time and
the path of the source folder it was built from, into `build_info.json` inside
the bundle.

**Check for a newer version** asks whichever source of truth the machine
actually has, in this order:

1. **Running from a checkout** — the source *is* what is running, so it is
   current by definition. It mentions uncommitted changes.
2. **A build on the Mac that built it** (the stamped source folder is still a
   git checkout) — has `HEAD` moved since the commit this build was made from,
   and by how many changes. **Rebuild and update** builds the answer from that
   checkout; [below](#updating-on-the-machine-that-builds-it).
3. **A build somebody was sent** — no checkout to ask, so it asks the
   [published releases](#sending-docproof-to-someone) instead, and offers to
   install one.

Only the launch-time banner runs on its own, and only to *ask*. Nothing is
built, downloaded or replaced without a click.

### Updating on the machine that builds it

**In the app.** A build made from a checkout that has moved on says so at
launch — *"3 changes since this build"* — with a **Rebuild and update** button,
and Settings → *This version* has the same one. It does what the script below
does, in the same order and for the same reasons: pull, run the tests, build,
put the result in place of the running app, and reopen. About a minute, with
the stage it is on written on the button.

The tests are the part worth keeping. Installing a build that does not work
over your only copy is the failure this prevents, so a failing suite stops
before anything is replaced and says which test — the app you have is still the
one that works. A pull that cannot fast-forward stops it too: a dirty tree or a
diverged branch is a decision, and a decision needs a person.

It refuses for the same three reasons a release update does — mid-review,
running from the source, running off a disk image — and it refuses *before*
starting, so a click at the wrong moment costs nothing.

**From a terminal**, unchanged:

```bash
tools/update.sh --install
```

Prints where you are, pulls if there is a remote to pull from, runs the tests,
rebuilds, and replaces `/Applications/DocProof.app`. Without `--install` it
stops at `dist/` so you can look first; `--skip-tests` skips the suite. It
refuses to install while DocProof is running — macOS will let you swap a bundle
out from under an open app, and it misbehaves later rather than immediately,
which is the worst time to find out. (The in-app button has no such problem: it
is the running app, and it replaces itself the way the release update does.)

## Sending DocProof to someone

```bash
tools/package.sh
```

Builds `dist/DocProof-<version>-<commit>.dmg` (~38 MB): the app, a shortcut to
`/Applications` to drag it onto, and a **Read me first** page generated from
[`tools/readme_dmg.html`](../tools/readme_dmg.html) with this build's version,
date and commit substituted in — a stale read me is worse than none. Send that
one file.

### The unsigned-app problem

DocProof is **not signed with an Apple Developer ID**, so a copy that arrives
over email or Drive is quarantined. The recipient double-clicks, macOS refuses,
and they have to go to **System Settings → Privacy & Security** and press
**Open Anyway** — once per Mac, and again for each new version. The read me
walks them through it with the exact wording of the buttons, because the
alternative is that it reads like malware and they bin it.

The fix, when it's worth $99/yr: an Apple Developer ID, `codesign --deep
--options runtime`, then `xcrun notarytool submit --wait` and `xcrun stapler
staple`. That slots into `package.sh` between the PyInstaller step and
`hdiutil`, and the Open Anyway paragraph comes out of the read me.

### Publishing a release

```bash
# 1. bump __version__ in docproof/__init__.py, commit
# 2.
tools/release.sh
```

Refuses to run on a dirty tree or over an existing tag, builds the image, tags
`v<version>`, pushes, and creates the GitHub release with the image attached
and the commit subjects since the last tag as notes. `--draft` to look before
it is public.

That release is what a sent copy checks against.

### How a sent copy checks

The repo is private, so the GitHub API needs a token. Make a **fine-grained
personal access token** scoped to that one repository with **Contents:
read-only**, and hand it out with the DMG — one token does for every recipient,
and read-only on one private repo is about as small as a credential gets.

They paste it into Settings → This version. It goes into the Keychain like the
AI keys ([`app/settings.py`](../app/settings.py)), never into a file and never
back to the page. `GITHUB_TOKEN` in the environment works too.

Then **Check for a newer version** reads
`/repos/<owner>/<repo>/releases/latest`, compares the tag against this build's
version, and offers **Download** if there is a `.dmg` attached. Which repo to
ask is stamped into the build at packaging time from `git remote get-url
origin`, not hardcoded.

### Updating in one click

A packaged build also checks quietly once at launch (never from a checkout,
and any failure is silence — no token, no internet, no release yet). When a
newer release exists, a banner appears: *"DocProof 0.1.2 is ready — Update
now / Later"*. **Nothing installs without that click.**

On the Mac that built it the banner says the other thing it found — *"3 changes
since this build — Rebuild and update"* — and the button builds rather than
downloads. Same banner, same click, different source of truth; see
[Updating on the machine that builds it](#updating-on-the-machine-that-builds-it).

The click does what a person would: downloads the release's disk image, copies
the new DocProof out of it, checks the copy's own build stamp says the version
the release promised, moves the old app **to the Trash** (not to nowhere — a
bad build is a drag away from undone), puts the new one in its place, and
reopens itself. A tiny detached script does the reopening once the old process
has exited; the folder lock lifts with the process, so the new copy starts
clean. If the install step fails, the old bundle is put straight back and the
banner says nothing was changed.

It refuses, before downloading anything, in three cases, each with its own
sentence: **a document is being worked on** (an interrupted review would
resume from its checkpoint now, but an update should still never be the thing
that interrupts it); **running from the source** (pull and rebuild instead);
**running straight off the disk image** (macOS runs those from a randomized
read-only mirror — drag it to Applications first, which the read me already
says).

The manual path still exists in Settings — **Check for a newer version** and
**Download** put the disk image in `~/Downloads` for anyone who prefers to
install by hand.

Without a token nothing breaks: the launch check is silent and the manual
check says to ask whoever sent it. Every other way it can fail — expired
token, repo not visible, no release yet, no internet — gets its own sentence.

### Who can run it

Apple Silicon only (M1 or later, 2021+), macOS 11+. PyInstaller builds for the
architecture it runs on; an Intel-compatible build needs a `universal2` Python
and a rebuilt virtualenv. Windows is a separate project — see
[windows.md](windows.md).

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
up where it left off.

A "right now" review — and a prep run — survives quitting too, a different way.
There's no vendor-side state to reconnect to, so each completed call's results
are checkpointed into the job folder as they land
([`docproof/checkpoint.py`](../docproof/checkpoint.py)). Reopening DocProof
re-queues the job, replays the checkpoint, and pays only for the calls that
never happened; a run that failed and is retried resumes the same way. It used
to start over from call one — the afternoon a 192-section review was
interrupted four times and billed four times is why this exists.

The checkpoint is only trusted while its world is unchanged: edit the
manuscript, switch the model, change a prompt or the section selection, and
every cached answer is stale — the file wipes itself and the run starts clean.
A call that *failed* (a refusal, a truncation, a 5xx) is recorded but never
replayed; the resume asks again. Prep keeps its end-of-run word-for-word
verification either way, and a prep run that fails *that* check discards its
checkpoint on purpose — replaying the same tags would fail the same way. The
file is deleted the moment a job finishes.

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

## Not opening it at all

There is a third way in, for the case where the manuscripts arrive somewhere
by themselves. DocProof can watch one Google Drive folder, prepare anything new
it finds a few times a day, and put the results back beside the original — no
window, no terminal, nobody remembering to. See [watch.md](watch.md).

The **DocWatch** tab manages all of it: signing in to Google, choosing the
folder, looking now, seeing what a pass *would* do before it does it, and
turning automatic passes on. The same watcher answers to a terminal, which is
what a scheduled pass uses:

```bash
docproof-watch auth              # sign in to Google, once
docproof-watch init --folder …   # which folder to watch
docproof-watch once --dry-run    # what a pass would do, spending nothing
docproof-watch schedule          # hand it to macOS, four times a day
```

It keeps its own home, so a pass and this window never contend for a folder
lock — one pass runs at a time whichever started it, and the other stands
aside. Its spending is in the Spending tab with everything else.

## How it fits together

```
DocProof.spec     PyInstaller recipe for the Mac app
docproof_desktop.py  the packaged app's entry script (see its docstring)
app/            FastAPI + static frontend. Job queue, scheduler, settings.
  desktop.py      native window + uvicorn on a thread; attaches to a running copy
  run.py          browser entry point: picks a port, opens a tab
  lock.py         one DocProof per app home, enforced with flock
  jobs.py         worker thread (sync reviews + prep) + ticker (scheduled, batch)
  main.py         HTTP routes
  prompts.py      reading and editing the shipped prompts
  report.py       findings.json → the reading view
  usage.py        every job's tokens and cost, added up for the Spending tab
  static/         the six screens — no build step, no framework
  watch/          the headless Drive watcher (see watch.md)
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

292 tests, none of which touch a network — GitHub included, whose releases API
is driven through an injected opener the same way vendors go through a fake
provider — and none of which start InDesign or run git against the real
checkout. Every vendor call goes through the
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
