---
name: draft-plan
description: Turn a profile into a priced, gated execution plan — lanes, models, passes, expected yield, and the stop rules.
---

# /draft-plan — price it before you spend it

Goal: a plan the human can approve in one read: what runs, what it costs, what
it should find, and when you'll stop.

## Reference rates (update from `galley calibrate` when a calibration exists)

`docproof galley calibrate --from-run RUN SOURCE --config C` folds a finished
run's real spend into the calibration store ($/kword per adapter+model); a bare
`review`/`replay` run with no `casefile.json` is projected from its
`findings.json` cost envelope automatically. Without `--from-run` the verb runs
the $0 seeded loop (plant → detector floor → score) and records recall only;
`--model` is not wired into that loop yet. Read the store's rates back into
this table before pricing a plan.

| Item | Measured | Notes |
|---|---|---|
| Luna full ladder, 44k words | ≈ $1, ~10 min | 9 typed passes + LT + gates + smoothing |
| Luna+Haiku ensemble w/ verifier, LT picky, repair, both gates | ≈ $4.90/44k | the Redding run-1 recipe |
| Luna chapter sweep, 6 windows | ≈ $1–2/book | WAVE 1 LINE 1 on every book; Fable ≈ $9–10; plus Sonnet 6 windows at $0 as session subagents |
| Flight deck (12 flights + Fable judge) | ≈ $27 sync / $13 batch per book | judge is ~90% of cost; scales with clusters |
| Repair channel | ≈ $0.36/book | triggered by ≥3 flags/sentence |
| Deterministic sweeps, mock replay, merge | $0 | always in the plan |

## Scope first — is this a mechanical run?

Go-live Galley is **MECHANICAL PROOFREADING ONLY** (Quinton, 2026-09-03), and
the driver runs every book that way unless told otherwise. When the run is
mechanical-only — the phase prompt says so, or the run is under `--stage
mechanical-wave` — the plan carries **no copy-edit flights, no merge desk, and
no wave-2 re-read line at all**. Not "recommend NO"; simply absent. The
approval is written `docproof galley approve … --stage mechanical-wave
--mechanical-only`, which refuses over a config with smoothing or rewrite on,
and `certify` fails the delivery if a copy-edit finding or a
`flights_findings.json` turns up. `docproof galley drive --approve auto` reads
this plan without a model: it approves only a plan with a parseable `TOTAL`
line, inside the budget, and with no copy-edit line — so keep the TOTAL line in
the skeleton's shape.

## Procedure

1. **Lanes.** Wave 1 mechanical, in this order: **the six-window chapter
   sweep FIRST** — `chapter_sweep` on Luna (paid; `--stage mechanical-wave`
   enables it, `chapter_sweep.model: gpt-5.6-luna`) plus a six-window Sonnet
   sweep run as $0 session subagents and imported (Quinton, 2026-09-04: the
   best bang for the buck by far, so it is line 0 of every plan) — then the
   ensemble ladder (Luna+Haiku union, Luna verifier) + LT (picky per posture)
   + full sweep list + repair + low-conf confirm + both gates. **The number
   audit** is its own $0 line on every plan: the profile's extraction of every
   numeral and spelled number, reviewed for house style, consistency, and
   arithmetic; contradictions become author queries. Genre pack scans as
   configured. Bespoke sweeps from the profile, each listed individually.
   The copy-edit lane (flight deck on
   the PROOFREAD text, judge posture from the profile's genre) is a
   COPY-EDIT-SCOPE line — include it only when the run is not mechanical-only.
2. **Config discipline.** Get knob names, defaults, and mechanics from
   `KNOBS.md` — never `cat` `config.py`/`default.yaml`/`sweeps.py` into context.
   Write the full run config; restate every section you touch (a config REPLACES
   default.yaml — an omitted `sweeps:` kills all sweeps). Per-category
   `passes`/`token_budget` knobs are per-run only.
3. **Price each line** = words/1000 × calibrated rate. Sum, add 15% headroom,
   compare to the API ceiling — **$10 per book** unless the gate says another
   figure. That ceiling is what `approval.json` freezes and what every paid
   verb refuses past, so a plan that does not fit under it is not a plan. Mark which lines are $0.
4. **Expected yield** per line from calibration/recall history (seeded-recall
   numbers, prior books). Be honest about what a lane can't reach.
5. **Stop rules.** Marginal-cost ceiling per wave (default: stop when a wave
   costs more than ~$2 per finding added); max waves per tier; comment budget.
6. **The gate.** Present: plan table, total, bespoke sweeps with counts and
   samples, intent zones you will protect, anything unusual. WAIT for approval.
   Scope changes after approval re-gate.

## Fleet shape and the reread — what to plan, what not to

Every $0 subagent lane is **fixed windows × two passes**: a mechanics pass,
then a slow type-and-compare pass for omissions, duplicated passages, and
sense (the Astra Georgis run: 14 windows, 3 readers, two passes each). When
recall feels thin, plan the second pass, not a fourth fleet. The verify
walk is planned as a ROTATED reread: windows assigned so no reader checks
edits it wrote. Plan the comment budget as a number (`profile.json`
`comment_budget`) and carry it to `approve --comment-budget`; plan ONE
rebuild after adjudication. A plan whose settle line expects "leftovers
ship as questions" in volume is planning to blow the ceiling.

## Plan skeleton

```
BOOK · genre · NN,NNN words · tier TN · budget $NN
0. chapter sweep (Luna 6 windows + Sonnet 6 windows $0)  $X.XX  est. NNN findings
1. sweeps + normalize            $0        (13 rules + 2 bespoke: <keys>)
1b. number audit                 $0        NNN numbers extracted; contradictions -> queries
2. mechanical ladder (ensemble)  $X.XX     est. NNN findings
3. LT (picky=<posture>)          $0.XX
4. repair + low-conf confirm     $0.XX
5. audit (hypotheses, no re-read) $X.XX
6. adjudicate (concordance) + ONE rebuild   $0
7. verify (rotated reread) + settle + certify  $0   comments <= NN (budget)
8. letter + style sheet + verification report $0
TOTAL $XX.XX  ·  stop: $2/finding marginal, N waves max
```

Copy-edit scope only (omit entirely on a mechanical run): flights (6 lenses ×
models, judge posture), merge + artifact scan, the wave-2 re-read reserve.
