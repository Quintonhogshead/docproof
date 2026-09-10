---
name: flight-deck
description: Run the copy-edit flight deck — 6 focused lenses over proofread text, union to clusters, posture-judged. Supports $0 subagent flights.
---

# /flight-deck — the copy-edit lane

Measured facts this skill encodes: decomposition is the recall lever
(1 generalist read proposed 32% of the human line/copy edits → 6 focused
lenses 47% → +Sonnet flights 55%). Judge posture is the dominant dial: the
same proposals judged strict = 24% accepted recall, lenient-with-hard-vetoes
= 57%. Recall is PROPOSER-bound — proposed ≈ accepted — so fly more lenses,
not stricter judges.

## Preconditions

- Input is the ALREADY-PROOFREAD text. Two-stage order is the clobbering fix:
  nothing mechanical is left to fold into a rephrase. Never fly on raw text.
- Headings excluded structurally. Dialogue spans protected at the filter.
- Posture set from the profile's genre (`flights.posture`; lenient is the
  house default — the lane offers, the author decides; literary/memoir and
  academic packs set strict).

## Procedure

1. **Fly the lenses**: economy, word_choice, flow, clarity, rhythm,
   repetition — each a separate focused read, never one generalist pass.
   - API mode: `docproof galley flights` with the plan's flight matrix
     (default Luna ×6; add Sonnet flights when the plan pays for recall).
   - **$0 subagent mode**: run each lens as a session subagent over
     ~24k-char windows with the `[Pn]` paragraph payload, verbatim
     quote→correction JSON, incremental output (dense windows die at the
     output cap AFTER the full read — split, don't retry bigger). Tell
     every subagent explicitly that it MUST WRITE its findings to the
     output file and that a reply without the file is a failed task —
     on the Purpura beta ~20% of subagents narrated their findings into
     the chat reply instead and the rows were lost. Then feed the union in
     as proposals: `docproof galley flights IN --models "" --external-
     proposals P.json` (`--models ""` = no API proposer; each row carries
     `para_id`, `quote`, `replacement`, `rationale`, optionally
     `model`/`lens`) — the rows ride the same deterministic filters and
     clustering as an API flight, then the judge rules. The judge is the
     paid step (default `gpt-5.6-luna`, from `flights.judge_model` when the
     config names one; a Claude model named with `--judge-model` BILLS via
     the API). To keep the judge at $0 too, add `--propose-only` and run
     `export-judgments` → a session subagent rules → `import-judgments`.
     Every `flights` call takes `--approval approval.json` and `--budget N`;
     it refuses (exit 5) when a model it would call is outside the approved
     set or the projection exceeds the cap.
2. **Union → clusters.** Per-paragraph transitive span-overlap merge.
   Agreement (distinct flights on a span) is a SIGNAL, never a filter — no
   cluster is dropped for being single-source.
3. **Judge with the posture's vetoes.** Lenient rejects ONLY when a change:
   alters meaning or clearly shifts emphasis; touches dialect, idiolect, a
   coined term, or a character's voice; alters a deliberate fragment or
   rhetorical repetition; is a pure lateral swap or a disimprovement. Short
   of those, a plausible improvement ships — the author decides. Confidence
   "low" = marginal but defensible → still accept at lenient.
4. **Cost control.** The judge is ~90% of spend and prices per cluster;
   keep the shared judge system prompt byte-stable so prompt caching holds.
   Batch mode halves it when the timeline allows. Send flights/judge output to
   files (`--out`, or `> runs/flights.log`) and read a summary slice — never
   dump the full proposal union or judge trace into your own context.
5. **Output**: lane-tagged (`copyedit`) edit-channel findings for the merge
   desk. Never emit force_query rows from this lane; queries belong to the
   mechanical lane's channels.

## Never

- Fly on unproofread text.
- Let a Gemini/extra proposer auto-join because a key is set — flights are
  what the plan says, exactly.
- Judge proofread findings lenient or copy-edit findings strict. Same model,
  opposite postures, by lane.
