---
name: adjudicate
description: Screen the accumulated findings with the practitioner rulebook, then rebuild the deliverable at $0.
---

# /adjudicate — the screening rulebook

Every finding that reaches the deliverable passes this screen. **Read the
whole sentence with the change applied before deciding; reject if the applied
sentence is ungrammatical** (Georgis: "standing silent" → "stood silent" read
fine alone and produced "was stood silent" in its sentence). The output is
a curated set replayed through the engine at $0 — you adjudicate findings and
write rules; the engine writes the document.

## Rules, in order

1. **Verbatim or nothing.** A finding's quoted original must anchor exactly in
   the canonical text (post-normalization: curly quotes, canonical spaces).
   No fuzzy matching. Unanchorable → drop with a note.
2. **Intent zones are inviolate.** Anything the profile marked — declared
   caps, wordplay passages, meta-text, dialect, personified capitals — is
   kept as written. An edit inside a zone is rejected regardless of "rules";
   at most it becomes ONE query if genuinely ambiguous.
3. **Mechanics stand.** A correct Chicago fix survives style objections. A
   held mechanical fix with confused judge reasoning (vocative commas, tense,
   action-beat periods are the known buckets) is promotable — re-check it
   yourself; if the fix is objectively right, promote it.
4. **Style edits face the posture vetoes** (see flight-deck): meaning,
   voice, deliberate fragment, rhetorical repetition, lateral swap. Beyond
   the vetoes, lenient lanes ship the edit as rejectable.
5. **Cross-wave duplicates**: earliest wave wins the span; a later wave's
   identical re-find is a duplicate, not new coverage.
6. **Clusters are atomic** — adjudicate the cluster, not the member.
7. **Queries are budgeted.** Comment budget ≈ 1 per 1k words unless the plan
   says otherwise. Every query must be answerable by the author in one read;
   consistency batteries that verified CORRECT usage are not queries at all.
   Author questions ride as O==C rows.

## The $0 rebuild

1. Export kept findings; relabel non-typed rows (languagetool / rewrite /
   repair / low_confidence) onto an EDIT-channel replay type — `general_error`
   is query-channel and silently demotes.
2. Curated pass FIRST in error_types so composites win their spans; raise the
   edit guard only for hand-verified composites.
3. Replay hygiene: corrected_text → curly quotes; exclude rows where
   sanitizing would fake a whole-paragraph diff; residuals/recurrence/
   consistency/unclosed-quote OFF in the final build (their queries re-fire
   on ingested text as stale comments).
4. `docproof import-findings` / `replay` (or `--mock-findings` where those
   verbs are absent), sweeps and normalize regenerating, all paid passes off.
5. Then the merge-desk closing checks: artifact scan to clean, reject-all
   round trip byte-identical.
6. What verify raises on the rebuilt text is NOT re-adjudicated by hand: run
   `/settle`. Never patch an owning row's replacement to fit a residual in.

## Memory

Rulings the author or the human editor made (accept/reject/swap patterns,
declared conventions) get written to the practitioner memory store as
precedents — the next book starts smarter. An author's SWAP reply overrules
deterministically: their word applies, the proofreader note drops.
