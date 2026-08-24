# The repair channel

Route error-dense sentences to a strong model, and fix each as one atomic
cluster.

## Why it exists

The field test against a delivered human proofread of *HERO* found DocProof
strong on anything it could apply to a token and weak on anything that needed it
to read a sentence and decide what the author meant. The scores are the
fingerprint: **10%** on words added/omitted, **6%** on grammar and sentence
repair. The worked example says why. The manuscript read:

> I was somewhat babysitting by the age of 8 the two young children that lived
> on the floor below us. Scotty age 4 and Sarah just 2.

The human repaired it as one decision that surfaced eight edits — insert
"watching", a period to a colon, a run of appositive commas, two spelled-out
numbers — because a broken sentence is repaired *as a sentence*. DocProof made
four of the eight token edits (the numbers, `that`→`who`) and left the sentence
ungrammatical, because the remaining edits are **right only together**: a lone
period→colon looks wrong without the appositive that follows it, so a per-edit
detector cannot propose it and a per-edit confirm would reject it on its own.

The repair channel adds the missing primitive: the **repair cluster** — a group
of co-dependent atomic edits that ship or die as a unit, judged on the composed
sentence rather than the comma.

The trigger is the other passes' own output. A sentence in which the ordinary
detectors — the model, the sweeps, LanguageTool, Sapling — flag several separate
errors is probably broken as a whole, not merely dotted with isolated slips. So
the pass counts the corrections landing in each sentence and, at or above
`repair.error_threshold` (default 3), routes **that** sentence to a strong model
(Fable by default) for one minimal repair. The read is cheap because it is scoped
to the handful of error-dense sentences the review already surfaced, not the
whole manuscript.

## The broken-vs-improvable line

This is the hardest line the pass draws, and it is drawn twice: in the proposer's
prompt and again, from the other side, in the judge's. A fiction manuscript is
full of deliberate fragments, dialect, and voice, and the cost of a miss (a
broken sentence the human proofreader still catches) is far smaller than the cost
of a false positive (the pass "repairing" a stylistic fragment — the exact voice
damage the whole pipeline is built to avoid). So both prompts err toward silence.

**BROKEN** — the pass may repair it:

- a dropped word makes the sentence ungrammatical (a missing article, verb,
  preposition, or subject)
- clauses are fused or mispunctuated so the sentence runs together or breaks in
  the wrong place
- a garble: words out of order, a doubled or half-deleted phrase, an appositive
  or list left without the punctuation that makes it parse
- a missing-word cascade where several small omissions compound

**NOT BROKEN** — leave it alone:

- merely awkward, wordy, or improvable but grammatical → that is line editing,
  and the smoothing pass owns it (query-only)
- a deliberate sentence fragment used for rhythm or effect
- dialect, idiolect, an in-voice or illiterate narrator, a stylized spelling
- a matter of style, tone, or preference with no objective error

When in doubt on any axis, the answer is "not broken."

## How it works

1. **Trigger** (`repair.triggered_sentences`). Every correction the run has
   gathered so far — the sweeps, consistency, Sapling, and the model edits — is
   attributed to the sentence its edit falls in (found by shrinking to the
   changed span, so a finding that quotes a whole paragraph still lands in the
   right sentence). A sentence carrying at least `error_threshold` separate
   corrections becomes a `BrokenSite` routed to repair. Questions do not count (a
   `force_query` finding is not evidence a sentence is broken), and a run of
   legitimate house-style commas that crosses the threshold is caught by the
   judge, not here.

2. **Repair** (`repair.repair_sites`). Each triggered sentence goes to the strong
   model (Fable by default) for one minimal repair, batched, with the same
   whole-book context the detectors get — the vocabulary, the variant
   conventions, the story sheet. The repair is diffed against the original
   (`agreement.canonical_anchors`) into member edits that share one `cluster_id`.
   Fail-closed: a sentence the model returns unchanged, or a "repair" that rewrote
   more than `max_added_chars` / `max_members` (a paraphrase, not a repair), is
   dropped for cause and counted.

3. **Judge** (`repair.confirm`). One skeptical verdict per cluster, on three
   independent axes — is the original genuinely broken, does the repair fix it
   minimally, does it preserve meaning. A cluster affirmed on all three at
   `edit_confidence` becomes its member findings (separate tracked changes
   sharing the cluster_id). A cluster affirmed as broken but at a softer
   confidence becomes **one margin question** carrying the whole repair. Anything
   else is dropped (and logged to `repair_rejected.json`).

