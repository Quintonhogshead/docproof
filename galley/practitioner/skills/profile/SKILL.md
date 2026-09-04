---
name: profile
description: Profile a manuscript before any paid work — genre, posture, tics, intent zones, bespoke-sweep candidates. $0-first.
---

# /profile — read the book before you touch it

Goal: a profile JSON the plan and every later stage trusts. Spend nothing, or
at most one cheap model call to confirm genre and curate tics.

**Context discipline (this stage is where the book leaks in).** Do NOT read the
whole extracted manuscript into your context — that block then rides every turn
for the rest of the run. Send scans to files and read back only summaries:
`docproof inventory IN > runs/inventory.txt` then `head`/`grep`; grep the
extracted text for tics/proper-nouns and read only the matching lines with a
line or two of context. You are building counts and samples, not memorizing the
book.

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
1b. **Chapter/part label map ($0) — and the renumber rows.** `galley
   profile` lists every "Chapter N"/"Part N" line as written (`chapter_labels`)
   and, when any is out of sequence or style, the import-findings rows that
   make the sequence continuous in the dominant style
   (`chapter_label_rows`; `--chapter-rows runs/chapter_rows.json` writes
   them). Labels are MECHANICS, not facts (Quinton, 2026-09-04): a gap
   (Fifteen, Seventeen…), a mangled number ("Twenty-Thirty"), "PART 3" beside
   "PART ONE" — import the rows as tracked heading edits, note the renumbering
   once in the letter, never query the author. The fact guards (settle, the
   flights judge) stand down on label paragraphs.
3b. **Number extraction ($0) — every number, always.** Grep the canonical
   text for every numeral (`\d`) and every spelled number (one … one
   hundred, thousand, million, ordinals, `half`/`dozen`) and write each hit
   with its para_id and a line of context to `runs/numbers.txt`. Tag each
   as age / date / year / time / sum / count / distance / other. This file is
   the NUMBER AUDIT's input (Quinton, 2026-09-04: Galley audits every number
   in the text): wave 1 reviews it for house style (spell out to one hundred,
   `4:00 AM`, `40 percent`), internal consistency (ages vs. years, running
   totals, weekday vs. date), and arithmetic against the rest of the book.
   A contradiction is an author QUERY, never an edit. Record the count in the
   profile (`numbers: {count, by_kind}`).
4. **Intent zones.** Read for author-declared conventions: capitalized terms of
   art ("my Mom" declared in-text), deliberate wordplay or big-letter passages,
   meta-text that discusses its own wording ("TRY and live by"), dialect and
   idiolect, personified capitals (Universe, God-adjacent emphasis). List each
   zone with its paragraph range. These are NO-EDIT zones for style and
   case — record the dominant form and protect it.
5. **Genre call + posture.** Genre determines ONLY the copy-edit dial — so on
   a MECHANICAL-ONLY run (go-live default; the phase prompt says so) the call
   is recorded for the letter and the genre pack's query scans, and no
   copy-edit posture is proposed or planned.
   - self-help/business → aggressive stylistic lane, lenient judge
   - fantasy/SF → protective, world-aware; coined words untouchable
   - literary/memoir → near proofread-only on style
   - general fiction → middle
   Mechanics posture is identical for all: hammered.
6. **Write the profile JSON** into the workspace and the casefile: genre,
   posture, word count, chapters, proper_nouns{enforce,protect}, tics[],
   intent_zones[], bespoke_sweeps[], numbers{count, by_kind, file},
   comment_budget (≈1 per 1k words unless told otherwise), reading band.

## Outputs feed

- plan pricing (word count × calibrated rates)
- consistency seeding (allowlist + respell map)
- genre pack / flights posture
- the adjudication rulebook (intent zones)
