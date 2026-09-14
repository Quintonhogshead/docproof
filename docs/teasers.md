# Automatic author teasers

Every successful formatting job can create one native Google Doc containing five
distinct back-cover teasers, three optional hooks, a short editorial note, and
“Teaser elements & best practices” with a modification checklist. All books use
one **Author teasers** folder in the connected Google account's Drive root.
The recommended option appears first. Teasers are 140–190 words each in two to
four paragraphs. The guide is optional reading for authors; there is no human
editorial approval step.

## Cloud workflow

1. Both manual and watched formatting jobs enqueue the same accepted manuscript
   before archiving. The queue freezes its complete text and source identity.
2. The Fly agent sidecar reads every contiguous manuscript portion through
   `gpt-5.6-sol` at `high`, using the existing cloud ChatGPT subscription login.
   Sol creates a private storysheet and writes the complete five teasers, hooks,
   note and author guide. It makes every editorial and factual decision, then
   checks and makes localized corrections to the finished public copy before
   rephrasing. This copy edit can replace at most ten exact spans and 160 words;
   the normal post-Qwen source review remains required. Saved finished writing
   survives retries, so a small correction does not restart the writing pass.
   Large paragraphs and long
   books use coverage-checked reading portions, never silent truncation. A source
   that fits in one call is read directly without an intermediate summary.
3. The Fly web server calls `Qwen/Qwen3.6-27B` through DeepInfra, using the
   key already stored in DocProof settings. Qwen only rephrases Sol's finished copy.
   Its input contains that complete copy and any previously approved rephrasings.
   It receives no open-ended writing brief, manuscript
   passages, private storysheet, ending details, protected-revelation list, raw
   review feedback or rejected draft. The actual public handoff is recorded for
   audit. Older tasks without finished Sol copy rebuild it automatically before any
   further writer call.
4. Sol compares the exact rephrasing with its finished baseline and every source
   portion, checking all five options and the author guide. A source that fits in
   one call is included directly in this review. Approval is bound to the saved draft hash
   and the complete list of manuscript portions. Sol can directly correct names,
   short factual phrases or individual sentences. Each exact replacement needs
   manuscript evidence and is limited to 40 words/320 characters; a review can
   replace at most five spans and 80 words in total. The corrected package is saved
   with its edit history and receives a complete fresh source check and approval.
   Proposed edits cannot approve their own output. After two correction rounds,
   Sol resolves remaining issues in the original copy before further rephrasing.
5. For larger revisions, Sol corrects the complete public copy itself and checks
   it for accuracy and spoilers before Qwen rephrases it. Qwen makes no editorial
   decisions and receives no ending or private review text. Previously passing
   options retain their exact text only when Sol's corresponding baseline remains
   unchanged. The complete assembled
   package still receives a fresh, source-bound review; retained prose has an
   internal provenance link to its original draft. After five unsuccessful Qwen drafts,
   Sol refreshes the brief. Temporary failures retry with increasing delays, up
   to six hours; completed, validated Sol answers are reused. At most twelve Qwen
   submissions per book per rolling day are allowed; work resumes automatically
   when that allowance becomes available. There is no editor queue or paid OpenAI
   API fallback. A service credential that expires still needs normal account
   maintenance; the job keeps retrying rather than publishing unverified copy.
   Qwen receives a 16,000-token output allowance, automatically increasing to
   32,000 after truncation. Incomplete packages never replace a valid draft;
   generation receipts include usage for unsuccessful responses too.
6. Only a passing package is formatted as DOCX, converted to a native Google Doc,
   and read back to verify all five options and every guidance section. Delivery
   uses the formatting workflow's Google OAuth connection. The upload session and
   document identity survive retries. Existing author documents are never replaced.

The program does not add a text watermark. It records the actual generation
provider and review path internally. It does not certify DeepInfra's implementation
or promise that a probabilistic watermark detector can never flag the output.

## Operation

The formatting workflow's **Author teasers** card enables the feature and shows
progress and completed document links. Activation creates/reuses the shared
folder. Only jobs completed through the formatting hook or created after
activation are recovered automatically; historical books are not bulk regenerated.

Server state lives at `<watch-home>/teasers/queue.sqlite3`, alongside settings,
progress receipts, approved DOCX files, and revision history. The agent's validated
Sol answers and transport receipts live at `/data/docproof-teasers/books`.
Authentication remains in `/data/galley-codex`; no Mac files or processes are used.

The existing Fly `galley-agent` entrypoint launches a supervisor that keeps the
teaser sidecar running beside the existing proofing poller. It restarts the sidecar
if it exits and stops both children cleanly at machine shutdown. A process lock
prevents two copies of the sidecar from working on the same persistent queue.
The cloud login is serialized using the existing Codex transport.

The exact `/api/teasers/worker` route requires the existing long agent bearer
credential before reading its bounded request body. It accepts only queue, draft,
review, heartbeat and delivery operations for the claimed task. It does not accept
arbitrary model names, destination URLs, file paths or replacement final prose.
Settings and status endpoints require an administrator on the hosted build.

## Prompt maintenance and validation

`config/teasers/editorial-standard.md` adapts the supplied manuscript-to-back-cover
prompt. It retains source verification, whole-book understanding, truthful reader
promise, functional spoiler boundaries, genre-specific positioning, sentence-level
craft, and final accuracy checks. Its old two-option deliverable is replaced with
five options and the author guide. `docproof/teasers/prompts.py` assigns the
stage-specific responsibilities and structured outputs.

Run the teaser suite together with formatting, watcher and subscription-runner
tests before release. Also run a cloud-only end-to-end test with fictional source
material, inspect the resulting native Google Doc, and render the generated DOCX
to check pagination. Deploy worker changes only while the proofing worker is idle;
never interrupt or resume a protected manuscript launch to install this feature.
