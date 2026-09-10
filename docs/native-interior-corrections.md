# Native InDesign corrections

DocProof's desktop worker reads the Pre-Proof Interior Design Corrections Form, freezes each submission, resolves its book through a verified local registry, and saves a new numbered InDesign edition. The original edition is preserved. Native Windows support uses InDesign's COM interface; macOS uses Apple Events. The form workflow is guarded: intake may continue while InDesign is busy, but a book is claimed only after its identity, quiet period, evidence, and source checks pass.

## Windows setup

Use a signed-in Windows desktop session with a licensed InDesign installation,
the book's fonts and links, Python 3.11+, Codex CLI, and Poppler (`pdftoppm`) on
PATH. Install the Python dependencies with `pip install -e ".[interior,dev]"`.
Run these commands in that desktop user's session: a separate sandbox or service
account cannot access the user's InDesign instance or Windows Credential Manager.

```powershell
python tools/windows_interior.py native-test --home C:\DocProof\review
python tools/windows_interior.py login --home C:\DocProof\review
python tools/windows_interior.py astra-test --home C:\DocProof\review
python tools/windows_interior.py ui --home C:\DocProof\review
```

Both rehearsals create disposable books outside the live queue. The first
checks the actual InDesign automation connection, exact text/style changes,
saved output and source preservation. The second also requires the separate
Astra subscription login and exercises planning and final visual review.

Configure the **Corrections Engine** HubSpot private-app token through the local
Native workflow connections panel. A general HubSpot token used by other stages
may lack form-submission and private-file access. Tokens are stored in Windows
Credential Manager; do not put them in command arguments or startup scripts.
Google Drive uses its existing OAuth connection.

`python tools/install_native_interior_windows.py --home C:\DocProof\review`
generates review-only startup tasks for inspection. Add `--install` to register
them for this user's logon. They use the interactive desktop token, prevent
overlapping task instances, and restart after failures. The worker always uses
`--local-only`; installing it cannot enable uploads. The review UI does not
start unrelated DocWatch stages. Logs live under the worker home's `logs/`.

Before enabling a production queue, pause any old worker and reconcile its
ledger, cutoff, and receipts with the destination home. Complete the real Astra
rehearsal, confirm HubSpot form and private-attachment access, and select the
submission start date. Do not use a rehearsal home as the live queue. Production
delivery requires a separately reviewed launch without `--local-only`, as
described below.

**Installation defaults to local review.** The Windows launcher remains `--local-only`; it does not enable live delivery. A later, separately reviewed staging may pass `--enable-delivery`; that flag only removes `--local-only` when the saved watch settings already have both `corrections_native_auto_upload` and `corrections_native_form_poll` enabled. It never changes settings itself. Enable Drive delivery only after a separately reviewed launch decision confirms that the live queue contains no test jobs. Use a separate watch home for synthetic tests; source editions are preserved and delivery creates a new half-step Book edition.

The continuous native poller starts a background HubSpot collector so new submissions are captured while the serialized InDesign worker is busy. It does not activate the workflow or process historical submissions by itself. Private attachments can be supplied manually while HubSpot file permission is pending. A dedicated Mac can use the same guarded worker later after its own setup and ledger reconciliation; no Mac worker or historical cache is assumed by this guide.

## What runs where

- HubSpot is the intake source; attachments and the form's Additional Notes travel together.
- The signed-in desktop runs InDesign and the subscription worker. Luna handles straightforward correction planning; Astra handles escalations and a separate final review. There is no paid API fallback.
- Google Drive holds the source edition, artwork, fonts, and uploaded deliverables.
- DocProof's Interior corrections panel shows job outcomes, downloads, and worker status. Optional HubSpot writeback uses explicitly configured properties and values.

The interactive desktop must remain awake and the user must be logged in for
InDesign. A dedicated Mac can use the same worker later. Only one book is
processed at a time.

## Intake mapping

Portal: `7896257`. Form: `2be3b465-b0d6-4bab-b32e-6bd74dcca403`.

| Meaning | Form field | Project field |
| --- | --- | --- |
| First name | `firstname` | `author_first_name` |
| Last name | `lastname` | `author_last_name` |
| Book title | `which_book_are_these_corrections_for_` | `book_title` |
| Project ID (hidden) | `docproof_project_id` (configured name) | Project record ID |
| Attachments | `interior_design_corrections_documents` | Optional property-mode mapping |
| Attachment count (preferred) | A configured count field | — |
| Additional Notes | `anything_else_` | `additional_notes` in property mode |

The form and Project note fields are different. Native form mode reads the actual submission, preserving earlier rounds even if a CRM field is later overwritten.

The worker requires a start date. Older submissions remain out of the automatic queue unless deliberately included. Missing or ambiguous identity must be resolved before applying an author's instructions to a book. Book-title variations and typos can require a person to match the submission.

