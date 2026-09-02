---
name: settle
description: Close every open item the verify gates raised — absorb, add, drop with a reason, or query — until the run has zero open candidates and a recorded outcome.
---

# /settle — zero open candidates

Galley finishes with **no to-do list**. Every finding a lane ever raised ends
as an APPLIED tracked edit, a DROPPED row with a recorded reason, or an author
QUERY. The finished-text walk and the change verifier raise the last open
items; this stage closes them **through the engine** — never by hand-patching
a row's replacement (that leaked editor notes and overran the edit guard on
Redding).

## Preconditions

- `galley verify` has run on the current build (`finished_walk.json` +
  `change_verify.json` are fresher than `findings.json`).
- The run's `--config` is the $0 replay config the final build used.

## Procedure

1. **See what is open** — `docproof galley residuals RUN --source BOOK
   --config C` lists every residual and flagged edit with its owner
   resolution (`untouched` / `inside` an applied edit / `straddle`).
2. **Settle** — `docproof galley settle RUN --source BOOK --config C
   [--rounds 3] [--engine auto|subagent|provider|none] [--context BRIEF]
   [--approval A --budget N]`. Per round it:
   - translates each residual to a SOURCE span through the build's edit map
     (`editmap.json`, the engine's own offsets — no text matching);
   - decides deterministically first: duplicate → drop; inside a locked
     intent zone → drop; editorial note → stripped (nothing left → query);
     **a number/name/title/date/quoted line change → query, never an edit**;
     pure space deletion → query; unanchorable → drop (recorded);
   - otherwise absorbs (revises the owning edit as ONE composite, absorbing
     every row it touches whole) or adds (a new edit on untouched text);
   - asks the narrow judge only when there is no usable suggestion or the
     composite grew past 1.5× — the judge answers absorb / add / drop / query
     for the FLAGGED SPAN ONLY, and its answer faces the same guards;
   - rebuilds at $0, re-verifies ONLY the touched paragraphs; a verifier flag
     on a composite reverts it to the owner's previous text and queries;
   - repeats until nothing is open or `--rounds` is spent; leftovers ship as
     `unresolved_after_N` queries carrying the walker's suggestion.
   For an unattended sweep add **`--until-clean`**: she keeps going while a
   round still finds real work (100 new items after re-reading 2,000 edits
   means keep looking) and stops after a quiet round (≤3 new, or ≤2% of what
   the round re-read) or the turn budget (`--max-turns 400`). A sweep that
   is STILL noisy when it has to stop is itself evidence: the outcome flips
   to `needs_human`. A settled fix propagates to the same word in the same
   and neighbouring paragraphs, so one flagged "recieve" fixes its twin.
3. **Read the counts** in `settlement.json` (`counts`, `notes`, `open` must
   be `[]`) and the verdict in `outcome.json`.
4. **Advance** — `galley state WS --advance settled --results RUN --source
   BOOK --config C` (refuses, exit 7, while anything is open). Then certify.

## Engine doctrine

`--engine auto` picks the **$0 subscription subagent lane** when this machine
has the Agent SDK and a Claude login (the judge and the delta verify run as
fenced single turns of Claude Code, Opus by default — set `--model sonnet`
for easy books); falls back to the configured API model (bills; carry
`--approval`/`--budget`); `none` is deterministic-only and queries what it
cannot decide. A headless run is as empowered as a session: the loop
iterates on its own.

## Outcome

`galley settle` ends by writing `outcome.json`: **`done`** (no more errors the
loop can find or decide) or **`needs_human`** with the reason — reserved for a
book with major grammatical problems where most sentences must be rewritten
(≥50 % of paragraphs took rewrite-class work, ≥60 edits per 1,000 words, ≥25 %
of paragraphs left as undecidable questions, or the verifier flagged ≥20 % of
applied edits). `galley outcome RUN --set needs_human --reason "…"` overrules
with a stated reason. The file carries the HubSpot property/value DocWatch
flips (`docproof` → `Proofing Complete` / `Needs Human Proofreader`).

## Never

- Hand-edit an owning row's replacement to fit a residual in — run settle.
- Leave a residual as a "candidate" or a "note for the next wave."
- Ship with `settlement.json` absent or `open` non-empty; certify refuses.
