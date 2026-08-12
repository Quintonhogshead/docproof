# The Drive output archive

DocProof's hosted build runs on a server whose local disk is thrown away on
every redeploy — which happens several times a day. A mounted volume survives
that, and finished documents live on it, but a volume can still be lost, a
region moved, an app recreated. The output archive is the durable, off-box
record: every finished job's produced files, copied into one Google Drive
folder, organised so a person can find any book and so DocProof itself can
rebuild its whole list from nothing.

It reuses DocWatch's Google sign-in — the same one the completion email uses —
so it needs no separate account or consent. It is off until you turn it on, and
turning it on files everything already finished as well as everything new.

## Turning it on

On the **DocWatch** screen, the **Output archive** card:

1. Tick **Keep a copy of every output in Drive**.
2. Paste the address of a Drive folder to archive into (open it in a browser and
   paste the whole address from the bar, exactly like the watched folder). Make
   this a folder of its own — not the folder authors and designers share.
3. Leave **Keep the original manuscript too** on unless you would rather the
   submitted files lived only in their own folders. With it on, a job can be
   re-run after a total loss, not merely read.
4. Save. If you have not signed in to Google yet, do that first (the first card
   on the same screen); the archive uses that one sign-in.

The archive serves **every** finished job — reviews, formatting and promo,
whether dropped in the app or found in the watched folder, single- or
multi-round, including a "download anyway" rewrite. It is independent of the
per-author subfolder delivery (`subfolders_enabled`): that is the author-facing
hand-off; this is DocProof's own record, and deliberately keeps its own copy.

## How it is organised

```
<the archive folder you chose>
├── Reviews/
│   └── 2026-08/
│       └── Johnson - Book - 2026-08-12 1432 - rev-a1b2c3/
│           ├── reviewed Johnson - Book.docx
│           ├── summary.md
│           ├── findings.json
│           ├── source - Johnson - Book.docx
│           └── docproof.json
├── Prep/
│   └── 2026-08/ …
└── Promo/
    └── 2026-08/ …
```

- **Kind, then month, then one folder per job.** Months keep any single listing
  short; a Drive search finds a book by name regardless.
- The job folder is named `<book> - <date time> - <job id>`. The id is what
  makes it unique and what DocProof matches on — the name is for you.
- Everything the results screen would let you download is here, plus (by
  default) the manuscript as submitted.
- `docproof.json` is the **manifest**: the whole job record, plus every file's
  Drive id. It is written **last**, so its presence means "this archive is
  complete". Restore reads it.

## What it guarantees

- **Nothing is ever lost to a redeploy.** The moment a job finishes it is
  copied to Drive; a hiccup is retried automatically with backoff, and the
  results card shows **In Drive** with a link once it lands.
- **A wiped results folder still serves.** If the local copy is gone but the job
  record remains, a download fetches it back from Drive and re-caches it.
- **A lost volume is recoverable.** From the archive alone, DocProof can rebuild
  its entire job list — the manifests carry every record. On a fresh, empty
  volume this happens on boot; an administrator can also trigger it.

## What it does *not* do

- It never deletes anything. Clearing or deleting a job in the app removes only
  the local copy and the record; the Drive archive is append-only, so the
  press's record of what was produced is never touched from here.
- It is not a backup of settings, accounts or edited prompts — those are tiny,
  live on the volume, and a `fly volumes snapshot` covers them.

## When something is wrong

- **"saving to Drive…"** on a card: the copy is in flight or waiting to retry.
  It clears to **In Drive** on its own.
- **A "Retry Drive copy" button** with a reason: Drive kept refusing (most often
  a sign-in that needs renewing — re-run the Google sign-in on the DocWatch
  screen). The button tries again immediately.
- **The whole archive silently doing nothing**: it is off, has no folder set, or
  DocProof is not signed in to Google. None of these ever fail a job — the work
  is done and safe on the volume regardless; the archive is the belt to that
  brace.
