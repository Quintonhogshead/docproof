# Automatic author teasers

Every successful formatting job can create one native Google Doc containing five
distinct back-cover teasers, three optional hooks, a short editorial note, and
“Teaser elements & best practices” with a modification checklist. All books use
one **Author teasers** folder in the connected Google account's Drive root.
The recommended option appears first. Teasers are 140–190 words each in two to
four paragraphs. The guide is optional reading for authors; there is no human
editorial approval step.

## Who writes what

The delivered document is written entirely by an open-weight model. That is the
point of the design, and every stage protects it:

- **Sol** (`gpt-5.6-sol` at `high`, the cloud ChatGPT subscription) reads the
  whole manuscript, keeps the ending and the protected-revelation list private,
  writes a public-safe editorial brief, checks that brief, and judges each draft.
  Sol never produces published text. A review carries findings only — private
  feedback for the record and public-safe *writer notes* — and the schema has no
  field for replacement wording.
- **The writer** (`deepseek-ai/DeepSeek-V4-Pro` through DeepInfra, the strongest
  open-weight writer in DocProof's catalog, with its default reasoning on) writes
  the five teasers, hooks, note and guide from the brief alone. It never sees the
  manuscript, the private storysheet, the ending, or Sol's private feedback. It
  revises from the writer notes; options Sol passed come back word for word.

The model that wrote each draft is recorded on the draft (`model`, `provider`);
every entry in `drafts` is the writer's.

## Cloud workflow

1. Both manual and watched formatting jobs enqueue the same accepted manuscript
   before archiving. The queue freezes its complete text and source identity.
2. The Fly agent sidecar reads every contiguous manuscript portion through Sol
   (coverage-checked portions, never silent truncation; a source that fits in one
   call is read directly). Sol writes the private storysheet and the public brief,
   then checks the brief for accuracy and spoilers. A rejected brief is rebriefed
   once from the checker's findings within the same attempt; a second rejection
   discards those answers so the next attempt asks afresh. Validated readings and
   briefs are cached and survive retries. A brief prepared for different review
   findings is never reused, so a rebrief after a stalled cycle really rebriefs.
3. The Fly web server calls the writer with the brief, the previous draft (if
   any), the writer notes and the list of approved options. Word and item counts
   are enforced in a tight loop on the writer — up to three cheap writer calls —
   before Sol ever sees a draft. The writer gets a 32,000-token output allowance
   (its reasoning shares it), doubling once to 64,000 after truncation. At most
   thirty writer calls per book per rolling day.
4. Sol reviews the saved draft against every manuscript portion (per-portion
   source reviews for long books, the original text directly for short ones) and
   returns a verdict bound to the draft hash and the full list of portions:
   per-option accuracy, spoiler safety, clarity, voice and distinctness, plus
   guidance approval, a recommended option, private feedback and writer notes.
5. A rejected draft goes back to the writer with the notes. After four rejected
   drafts under one brief, Sol rebriefs from its own private findings and the
   writer starts clean. Transport failures back off from one minute to at most
   fifteen; a busy subscription lock is a yield, not a failure, and never counts
   against the book. Errors are shown in the panel and are never fed to Sol as
   editorial feedback.
6. Only an approved package is formatted as DOCX, converted to a native Google
   Doc, and read back to verify all five options and every guidance section.
   Delivery uses the formatting workflow's Google OAuth connection. The upload
   session and document identity survive retries. Existing author documents are
   never replaced.

The program does not add a text watermark and records the actual generation
provider internally. It does not certify DeepInfra's implementation or promise
that a probabilistic detector can never flag the output.

## Operation

The formatting workflow's **Author teasers** card enables the feature and shows
progress and completed document links. Activation creates/reuses the shared
folder. Only jobs completed through the formatting hook or created after
activation are recovered automatically; historical books are not bulk regenerated.

Server state lives at `<watch-home>/teasers/queue.sqlite3`, alongside settings,
progress receipts, approved DOCX files, and revision history. The agent's validated
Sol answers and transport receipts live at `/data/docproof-teasers/books`.
Authentication remains in `/data/galley-codex`; no Mac files or processes are used.
The sidecar shares that login's serialization lock with the Galley proofing
worker, so the two take turns on the subscription; the sidecar waits rather than
failing when the lock is held.

The existing Fly `galley-agent` entrypoint launches a supervisor that keeps the
teaser sidecar running beside the existing proofing poller. It restarts the sidecar
if it exits and stops both children cleanly at machine shutdown. A process lock
prevents two copies of the sidecar from working on the same persistent queue.

The exact `/api/teasers/worker` route requires the existing long agent bearer
credential before reading its bounded request body. It accepts only queue, draft,
review, heartbeat and delivery operations for the claimed task. It does not accept
arbitrary model names, destination URLs, file paths or replacement prose.
Settings and status endpoints require an administrator on the hosted build.

## Prompt maintenance and validation

`config/teasers/editorial-standard.md` is the shared craft standard; its section
13 states the two models' responsibilities. `docproof/teasers/prompts.py` assigns
the stage-specific tasks and structured outputs. To change the writer, edit
`WRITER_MODEL` in `docproof/teasers/__init__.py`; it must be a DeepInfra model
in `docproof/providers/catalog.py`.

Run the teaser suite together with formatting, watcher and subscription-runner
tests before release. Also run a cloud-only end-to-end test with fictional source
material, inspect the resulting native Google Doc, and render the generated DOCX
to check pagination. Deploy worker changes only while the proofing worker is idle;
never interrupt or resume a protected manuscript launch to install this feature.
