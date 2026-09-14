# Interior corrections: one round, step by step

This is the operational walkthrough — what the HubSpot form has to write where,
how to turn the stage on (locally and on the server), what a round actually
does while you wait, and how to rehearse a single record before trusting a
tick with it. The mechanics of what the stage reads and writes are in
[watch.md](watch.md#interior-corrections-optional); this is the runbook for
running one round for real.

## The HubSpot workflow

One HubSpot workflow does the copying, from the *Pre-Proof Interior Design
Corrections Form* onto the Project record:

| the form | the workflow copies it to | DocWatch reads it as |
|---|---|---|
| the uploaded file (a marked-up PDF proof, or a Word list) | `corrections_file` (or whatever `--hubspot-corrections-file-property` names) | `ws.hubspot_corrections_file_property` |
| the typed correction text | `corrections_text` (or `--hubspot-corrections-text-property`) | `ws.hubspot_corrections_text_property` |
| the book's title | `book_title` | which of an author's several books the round belongs to |
| — | the status dropdown, set to `Ready for Corrections` | `ws.hubspot_corrections_ready_value` |

A record needs **either** the file property or the text property populated —
not both — since a form with no upload leaves `corrections_file` blank and a
form with no typed box leaves `corrections_text` blank. `_resolve` (in
`app/watch/corrections.py`) refuses a record with neither: check the workflow
before assuming the form itself is empty.

If your install polls the form directly rather than waiting on the workflow's
copy (`corrections_form_poll` on), DocWatch reads the form submissions API
instead of the record's properties, using the **forms** read scope on the
OAuth client — add it in the Google Cloud console alongside Drive's, or a poll
that should see new submissions silently sees none.

## Turning it on

Locally:

```bash
docproof-watch init --enable-corrections \
  --hubspot-corrections-file-property corrections_file \
  --hubspot-corrections-text-property corrections_text
```

This needs the HubSpot gate and per-author subfolders on first — the form
flips a CRM value, and the designer's IDML lives in the author's own folder —
so a first-time install is usually `--enable-hubspot --enable-subfolders
--enable-corrections` together, with the object, key and name properties HubSpot
already needs for formatting.

On the server, the same command runs over SSH, against the watcher's home on
the `/data` volume:

```bash
fly ssh console -a atmosphere-docproof -C \
  "docproof-watch --home /data/docproof/watch init --enable-corrections \
   --hubspot-corrections-file-property corrections_file \
   --hubspot-corrections-text-property corrections_text"
```

`docproof-watch init` prints what it is still missing (a HubSpot token, a
Google sign-in) and asks for anything required it was not given, the same as
running it interactively — see DEPLOY.md for the shape of these `fly ssh
console -C "..."` one-liners generally.

The same settings are also reachable from the app itself, for anyone without
shell access to the server: **Automations → Workflows → Interior
corrections** opens the workflow's drawer, where the switch, the two HubSpot
values, the two form properties, the designer's subfolder name, the quiet
period (in hours) and the "Read the form's own submissions" details all live
next to each other. Fill them in and click **Save correction settings** —
that is the `docproof-watch init --enable-corrections ...` flags, written the
same way, without a terminal. Turning the switch on is the last step, same as
on the command line: it is worth saving the rest first so the stage's first
pass already has somewhere to read from.

When form-poll mode goes live, pass `--corrections-form-start-after
<ISO date>` with that day's date: submissions older than it are rounds the
press already handled by hand, and they must not be folded into the next job.
Without it, an author's whole submission history is on the table.

## The three-hour quiet period

An author rarely submits the form once. A first pass at the marked-up PDF is
often followed by "actually, one more thing" twenty minutes later — and a
correction round that started on the first submission would either miss the
second one or double-apply overlapping edits.

So a ready record does not run the moment it is seen. It waits out a quiet
period (`ws.corrections_quiet_seconds`, default 10,800 — three hours) measured
from the **most recent** submission. A second form landing inside that window
pushes `ready_at` out another three hours from itself, exactly as if the first
submission had not happened; only once three hours pass with nothing new does
a pass treat the record as ready and read it. `docproof-watch corrections
status` shows exactly where each waiting record stands in that countdown —
see below.

## What comes back in Drive

Beside the designer's export, in the same `Interior Design` folder:

| file | what it is |
|---|---|
| `<surname> - Book N.5.idml` | the export with the readable corrections applied |
| `<surname> - Book N.5 - corrections.xlsx` | two sheets, **Applied** and **Not applied** — every correction the author sent, on exactly one of them |
| `<surname> - Book N.5 - notes.md` | the change log, in prose |
| `<surname> - Book N.5 - checks.jsx` | an InDesign script that walks the designer through what to check by hand |

The record moves to `Corrections Applied` once all four are uploaded — the
designer's cue to open the `.5`, run the check script, and finish the *Not
applied* sheet.

## The `.indd` rule

The engine reads **IDML only** — never the InDesign document itself. If the
folder holds a `.indd` and no matching `.idml`, the stage sees nothing to
correct and the record sits at `missing_source` forever, waiting on a file
that will never appear on its own. The designer exports it once, alongside the
`.indd`, after each round of typesetting:

**File → Export → InDesign Markup (IDML)**, saved into the same `Interior
Design` folder as `<surname> - Book N.idml` — a plain integer, never a `.5`.
`.5` is DocProof's to write.

## Multiple books, one author

An author with more than one book in flight — a series, or a second title
under contract — has more than one Project record in HubSpot, and the form's
own **title** field (copied to `book_title`) is what tells DocWatch which
book a submission belongs to. Get the book's title right on the form; a title
that does not match any open Project reads as "no book to apply this to,"
which is a `needs_human` verdict, not a silent guess at the newest export.

## Rehearsing one record

Before trusting a live tick with a record — a first submission from a new
author, a form that looks like it might have come in oddly — run the same
stage code by hand, scoped to just that record:

```bash
docproof-watch corrections status
```

lists every record currently waiting, its HubSpot id, how many times the
author has submitted, and either `ready` or `holding for Xh Ym` — how much of
the three-hour quiet period is left. Nothing here touches Drive or HubSpot; it
only reads the local state file.

```bash
docproof-watch corrections rehearse --record <hubspot-id> --dry-run
```

resolves that one record exactly as a tick would — finds the author's folder,
picks the highest `Book N.idml`, checks for a rival `.5` already there — and
says what a real pass would do, without downloading anything, reading the
model, or writing a job. Use it to catch a folder mix-up or a missing form
property before it costs anything.

```bash
docproof-watch corrections rehearse --record <hubspot-id> --now
```

runs the round for real — the same job, the same uploads, the same HubSpot
write-back a tick would do — ignoring the three-hour quiet period so you are
not left waiting on the clock while you are already watching. It counts as a
pass: state is saved and locked the same way `once` does, so a scheduled tick
afterwards picks up from wherever the rehearsal left it rather than repeating
the work.

A rehearsal exits `0` when the record came through clean, `1` when it ended in
`needs_human` or `failed` (something for a person, not a crash), and `2` when
it could not run at all — corrections switched off, HubSpot switched off, or
the install running the native InDesign engine, which this rehearsal does not
drive.

The same rehearsal is one click in the app, no terminal needed. The Interior
corrections drawer's **Waiting for corrections** readout lists every record
currently held for its quiet period — the same account `corrections status`
prints — each row with its own **Dry run** and **Run now** buttons: Dry run is
the equivalent of `--dry-run`, Run now of `--now` (it asks you to confirm
first, since it applies for real before the quiet period is up). A record that
has not shown up in that table yet can still be rehearsed by typing its
HubSpot record id into the small form underneath and using the same two
buttons there. Either way, the drawer's **Last rehearsal** readout shows what
happened — corrected, uploaded, needs a person, or failed, in the same words
the command line prints — and keeps polling while one is still running.

## Recovery

| what you see | what happened | what to do |
|---|---|---|
| stuck at `Ready for Corrections`, a `.5` already in the folder that DocProof did not write | a **rival** file — something else is already named where the output would go | rename or remove it, or move the status on by hand |
| `needs_human`, "two files ... carry the same highest Book number" | a **tie** — two exports at the same highest `Book N`, nothing to prefer | remove or rename the stale one so exactly one `Book N` is highest |
| `needs_human`, "already tried ... and marked failed" | a **failed marker** on the source IDML from an earlier attempt | fix the form or the file, then `docproof-watch clear <name-or-id>` to try again |
| `corrections status` shows the record with `0 submission(s)`, or `needs_human`, "the record carries no corrections" | **no submission** actually reached the properties — the form fired the workflow but the file or text never landed on `corrections_file` / `corrections_text` | check the workflow against the mapping above; a form resubmitted after the fix shows up as a new submission and restarts the quiet period |
