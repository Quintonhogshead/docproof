# Fixed Galley proofreading

New mechanical jobs use `--execution-mode fixed`. Python controls the sequence,
chunk boundaries, model assignments, adjudication, verification, and delivery.
There is no supervising Brain. Every reader corrects only clear proofreading
errors: no stylistic polishing, copyediting, smoothing, or rewriting.

Incoming Word revisions use the same accepted-view policy as Galley's prep
intake. Before any model call, Galley preserves the uploaded original and
creates a separate revision-free working baseline under `runs/fixed/intake`.
The receipt binds both file hashes, the accepted text, resolved revision counts
and changed package members to the review and final certificate. Existing
comments and untouched package members are preserved. Rejecting Galley's new
corrections restores this baseline, including the edits the book arrived with.
Unsupported revision types stop before paid reads with a specific intake error.

Intake then applies the house conventions silently: straight quotation marks
are curled, runs of spaces collapsed, and every ellipsis set to the house form
(… with a non-breaking space before it, `style.ellipsis`), in every part that
holds paragraphs, with no revision markup. These are conventions, not
corrections: they belong in the working baseline, and the tracked copy and the
report list only editorial corrections (the report states the counts once).
Rejecting Galley's corrections restores the normalized baseline. The receipt
records the policy (`silent-quotes-spaces-house-ellipsis-v1`), the variant
that chose the primary quotation mark, the ellipsis style, counts and touched
parts; a baseline that still holds a straight quote, a double space or an
off-house ellipsis cannot be certified. This is `fixed-intake-v3`; a v2
baseline requires a fresh workspace. The local normalization scan still runs
and now reports what remains, which should be nothing.

Intake also rejoins page-runover paragraphs. A typeset export (an InDesign or
PDF-derived .docx) stores a paragraph that spills across a page as two
consecutive paragraphs, and every reader then "repairs" the seam: a period on
the first half, an opening quotation mark on the second, a line-break hyphen
left inside a word. The export records the truth in paragraph geometry: under a
first-line-indent convention a true paragraph's first line starts one indent in
from the body margin (`left + firstLine`, whether written as 248+300 or 548+0),
and a continuation's first line starts flush at the margin (248+0). When at
least 60% of prose body paragraphs carry explicit indent geometry and at least
a quarter of those start indented, each consecutive same-style body paragraph
whose first line starts at the margin is folded onto the paragraph before it,
keeping the head's properties, indent and section break, moving the
continuation's runs, bookmarks and comment ranges, and dropping a line-break
hyphen when the dictionary knows the joined word. Text shape is never consulted.
Refused seams (a heading, a style change, an empty paragraph or a section break
on both halves, a flush head) are counted in the receipt. The join is recorded
in `intake/receipt.json` (`runover_joins`: ids, seam offsets, separators,
hyphen decisions) and in the proofreading report; it is not a tracked change,
so rejecting Galley's corrections restores the joined baseline. This is
`fixed-intake-v2`. Clean files that need none of the three transformations
keep their existing source identity; no baseline is created.

The agreed sequence (`fixed-proofreading-v8`, 2026-09-22) is below. From v8,
"Opus" means **Opus 5.5** (`claude-opus-5-5`) everywhere: it took every Opus 5
role and every Fable role. It needs Claude Code 2.1.280 or newer: the subagent
lane runs the newest CLI on the machine (`docproof.agent_lane.cli_path`), and
an older one stops the run as a deterministic failure rather than turning
every Opus read into a skipped review.


1. Preserve and identify the incoming manuscript, accept its revisions and
   rejoin its page-runover paragraphs.
2. Sonnet reads small samples distributed across the manuscript to determine
   whether it is poetry. Poetry follows the verse route — house mechanics at
   the character and word level, never a change to the poem's structure: the
   Sonnet and Luna verse typed passes, the deterministic verse sweep packet,
   the number stage and the meaning/correction checks — and finishes without
   the prose stages or requiring a ChatGPT login. In a mixed book the
   whole-book readers see the verse too, and their verse proposals pass the
   same mechanics-only gate.
3. Opus creates the Story Sheet, then opens the proofread (`opening_read`):
   it reads the whole untouched manuscript under the final readers' brief —
   the same windowed read, walk-through scope and focused sites as the final
   readers — concurrently with the typed detectors below.
4. Sonnet and Luna independently run the typed detectors. The full local
   checking pass supplies additional candidates, and so do the opening read's
   findings (tagged `origin: opening_read`). Unresolved candidates receive
   independent Sonnet and Luna screening; Opus receives only their explicit
   conflicting decisions. An omitted finding is not a rejection vote. The
   opening read's fact/logic, continuity and structure questions skip that
   screen and go to the comment reviews, and a screen that drops one of its
   evidenced edits in those categories puts it to the author, exactly as for
   a final reader. Its findings never trigger the broken-sentence repair.
