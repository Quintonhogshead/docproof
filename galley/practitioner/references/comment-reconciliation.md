### Mechanical corrections and final comments

`U.S` → `U.S.` is seeded automatically on each settlement run. The rule cannot
match an already punctuated abbreviation. Routine grammar, punctuation, and
spelling corrections are tracked edits; an engine error is never an author query.

Internal repairs persist in `settlement.json` under both the latest decision and
`open`. They survive new verification snapshots and restarts. A round limit stops
work without declaring it settled. Standalone/legacy settlement can record an
incomplete proof using the existing `needs_human` status, with the production
failure named in its reason and `unresolved_internal` evidence. In an
Astra-enrolled workspace this evidence is an operational block, not permission
to replace Astra's final editorial verdict or apply legacy assessment thresholds.

Before delivery, finding-owned comments are checked against the final text.
Already corrected targets and duplicate questions are removed by rebuilding from
the source, preserving the author's original comments. Distinct questions in one
sentence remain distinct. `comment_reconciliation.json` binds the check and actual
comment count to the delivered DOCX hash; a later document change invalidates it.
The letter inventories actual Word comments and distinguishes internal work from
author questions. Certification refuses stale reconciliation or any internal repair.
