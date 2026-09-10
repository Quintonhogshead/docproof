# Settlement artifact fields — load only when needed

These core fields describe owner resolution and settlement evidence. Enrolled
outcomes additionally carry Astra receipt/authority evidence and
`set_by: "gpt-6-astra (final editorial review)"`; the legacy outcome example below
must not be used to fabricate or overrule an enrolled verdict.

- **`editmap.json`** (every build): `{"paragraphs": {para_id: [{"src":
  [s0,s1], "acc": [a0,a1], "text": …, "owner": key_id|null, "rows": [ids]}]},
  "owner_of": {finding_id: key_id}, "unmapped": {para_id: why}}`. The
  accepted paragraph is the concatenation of `text`; `owner` null = untouched.
- **`settlement.json`**: `{"rounds", "engine", "model", "counts": {action:
  n}, "records": [{"residual_id", "round", "action": absorb|add|revise|drop|
  query|internal_repair, "owner_finding_id", "before_replacement", "after_replacement",
  "reason", "verified_by", "para_id", "question", "kind": residual|
  edit_damage}], "open": [], "residuals_seen": [...], "cost", "notes"}`.
  Reason prefixes: `duplicate | overlap_loser | voice | intent_zone |
  style_only | fact | unanchorable | walker_wrong | unresolved_after_N |
  ambiguous_anchor | editorial_note | verifier_reverted | verifier_confirmed |
  verifier_overruled | oversize | space_deletion | rejected_* |
  no_suggestion | edit_damage:<verdict> | rewrite_class:<why> |
  undoes_house_style:<sweep key> | composite_mismatch | duplicated_fragment`.
- **Settle guards (v0.185.0, from the Georgis run).** (1) The walk, the
  change verifier, and the settle judge all carry ONE house-rule block
  (`galley/house_style.py`) and are told a house form (`4:00 AM`, `40
  percent`, unspaced em dash, serial comma, punctuation inside quotes,
  singles inside dialogue, numbers spelled to one hundred) is never an
  error; settle additionally sweeps the candidate paragraph and DROPS any
  settlement the configured sweeps would re-fire on (`undoes_house_style`).
  (2) `--mechanical-only` recognizes punctuation, spelling, grammatical
  inflections, function words, and equivalent number/time formatting. Each
  proposed correction is assessed independently; multiple necessary mechanical
  changes are allowed. Unrecognized repairs go to internal grammatical judgment.
  The judge must name a mechanical category and affirm preserved meaning. A
  query must identify missing author knowledge and supply a specific question.
  Actual number-value changes cannot be approved as mere formatting.
  (3) Every rebuilt paragraph is compared with the planned result. Mismatches,
  overlapping plans, missing edits, and introduced duplication restore the last
  verified rows for that paragraph. Corrections retry individually using fresh
  locations; independent edits are rebased onto the current text. Failed retries
  remain internal work and block certification; they never become author comments.
  The artifact
  scan fails on the same repeat. (4) A verifier flag on a composite gets a
  judge SECOND LOOK (keep|revert) before a revert: `verifier_overruled`
  keeps it, `verifier_confirmed` reverts; `verifier_reverted` only when the
  second look could not be had (deterministic engine unchanged). (5) The
  change verifier's packet is the WHOLE paragraph in both views — never a
  sentence slice — so "$0.05" and "Slow down!”" no longer read as truncated.
- **`outcome.json`**: `{"outcome": done|needs_human, "reason", "evidence":
  {words, applied_edits, edit_density_per_kword, rewrite_share,
  unresolved_queries, edit_damage, …}, "hubspot": {"object": "0-970",
  "property": "docproof", "value": …}, "set_by": assess|human}`.
- Settlement rows in `findings.json` ride `error_type: galley_settle` with
  `chunk_id: settle:<residual_id>`; every row carries `state`
  (applied|dropped|query) and `disposition_reason` after settle.