5. Code collects numerals, times, and spelled-out number expressions with their
   surrounding text. Luna and Sonnet check those extracts against the existing
   Galley number policy; Opus adjudicates disagreements. This dedicated sweep
   replaces the number group in the typed pass.
6. Opus repairs clearly broken sentences while preserving intended meaning.
7. Luna checks meaning preservation and the correctness of proposed repairs
   through the ChatGPT subscription.
8. Opus and Sol independently sweep every paragraph of the corrected book.
   Sol uses the saved ChatGPT subscription login. Unresolved proposals go to
   Sonnet and Luna screening, with Opus settling only disagreements from that pair.
   The deterministic propagation and consistency sweep then runs (below).
9. Opus reads the whole current book in one request for continuity:
   names, places, businesses, relationships, timeline and geography the book
   contradicts about itself. Every finding cites verbatim evidence elsewhere in
   the book, verified by code. Evidenced edits go straight to Opus adjudication
   with the cited paragraphs attached, then the usual Luna checks; unresolved
   contradictions become author questions. Opus drops only an alias, nickname,
   deliberate variation or in-world explanation; an evidenced edit it declines
   is not discarded but demoted to an author question (Wilder 2026-09-14:
   "Mad Crabber" for the Rusty Hook Tavern was dropped as "not settled by the
   evidence" and never reached the author).
10. Opus (`opus_read`, `fable` before v8) sweeps the resulting book under the final walk-through scope and
    reviews every proposed Galley comment; the propagation and consistency
    sweep then carries its accepted decisions book-wide.
11. Astra sweeps the Opus-corrected book under the same scope and reviews
    every surviving comment, followed by the final propagation and
    consistency sweep.
12. Astra reads the finished book a second time (`final_astra`), under the
    same scope plus the last-gate brief: it corrects every clear mechanical
    error that still remains, reviews every surviving comment, and lists
    **publication blockers** — problems a proofread cannot repair (missing,
    duplicated or garbled passages, placeholder text, a chapter out of order,
    damage an earlier correction did), each anchored to an exact paragraph
    and verbatim quote that code verifies, and each carrying a `kind` and a
    `resolution` so code can tell what blocks from what is filled, asked or
    fixed. Its propagation pass runs on the resulting text, and the verdict
    below is counted here.
13. Opus gates Astra (`astra_gate`): every paragraph the two Astra readings
    changed, in text or formatting, is judged for meaning and then for
    correctness against the text Astra received. A rejection returns the
    paragraph to its pre-Astra text and drops Astra's formatting there; a
    fact, continuity or structure edit so undone becomes an author question,
    and an Astra question whose quote left with the restored text moves to
    the whole paragraph. Nothing is asked when Astra changed nothing, and an
    unavailable gate read leaves the (already Luna-checked) text standing. The
    gate never revisits the verdict. The press-method final audit runs on the
    gate's text, which is the delivered text.

## The verdict

The second Astra reading is the **only** stage that can send a book to a
human proofreader, and the rule is code's, not the reader's
(`galley.fixed_workflow.final_review_verdict`):

- more than **25** core mechanical corrections still found by that reading
  (spelling, grammar, punctuation, number and currency style, broken
  sentences — never a title italic, a usage or typesetting note, or a
  question), **or**
- any verified publication blocker

is `needs_human`; otherwise the proofread is complete (`done`).

### A blocker is only what neither a question nor a correction resolves

A publication blocker never triggers on something that could be a query to
the author or an easy fix (Quinton, 2026-09-18). The reader is told to make
the edit or raise the question instead, and each blocker it still names
carries a `kind` and a `resolution` (`query`, `edit` or `none`) so code, not
the reader, decides what counts:

- a `placeholder` — `Cover design by XXX`, `Copyright Page Placeholder`, TK,
  TBD — is **waived wherever it sits**: a proofread book goes to an interior
  designer next, not to press, and the designer or the author fills it;
- a blocker whose `resolution` is `query` or `edit` is **waived**: what can
  be asked or fixed is asked or fixed;
- only a blocker with `resolution: none` counts — or one with no resolution
  recorded, the stricter reading when the reader did not say.

A waived blocker is kept in `waived_blockers` with the reason it did not
count, and the report names each under *Reported and not counted*, so
nothing is lost.

Gunn - Book One and Jimenez - Book 1 are why. On 2026-09-18 Gunn went to a
human proofreader over `Cover design by XXX` on its copyright page, holding 24
of an allowed 25 mechanical errors, and Jimenez over `Copyright Page
Placeholder` with 17 — a poetry book with no chapter headings, which the
first, location-based waiver could not place outside the chapters. Passes,
failed by a rule written for a book going to press. A reader's own
window verdict, an earlier stage's verdict, a skipped (unavailable) window and
a skipped model review in any earlier stage are recorded as evidence and never
decide: an operational failure is not an editorial judgment. The outcome file's
reason carries the reading's own sentence and, when reads were skipped, their
count; the report and the review evidence list them. (A subscription limit
never produces skips: the lane pauses and resumes.) The counts, the blockers, the ceiling and the reason are
recorded in `final_astra.json`, in `result.json` (`final_review`), in the
outcome file and in the report; certification refuses a result whose verdict
does not follow from that recorded evidence.

## What is handed back, and where

The author folder receives the redline alone, always named
`<surname> - Book One - Pre-Proofread.docx` (spelled out whatever the source
carried; `app.watch.naming.pre_proofread_name`). Everything else is the press's
record and is filed in DocWatch's Drive **archive** only, under
`Proofing/<YYYY-MM>/<surname> - Book One/` (tagged with the Book 1's Drive id):

```
<surname> - Book One - proofreading report.md
<surname> - Book One - review evidence.json
<surname> - Book One - fixed certificate.json
<surname> - Book One - outcome.json        the verdict DocWatch reads
<surname> - Book One - clean.docx          every change accepted, comments removed
```

The base carries proofing's own input token, so the stage token can no longer
say that these are DocProof's output. Their tails do — see
`app.watch.naming.PRE_PROOFREAD_TAILS`, which `is_output_name` reads, and
`is_proof_outcome_name`, which accepts the `Book One` verdict alongside the
legacy `Book 2`/`Book Two` spellings.

Locally the same split is `handoff/` and `handoff/archive/`. The redline is
uploaded first; if DocWatch names no archive folder the run is kept and
delivery stays pending until it does. DocWatch's ticker finds the verdict in
the archive by the source tag (`proof.outcome_in_archive`) and applies it as
it always has — the HubSpot write still obeys `proof_write_back`.

## Final walk-through scope

Opus and Astra are the last human-grade pass before the book is presentable,
so their read is widened beyond clear mechanical errors — still minimal edits,
never rewrites: typesetting and layout artifacts (a line-break hyphen inside a
word, page-split fragments, stray characters); a continuity backstop with
verified evidence; facts and logic a general reader would notice (edit only
when the wording makes the fix unambiguous, otherwise query); headings, running
heads and front/back matter, read against `book_map`, a complete inventory of
every current heading and every header/footer paragraph (the older
`structure_context` is only a bounded opening-pages excerpt); and copyedit-grade
usage (brand-new before a noun, a nonrestrictive appositive, faulty
parallelism). Author voice, dialect, dialogue, deliberate fragments, invented
terms, quotations, the established variant and poetry keep their protections.
Header and footer paragraphs are owned, editable paragraphs. A deterministic
`seam_hyphen` focused check flags a hyphenated word whose parts the dictionary
does not know but whose joined form it does, or which the book writes
unhyphenated elsewhere. Continuity findings from these readers must cite
evidence like the continuity lane's; a continuity edit without verified
evidence is discarded. A citation of the finding's own paragraph is context
the reader already holds, not evidence: it is ignored, and only the remaining
citations are verified (Wilder 2026-09-14 lost sixteen walk-through findings,
twelve of them page-split queries, to a self-citation beside a valid one).
A fact/logic, continuity or structure edit these readers propose that the
screen or the Luna checks then decline is demoted to an author question and
judged, like the readers' own questions, by the comment reviews in the
walk-through scope; the stage history records it as `<stage>_demoted`.
Replacements must be plain manuscript text: a reader
replacement that introduces Markdown or backslash characters, a line break or
tab into a paragraph that has none, or whitespace at a paragraph boundary is
rejected as `rejected_invalid_proposal`. The Luna meaning and correction checks
for these stages receive the same widened scope and the verified evidence, so
an evidenced name reconciliation is not bounced as a fact change.

## What each request is sent

Every request is headed by a contract. The poetry, Story Sheet and continuity
requests get the bare proofreading contract. The number stage, the whole-book
readers (Opus, Sol and Astra, who may raise number errors themselves)
and any screening, adjudication or check request whose payload carries a
`number_style` or `currency_style` proposal get the complete number policy.
Every other request gets the editorial brief alone: on the first production
book the 3,500-token number policy rode on about 700 screening, check and
comment-review requests that had no number in question. A check request lists
the `categories` of the corrections accepted in each changed paragraph, which
is how a number correction brings its policy along. The workflow identity
hashes all four contracts.

Everything identical across the windows of one whole-book read — the Story
Sheet, the `book_map`, the opening-pages `structure_context`, the focused-check
legend and the notes below — travels once in that read's system prompt, so the
transport serves it from its prompt cache instead of writing it once per
window. The per-window payload holds only the owned and context paragraphs,
comments, focused sites, the narrative profile, citation context and paragraph
metadata. Focused sites of a check whose guidance is the same everywhere carry
no per-site `detail`; the legend states it once. `narrative_tense` sites are
sent only for paragraphs that read against the book's tense baseline or mixed
(every site is sent when the book has no baseline or fewer than 20 classified
narration paragraphs); the narrative profile still lists every owned paragraph,
without its sample text, and each window's coverage records
`tense_sites_omitted`. `paragraph_metadata` lists only owned paragraphs outside
the main document body or carrying italic or unknown formatting; an absent
entry means main-document body text whose formatting is entirely known roman.
On the first production book the focused sites were the largest part of every
final-read window, larger than the text being read.

Screening and number sites are presented under short per-request names
(`s01`…`s25`, `n01`…) that code maps back to their durable ids; the ids
stay in the stage evidence, and the stage history records each site's label.
A site's durable id is a 22-character hash, and asked to copy 25 of them a
reader sometimes returns one with a character added or dropped — Luna did so
three times running on the Gull Point book — and the coverage check then
rightly refuses the whole window. Nothing is matched approximately: a mistyped
label still fails coverage. Screening windows hold at most 25 sites, and
screening requests may answer with up to 16,000 output tokens and are asked
for one-sentence reasons: at
43-50 sites per window Sonnet's answers ran to about 11,000 tokens against a
12,000 ceiling and Luna dropped IDs from its coverage; the resulting skipped
reviews are recorded in the reason and the evidence but, since v0.217.1, do
not by themselves make a book `needs_human`.

The exhaustive `comma_boundary` generator, which the base recipe also leaves
opted out, is not part of the fixed local checks: on the first production book
it produced 6,961 screening sites of which 33 were applied, and screening them
took 48 of the run's 103 minutes.

## Screening a final reader's questions

An Opus or Astra edit is accepted directly; an Opus or Astra question is a
disputed site and goes to the Sonnet/Luna screen, with Opus on disagreement.
That screen, and that Opus ruling, carry the walk-through rider: a question
about a fact or logic a general reader would notice, a continuity
contradiction, or a heading or running-head inconsistency is in scope when the
reader names what only the author can supply and the text does not settle it.
On Wilder's first v5 run the screen, judging under the plain contract, dropped
seven of the eight such questions as "outside proofreading scope".

A completed run can be extended once, under its own identity and with new
receipts, by `docproof galley fixed-reinstate-questions <workspace> --book
<manuscript>`: the dropped fact/logic, continuity and structure questions are
screened again with the rider, Astra reviews every surviving question, a
`walkthrough_questions` stage is recorded after `astra`, and the result and
checkpoint are rewritten so the driver packages and delivers again. Nothing is
re-read; no correction is added or removed (an edit a screener proposes in this
step is recorded unapplied, because the run's meaning and correction gates did
not see it). A fact/logic, continuity or structure EDIT the screen dropped is
reinstated as the question it would now become during a run. The certificate
accepts the extra trailing stage.

## One question, one id

An author question's id is a hash of its paragraph, its quote and the missing
knowledge it asks for, so the same three are the same question however many
stages arrive at it. `_add_questions` is the only way one joins the list, and
it keeps the first: the child workflow each check window runs starts with an
empty list and cannot see what the run already holds, so its questions are
merged back through that door rather than appended.

A comment review may replace a question's wording. The replacement takes the
id its new content hashes to, and the id it left behind is retired — a later
stage that regenerates the original wording finds it already asked. Keeping
the old id on new wording is what broke the Gunn run of 2026-09-17: the
`final_astra` comment review was handed 28 questions under 27 ids, and the
call contract refused the inventory ("Coverage inventory needs unique string
IDs") two layers from the merge that produced it. Where the questions become a
call's inventory the run now names a duplicate id as a duplicate id.

## A failure that replays identically

A resume replays the call cache to reach the stage it stopped at, so a defect
in the recipe or a refused local contract costs the whole run again at every
poll and never converges. That Gunn run spent two hours twice to be refused
the same inventory, and would have gone on every five minutes.

So `deterministic_failure` names the failures that will certainly repeat — a
refused call contract, and any `FixedWorkflowError` that is not explicitly
retryable (`FixedWorkflowBusy`, another worker holding the workspace lock, is
the one that is) — and the driver reports them to the agent as exhausted
recovery. The agent holds the book for a new release, alarms once, and keeps
every checkpoint for the resume. Independently of that classification, a book
the agent blocks on the same reason twice is held too, unless the reason reads
as temporary or the driver asked for a later retry.

## Explicit disagreement gate

Every Opus adjudication request must contain actual, conflicting decisions from
both Sonnet and Luna for every assigned site. Code enforces this before transport.
Agreement to drop needs no further review; agreement to apply proceeds to the
normal correction checks. Different explanations for the same action and
replacement do not count as disagreement. Missing, exhausted, or unsafe screening
answers cause the affected suggestion to be dropped, not escalated to Opus.

Local heuristic signals are review candidates, not established errors. The pair
screens compact packets that share each full paragraph and relevant context once
while preserving all site anchors and proposal explanations. Full generator
metadata stays in the audit. All candidates remain covered; none are sampled or
truncated to reduce the queue. Opus's scheduled broken-sentence repair and full-book
sweep remain separate from adjudication and are unchanged.

The workflow identity includes `explicit-sonnet-luna-disagreements-v1`. Runs made
under the old routing policy require a fresh workspace; old adjudication receipts
cannot silently resume under the new policy.

## Concurrent execution

Independent windows run concurrently in typed detection, number review, paired screening, Opus
adjudication, poetry-section classification, whole-book sweeps, and comment
review. Separate provider pools prevent queued Claude calls from blocking Luna.
The current configuration permits eight Claude subscription reads and eight
ChatGPT subscription reads in flight; since 2026-09-16 every OpenAI model,
Luna included, reads through the ChatGPT subscription, so no OpenAI API pool
is used by default (the 24-wide API pool remains only for an explicitly
requested API read). Concurrent batches
share these ceilings; they do not multiply them. The explicit serial diagnostic
setting still enforces one call globally.

Preparation overlaps Story Sheet generation. Local scans and the opening read overlap typed reads,
and embedded-poetry and prose detectors can run together. Each independent
correction window advances through meaning review and correction review. A Luna
rejection receives an independent Sonnet check, and only a Sonnet approval against
a Luna rejection goes to Opus. These chains proceed without waiting for unrelated
windows. Changes remain isolated until committed in manuscript order. Opus
waits for the checked ensemble result; Astra waits for the checked Opus result
and comment dispositions. These are genuine text dependencies.

Sol and Astra use one [Codex App Server](https://learn.chatgpt.com/docs/app-server)
process with isolated ephemeral threads. One owner protects the refreshable
ChatGPT login while concurrent turns keep distinct schemas, outputs and durable
receipts. No authentication tokens are copied and no API fallback is introduced.
Same-request locks still prevent duplicate submission. Completed turns can be
recovered after interrupted bookkeeping; an unknown turn cannot be replayed.
Claude turns have a 15-minute timeout and Codex requests retain their bounded
30-minute execution allowance. An unavailable review follows the safe-skip
policy below rather than requiring an operator to rescue it.

The offline 720-detector/155-dispute scheduling benchmark, using an identical
20 ms simulated delay per call, ran about 5.1 times faster. This is a scheduler
measurement, not a promised production-book duration or quality comparison.

## Unattended model failures

After bounded retries, production runs freeze unusable model requests as
explicitly skipped and continue. Skipped readings receive no coverage credit,
unreviewed proposals are discarded, and failed correction checks restore the
preceding text and formatting. Operational problems never become author
questions. Original responses, failed/unknown receipts, coverage contracts,
and spending reservations remain intact; restarts reuse the saved skip.
The result and report explicitly disclose incomplete review coverage, and the
certificate rejects hidden or altered skip evidence. Source/package integrity
and local-check evidence gates remain enforced.

The number sweep reads the house policy shipped in
`config/error_types/number_style.yaml` and `galley/house_style.py`, including
contextual exceptions such as already written-out large numbers. A model does
not invent a separate number policy from the Story Sheet.

Model disagreement does not automatically create an author comment. Opus can
apply a supported correction, drop a false alarm, or identify a question that
requires author knowledge. Opus must resolve, remove, combine, or retain each
proposed comment after reading the corrected manuscript. Astra checks the
survivors. Late corrections receive targeted verification of their changed
passages. The original source and author-supplied comments remain preserved.

Whole-book sweeps record coverage of every paragraph, including books that must
be divided into multiple requests. The workflow checkpoints completed work and
reuses saved responses on resume. A fresh run can still yield different model
judgments; fixed orchestration does not promise identical AI output.

Typed coverage must include every owned paragraph exactly once. If a reader
also lists paragraphs supplied as read-only context, Galley removes only those
known context IDs from the working coverage view. Raw responses remain intact,
and context findings cannot become edits in this chunk. Missing owned IDs,
duplicate IDs and unknown IDs invalidate the response; exhausted requests are
skipped without approving their proposals.

New model suggestions need exact quotations in their assigned paragraphs. An
unknown paragraph, absent quotation or invalid occurrence rejects that proposal
as `rejected_no_anchor`, preserving the original response and a source-bound
diagnostic in the review evidence. It creates neither an edit nor an author
comment, and other valid suggestions from the completed read still proceed.
This applies to typed readers, number checks and whole-book readers, including
broken-sentence repair and frontier formatting proposals. Out-of-scope suggestions,
unsafe replacement characters, unsupported title formatting, unusable Opus
corrections and invalid generated author questions are also dropped individually
as `rejected_invalid_proposal`. A rejected check-stage replacement restores the
previous paragraph and removes the disputed formatting. Bad retained-comment
proposals are dropped; valid comments and adjacent corrections continue. The
raw model output stays unchanged, and no extra call is needed just to discard a
bad proposal. Final comment review still rechecks resolutions that may have
depended on rejected edits. This never authorizes fuzzy matching or suppresses
incomplete coverage, invalid local evidence, stale applied edits or final
document-integrity failures.

Coverage is validated inside the durable call layer before an answer is marked
complete. Orchestration supplies an immutable, request-bound `coverage.json`
inventory for typed paragraphs, number sites, full-book reads, focused checks,
adjudications, meaning/correction checks, poetry sections and comment decisions.
The inventory comes from assigned work, not IDs parsed from manuscript prose.
Incomplete terminal answers consume an attempt and retry only that request
within its original allowance and budget. Raw responses and usage remain
preserved. A previously cached, schema-valid incomplete answer can be reconciled
without changing its request identity or repeating other completed reads.
Missing or changed coverage contracts block recovery and certification.
Subscription retries use distinct transport request IDs, so a retry cannot
simply return the same incomplete cached answer. Unknown submissions retain their reservation and are skipped without automatic
resubmission; exhaustion never grants a fresh retry allowance.

The supplied Atmosphere pasted-chat method is preserved with an
[item-level coverage review](galley-press-prompt-coverage.md). Its 120 indexed
rules plus prose and table instructions become 149 accounted-for source items.
Opus and Astra share the extracted editorial brief; Opus adjudication and Luna
checks use the same rules. Narrow typed readers retain their category prompts
plus the shared scope, variant and punctuation guards. The original chat prompt
cannot schedule additional agents, change the existing number policy, make
silent edits, or broaden proofreading into stylistic rewrites.

The Story Sheet records variant/genre evidence, vocabulary choices and intended
tense/person with explicit section exceptions. Opus and Astra receive current
dialogue-matrix, serial-comma, quotation and narrative-tense sites and must
acknowledge every assigned site ID. The local tense profiler is a heuristic,
not authority to rewrite a deliberately present-tense chapter. Applicable
citation passages, real note locations and conservative formatting evidence
support the final reads. Confirmed roman long-work titles can be proposed as
tracked italics and pass through the correction gate. The final report records
actual final pattern counts, coverage and limitations, not assumed zeroes.

Local checks cover spelling and near-miss words, all house punctuation sweeps,
heading capitalization and vocabulary, quotation balance, name and spelling
consistency, abbreviations and accents, explicit date/weekday mismatches,
and the local LanguageTool grammar rules. The existing candidate generators
also examine commas, homophones, lists, heading sequence, repeated words, and
term consistency. Number and currency checks remain assigned to the bespoke
number stage so they do not receive a second independent number policy.

Quote and space normalization and possible speaker boundaries are recorded as
proposals; scanning never reformats the incoming manuscript. Citation-pattern
consistency and a deterministic structure extract supply further evidence.
Opus and Astra receive the current opening/heading excerpt when reading
structural or opening passages. This excerpt is explicitly incomplete and
cannot establish that a contents entry is missing; only supported wording or
numbering mismatches qualify for proofreading review.
Anachronism checks require an explicitly stated era. Reading-level and word-echo
measurements stay in internal diagnostics and cannot justify edits or author
questions. Poetry stays on its verse route: house mechanics only, with poetry
sections excluded from the prose checking pass and swept by the verse packet
instead; an edit that would add a terminal mark, recase a line head or change
a line break is dropped at application whatever reader proposed it.

After the ensemble sweep, and again after each of the Opus and Astra reads,
the deterministic propagation and consistency sweep runs over the current book
so that every accepted decision is applied consistently. It re-emits every
accepted word or short-phrase swap at its other occurrences, including casing
decisions (`god` changed to `God` in six of seven places proposes the seventh:
exact-case sites only, sentence-initial positions excluded, as screened edits)
and hyphenation decisions (`band-aid` to `Band-Aid`, `grown up` to `grown-up`);
the common-word query cap is lifted because every row is screened. It also
raises casing splits no edit created (`earth` ×11 against `Earth` ×3 outside
sentence-initial position, `easy speed` against `Easy Speed`): when the
dominant form leads 3:1 over at least five uses it is proposed as a tracked
edit at each outlier, and a closer split is proposed with its counts and no
preference. ALLCAPS forms, capitalized name phrases (Atlas the Elephant, Easy
Speed counted only as the phrase) and determiner-led kinship nouns (my mom
against Mom) are excluded; a casing the run has already decided by an edit
outranks the counts. Residual house-rule errors are rechecked in the same pass.

Version 0.218.0 (after the Cooper QA of 2026-09-17) widened the same pass with
whole-book scans a paragraph read structurally cannot make, each screened like
any other row: a named vessel the book pronouns as *she* proposes *her* at the
*its* strays in sentences that name her; a dialect speaker's marker families
(`dinnae`/`dinna`, `ye`/`yeh`, elided `no'`) take the speaker's own majority
spelling at 3:1 and are asked about below it; an open/closed pair the shipped
Merriam-Webster table decides (`sat phone`/`satphone`) is closed regardless of
which form leads; a figure with a unit that recurs in one form and once in a
near form (`282.6°` ×9 against `282.8°`) is a question, never an edit; a
remembered or re-quoted sentence that nearly matches an earlier one inside a
memory frame ("the words kept burning", "read it again") is corrected to the
earlier text, a frameless near-match or a verbatim repeat in the same chapter
is a question; and a casing split whose capitalized form carries a proper noun
(`Atacama plateau`) needs only three uses to be judged. Two deterministic
sweeps joined the house floor: prefixed compounds close where Merriam-Webster
closes them (`non-reflective`, `re-evaluating`, `pre-launch`; U.S. books only,
meaning-changing pairs such as `re-sign` held back) and Chicago's lowercase
`solar system`/`the universe`.

The same release put a last gate in front of every text change, whatever stage
proposed it (`galley/proposal_guards.py`, applied in `_apply`): a time already
written in 24-hour form (`17:03`, `00:05 UTC`, `0830`, "thirteen hundred")
stays 24-hour and is never given a meridiem; a pronoun that changes number
(`they` to `he`) is refused when the paragraph's only antecedent in view is
plural; a comma is never inserted beside an ellipsis; and a comma-closed line
tagged `said mildly`/`flatly`/`dryly`, or a verbatim echo of the previous
speaker, keeps its comma rather than becoming a question. A refused row is a
dropped row with a receipt in the history, not a run failure. The propagation
sweep also folds accepted swaps by case, space and hyphen: when one folded
surface was replaced two different ways (`lambchops` closed to `lamb chops` in
one place and `lamb chops` to `muttonchops` in another) none of its sites
propagate and every site is put to the screen as a question naming both
replacements, and certification fails a run whose applied edits still carry
two replacements for one folded surface.
Every candidate receives Sonnet and Luna screening, Opus on disagreement, and
the usual correction checks; each pass has its own local receipt
(`completion`, `completion_fable`, `completion_astra`, `completion_final_astra`). Local rules never
authorize an edit on their own, and Opus and Astra still review any resulting
comments.

Local scan receipts record paragraph coverage, configuration, implementation
hashes, and results. Successful scans are reused on resume. A missing checker,
incomplete LanguageTool response, or failed scan blocks completion rather than
being counted as a clean paragraph. Final native-document checks still require
that rejecting tracked edits restores the source, accepting them matches the
clean manuscript, and the delivered files match their certified evidence.

A failed local scan stores its complete normalized request. After a code repair,
resume may rerun that unpublished scan when only implementation hashes changed.
The failure and marker transition remain recorded, and every local check runs
again. Completed packets are never relabeled or replaced; changes to source,
configuration, runtime assets or prepared findings still require a fresh run.
Earlier model responses and their original budgets remain intact. An old failure
without a complete request cannot take this automatic recovery path.

Fixed driver checkpoints report `running` until certification and packaging
finish. A startup checkpoint is never proof that a manuscript is complete.

The hosted image includes Java and a pinned LanguageTool 6.8 distribution,
installed and smoke-tested during the image build. It uses the
[Python wrapper maintainer's verified build](https://github.com/jxmorris12/language_tool_python/releases/tag/LanguageTool-6.8)
of the official LanguageTool 6.8 source, with `language_tool_python==3.4.0`.
The archive SHA-256 is
`6a7f6b67b779ae9505f7579f0c41453ea8d1bd72ae750bdc2c55ba974281467d`.
Galley also verifies a pinned inventory of every extracted file before starting
the server. The image stores it at `/opt/languagetool/LanguageTool-6.8`, outside
the writable manuscript volume. The checker runs on loopback, ignores HTTP
proxy settings, and has no runtime download or public grammar-service fallback.

For a local installation, install the `galley` and `languagetool` extras and
[Java 17 or newer](https://dev.languagetool.org/java-api). Download that exact
archive explicitly, then install it into a directory you own:

```sh
python -m galley.local_runtime install --archive /path/to/LanguageTool-6.8.zip \
  --directory /path/to/LanguageTool-6.8
export GALLEY_LANGUAGETOOL_HOME=/path/to/LanguageTool-6.8
python -m galley.local_runtime verify
python -m galley.local_runtime smoke
```

`verify` checks files and Java without starting a server. `smoke` starts only
the local server and checks a synthetic sentence; it makes no model call and
reads no manuscript. The installer validates the archive before writing files.
Replace an altered installation explicitly rather than allowing a book run to
repair or download its dependencies.

Preview a new job without calling models:

```sh
docproof galley drive --book "Author - Book 1.docx" --slug author \
  --execution-mode fixed --dry-run --json
```

Run it by removing `--dry-run`. Resume with the same book and workspace; the
fixed runner resumes its own checkpoints. `--from` and `--phases` are legacy
options and cannot skip fixed stages. Model and effort overrides are rejected
because they would change the prescribed recipe. `--approve auto` accepts the
fixed recipe within the supplied budget; inspect it with `--dry-run` first when
needed. The preview names the actual stages and readers instead of listing
supervising Brain models.

Astra uses the saved ChatGPT subscription in fixed mode. The legacy
`--astra-transport api`, Astra budget, chunk-size, and output-size overrides
are rejected for this recipe. The supplied API budget covers the API readers;
saved usage and remaining engineering limits are recorded in the driver ledger
and worker progress.

The fixed call ceilings are 10,000 attempts and 20,000,000 output tokens, with
the API spending ceiling supplied by `--budget` (default $10). These are durable
engineering limits, not estimates or claims about subscription capacity.
Completed calls are reused; unknown usage retains its reserved allowance.
Nondefault legacy `--review-rounds`, `--review-calls`, and
`--review-output-tokens` overrides are rejected because they configure the
earlier verification and settlement loop.

Existing workspaces retain their saved `code` or `session` mode. Unversioned
workspaces with legacy progress stay on the legacy workflow. A fixed job cannot
be selected halfway through a legacy manuscript; use a separate workspace for a
fresh run. Fixed checkpoint identity also survives an interruption before the
first driver result is written.

The compulsory local checks belong to fixed recipe version 2; the continuity
lane, the final walk-through scope, the post-Opus and post-Astra propagation
passes and the casing sweep belong to version 3; the stage-specific contracts,
shared window context, focused-site and metadata diet, screening caps and the
removal of the comma-boundary generator belong to version 4; silent
normalization at intake and per-request site labels belong to version 5;
verse mechanics belong to version 6; the second Astra reading, its counted
verdict rule and the archive/hand-off split belong to version 7. A
workspace
created by an earlier fixed version cannot resume under the new recipe: start a
fresh workspace so the added checks cover the original manuscript and become
part of its certification. Existing checkpoints are not silently relabeled as
having completed checks that were absent from their recipe.

## Measuring a run

```sh
docproof galley fixed-timeline /path/to/workspace
```

prints one row per stage, model and effort — attempts, failed attempts, the
stage's wall-clock span, median and slowest call, input and cached tokens,
output and thinking tokens, and API spend — in order of first start, with run
totals, all read from the durable call receipts, budget ledger and transport
ledgers the run already keeps. `--json` prints the report; `--write` also saves
`runs/fixed/timeline.json`, which the fixed driver writes after every certified
delivery. Nothing is written inside `calls/`. The first production book
(2026-09-14, recipe v2) is the baseline: 1,534 calls in 103 minutes, of which
Sonnet screening took 48; 24.8 million input-side tokens, 4.25 million output
tokens, $2.16 of API spend.

The fixed sequence removes supervisory sessions and duplicate number checking.
Total time, usage, comment counts, and proofreading quality still need a
same-manuscript comparison before claiming a measured efficiency gain;
`galley fixed-timeline` is how the comparison is read.

Grouped dispute responses may be adapted from a complete inventory of nested
proposal decisions. This requires every exact assigned proposal ID once, no
mixed group/proposal assignments, and verified source coordinates for every
group and proposal. Unassigned decision rows are discarded and audited; they
never count toward coverage. Exact
approved proposal replacements are composed into the group span; ambiguous,
novel or conflicting proposal-ID decisions discard that group rather than
inventing a correction or author comment. Proper group-ID decisions retain the
usual Opus adjudication contract. A request-bound normalization audit records
the mapping and resulting hash; raw responses and coverage inventories remain
unchanged. Saved validation failures are rechecked before considering another
attempt, so an already-complete alternative representation can recover even
at its saved retry limit without another submission or additional allowance.
Missing actual decisions discard the unresolved suggestions when the original
allowance is exhausted; the skipped review remains explicit in the evidence.

Extra unassigned decisions are discarded consistently for disputes, meaning and
correction checks, number-reader comment outputs, and final comment reviews.
This is allowed only when every real assigned decision is present exactly once.
An unknown row cannot fill an omission or resolve duplicate assigned decisions.
Empty comment inventories remain empty even when a reader invents a comment
resolution. Request-bound audits retain the discarded rows and the normalized
result hash, while raw responses stay unchanged. This does not relax paragraph,
focused-check, poetry-section, source, or final document-integrity validation.