The Google author root is configured in watch settings for the older property-driven workflow. Guarded form intake uses the verified registry's exact folder and source IDs. Source selection uses the highest numeric `Last Name - Book N.indd` within that registered folder; duplicate highest versions require attention. The operator maps each verified source through the local UI before a book can be claimed.

## Guarded form queue

The **Release verified result for delivery** button applies only to a saved
`local_complete` batch, and refuses while uploads are disabled. It queues the
existing result for its source, artifact and remote checksum checks; it never
reapplies the edits. A `held` batch cannot use that release path.

The queue requires a verified book registry entry containing the Project ID, title,
author, surname, source file, and exactly one `Interior Design` folder for that
book. A folder cannot be shared by two registry entries. The registered source
must remain the highest matching numbered source; a newer or ambiguous highest
version holds the book for review. The title and byline in the document's
frontmatter are checked against the registered values and their explicit aliases;
aliases do not bypass the Project ID or registry checks.

Every valid form submission is retained as an event and every claimed group gets
a durable batch and slot receipt. Identical event content is deduplicated by its
stable event ID and form ID. A changed payload with the same event ID is kept as
a conflict and blocks the affected book rather than replacing the original.

The minimum quiet period is three hours per book. Readiness is based on the later
of the submission time and the first time that event was received locally. A
repeated poll of unchanged content does not reset the timer; a genuinely new
event for the book starts a new three-hour window for the complete pending group.
If the worker restarts while receipts are offline or incomplete, it uses the
conservative three-hour window again until the local ledger is reconciled.

An unknown or unresolved Project holds all **new** claims until its mapping and
record are verified. An already frozen batch continues unless a conflict blocks
it. A `held` batch requires explicit reviewed recovery. A `local_complete` batch
has verified local results with delivery disabled and continues to reserve its
book, so later submissions cannot overtake it.

## Applying and checking corrections

Native correction editions use `.5`: `Writer - Book 4.indd` produces
`Writer - Book 4.5.indd`; a later accepted round based on that edition produces
`Writer - Book 5.5.indd`. An integer source uses the half-step immediately above
it; a half-step source advances by one. Both integer and `.5` sources participate
in numeric highest-version selection. Other fractional versions are rejected.
Source registration, collision checks and registry advancement use the same rule.
Existing completed jobs retain their frozen filenames and hashes; they are not
silently renamed or republished under the new convention.

On Windows, the subscription worker receives a per-request read-only MCP evidence
server. It exposes only the job's registered JSON and page images, checks their
hashes on every read, and provides exact story searches without shell access.
The registry is part of the durable request identity. Install the `interior`
extra to include the MCP runtime. Codex continues to use its read-only sandbox
and the worker's separate ChatGPT login.

Word correction evidence preserves direct run formatting, including struck
deletions as well as tracked changes. Substituted fonts count as unresolved fonts.
A plan-only checkpoint can validate the complete instruction list without making
a new InDesign copy. A plan with no actionable edits and unresolved instructions
stops before the native apply stage.

1. Save the source identity, revision, attachment files, notes, and extracted evidence in a local job folder.
2. Inspect the book in InDesign and export a baseline PDF and IDML.
3. Have Luna (medium reasoning) account for each correction evidence entry and propose straightforward text edits. Exact anchors, complete evidence ownership and non-overlap are checked locally. Route ambiguous, layout, style, or repeated-anchor instructions to Astra (high reasoning) before applying the merged plan. Unsupported work remains assigned to a designer or clarification.
4. Apply supported text and font-style corrections to a separate half-step Book document.
5. Reopen that saved document in InDesign. Check every story's text, preserved formatting, fonts, links, and overset text.
6. Compare every PDF page, then have Astra visually review changed pages and adjacent pages against the original evidence.
7. Recheck the source revision and conflicting newer editions. Read back and verify the remote checksums and file identities before any HubSpot writeback or registry/book-version advance. Only then can the verified INDD, PDF, correction spreadsheet, JSON report, and portable package ZIP be delivered.

The package contains the verified INDD/PDF/IDML, reports, copied links and document fonts, and original submitted attachments. Corrections requiring frame movement, artwork redesign, or unsupported layout operations remain explicitly recorded for a designer.

## Corrections spreadsheet

Every new local workflow outcome includes `correction-audit.xlsx`, downloadable
as **Corrections spreadsheet**. Delivery names it alongside the book, for example
`Writer - Book 4.5.corrections.xlsx`, and includes it in the portable package.
The workbook is required and hash-protected: missing or changed spreadsheets
block delivery, and a retry reuses the completed report rather than rewriting it.
Older jobs without this artifact require reviewed recovery before delivery.

The report is built locally from saved evidence and receipts without another
model request. Its five sheets contain:

