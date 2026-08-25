---
name: profile
description: Profile a manuscript before any paid work — genre, posture, tics, intent zones, bespoke-sweep candidates. $0-first.
---

# /profile — read the book before you touch it

Goal: a profile JSON the plan and every later stage trusts. Spend nothing, or
at most one cheap model call to confirm genre and curate tics.

## Procedure

1. **Structure scan ($0).** Word count, chapter map, heading style (structural:
   short line, no terminal punctuation — headings may be Normal-styled),
   dialogue density, front/back matter, textboxes, tables and image lines.
   Use `docproof galley profile` if present; otherwise `docproof inventory` +
   your own reads of the extracted text.
2. **Proper-noun harvest ($0).** Capitalized-token frequency. Separate:
   character/place names (fiction), real names/institutions (non-fiction),
   coined terms (never to be "corrected" — feed the consistency allowlist).
3. **Tic hunt ($0).** Grep the canonical text for repeated author habits:
   doubled punctuation, doubled quotes (`""hello""`), unusual scene-break
   glyphs, spacing quirks, pet phrases. Record each with COUNT and 2-3
   before/after samples. Candidates with a clean regex become bespoke-sweep
   proposals for the plan gate (`docproof sweep --rule` dry-run gives you the
   canonical-form check for free — quotes are already curled post-normalization,
   so target the canonical form).
4. **Intent zones.** Read for author-declared conventions: capitalized terms of
   art ("my Mom" declared in-text), deliberate wordplay or big-letter passages,
   meta-text that discusses its own wording ("TRY and live by"), dialect and
   idiolect, personified capitals (Universe, God-adjacent emphasis). List each
   zone with its paragraph range. These are NO-EDIT zones for style and
   case — record the dominant form and protect it.
5. **Genre call + posture.** Genre determines ONLY the copy-edit dial:
   - self-help/business → aggressive stylistic lane, lenient judge
   - fantasy/SF → protective, world-aware; coined words untouchable
   - literary/memoir → near proofread-only on style
   - general fiction → middle
   Mechanics posture is identical for all: hammered.
6. **Write the profile JSON** into the workspace and the casefile: genre,
   posture, word count, chapters, proper_nouns{enforce,protect}, tics[],
   intent_zones[], bespoke_sweeps[], comment_budget (≈1 per 1k words unless
   told otherwise), reading band.

## Outputs feed

- plan pricing (word count × calibrated rates)
- consistency seeding (allowlist + respell map)
- genre pack / flights posture
- the adjudication rulebook (intent zones)
