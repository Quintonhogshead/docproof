# Cover Canvas — Spell & Check's cover editor

*v1 spec, 2026-08-31. Companion to `docs/cover_designer_spec.md` (the one-shot
pipeline). That document is about generating a cover; this one is about the
last 80% — the part where a person wishes they could reach into the image and
move something 20% to the left.*

## 1. What this is

A stripped-down, AI-forward cover editor: a layered canvas in a Mac app.
Spell & Check's frame is a full AI hybrid press for under $100 a book; this is
its cover product. The one-shot pipeline stays exactly as it is and becomes
the *entry point*: "New cover" runs it, and the finished draft lands on the
canvas as editable layers instead of a flat PNG. Everything Quinton is
constantly telling the art director to do becomes either a direct
manipulation (drag it 20% left) or one click (scrim it, ground her, shadow
the cutout, rebalance).

Not Photoshop. The layer count is small (a cover spec has maybe a dozen
layers), the tool count is small, and every tool is either a transform, a
type control, or an AI verb.

## 2. Architecture (the hybrid decision)

**The browser canvas is the source of truth for layout and type. The engine
is a set of callable services.** Decided over "engine renders everything"
(too slow to feel like reaching into the image) and "pure native canvas"
(re-implements masks/balance/typography and orphans the §15 doctrine).

Concretely:

