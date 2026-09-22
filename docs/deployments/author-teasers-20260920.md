# Author teaser release 2026 09 20

Enabled the formatting-parallel Codex Sol → DeepInfra DeepSeek V4 Flash workflow.

- Web machine: `286d2d7c11e738`; image `registry.fly.io/atmosphere-docproof:author-teasers-20260920-verified`.
- Teaser worker: `78126d2f0d7e28`; image `registry.fly.io/atmosphere-docproof:author-teasers-worker-20260920-final`.
- Google folder: `1diqWCyRSzGYsKL4Qm_x5PMcP-knQEsIG`, named exactly `author teasers`.
- DeepInfra credential stored as a staged Fly secret and applied through the web release. No credential is stored in this repository.
- Repaired the saved Google OAuth client settings after verifying the existing cloud credential pair.

Both releases overlay only teaser code on their respective prior production images.
The web jobs module preserves its newer `MODEL_DOWN` handling; only the enqueue
hook was inserted. The worker image preserves all Galley code. The protected
Wilder launch had already ended with a saved `needs_human` summary and no running
process; it was neither restarted nor resumed.

Validation: 180 tests covering teaser contracts, privacy, leases, retry recovery,
formatting jobs, exact folder naming, document delivery and guide checksums.
A live five-option writer test passed length/paragraph checks after bounded retries.
The author document layout was rendered and checked on all five pages; the fixed
guide PDF has two pages. A cloud Codex Sol structured-output probe succeeded with
subscription authentication and no API fallback.

Workflow version 3 migrates unfinished production version 2 teaser tasks on claim,
retaining their earlier work in the audit record. Completed and stopped packages
are unchanged. The formatting job itself is never rerun by migration.
