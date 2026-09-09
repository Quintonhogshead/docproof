# Native InDesign corrections

DocProof's Mac worker reads the Pre-Proof Interior Design Corrections Form, freezes each submission, resolves its book in Google Drive, and saves a new numbered InDesign edition. The original edition is preserved.

**Current rollout: local review only. Google Drive uploads are disabled at the user's request.** The delivery behaviors below describe the built capability; they do not authorize enabling uploads. Obtain the user's instruction before changing that setting.

The installed Mac worker checks for new submissions every five minutes, starting September 9, 2026 at 12:39 p.m. Eastern. Private attachments can be supplied manually while the HubSpot permission is pending. The review interface is at [DocProof](http://127.0.0.1:8767/#watch); its saved home is `output/interior-worker` in this checkout. The Choly and Arendell Word files supplied during setup have been cached against their exact attachment IDs; caching does not process historical submissions or change their books.

## What runs where

- HubSpot is the intake source; attachments and the form's Additional Notes travel together.
- This Mac runs InDesign and the signed-in Astra subscription worker. Astra interprets correction evidence and performs a separate final review. There is no paid API fallback.
- Google Drive holds the source edition, artwork, fonts, and uploaded deliverables.
- DocProof's Interior corrections panel shows job outcomes, downloads, and worker status. Optional HubSpot writeback uses explicitly configured properties and values.

The Mac must remain awake and the user must be logged in for InDesign. A dedicated Mac can use the same worker later. Only one book is processed at a time.

## Intake mapping

Portal: `7896257`. Form: `2be3b465-b0d6-4bab-b32e-6bd74dcca403`.

| Meaning | Form field | Project field |
| --- | --- | --- |
| First name | `firstname` | `author_first_name` |
| Last name | `lastname` | `author_last_name` |
| Book title | `which_book_are_these_corrections_for_` | `book_title` |
| Attachments | `interior_design_corrections_documents` | Optional property-mode mapping |
| Additional Notes | `anything_else_` | `additional_notes` in property mode |

The form and Project note fields are different. Native form mode reads the actual submission, preserving earlier rounds even if a CRM field is later overwritten.

The worker requires a start date. Older submissions remain out of the automatic queue unless deliberately included. Missing or ambiguous identity must be resolved before applying an author's instructions to a book. Book-title variations and typos can require a person to match the submission.

The Google author root is configured in watch settings. An explicit Project Drive-folder property can override name-based folder resolution. Source selection uses the highest numeric `Last Name - Book N.indd`; duplicate highest versions require attention.

## Applying and checking corrections

1. Save the source identity, revision, attachment files, notes, and extracted evidence in a local job folder.
2. Inspect the book in InDesign and export a baseline PDF and IDML.
3. Have Astra account for each correction evidence entry. Every proposed change uses an exact story and text anchor. Ambiguous or unsupported layout work is assigned to a designer or clarification.
4. Apply supported text and font-style corrections to a separate Book N+1 document.
5. Reopen that saved document in InDesign. Check every story's text, preserved formatting, fonts, links, and overset text.
6. Compare every PDF page, then have Astra visually review changed pages and adjacent pages against the original evidence.
7. Recheck the source revision and conflicting newer editions, then upload the INDD, PDF, correction report, and portable package ZIP.

The package contains the verified INDD/PDF/IDML, reports, copied links and document fonts, and original submitted attachments. Corrections requiring frame movement, artwork redesign, or unsupported layout operations remain explicitly recorded for a designer.

## Outcomes

| Outcome | Delivery behavior |
| --- | --- |
| Verified | Upload the completed Book N+1 artifacts. |
| Designer needed | Upload completed work when partial delivery is enabled; list the remaining work. |
| Clarification needed | Record the unresolved questions; partial delivery follows the configured policy. |
| Technical block | Keep the evidence and error locally; do not publish an unverified document. |

A text-integrity failure blocks publication. Formatting or visual problems cannot receive a Verified outcome. Automatic email or chat notifications are not part of this worker.

## Connections and operation

Save the HubSpot app token through Interior corrections → Native workflow connections. On this Mac the token is held in Keychain, not in source code or reports. The app requires form submission read access (`crm.objects.form_submissions.read` or `forms`), Project read access, and `files.ui_hidden.read` for private form attachments. Project write access is only needed if HubSpot writeback is enabled. Attachment download URLs are resolved through the HubSpot Files API; the token is not sent to the returned download host.

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

The native worker reloads settings before each queue pass. Turning off Interior corrections pauses new passes. An already running book finishes its current operation. `native-worker.json` records the latest check; `native_jobs/<job-id>/job.json` records each frozen submission and delivery receipts.

The installed review worker uses `--local-only`, which also prevents uploads if someone changes the saved upload checkbox. Publishing requires a deliberate worker restart without that flag as well as enabling the upload setting, after the user authorizes it.

Keep the general DocWatch clock disabled in a dedicated native-worker home; the native poll command already provides its own loop and does not start unrelated stages.

## Recovery

An interrupted upload reuses the same verified files and adopts existing matching Drive receipts. It does not run InDesign edits again. Changed source revisions, conflicting numbered exports, and altered output files require attention.

An interruption during InDesign's apply step is deliberately not replayed. Inspect the source copy, saved output, and native audits. Preserve that job as evidence. A reviewed restart must use a fresh job with confirmed source and instructions; do not simply delete the apply marker and risk repeating a partial edit.

To answer a clarification, preserve the original submission and supply the answer as a new correction round or a new reviewed local job. House rules and answers can also be supplied to a local run with `--rules` pointing to a JSON file.

For migration, pause this Mac first, copy the worker's settings and complete native job ledger to the new Mac, install the same code and InDesign/fonts/PDF renderer, then sign into Google, HubSpot, and Astra on that Mac. Do not copy login tokens through chat. Verify a synthetic job before activating the new worker; never let both Macs consume the same queue independently.