4. **Validate** (`validator.validate_findings`). Members anchor and shrink like
   any finding, but repair findings are **guard-exempt**: a sentence repair
   legitimately inserts a dropped clause the ordinary 16-char growth cap would
   refuse, and its fabrication defence is the judge's meaning ruling, not a
   character count. The deterministic `max_added_chars` cap in step 2 still stops
   a paraphrase from riding in under the exemption.

5. **Enforce atomicity** (`repair.enforce_cluster_atomicity`), after the judge
   gates. This is the load-bearing invariant. A cluster is intact only when every
   member is still a clean tracked change; if any member did not survive — dropped
   at validation for overlapping a surer edit, or withdrawn by the meaning gate —
   every other member is withdrawn to the margin with it, via the same
   span-preserving `validator.to_query` the gates use. **The run never ships half
   a repair.**

## Delivery: separate changes, atomic gating

The members are delivered as **separate tracked changes**, not composed into one
whole-sentence strikethrough. That matches the human file — whose worked example
shows eight distinct tracked changes for one repair — and keeps each piece
reviewable. The tool's guarantee is that it never *ships* a partial repair, not
that it fuses the pieces into one un-splittable blob. Once the cluster is written
whole, what the author accepts or rejects in Word is their call, exactly as with
the human's file.

## Arbitration order

Repair members sit early in the finish() arbitration — right after the
deterministic sweeps and consistency, ahead of Sapling and the model — so the
coherent whole-sentence repair claims its spans first and a later pass's partial
token-edit inside the same sentence is dropped as overlapping. The one case that
withdraws a whole repair is a *deterministic* sweep edit inside the sentence
(sweeps are surer and go first): the member overlapping it is dropped, and
atomicity withdraws the rest to the margin. That is the safe direction — the
repair still reaches the author as a question — and it is logged.

## Measuring it first (shadow mode)

This is the highest-risk pass in the pipeline — judgment edits that write were the
failure class in the corrections engine's QA cycles — so it is meant to be
**measured before it is trusted to write**. `docproof/eval/repair_shadow.py` runs
the real repair+judge path, writes nothing, and scores each triggered sentence:

- `run_shadow(sites, provider, human_repairs=…)` — where `sites` come from
  `repair.triggered_sentences(findings, paragraphs, threshold=…)` on a real run —
  reports, per sentence, whether the pass would have written an edit, asked a
  margin question, or the judge rejected it — and, against a human reference,
  whether the machine repaired the same sentence the same way (`same` /
  `different` / `machine_only`), plus every human repair the machine `missed`.
- `human_repairs_from(base_paragraphs, repaired_paragraphs)` builds that reference
  from a before/after pair of the same manuscript (a delivered human proofread),
  counting only clustered repairs — a lone token edit is the typed passes' work,
  not this channel's.

This is Avenue G's disposition scorer at the sentence-repair granularity: not
"did the atomic comma match" but "did the machine repair the same broken
sentence, and repair it the same way."

## v1 scope and limits

- **Off by default**, opt-in per run (`repair.enabled`). It costs a strong-model
  repair of only the triggered sentences plus a judge — scoped to the error-dense
  sentences, not the whole book.
- **Whole-document only** (like the chapter sweep, smoothing, continuity). It
  needs the whole run's findings to trigger, so it runs once in `finish()` after
  the detectors are in hand, covers the sync and batch paths from one call site,
  and is disarmed on the download-anyway rebuild so a replay never re-charges it.
- **No auto-propagation.** A broken sentence is repaired where it is read. Repair
  members are excluded as a recurrence-propagation source, because a single member
  swap propagated to another identical sentence would land there with no
  cluster_id and so no atomicity guarantee — a partial repair, the one thing this
  channel must never produce. If a broken sentence recurs verbatim it is broken
  there too, and the pass reads it there.
- **Truncation is counted, not hidden.** A judge batch that comes back short has
  its unruled clusters logged and carried to the coverage ledger, so a lost window
  reads as lost work rather than as a run that found nothing to repair.

See `docproof/repair.py`, `docproof/eval/repair_shadow.py`, and the Avenue D
section of the DocProof Parity Map.