- **CanvasDoc** — a new JSON document (pydantic, `docproof/canvas/model.py`)
  that wraps the job's `CoverSpec` and adds what the editor needs: a `layers`
  list in z-order, each layer carrying `{id, kind, source, transform, warp,
  effects, visible, locked}`. Coordinates stay 0–1 fractions of the canvas,
  same as CoverSpec — one convention everywhere.
  - `kind: art` — source is a plate PNG in the job dir (the pipeline's
    generated art, or an inpainted/re-rolled successor). The *prompt that
    made it* rides along, so regeneration verbs always have it.
  - `kind: text` — live text: string, family (the engine's FAMILIES),
    size, color, tracking, alignment, optional warp (arc/arch/flag/bulge).
    Rendered client-side with the same OFL font files served as webfonts.
  - `kind: scrim | frame | shape` — vector layers (gradients, ornamental
    bezels, rules, panels) described by parameters, rendered client-side.
- **Import** (`docproof/canvas/ingest.py`) — converts a finished cover job
  (spec + plates) into a CanvasDoc. Art slots become art layers; text slots
  become live text layers seeded from the spec's fitted values; scrims the
  autopilot added become scrim layers. Lossy in the reverse direction is
  fine: we never round-trip back into the one-shot pipeline.
- **Engine as services** — the canvas calls the engine for what pixels can't
  do locally: plate generation and inpainting (`imaging`), balance/legibility
  measurement (`balance`), doctrine moves that are really prompt recipes
  (ground-the-figure), and (v2) a server-side high-res parity render.

## 3. The app

- **Server**: FastAPI routes in `app/routes/canvas.py`, registered like
  `cover.py`. Jobs live in the same data root as cover jobs; a canvas
  session is keyed by the cover `job_id` **and the concept** it was opened
  from — `canvas.json` for the job's first concept and `canvas_c<n>.json`
  for the others. One session per job was a bug (fixed v0.163.0): a job's
  concepts are different covers, so clicking "Edit in Cover Canvas" on the
  second one handed back the first one's document and the `concept`
  parameter was explicitly ignored. Every `/api/canvas` request carries
  `?concept=N` (appended once, in `api.js`'s single fetch door), the
  document records its own concept, and the shelf says which cover you are
  editing.
- **Front-end**: `app/static/canvas/` — a single-page static app, no
  bundler (house style; `sc-cover.html` precedent). Rendering via Konva
  (vendored single file) for the layer canvas, selection handles, and
  transforms; Canvas2D mesh subdivision for warps.
- **Mac shell**: pywebview wrapper reusing the `app/desktop.py` /
  `docproof_desktop.py` pattern — local uvicorn + a native window. Same UI
  hosts on Fly later; only the shell changes.
- **The shell keeps itself current (v0.162.0)**: `app/autoupdate.py`. Every
  launch checks `origin/main`; when the app is behind it, the suite runs and
  a new bundle is built in the background, in a detached git worktree under
  the app home — the owner's checkout is only ever fetched, never pulled,
  reset or built in, so an agent working in it is undisturbed and the app
  follows what was actually merged. The build is STAGED and swapped in at
  the start of the next launch, before anything is read out of the bundle:
  replacing a running app's bundle pulls its own config, fonts and static
  tree out from under it, and no update gets to close a window somebody is
  working in. Nothing that fails its tests is ever staged, the old bundle
  goes to the Trash rather than nowhere, and `DOCPROOF_NO_AUTO_UPDATE=1`
  turns the whole thing off. (The web build has never needed this: merging
  to main redeploys Fly on its own.)
- **Layout**: canvas center; layer list left; properties panel right
  (contextual: type controls for a text layer, prompt + regen verbs for an
  art layer); the **button shelf** across the top; the **AI box** docked
  right below the properties panel.

## 4. Editing surface (v1)

- **Transforms** (any layer): move, scale, rotate, flip, and corner-pin
  perspective. Direct manipulation with handles; arrow keys nudge; a
  position readout in fractions so "20% left" is literally typable.
- **Type**: size, color, family, tracking, alignment, line breaks; warp
  presets arc / arch / flag / bulge with an amount slider. Text stays live
  text until export.
- **Bezels** (both senses):
  - *Ornamental frames*: a vector ornament library (rules, corner motifs,
    inset frames, panel borders) as `frame` layers with stroke/fill/inset
    parameters.
  - *Bevel/edge effects*: a raster effect on a layer (bevel, inner shadow,
    edge glow) in the client effect stack.
- **Undo/redo**: every mutation is an **op** on the CanvasDoc (a small
  reversible command). One op log serves undo, the button shelf, and the
  assistant — the AI edits the canvas through exactly the ops a human
  click produces, so anything it does is undoable the same way.

## 5. AI verbs

### Regeneration (per art layer)
- **Re-roll** — one click: same prompt, fresh call. New plate lands in the
  layer with the old one kept in a per-layer history strip (click to swap
  back — a re-roll is never destructive).
- **Tweak-then-roll** — click opens the layer's prompt for a quick edit,
  then rolls.
- **Region inpaint** — draw a region on the plate, type an instruction
  ("fix her hand", "remove the lamp"), and the region regenerates in place.
  New `imaging.edit()` beside `imaging.generate()`: `client.images.edit`
  with image + alpha mask, gpt-image-2 first with the gpt-image-1 shape as
  fallback, same retry discipline. The mask is the drawn region rasterized
  at plate resolution.

### The wait (shipped v0.160.0)
A plate is tens of seconds of somebody's afternoon, so the wait is part of
the design rather than an accident of it:
- **Progressive frames.** Every money verb can answer as NDJSON — the
  vendor's own partial images as they are painted, then the finished
  document (`stream: true` on the request; `_plate_answer` in
  app/routes/canvas.py). The client draws each partial in place of the plate
  it is replacing, so the picture resolves on the canvas instead of arriving
  at the end of a blank overlay.
- **Nothing is modal.** A render puts up a corner chip naming the layer, not
  a screen-blocking overlay: type, panning and selection stay live, and only
  a second call on the SAME plate is refused. The server owns the rule that
  the overlay used to enforce — one writer per job (`_job_lock`), held
  across the vendor call, so a type edit made mid-render cannot be clobbered
  by the plate that lands after it. The client folds the returned plate into
  the document it has rather than replacing it wholesale, for the same
  reason.
- **A draft is actually a draft.** The tier is a ladder in BOTH vendor
  parameter shapes (`_FALLBACK_QUALITY`); the gpt-image-1 fallback used to
  send `quality="high"` whatever tier was asked for, which silently made the
  draft rung the slowest and dearest one while still billing it three cents.
  Draft rolls also come back as webp (`regen.DRAFT_FORMAT`) — same picture,
  a fraction of the bytes to ship — while finals and every cutout stay PNG.
- **The vendor is probed once.** Which parameter shape works, and whether
  streaming is supported, is remembered per process (`_SHAPE_CACHE`,
  `_STREAM_CACHE`), so a deployment where the guessed gpt-image-2 names are
  wrong stops paying a rejected round trip before every single image.

### The button shelf (day one)
§15 doctrine as buttons, each implemented as ops + engine calls:
- **Scrim behind type** — selected text layer gets a scrim layer beneath
  it, sized from the text's bounds, strength slider live.
- **Generate ground** (§15.23) — an inpaint recipe: band under the
  figure's measured base + a generated ground prompt + contact shadow
  effect layer. Named for what it does, because it sits beside a free
  shadow toggle and is nothing like one: it buys a new image of the bottom
  of the plate. A cutout layer keeps its transparency through the call
  (`imaging.edit(transparent=…)`) — without that the model reads the empty
  background as canvas to fill and hands back a full frame.
- **Cutout shadow stack** (§15.22) — the planned drop-shadow pair, as a
  TOGGLE on the selected layer: pressing it again takes the shadows back
  off, because the point of a planned pair is being able to see the layer
  with and without it. The button owns that layer's drop shadows; anything
  hand-tuned lives in the effects list on the properties rail.
- **Rebalance values** — run `balance` on the current composite; nudge the
  field layer's exposure/contrast so the focal subject stays loudest;
  report what it measured in the AI box.

## 6. The AI box

A chat panel with a **Plan / Act** toggle, in the first build.

- **Brain**: Claude on Quinton's Max subscription — the Galley pattern
  (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), driven through the
  Agent SDK by the server. Image-gen keys stay server-side with the
  existing cover key plumbing. BYO-key is a later seam; not built now.
- **Tools** = the same op layer the UI uses: `move_layer`, `set_type`,
  `add_frame`, `warp`, `reroll`, `inpaint`, `scrim`, `ground_figure`,
  `shadow_stack`, `rebalance`, plus read-only `inspect` (the CanvasDoc) and
  `look` (a preview-res composite screenshot so it can actually see the
  cover it's editing).
- **Act mode**: full tool set; "move the title down and left, off her
  face" just happens, each tool call appearing in the undo log.
- **`look` is live (v0.161.0)**: it re-renders the working document
  server-side (`docproof/canvas/render.py`) rather than handing back the
  snapshot the browser attached to the message. The snapshot was taken
  before the turn ran, so a director that edited and then looked was shown
  the cover as it was BEFORE its own edit — it could iterate, but only
  blind. `look(grid=true)` adds a measuring grid in the document's own
  units (tenths, thirds, centre, and a wrap's fold lines), so a judgement
  becomes a number an op can take. The system prompt's **Measuring**
  section is the discipline that goes with it: measure with `inspect` and
  the grid, use the free `rebalance` before spending on a re-roll, change
  one thing, look at what you did, and never roll twice without looking in
  between. A look is a JPEG on a size ladder (v0.164.0): a tool result
  travels back as one line of JSON into the SDK's 1MB buffer, so an
  oversized frame is not a truncated picture but a decode error that kills
  the turn — which is what a 900px PNG of a real cover did to every turn
  that looked.
- **Plan mode**: read-only tools only (`inspect`, `look`); it critiques,
  proposes, and drafts a numbered plan. One click ("do it") flips the plan
  to Act and executes it.
- The doctrine (`cover_designer_spec.md` §15) is in its system prompt — the
  assistant is the same art director, now resident in the app.

## 7. Export

- **v1**: client-side full-resolution composite (front cover at print res —
  6×9 + bleed at 300dpi ≈ 1875×2775 px is comfortably within browser
  canvas limits), downloaded as PNG; plates were generated at 2K–4K so
  they hold up. Text rasterizes from the same OFL faces at full res.
- **Shipped in M3** (was planned as v2): the server-side parity render
  (`docproof/canvas/render.py`, `GET /api/canvas/{job_id}/render`) and the
  full print wrap — `Wrap` on the document, `to_wrap` conversion,
  `set_wrap` spine/bleed/dpi re-measure, and print-exact PDF export.

## 8. Cost visibility

The press costs under $100 a book, so the canvas shows money: every AI verb
displays its price before the click (IMAGE_COST tiers; assistant turns are
subscription-covered and shown as $0), and the session keeps a running
total the way cover jobs already do (`total_usd`).

## 9. Milestones

- **M1 — "reach into the image"**: ingest a finished cover job as a
  CanvasDoc; Konva canvas with move/scale/rotate/flip; live type with
  size/color/family; undo/redo op log; AI box in Act mode with the core
  ops + `look`; re-roll and tweak-then-roll; client PNG export; pywebview
  shell. *Usable on a real cover the day it lands.*
- **M2 — the doctrine in buttons**: corner-pin + type warps; bezels (frame
  library + bevel effects); the four shelf buttons; region inpaint; Plan
  mode + plan-to-Act handoff; per-layer plate history strip.
- **M3 — product finish**: full wrap export, server parity render,
  packaged .app, cost polish, and whatever the first real covers demand.

## 10. Open questions (deliberately deferred)

- gpt-image-2's real `images.edit` parameter shape — verify live, same
  loud-note discipline as `imaging.py`.
- Whether ground-the-figure can run purely as inpaint or sometimes needs a
  full plate re-plan (start with inpaint; escalate manually).
- The Spell & Check umbrella (accounts, billing, the other press products)
  — explicitly out of scope here; this app assumes a local owner-operator
  until the press exists around it.
