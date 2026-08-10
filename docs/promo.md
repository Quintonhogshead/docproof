# Promo — marketing copy from a finished manuscript

Promo is the third DocProof pipeline, beside **review** (`[proof]`) and **prep**
(`[format]`). It takes a whole dev-edited novel, makes **one large-context call**
to a model, and produces two Word documents — a **teaser** and a set of **social
posts** — delivered to the same Google Drive folder as the formatted manuscript.
It is triggered either by hand from a new Promo panel, or automatically by a
HubSpot status the way prep is.

Copy specifications are intentionally decoupled from the code: the prompt lives
in `config/promo/generation.yaml`, so the house's teaser and post specs drop in
later with no code change.

## Shape

```
                        ┌──────────────  Drive folder  ──────────────┐
                        │   [last name] - book 1.docx (dev-edited)   │
                        └──────┬─────────────────────────────▲───────┘
                               │ download                    │ upload (2 docx)
   HubSpot dropdown            ▼                             │
   "Ready for Promo text" ─▶ tick() promo stage ─▶ Job(kind="promo") ─▶ docproof/promo
   "Promo text finished"  ◀─ writeback ◀─ deliver ◀─ [approval gate] ◀─ teaser + 12 posts
                                              ▲
                                   Promo panel (review / edit / approve / manual run / history)
```

Everything above the pipeline box is the existing DocWatch machinery (Drive +
HubSpot + the tick loop) generalized to a second stage, not duplicated.

## Decisions (from the client)

- **Trigger input:** the dev-edit-finished manuscript, named in press as
  `[last name] - book 1`.
- **A third, independent pipeline** on the same Drive + dashboard + HubSpot
  rails — a new panel, a new HubSpot value pair.
- **HubSpot:** the existing status dropdown gains `Ready for Promo text` →
  `Promo text finished`.
- **Panel** does all three: manual run, review/edit/approve, history.
- **Output:** two `.docx` — one teaser, one with the twelve posts — into the
  same Drive folder.
- **Approval:** a setting. Auto-upload, or hold the drafts in the panel for a
  human to edit and approve before they ship. Both modes are available.
- **Model:** fully provider-agnostic through the existing `providers/` layer;
  default a strong model for grounding, switchable per run.
- **Input size:** the whole book in a single call.
- **Post platforms:** deferred. The pipeline is built now; the copy specs land
  later as a prompt edit.

## Phases

1. **Engine — `docproof/promo/`** *(this phase)* — `prepare` / `run` / `finish`,
   mirroring `docproof/prep/`. Reads the manuscript, one structured call, writes
   two `.docx` plus a `promo.json` (the editable source of truth for the panel).
2. **Config** *(this phase)* — `PromoConfig` in `docproof/config.py`, a `promo:`
   block in `config/default.yaml`, the prompt in `config/promo/generation.yaml`,
   and packaging so the wheel ships it.
3. **Job layer — `app/jobs.py`** — a third `kind="promo"`, a `_run_promo()`
   branch, approval state on the job.
4. **Watch stage — `app/watch/`** *(DONE)* — the tick loop's HubSpot gate
   parameterized by stage (format, then promo); a self-contained promo stage
   (`app/watch/promo.py` + `run_promo`/`_deliver*` in `tick.py`) with its own
   Drive marker (`docproof.promo`) and its own `promo_*` state fields, so the
   format path is untouched. New `WatchSettings`: `promo_enabled`,
   `hubspot_promo_ready_value`/`_done_value`, `promo_auto_upload`, `promo_model`.
   Auto mode ships in the tick; hold mode generates + marks `pending` and a
   deferred sub-step delivers once approved (idempotent). **Flat mode only** —
   under `subfolders_enabled` the promo stage stands aside with a logged note.
   Promo settings are set via `watch.json` / the CLI (same path HubSpot uses);
   the web settings form + `init` wizard are wired in Phase 5.
5. **Panel + routes — `app/routes/promo.py` + the SPA** *(DONE)* — a new Promo
   tab/screen with three parts: a manual run (drop a manuscript → generate), the
   copy list with an inline teaser+posts editor (save re-makes the two .docx),
   and the automatic-pipeline settings form (enable, ready/done values,
   auto-upload toggle, model). Endpoints: `POST /api/promo/run`,
   `GET /api/promo/jobs`, `GET|PUT /api/promo/jobs/{id}/draft`,
   `GET /api/promo/jobs/{id}/file/{which}`, `GET|PUT /api/promo/settings`
   (settings admin-gated on web). Manual runs are per-owner app-store jobs;
   delete reuses `DELETE /api/jobs/{id}`.

   **Deferred:** approving a *watcher-generated* hold-mode draft from the panel
   (crosses into the watch job store + its Drive/HubSpot context + web-auth
   scoping). Those still deliver automatically once `job.approval=="approved"`
   via `tick._deliver_approved_promo`; until the panel can set that, auto mode
   ships without a gate. This is the immediate Phase-5 follow-up.
6. **Model agnosticism** — no new work; a future model is one `catalog.py` entry.
7. **Grounding / verify** *(DONE)* — two checks. The deterministic proper-noun
   check (`verify_grounding`) ships always-on and free. The LLM **entailment**
   pass (`verify_claims`, `config/promo/verify.yaml`) is opt-in
   (`promo.verify_claims`, default off — it re-reads the whole book): a second
   structured call reports which claims the manuscript doesn't support. Both
   surface, neither blocks; results are written to `promo_verify.json`, folded
   into the job's flag count, and shown in the panel editor. A broken check
   never fails the run.

   **Also:** a document dropped under **Drop Documents** can now be routed to
   promo — a third "Write promo copy" kind alongside review/prep. Staging gained
   a `can_promo` preflight (`promolib.read_manuscript`, tolerant of tracked
   changes, so a dev-edited manuscript review/prep would refuse is promo-usable);
   the drop routes to `POST /api/promo/run` and lands in the Promo tab. Promo
   jobs are filtered out of the Results list (`/api/jobs`, `/api/tick`) since
   they have their own panel.
8. **Testing & rollout** — offline pipeline tests, a dry-run tick against the
   DocProof test authors, version bump, Fly deploy.

## Engine contract (`docproof/promo`)

Same three steps as prep and review, so the app and a future CLI drive it the
same way:

- `prepare(cfg, input_path, *, config_dir, override_dir=None) -> PreparedPromo`
  — read the whole manuscript to plain text, render the prompt, and refuse a
  book whose estimated size overflows the single-pass limit (splitting across
  calls is a planned extension, never silent).
- `run(cfg, prepared, provider) -> (PromoResult, Usage)` — one
  `complete_structured` call; `run_mock(prepared, canned=…)` is the offline path.
- `finish(prepared, result, usage, cfg, *, out_dir, source_path) -> PromoOutputs`
  — write `{stem} - teaser.docx`, `{stem} - social posts.docx`, and `promo.json`;
  attach any grounding flags.

`PromoResult` is deliberately loose in v1: a `teaser` string and a list of
`SocialPost { platform, text, hashtags }`, `platform` blank until the copy specs
define the split. The post count is enforced in the prompt and checked after
parsing, not in the schema (strict JSON schema can't carry a list length).

## Open items

- The contents of `config/promo/generation.yaml` — a placeholder ships now; the
  real teaser/post specs replace it with zero code change.
- Whether `social posts.docx` groups the twelve by platform — absorbed by the
  optional `platform` field with no migration.
- Very long novels vs. the context window — a clear error in v1; chapter
  map-reduce is the designed-for extension.
