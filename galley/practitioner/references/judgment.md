## The judgment subagent — a $0 Opus/Fable read for whole-book judgment

The Purpura head-proofreader comparison drew the line precisely: the rack's
chunked passes win the mechanical tail (deity caps, that→who, tense slips —
all "Claude missed this"), and a single frontier context wins the JUDGMENT
tail (a "lower" that should be "higher", a sentence repeated from earlier in
the paragraph, wording that drifted between two copies of itself). A frontier
model degrades as you widen its mandate over a long, error-dense book — so
the recipe is a **session subagent with a deliberately narrow mandate**, per
the model doctrine (Claude subagents never bill; Opus for the difficult read,
Fable for long-horizon threads; never Haiku here).

**Mandate (verbatim, keep it this narrow).** The subagent hunts ONLY:
- **wrong-direction words** — a word whose opposite is plainly meant
  ("bilirubin levels will be much *lower*" the morning after a decline);
- **in-scene sense breaks** a grammar pass can't see (an action or claim the
  surrounding paragraph contradicts);
- **self-repetition** — a sentence or clause repeating from earlier on the
  page with no rhetorical purpose;
- **wording drift between two copies of one line** (an epigraph, a refrain, a
  quoted callback) — beyond what `toccheck` already covers for the contents.

It does NOT flag spelling, punctuation, numbers, style, or anything a typed
pass owns — every mechanical catch it reports is noise that erodes the audit.

**Shape.** Chapter-scoped windows (the chapter_sweep window discipline: ~24k
chars, incremental output), each prompt carrying a ~1-page rolling casefile of
whole-book facts (names, established directions/quantities, refrains seen) —
never the whole book in one context. One Opus subagent per window is the
default; use Fable when a thread genuinely spans the book. Require each
subagent to WRITE its rows to a file (subagents that answer inline lose work).

**The strict screen (before any fleet row is imported).** Read the WHOLE
sentence with the change applied before deciding; reject if the applied
sentence is ungrammatical. A row that reads fine in isolation can break its
sentence: Georgis (2026-09-04), "standing silent" → "stood silent" produced
"was stood silent". The screen has the full sentence — use it.

**Output contract.** Verbatim quote→correction rows, `import-findings`
schema, on the **EDIT-channel `galley_read` type** (declared in
`error_types`, listed first) when the fix is decidable — a wrong-direction
word is mechanics, apply it tracked. A genuine author-knowledge question
rides a query row instead, under the queries-last-resort doctrine (decide or
stay silent first; collapse families). Same adjudication, artifact scan, and
reject-all audit as every other lane — this is the "Your own pen" path run
as a fleet, not a new channel.

**Plan line.** $0 (session subagents), but it is still a plan line with an
expected-yield note, and its rows are attributed to `galley_read` in the
ledger so the audit can see what the judgment lane contributed. Redding
calibration: chapter reads of this shape found ~57 real fixes the rack
missed; Purpura's judgment misses (lower/higher, the repeated sentence) are
the acceptance test — a run of this lane that would not have caught those
two is mis-prompted.