- **Corrections:** every planned instruction plus explicit uncovered evidence,
  source wording, status, reason, saved-text/formatting confirmation and source references.
- **Changes:** every proposed edit, exact before/replacement wording, confirmed
  saved replacement, occurrence counts, formatting requests and story offsets.
- **Evidence:** every extracted evidence unit, including context, source location,
  coverage and preserved correction wording/formatting metadata.
- **Files:** every submitted attachment slot, including missing files and duplicate
  bytes, with hashes and originating submission IDs. Identical files are analyzed
  once but their separate receipts remain visible.
- **Run details:** book identity, stage, verification/review coverage, all recorded
  blockers and output checksums. Upload status remains in the separate delivery receipt.

**Done — verified** requires saved text and formatting checks plus the complete
whole-book review. **Applied — book review required** confirms the saved text but
does not claim the book is ready. **No edit** records a planner assessment and
whether the reviewer covered it; it is never counted as an applied change.
Designer requests, clarification, invalid plans, uncovered evidence and interrupted
apply operations stay explicit. Pre-plan failures report an unknown correction
count and list all available file receipts rather than asserting zero requests.
The reviewer currently returns a whole-book verdict, not per-item visual approvals.

Long text continues in numbered Part rows. Summary totals count only the first
part of each item. Source text is serialized as literal spreadsheet text to prevent
formula execution or automatic date conversion; counts remain numeric.

Spreadsheet generation requires Node.js and `@oai/artifact-tool` on the desktop
worker. The bundled Codex Windows runtime is discovered automatically. Other
installations may set `DOCPROOF_AUDIT_NODE` to the Node executable and
`DOCPROOF_AUDIT_MODULES` to the directory containing `@oai/artifact-tool`.
The report builder ships in the Python package. A missing or failing spreadsheet
runtime causes a technical block and cannot yield a verified delivery.

## Outcomes

| Outcome | Delivery behavior |
| --- | --- |
| Verified | Deliver the completed half-step Book artifacts after remote identity/checksum readback. |
| Designer needed | Hold the batch and reserve its book; no partial automatic upload. |
| Clarification needed | Hold the batch and record the unresolved questions; no partial automatic upload. |
| Technical block | Keep the evidence and error locally, hold the batch, and do not publish an unverified document. |

The native workflow is verified-only. Partial automatic upload is disabled even
when a job has a usable local artifact. A `local_complete` result means the
local verification finished while delivery was disabled; it remains a book
reservation until an explicit reviewed delivery step. A `held` result likewise
requires a person to resolve the stated issue. The worker never automatically
recovers or repeats partial InDesign edits after an interrupted apply step.

A text-integrity failure blocks publication. Formatting or visual problems cannot receive a Verified outcome. Automatic email or chat notifications are not part of this worker.

## Efficient planning and review

The native workflow defaults to `LunaFirstReviewer`: `gpt-5.6-luna` with medium
reasoning for simple planning, `gpt-6-astra` with high reasoning for escalations,
and an independent Astra final review. `AstraReviewer` remains available for an
explicit Astra-only caller. This changes the native corrections workflow only;
other Galley subscription tasks retain their existing Astra default.

Luna applies only submitted requests; it does not proofread the book for additional
changes. Every proposal still passes the same exact-anchor, source coverage,
overlap, saved-text and formatting checks. The router performs at most one Luna
planning request and one Astra escalation request. A completed but invalid Luna
proposal sends the complete assignment to Astra. Authentication, quota, timeout
and cancellation errors stop the job instead of launching another model.
`model-routing.json`, `luna-plan.json` and `astra-escalation-plan.json` record the
route and proposals; each subscription receipt records its model, effort and usage.

The model reads the complete correction evidence and retrieves only relevant book
passages and formatting. A compact review summary carries all instructions,
edits, verification results, warnings and required image pairs; full snapshots
remain available for targeted inspection. Windows evidence tools can search up to
20 anchors or show up to four before/after pairs per call. Image batching retains
the original resolution and does not reduce required visual coverage.

The first run still compares every PDF page to detect artwork-only or formatting
changes. A completed verification checkpoint lets a review-only resume reuse the
saved inspection and rendered evidence after their hashes pass. Changed artifacts
block that shortcut. These changes reduce repeated work; Luna does not speed up
InDesign's own document-opening, recomposition or export operations.

## Connections and operation

Save the HubSpot app token through Interior corrections → Native workflow connections. Keep it in the platform's protected credential store, never in source code, reports, command arguments, or startup scripts. The app requires form submission read access (`crm.objects.form_submissions.read` or `forms`), Project read access, and `files.ui_hidden.read` for private form attachments. Project write access is only needed if HubSpot writeback is enabled. Attachment download URLs are resolved through the HubSpot Files API; the token is not sent to the returned download host.

