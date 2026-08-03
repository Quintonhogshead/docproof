# DocProof App — implementation plan

Turn the docproof CLI into a double-clickable Mac app: drop in a stack of
manuscripts, pick a model, run now or overnight at batch pricing, get back
tracked-changes Word files. No terminal, no config files, no environment
variables.

This plan is written to be executed phase by phase. Each phase ends with
working software and green tests; no phase depends on a later one. The CLI
must keep working unchanged throughout — the app is additive.

## Premise correction (read first)

Batch API pricing is **not** cheaper at night. Anthropic Message Batches and
OpenAI's Batch API are both a flat **50% discount at any hour**, with results
returned within 24h (usually much faster). What "overnight" buys is workflow:
submit at 11pm, results waiting at breakfast. The app therefore offers two
run styles, and the UI copy must describe them honestly:

- **Run now** — interactive API, full price, results in minutes.
- **Batch run** — 50% cheaper, results within hours; optionally scheduled to
  submit at a set time (e.g. 11pm) so results land by morning.

## Current state (what the app builds on)

- Pipeline: ingest → chunk → analyze (passes of grouped error types) →
  validate → reassemble tracked changes → report. All library code in
  `docproof/`, driven by `docproof/__main__.py`.
- The Anthropic SDK is touched in exactly one place: `Analyzer` in
  `docproof/analyzer.py` (`import anthropic`, `self.client = ...`,
  `messages.parse` in `_call`). Everything upstream/downstream is
  provider-agnostic already.
- `MockAnalyzer` + `--mock-findings` exercise the full pipeline with no API.
  Tests: `tests/` (14 passing). Keep it that way — no live API calls in tests,
  ever.
- Config contract: `docproof/config.py` is the schema; `config/default.yaml`
  and all consumers must stay in sync with it (see repo memory).

---

## Phase 1 — Provider abstraction + OpenAI support

**Goal:** `Analyzer` works against Claude or ChatGPT models through one
interface. This is requirement (D) and the foundation for batch mode.

### New package `docproof/providers/`

`base.py`:

```python
@dataclass(frozen=True)
class ProviderResult:
    parsed: BaseModel | None      # instance of the pass's FindingList model
    usage: NormalizedUsage        # provider-neutral token counts
    stop_reason: str              # "ok" | "refusal" | "max_tokens" | "error"
    error: str | None = None

class Provider(Protocol):
    name: str
    def complete_structured(self, *, model: str, system: str, user: str,
                            output_model: type[BaseModel],
                            max_tokens: int) -> ProviderResult: ...
```

