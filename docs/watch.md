# The watcher — a Drive folder in, formatted manuscripts out

An author signs a contract and their manuscript is uploaded to a Google Drive
folder. Somebody then opens DocProof and prepares it for the house template.
That second step is the one this removes.

`docproof-watch` looks in **one folder**, a few times a day. Anything in it
that looks like a manuscript and has not been prepared yet gets
[prepped](prep.md) and the results put back beside the original:

```
Wolves of the Yard.docx          ← the author's file, untouched
tagged_Wolves of the Yard.docx   ← what a designer places
prep_notes_Wolves of the Yard.md ← what prep decided, and what it flagged
```

Then it marks the original as done and exits. There is no daemon and nothing
running in the background: `launchd` starts it, it does what it finds, and it
stops.

**It does not review or copy edit anything yet.** Prep only. The places copy
editing joins are named at the [end of this page](#what-comes-next), and they
are seams in the code rather than a rewrite.

## From inside the app, or from a terminal

There is a **DocWatch** tab in DocProof that does all of this: signing in,
choosing the folder, looking now, seeing what a pass would do, and turning
automatic passes on. If you would rather click than type, open that and skip to
[the Google Cloud part](#1-make-an-oauth-client), which you need either way —
everything after it has a button.

The two are one watcher. Same folder, same settings file, same markers; a
sign-in from the tab works in the terminal and the other way round. Only one
pass runs at a time whichever door it came in by, because they claim the same
folder.

The rest of this page is written for the terminal, because that is where the
commands need naming. The panel says the same things with cards instead.

## Setting it up

Three commands, once. The first needs about five minutes in the Google Cloud
console; there is no way around that, because Google will not let an
application read a Drive without one.

### 1. Make an OAuth client

At [console.cloud.google.com](https://console.cloud.google.com):

1. **Create a project** — call it DocProof. Any organisation will do.
2. **APIs & Services → Library → Google Drive API → Enable.** Nothing works
   without this and the error if you skip it is not obvious.
3. **APIs & Services → OAuth consent screen.** Choose **Internal**.
   - Internal means "anybody with an @yourcompany.com Google account", which
     is the correct audience and also avoids a trap: an **External** app left
     in *Testing* hands out sign-ins that **stop working after seven days**.
     You would set this up, watch it work for a week, and then find a quiet
     folder and no explanation. Internal has no such expiry.
   - Internal is only offered on Google Workspace. On a personal Gmail
     account, choose External and then **Publish app** — the unverified-app
     warning at sign-in is expected and safe to click through for an app only
     you use.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.**
5. Keep the **client ID** and **client secret** on screen for the next step.

> The client secret is not really a secret. Google's own documentation says an
> installed application cannot keep one, which is why signing in works the way
> it does below — the answer comes back to a web server on your own Mac and
> never leaves it. It is stored in `watch.json` in plain sight. The thing that
> *is* worth protecting, the refresh token, goes to the Keychain.

### 2. Sign in

```bash
docproof-watch auth
```

It asks for the client ID and secret, then opens a browser so Google can ask
whether DocProof may read your Drive. **Nothing is typed into DocProof** — your
password stays with Google. What comes back is a refresh token, and it goes
into the macOS Keychain, never into a file. (`GOOGLE_REFRESH_TOKEN` in the
environment works too, and wins, the same as the vendor keys.)

`docproof-watch auth --status` says whether it is signed in and where the
sign-in came from, never what it is.

**Why it asks for the whole of Drive.** There is a narrower scope,
`drive.file`, that grants an application only the files it created itself or
that you explicitly opened with it. Every manuscript this exists to find was
put there by somebody else, so `drive.file` cannot see any of them. There is
no middle setting.

### 3. Say which folder

Open the folder in a browser and paste the address:

```bash
docproof-watch init --folder "https://drive.google.com/drive/folders/1AbC…"
```

The id is pulled out of whatever you paste — the whole address, the `?usp=`
version, or the bare id. While you are here:

```bash
docproof-watch init --model claude-sonnet-5 --output both
```

`--output` takes `indesign` (the file a designer places, the default),
`tracked` (the same decisions as Word revisions), or `both`. `--model` takes
any model in DocProof's catalog; see [app.md](app.md#choosing-a-reviewer) for
which are worth it.

## Trying it before trusting it

Two rehearsals, in order. Neither costs anything.

```bash
docproof-watch once --dry-run
```

Lists the folder and says what it would do with each file. It downloads
nothing, prepares nothing and uploads nothing — the listing is the only
request it makes.

```
6 file(s) in the folder:

  to prepare             Wolves of the Yard.docx
  already prepared       Kestrel.docx
  DocProof wrote this    tagged_Kestrel.docx
  DocProof wrote this    prep_notes_Kestrel.md
  not a manuscript       cover art.png
  needs attention        Broken.docx

A real run would prepare 1 manuscript(s). Nothing was downloaded, prepared or
uploaded.
```

Then the full round trip with no model call:

```bash
docproof-watch once --mock-tags
```

This really does download, prepare, upload and mark — it just labels every
paragraph as running text instead of asking a model. It is how you find out
that the folder permissions are right and the files land where you expect,
before any of it is billed. The word-for-word check still runs: a rehearsal
that skipped the one gate protecting the author's words would be rehearsing
something else.

When you are happy, drop the flags:

```bash
docproof-watch once
```

## Letting it run itself

```bash
docproof-watch schedule
```

Installs a launch agent that runs a pass at 06:00, 11:00, 16:00 and 21:00.
`--times 07:30,19:00` chooses your own. `docproof-watch unschedule` removes
it, and `docproof-watch status` says what is currently set. In the app it is
the **Look even when DocProof is closed** switch, which writes the same agent.

What gets scheduled depends on which DocProof you are running. From a checkout
it is the `docproof-watch` command; from the packaged app there is no such
command anywhere on the machine, so the agent runs **the app's own binary with
`--watch-once`** — one pass, no window, no server. Either way the plist is the
same shape, and `docproof-watch status` reads it back.

There is a second clock in the app: **Look while DocProof is open**, off by
default. It exists because of the caveat below — it is what picks up a time the
Mac slept through. The two cannot collide; whichever starts first claims the
folder and the other stands aside.

**A sleeping Mac runs nothing.** macOS does not start a calendar job while the
machine is asleep, and does not go back for the one it missed — it runs at the
next scheduled time the Mac is awake for. That is survivable here, because a
manuscript waits and nothing is lost, but a closed laptop is a quiet
afternoon. If that becomes the annoying part, see
[somewhere other than a Mac](#somewhere-other-than-a-mac).

If a pass is still working when the next one is due — a long novel can outlast
a five-hour gap — the second one sees the folder is taken and stops. That is
not an error and nothing is skipped; the next pass picks the work up.

## What it has been doing

```bash
docproof-watch status
```

```
Folder:  1AbCdEfGhIjKlMnOp
Model:   claude-sonnet-5
Home:    /Users/you/Library/Application Support/DocProof/watch
Signed in: yes
Runs at: 06:00, 11:00, 16:00, 21:00

3 manuscript(s) seen:

  formatted    Wolves of the Yard.docx  $0.41
               → tagged_Wolves of the Yard.docx
               → prep_notes_Wolves of the Yard.md
  formatted    Kestrel.docx  $0.38
  failed       Broken.docx
```

The fuller account is in `watch.log` in the same folder, which is where to
look when a morning was quiet. A scheduled run's own output goes to
`launchd.log` beside it.

## How it knows what it has already done

Every file DocProof touches gets marked, using Drive's `appProperties` —
invisible metadata attached to the file itself:

| On | Property | Means |
|---|---|---|
| a manuscript | `docproof.state` | `formatted`, or `failed` |
| | `docproof.job` | which run, so `status` can find its cost |
| | `docproof.at` | when |
| | `docproof.reason` | why, when it failed |
| an output | `docproof.output` | DocProof wrote this; never read it as a manuscript |
| | `docproof.src` | which manuscript it came from |

Markers rather than a list on this Mac, because they survive things a list
does not: renaming the file, renaming the folder, a new Mac, or moving this
whole thing to a server. And they are written **last** — after the outputs
are in the folder — so a marker means everything before it finished, not that
something started.

**One thing to know about them:** `appProperties` are private to the OAuth
client that wrote them. If you delete the client in the Google console and
make a new one, every marker becomes invisible and the folder looks
untouched. There is a local record too (`state.json`), which stops the same
Mac from re-preparing anything, but a fresh setup on a fresh machine after
that would start over. Keep the client.

Names are a backstop for markers that get lost some other way — somebody
duplicates a file, or re-uploads one out of their Downloads. Anything called
`tagged_…`, `tracked_…`, `reviewed_…`, `prep_notes…` or `prep_failed…` is
left alone whatever its properties say.

## When something goes wrong

**A manuscript that fails its word-for-word check gets nothing uploaded.**
Prep writes a file and then proves, token by token, that it still says exactly
what the author wrote; a file that fails is deleted rather than shipped
([prep.md](prep.md#the-one-rule)). The watcher's part is to put nothing in the
folder, mark the manuscript `failed` with the reason, and be loud about it in
`status` and the log. It is not tried again — the failure needed a person the
first time.

The folder an author and a designer are both looking at should hold
manuscripts, not apologies, so nothing is uploaded to explain. If you would
rather it said so out loud, `upload_failure_note: true` in `watch.json` puts a
short `prep_failed_<book>.md` there instead.

**Everything else is tried again.** A model that would not answer, a folder
that would not list, a network that was not there: the file is left unmarked
and the next pass has another go — resuming from the checkpoint, so the
windows already paid for are not paid for twice. After three passes failing
the same way it is marked `failed` and left alone. Three failures in a row is
a fact about the file, not about the weather.

**One bad manuscript never stops the others.** Each is handled on its own; a
file that cannot be prepared is reported and the rest of the folder carries
on.

**A pass will not do more than five manuscripts.** Ten files appearing at once
is more often somebody reorganising a folder than ten new books. The rest wait
for the next pass, and the log says how many were left. Change
`max_files_per_tick` in `watch.json` if five is wrong for you.

## What it costs

The same as preparing the same manuscripts by hand in the app — the watcher
does not change what prep costs, only who starts it. `docproof-watch status`
totals what each book cost.

It shows up in the app's **Spending** tab too, alongside everything you
prepared by hand. Two job stores — a separate folder is a separate lock, which
is what lets a pass and the window run at the same moment — but the money comes
off the same card, so it is added up as one figure. The "Started by" column
says which half a line came from.

## Where things live

```
~/Library/Application Support/DocProof/watch/
├── watch.json      the folder, the model, the OAuth client — no secrets
├── state.json      what has been done, for picking up after a crash
├── watch.log       what happened, run by run
├── launchd.log     what a scheduled run printed
├── owner.lock      one pass at a time
├── downloads/      manuscripts as they arrived
├── results/        what prep wrote, before it went back to Drive
├── jobs/           one folder per run: the record, and its checkpoint
├── prep/           a house style guide of your own, if you drop one here
└── error_types/    edited prompts, for when copy editing arrives
```

`--home` puts all of that somewhere else, and `DOCPROOF_WATCH_HOME` does the
same. It is deliberately **not** the app's home: a separate folder means a
separate lock, so a pass and the desktop app can run at the same moment
without either adopting the other's work and billing for it twice
([app.md](app.md#one-docproof-per-folder)).

That separation has one consequence worth knowing. **A `house_styles.yaml` in
the watch home's `prep/` folder is what the watcher tags to**, and the app's
copy is somewhere else — so a style guide you install through the app's
Settings screen does not reach the watcher. Copy the file across, or keep both
folders pointed at the same thing. What the style guide is and how to write
one is in [prep.md](prep.md#swapping-in-your-own-style-guide).

## Interrupting it is cheap

The expensive step is the model, and it sits in the middle with something
written down on either side. Whatever a killed pass was doing:

| It died | The next pass |
|---|---|
| after downloading | downloads again — seconds |
| while preparing | replays the checkpoint; pays only for what is left |
| after preparing, before uploading | uploads. **Asks no model anything** |
| after an upload, before recording it | adopts the file already in the folder |
| after uploading, before marking | writes the marker |

None of that needs doing by hand. Run it again, or wait for the schedule.

## What it does not do

- **`.doc`, `.rtf`, `.odt`, `.txt`** are converted with LibreOffice if it is
  installed, and refused clearly if it is not — same as prep.
- **A very long Google Doc may refuse to export.** Drive caps that at about
  ten megabytes. Google's own explanation is repeated in the log; the fix is
  to upload it as a `.docx` instead.
- **Two manuscripts with the same name** both get prepared, and both write a
  `tagged_<name>.docx`. Drive allows it; people find it confusing.
- **Subfolders are not looked in.** One folder, on purpose — see below.
- **It cannot tell you what changed inside a manuscript.** That is review, and
  review is not wired up yet.

## What comes next

Three things are deliberately left as seams rather than guessed at:

**Copy editing.** [`tick.py`](../app/watch/tick.py) runs three slots in order
— `collect_finished`, `submit_ready`, `run_prep` — and only the third does
anything today. A copy-edit pass is a [review](app.md) submitted to a vendor's
overnight batch queue at half price by one pass and collected by a later one,
which is exactly the shape those first two slots have. Prep cannot work that
way (its windows have to be read in order) which is why there is nothing to
collect yet.

**Knowing a book has cleared developmental edits.** That is one branch in
[`stages.py`](../app/watch/stages.py), which is one pure function over one
file. The first version will be a subfolder an editor drops the file into.

**HubSpot.** The same branch, asking a deal's stage instead of looking at a
subfolder — production status stays where the team already manages it, and a
note written back on the deal is what tells a copy editor their file is ready.
The linking piece is a custom deal property holding the book's folder id,
filled in once when the contract is signed.

### Somewhere other than a Mac

Nothing here is Mac-only except `docproof-watch schedule`. The Drive client
and the sign-in are plain Python with no dependencies; the Keychain already
falls back to an environment variable; prep's LibreOffice conversion finds a
Linux install the same way it finds a Mac one. A pass is a few minutes of work
four times a day, which is a scheduled container job costing pennies — and a
machine that is never asleep. If uptime turns out to be the annoying part,
that move is a `cron` line and a secret, not a rewrite.

## The shape of it

```
app/watch/
├── settings.py   the folder, the model, the home
├── stages.py     what each file is — the seam copy editing joins at
├── drive.py      Google Drive over plain HTTP; no SDK
├── auth.py       signing in once, from a browser
├── prep.py       one manuscript: fetch, prepare, upload, mark
├── tick.py       one pass: collect → submit → prepare
├── status.py     what it has done, for whichever front end is asking
├── runner.py     the pass held open inside the app: a thread and a lock
├── schedule.py   the launchd agent
└── cli.py        docproof-watch
```

`status.py` and `runner.py` are what make one watcher answer to two front
doors. The terminal and the panel read the same account out of `status`, so
they cannot drift into saying different things about the same file; `runner`
exists only for the app, because a pass that takes an afternoon cannot happen
inside a click.

Everything below `prep.py` is the app's own machinery, unchanged: the job
store, the checkpoint, the providers, the Keychain. The watcher is a second
front door to the pipeline, not a second copy of it.

## Testing

```bash
pytest -q
```

No test in the watcher's suite reaches Google. The Drive client takes an
injected opener the same way [`app/version.py`](../app/version.py) does for
GitHub, and `fake_drive` in [tests/fakes.py](../tests/fakes.py) is a folder
that stays live — an upload lands in it and the next listing sees it — so
"the second pass finds nothing to do" is something a test can simply ask for.

The load-bearing ones are the crash tests. Each kills a pass at a chosen
point, runs another, and counts the model calls across both: the number must
not go up. And the pair that corrupt a written file on purpose, then assert
the folder received nothing at all.

The exception is the sign-in listener, which uses a real socket on
`127.0.0.1`. A fake cannot answer whether a browser arriving at that port is
heard, and the failure it prevents is specific: a browser asks for
`/favicon.ico` given half a chance, and taking that as the answer would end
the sign-in before it started.