Google Drive uses DocProof's existing OAuth connection. Astra uses the worker's own Codex login. Check the local setup with:

```sh
.venv/bin/python -m docproof.interior doctor
```

### Temporary manual attachment bridge

If an administrator has not yet granted private-file access, download the correction attachment through your normal HubSpot account. In DocProof's Interior corrections panel, the waiting submission names its missing file. Select that downloaded file and choose **Use downloaded file**. It is stored locally against the exact HubSpot attachment ID. The worker resumes after all attachments for the submission are available. Notes-only submissions do not need this step.

An operator can also supply an already downloaded file without opening the panel:

```sh
.venv/bin/python -m docproof.interior supply /absolute/path/to/corrections.docx --file-id HUBSPOT_FILE_ID --watch-home /absolute/path/to/watch
```

This does not change HubSpot permissions or upload anything to Drive. Automatic attachment retrieval can take over once the administrator grants `files.ui_hidden.read`. Original file hashes are preserved, and a different file cannot silently replace an attachment already supplied.

Run one native queue pass without running formatting, proofing, or marketing stages:

```sh
.venv/bin/python -m docproof.interior poll --watch-home /absolute/path/to/watch --local-only
```

Keep the native worker polling every five minutes:

```sh
.venv/bin/python -m docproof.interior poll --watch-home /absolute/path/to/watch --continuous --interval 300 --local-only
```

With `--continuous`, this process also starts a daemon intake collector at the
same bounded interval. The collector records HubSpot events while the main
worker is in InDesign; the existing per-home lock still serializes claims and
native work. A collector failure is recorded without storing its token or
provider response, and does not start a second InDesign operation.

The native worker reloads settings before each queue pass. Turning off Interior corrections pauses new passes. An already running book finishes its current operation. `native-worker.json` records the latest check; `native_jobs/<job-id>/job.json` records each frozen submission and delivery receipts.

The installer creates a review worker with `--local-only`, which also prevents uploads if someone changes the saved upload checkbox. After authorization, publishing requires enabling `corrections_native_auto_upload` and `corrections_native_form_poll` in the saved watch settings, then explicitly staging with `--enable-delivery`; the installer refuses that flag when either setting is false. Keep test jobs outside the live worker home; completed local jobs can otherwise become eligible for delivery. HubSpot writeback is a separate setting. The panel's manual check remains a local-only pass when `corrections_native_worker_only` is enabled; the background worker handles delivery.

Keep the general DocWatch clock disabled in a dedicated native-worker home; the native poll command already provides its own loop and does not start unrelated stages.

### Readiness before any live launch

Complete this external setup and test it in a separate watch home before a live
queue is considered:

- Install the Windows prerequisites, sign into the interactive desktop session,
  and pass the native-test and review rehearsals. Keep the installed launcher
  local-only until a separate launch review authorizes delivery.
- Configure the HubSpot form with the hidden `docproof_project_id` field using
  the actual configured field name, a book-title field, and preferably an
  attachment-count field. Confirm form, Project, and private-file permissions.
- Register every book with one verified Project, title, author, surname, source,
  and one `Interior Design` folder. Map the source through the local UI and
  resolve title/byline aliases before collecting live work.
- Confirm the three-hour per-book quiet period and reconcile the intake cutoff.
  Prior jobs are imported automatically when they are in the same worker home;
  a ledger copied from a Mac is not trusted as a drop-in replacement and must
  be reconciled before launch.
- Submit a synthetic correction, verify event and batch receipts, attachment
  handling, content deduplication, native output, page coverage, remote checksum
  readback, and the held/local-complete behavior. Remove synthetic jobs from
  the live home before any delivery authorization.

The design may activate these settings later after that review. This document
does not activate a live queue.

## Recovery

An interrupted upload reuses the same verified files and adopts existing matching Drive receipts. It does not run InDesign edits again. Changed source revisions, conflicting numbered exports, and altered output files require attention.

An interruption during InDesign's apply step is deliberately not replayed. Inspect the source copy, saved output, and native audits. Preserve that job as evidence. A reviewed restart must use a fresh job with confirmed source and instructions; do not simply delete the apply marker and risk repeating a partial edit.

To answer a clarification, preserve the original submission and supply the answer as a new correction round or a new reviewed local job. House rules and answers can also be supplied to a local run with `--rules` pointing to a JSON file.

For migration, pause the old worker first and reconcile its cutoff and receipts
with the destination home. Jobs already present in the destination worker home
are imported automatically; a Mac ledger copied into a Windows home is not
trusted without reconciliation. Install the same code and InDesign/fonts/PDF
renderer, then sign into Google, HubSpot, and Astra on the destination machine.
Do not copy login tokens through chat. Verify a synthetic job before activating
the new worker; never let two machines consume the same queue independently.