`NormalizedUsage` maps provider-specific fields onto the existing `Usage`
counter fields (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`). OpenAI reports `prompt_tokens`/`completion_tokens`
and `prompt_tokens_details.cached_tokens`; map cached → `cache_read`, leave
`cache_creation` 0.

`anthropic_provider.py` — move the existing `_call` logic here verbatim:
`messages.parse` with `output_format`, explicit `cache_control` on the system
block (Anthropic caching is opt-in). Refusal/max_tokens handling as today.

`openai_provider.py` — structured outputs via the OpenAI SDK's parse helper
(`client.responses.parse(..., text_format=output_model)`, or
`chat.completions.parse` with `response_format` — use whichever the installed
SDK major version documents; pin the SDK and commit to one). The dynamic
`Literal[keys]` enum in `build_output_model` serializes to a plain JSON-schema
enum, which OpenAI structured outputs support — verify with a unit test on the
generated schema, not a live call. OpenAI prompt caching is automatic for
prompts ≥1024 tokens; no code needed.

Add `openai` to `pyproject.toml` dependencies.

### Analyzer change (minimal)

`Analyzer.__init__` takes a `Provider` instead of constructing an SDK client.
`_call` becomes ~10 lines: delegate to `provider.complete_structured`, keep the
existing logging and the truncation/refusal log messages. `MockAnalyzer` is
untouched.

### Model catalog

`docproof/providers/catalog.py` — a static table the UI and cost estimator
both read:

```python
@dataclass(frozen=True)
class ModelInfo:
    id: str            # "claude-opus-5", "gpt-5", ...
    provider: str      # "anthropic" | "openai"
    display: str       # "Claude Opus 5 (best, slower)"
    input_per_mtok: float
    output_per_mtok: float
    batch_discount: float = 0.5
```

Seed with current Claude models (opus-5, sonnet-5, haiku-4.5) and current
OpenAI equivalents; **verify ids and prices against both providers' docs at
implementation time — do not trust remembered prices.** The existing
`pricing:` block in config becomes a fallback for models not in the catalog;
`write_summary_md` reads catalog prices when available. This resolves the
current mismatch where config carries opus-5 rates while the default model is
haiku.

### Config schema (`config.py` + `default.yaml` + docs, in sync)

- `api.provider: Literal["anthropic", "openai"] = "anthropic"`
- Provider selection by model is also fine: if `api.model` is found in the
  catalog, its provider wins; `api.provider` covers unlisted models.
- Keys come from env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) in Phase 1;
  Phase 5 adds Keychain. The existing key check in `cmd_review` moves to the
  provider (each checks its own env var and raises a clear error).

### Tests

- `FakeProvider` returning canned `ProviderResult`s; analyzer tests run
  against it (happy path, refusal, truncation, malformed).
- Schema test: `build_output_model(...)` produces a schema OpenAI structured
  outputs accepts (`additionalProperties` handling, enum serialization).
- Usage normalization test for both providers' response shapes (fabricated
  response objects, no SDK calls).

**Acceptance:** `docproof review --model gpt-5 ...` works with an OpenAI key;
existing Anthropic path byte-identical in behavior; all tests green with no
network.

---

## Phase 2 — Batch execution mode

**Goal:** the same review, at 50% cost, via each provider's batch API. This is
requirement (B)'s engine; scheduling is Phase 3.

### Design

A review = (passes × chunks) independent requests — already embarrassingly
parallel, which is exactly what batch APIs want. Three-step lifecycle,
persisted so the app (or laptop) can restart mid-job:

1. **submit** — ingest + chunk the document, build every request with
   deterministic `custom_id` = `"{pass_idx}:{chunk_id}"`, submit one batch per
   document, write a job manifest.
2. **poll** — check batch status; both providers expose a status endpoint and
   per-request results keyed by `custom_id`.
3. **collect** — download results, parse each through the pass's
   `build_output_model` (batch APIs return raw JSON, not SDK-parsed objects —
   reuse the models for validation), feed into `_to_findings` → validator →
   reassembler → reports, exactly as the sync path does.

### Reassembly after restart

`collect` may run in a different process days later. Re-ingest the source
`.docx` (the walker is deterministic — there's a test for it) and verify a
content hash recorded at submit time (hash of the ordered
`(para_id, text)` list). Hash mismatch = the file changed since submission →
fail the job with a clear message rather than mis-anchoring edits.

### Job manifest

`jobs/<job_id>/manifest.json`: source path + content hash, config snapshot
(model, passes, min_confidence), provider batch id(s), state
(`submitted | in_progress | collecting | done | failed | expired`), timestamps,
and the custom_id → (pass, chunk) map. Plus `results/` for outputs. This
directory format IS the app's job store — Phase 3 builds on it, so keep it
clean and versioned (`"manifest_version": 1`).

### Provider additions

Extend the `Provider` protocol with:

```python
def submit_batch(self, *, model, system, user_contents: list[tuple[str, str]],
                 output_model, max_tokens) -> str            # batch_id
def poll_batch(self, batch_id) -> BatchStatus                # counts + state
def collect_batch(self, batch_id) -> dict[str, ProviderResult]  # by custom_id
```

Anthropic: Message Batches API (`client.messages.batches.*`). Note structured
outputs in batches: pass the JSON schema via the tool/output_format the SDK
supports in batch requests at implementation time — check current SDK docs;
if `output_format` isn't supported in batch request bodies, fall back to a
`tool_choice`-forced tool with the schema, and parse tool input. OpenAI: Batch
API (upload JSONL file, create batch, download output file). Per-request
errors (refusal, truncation) map to `ProviderResult` with the right
`stop_reason` so chunk-skip semantics match the sync path.

Prompt caching does not apply inside batches (each provider prices batches at
the flat discount); don't send `cache_control` in batch requests.

### CLI (thin, for testing before the app exists)

- `docproof submit <file>` → prints job id
- `docproof status [job_id]` → table of jobs and states
- `docproof collect <job_id>` → produces the same outputs as `review`

### Tests

`FakeBatchProvider` with scripted status transitions; full submit→collect
round-trip over the fixture docx producing identical outputs to a
`--mock-findings` sync run; hash-mismatch rejection; partial-failure handling
(one chunk errored → others still applied, failure logged in summary).

**Acceptance:** a real manuscript reviewed end-to-end through `submit`/
`collect` at batch pricing, surviving a process restart between the two.

---

## Phase 3 — App backend (job manager + scheduler)

**Goal:** a local FastAPI server wrapping Phases 1–2 as a job queue.

New top-level package `app/` (keep `docproof/` a pure library; the server
imports it, never the reverse).

### Endpoints

- `POST /api/files` — multipart upload (multiple files), staged into the job
  workspace. Validate: `.docx` only, runs `preflight` immediately so tracked-
  changes/corruption problems surface at drop time, not at 11pm.
- `POST /api/jobs` — `{file_ids, model_id, mode: "now"|"batch",
  schedule_at: null|"HH:MM", min_confidence, passes?}`. One job per file
  (simplest retry story); a "job group" id ties a multi-file drop together.
- `GET /api/jobs`, `GET /api/jobs/{id}` — states + progress (chunks done /
  total for sync runs; provider batch counts for batch runs).
- `GET /api/jobs/{id}/results` — reviewed docx, summary.md (rendered), and
  findings.json download links.
- `GET/PUT /api/settings` — default model, min_confidence, output folder,
  API keys (Phase 5 wires Keychain; until then, presence-only status of env
  keys — **never** return key values to the frontend).
- `GET /api/models` — the catalog, with per-model "estimated cost for this
  job" once files are staged (chunk token counts × catalog prices × batch
  discount — the `inventory` math, surfaced per model).

### Job runner

- Single background worker thread + `queue.Queue` for "now" jobs (they're
  minutes long; no need for multiprocessing). Batch jobs don't occupy the
  worker: submit, then a poller thread checks all in-flight batch jobs every
  few minutes.
- Job state lives in the Phase 2 manifest files; the server holds no state
  that isn't on disk. Restarting the app resumes polling automatically by
  scanning `jobs/`.
- Scheduler: a loop that checks once a minute for jobs with `schedule_at` due
  and submits them. Document the honest limitation: **the app must be running
  (laptop awake) at submit time.** Mitigation A (default): "Run overnight"
  actually submits immediately as a batch — results still cost 50% and arrive
  by morning; the schedule option exists mainly for users who want submission
  itself deferred. Mitigation B (optional, later): a `launchd` agent. Do not
  build B until asked.

### Tests

FastAPI `TestClient` + `FakeBatchProvider`: upload → job → poll → results;
scheduler fires a due job; restart-resume (new server instance picks up an
in-flight manifest); settings round-trip.

**Acceptance:** `uvicorn app.main:app` + a browser is a fully working product
for a technical user.

---

## Phase 4 — Frontend

**Goal:** requirement (A) and (C). No build step — plain HTML/CSS/JS (or htmx)
served by FastAPI as static files, so packaging stays one artifact.

Three screens, minimal chrome:

1. **Drop** — full-window drop zone ("Drop Word documents here") + file
   picker button. On drop: file cards with paragraph/chunk counts from
   preflight, a model dropdown (display names from the catalog, with price
   hint: "~$0.42 for these 3 files"), a Run now / Batch (50% cheaper,
   results within hours) toggle with optional "submit at [11:00 PM]", one big
   Start button. Sensible defaults everywhere: remembers last model, batch
   preselected for >2 files.
2. **Jobs** — list with plain-language states ("Waiting until 11pm",
   "Reviewing (12 of 30 sections)", "Processing overnight — check back in the
   morning", "Ready", "Needs attention"). Ready rows: "Open reviewed
   document" (download) + "View summary" (rendered summary.md). Failed rows
   show the human-readable reason (preflight abort message, key missing, batch
   expired) and a Retry button.
3. **Settings** — API key fields (password-type inputs, "Test" button that
   makes one cheap live call and reports ✓/✗), default model, confidence gate
   as a three-way radio with plain descriptions, output folder picker.

Copy rules for (C): no "chunks", "passes", "tokens", or "API" anywhere except
the settings key fields. Say "sections", "checks", "estimated cost".

**Acceptance:** a non-technical user given a running app and an API key can
review three manuscripts without instructions.

---

## Phase 5 — Packaging + first run (macOS)

**Goal:** double-clickable `DocProof.app`, no Python knowledge required.

- PyInstaller (onedir) bundling the server + static frontend; entry script
  starts uvicorn on `127.0.0.1:<free port>` and opens the default browser.
  Menu-bar presence via `rumps` ("DocProof — Open / Quit") so closing the
  browser tab doesn't kill in-flight work. Bind localhost only.
- API keys → macOS Keychain via `keyring`; settings UI writes there, providers
  read Keychain first, env var as override. Keys never touch config files or
  logs.
- First-run: Settings screen with a one-paragraph "paste your Anthropic or
  OpenAI key here, here's where to get one" explainer and the Test button.
- Outputs default to `~/Documents/DocProof/<document name>/`; jobs workspace
  in `~/Library/Application Support/DocProof/`.
- Unsigned app caveat: first launch requires right-click → Open (Gatekeeper).
  Put that one instruction in the README; signing/notarization only if
  distribution beyond this machine ever matters.

**Acceptance:** fresh macOS account, no dev tools: copy app, right-click Open,
paste key, drop file, get reviewed docx.

---

## Guardrails (all phases)

- `validator.py`, `reassembler.py`, `chunker.py`, `ingest.py`: **no changes.**
  Analyzer changes limited to provider injection. If a phase seems to need
  more, stop and reconsider the design.
- `docproof/config.py` is the schema of record — every config change updates
  it, `config/default.yaml`, and `docs/error-types.md`/this file together.
- No live API calls in tests. Every provider gets a fake.
- The CLI keeps working after every phase; `tests/` stays green after every
  phase.
- One phase per PR/commit-series; do not start phase N+1 with phase N red.
- Verify current model ids, batch API request shapes, and prices from provider
  docs during Phase 1/2 — several will have changed since this plan was
  written.

## Suggested order of value

Phase 1+2 alone (still CLI) already deliver the 50% cost cut and multi-model
support. Phase 3+4 deliver the usability. Phase 5 is polish. If effort must be
cut, cut from the end.
