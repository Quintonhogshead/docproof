# Cover Studio — implementation spec

**Audience:** an LLM implementing this feature in the DocProof repo. Read this whole document before writing code. Everything marked DECIDED is settled with the owner (Quinton, 2026-08-28); do not re-litigate it. Everything marked IMPLEMENTER'S CHOICE is yours.

## 1. What this is

An internal, AI-assisted **ebook cover designer** that ships as a new page on the Spell & Check site (the standalone Fly app served by [app/quest_site.py](../app/quest_site.py)). It does **not** work by generating one prompt and shipping whatever image comes back. It works the way a designer works:

- An **art-direction model call** turns a human brief into several structured design concepts.
- **AI image generation** (gpt-image-2) produces *art layers only* — backgrounds and focal elements with no text in them.
- A **deterministic composer** assembles layers and sets all typography itself with real embedded fonts: title fitting, tracking, scrims, contrast enforcement.
- Humans iterate by giving notes; notes edit the **spec**, and recomposition is free. Art is only regenerated when the art itself must change.

The unit of work is a **CoverSpec** — a JSON document describing everything about a cover except the raster art pixels. Spec + generated assets → pixel-identical render, every time. The spec is the layered source file; archive it and any cover is reproducible and hand-editable forever.

### Locked decisions (DECIDED)

1. **Internal tool.** Atmosphere staff use it; it may later surface in Spell & Check (a product that is explicitly AI-branded, so AI art is acceptable there). No accounts, no billing. Gate it with a shared key (§8).
2. **Ebook covers first.** Output target is a front cover: 1600×2560 JPG (KDP-ideal 1:1.6) plus PNG. Print wrap (spine/back/bleed PDF) is a designed-for-but-deferred phase — the geometry model must not preclude it (§12).
3. **Lives in this repo**, as new source files, served by the Spell & Check app (`quest_site.py`), deployed to the existing `spell-and-check` Fly app. It links against (imports) existing docproof machinery — providers, fonts, settings — it does not fork or vendor them.
4. **New tab:** a `/cover` page in the Spell & Check papery design system. The nav link appears **only when unlocked** (§9); the public site's nav is unchanged for visitors.

### Vocabulary

- **Brief** — the human input: title, author, genre, pitch, mood, constraints.
- **Direction** — one design concept produced by the art-direction call: archetype + palette + fonts + art prompts.
- **CoverSpec** — the full renderable document for one direction (Direction + resolved zones + text slots + layer stack).
- **Archetype** — a parametric layout template (data, not code): layer order, text zones, fitting rules.
- **Job** — one book's cover session: brief → N directions → renders → revisions. Persisted on disk.

## 2. Repo context you must know

The Spell & Check site is a deliberately tiny FastAPI app, separate from the main DocProof web build:

- Server: [app/quest_site.py](../app/quest_site.py) — `create_app()`, in-memory `RateLimiter`, static pages served from `resource_root()/app/static`, catch-all papery 404, `/assets` static mount. Console script `spell-and-check` ([pyproject.toml:80](../pyproject.toml)).
- Existing API pattern to mirror: [app/routes/quest.py](../app/routes/quest.py) — `register(app)` function, upload validation, content-hash response cache, SSE streaming, `_provider()` built via `build_provider(cfg, api_key=get_api_key(...))`.
- Existing model-call pattern to mirror: [docproof/quest/skin.py](../docproof/quest/skin.py) — one structured call through the `Provider` protocol, `strict_json_schema(PydanticModel)`, generous `max_tokens` (8000) because **structured replies on reasoning models share max_tokens with thinking, and a truncated structured reply parses as nothing**; model failure degrades to a default instead of erroring; `cost_of_usage(usage, fallback_model=...)` for pricing.
- Fonts: `config/prep/fonts/` has 14 TTFs, 10 families, all OFL/Apache with unrestricted embedding. Family names are in `config/prep/fonts/README.md`. Subject→display-face mapping lives in [config/prep/book_design.yaml](../config/prep/book_design.yaml) (`subjects:` section, keys: `fantasy, science_fiction, romance, mystery_thriller, horror, historical, literary, memoir_biography, nonfiction, young_readers`).
- Deploy: `Dockerfile.quest` + `fly.quest.toml`. python:3.12-slim, **512MB VM**, scale-to-zero, 1GB volume mounted at `/data` (currently only the waitlist). Deploy is **manual and owner-run**: `fly deploy -c fly.quest.toml`. You cannot deploy; Quinton does.
- Frontend: `app/static/sc-*.html` pages. Shared chrome (header/footer) renders from [app/static/sc-shared.js](../app/static/sc-shared.js) `headerHTML()`/`mountChrome()` keyed on `<body data-page>`; design tokens in `sc-shared.css` (parchment ground, IM Fell English display, Alegreya body, engraved double-rule frames, scrap-paper buttons whose hover/focus state is a translate+rotate+color shift). No framework; vanilla JS per page.
- **The `Provider` protocol is text-only** (`complete_structured`, batch verbs). Do NOT extend it for images. Image calls live entirely inside the new cover module using the `openai` SDK directly (already a dependency).

### House gotchas (violating these has burned prior sessions)

- **Package data:** anything read at runtime via `resource_root()` (config YAML, fonts, static files) MUST be declared in `[tool.setuptools.package-data]` in pyproject.toml, or it 404s / FileNotFounds on Fly where the app runs from an installed wheel with no source checkout.
- **Version bump:** bump `__version__` in `docproof/__init__.py` when shipping user-facing changes.
- **Worktree paths:** edit files under the worktree path you were launched in, never the main checkout's absolute path.
- **512MB box:** no headless browsers, no Java, no ML runtimes. Cap concurrency (§7.4).
- **CSS chrome selectors must be element-qualified** (`header.top`, not `.top`) — a bare class collided with page content once already.
- Mobile CSS traps already hit on this site: `aspect-ratio` + `min-height` forces width; bare `1fr` grid tracks leak min-content (use `minmax(0, 1fr)`).
- When visually verifying: a hidden browser pane stalls rAF/CSS animations and screenshots go stale — trust live DOM measurements or headless-Chrome `--screenshot`.

## 3. File plan

### New files

| Path | Purpose |
|---|---|
| `docproof/cover/__init__.py` | Public exports (`Brief`, `CoverSpec`, `run_directions`, `compose`, …) |
| `docproof/cover/model.py` | All pydantic models: `Brief`, `Palette`, `TextSlot`, `ArtSlot`, `ScrimSpec`, `CoverSpec`, `Direction`, `RenderReport` |
| `docproof/cover/fonts.py` | Font registry: family name → TTF path(s), role tags; resolves from `config/prep/fonts` |
| `docproof/cover/archetypes.py` | Load + validate archetype YAMLs; zone math (pct → px) |
| `docproof/cover/direction.py` | Art-direction call (brief → N `Direction`s) and revision call (spec + notes → edited `CoverSpec`) |
| `docproof/cover/imaging.py` | gpt-image-2 wrapper: generate, transparent cutouts, cost table, retry |
| `docproof/cover/typeset.py` | Text measurement, fit search, balanced line breaks, tracked/effect text rendering onto a Pillow layer |
| `docproof/cover/compose.py` | The deterministic renderer: spec + assets → PNG/JPG; luminance sampling, scrim escalation, contrast report, thumbnail |
| `docproof/cover/pipeline.py` | Job orchestration + on-disk store: states, per-step persistence, cost ledger |
| `app/routes/cover.py` | HTTP API: create job, poll, revise, download; key gate; rate limits |
| `app/static/sc-cover.html` | The Cover Studio page |
| `config/cover/archetypes/big_type.yaml` | Archetype 1 (§5.2) |
| `config/cover/archetypes/full_bleed_art.yaml` | Archetype 2 |
| `config/cover/archetypes/cutout_sandwich.yaml` | Archetype 3 |
| `tests/test_cover_model.py`, `tests/test_cover_typeset.py`, `tests/test_cover_compose.py`, `tests/test_cover_pipeline.py`, `tests/test_cover_routes.py` | §11 (match the naming style already in `tests/`) |

### Touched files

| Path | Change |
|---|---|
| `app/quest_site.py` | Serve `/cover` page; `cover.register(app)`; a cover-specific `RateLimiter`; read `COVER_KEY` / `COVER_DATA_PATH` env |
| `app/static/sc-shared.js` | `headerHTML()` appends a "Cover studio" nav link **only if** `sessionStorage['sc-cover-key']` is present (wrap the read in try/catch) |
| `pyproject.toml` | New optional extra `cover = ["pillow>=10.4"]`; package-data entries `"config.cover" = ["*.yaml"]`, `"config.cover.archetypes" = ["*.yaml"]` |
| `Dockerfile.quest` | Both `pip install` lines: `".[app]"` → `".[app,cover]"` |
| `fly.quest.toml` | `[env] COVER_DATA_PATH = "/data/cover"` |
| `docproof/__init__.py` | Version bump |

Do not touch: `Dockerfile` / `fly.toml` (main DocProof build), `app/routes/__init__.py` (that registry belongs to the main app, not the quest site), the `Provider` protocol, existing sc pages beyond the one nav line.

## 4. Data model (`docproof/cover/model.py`)

All models are pydantic v2, `model_config = ConfigDict(extra="forbid")`. Coordinates are **fractions of canvas (0–1)** so specs are geometry-independent (print wrap later re-targets the same spec at a different canvas). Colors are `#rrggbb` strings validated by regex.

```python
class Brief(BaseModel):
    title: str                      # required, 1..200 chars
    subtitle: str = ""
    author: str                     # required
    genre: str                      # one of the 10 subject keys OR free text
    pitch: str = ""                 # synopsis / back-cover copy / logline, <= 4000 chars
    mood: str = ""                  # comma phrases: "elegiac, wintry, hopeful"
    must_include: str = ""          # "a lighthouse", "the color red"
    avoid: str = ""                 # "no faces, no dragons"
    concepts: int = 4               # 1..6

class PaletteRole(str, Enum):
    background = "background"; primary = "primary"; accent = "accent"
    text = "text"; scrim = "scrim"

class Palette(BaseModel):
    background: str; primary: str; accent: str; text: str; scrim: str
    # five hexes; helper: .get(role)

class Zone(BaseModel):
    x: float; y: float; w: float; h: float          # fractions, 0..1
    # validator: inside canvas, w/h > 0

class Shadow(BaseModel):
    dx: float = 0.0; dy: float = 0.004              # fraction of canvas HEIGHT
    blur: float = 0.006                             # fraction of canvas height
    color: str = "#000000"; alpha: float = 0.55

class Stroke(BaseModel):
    width: float = 0.0                              # fraction of canvas height; 0 = off
    color: str = "#000000"

class TextSlot(BaseModel):
    id: Literal["title", "subtitle", "author", "series"]
    content: str = ""                               # filled from Brief at spec-build time
    zone: Zone
    font_family: str                                # must exist in fonts.FAMILIES
    case: Literal["upper", "title", "as_is"] = "as_is"
    tracking: float = 0.0                           # em/1000 units (e.g. 120 = loose caps)
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle", "bottom"] = "middle"
    max_lines: int = 3
    size_min: float; size_max: float                # fraction of canvas height (e.g. 0.02..0.12)
    color_role: PaletteRole = PaletteRole.text
    shadow: Shadow | None = None
    stroke: Stroke | None = None
    optional: bool = False                          # subtitle/series render only if content

class ArtSlot(BaseModel):
    id: Literal["background", "focal", "texture"]
    prompt: str = ""                                # what to ask gpt-image-1 for; "" = procedural
    transparent: bool = False                       # request transparent background (cutouts)
    fit: Literal["cover", "contain"] = "cover"
    anchor: tuple[float, float] = (0.5, 0.5)        # focal point kept in frame when cover-cropping
    scale: float = 1.0                              # post-fit zoom, 1.0..~1.4
    offset: tuple[float, float] = (0.0, 0.0)        # fraction nudge after fit
    opacity: float = 1.0
    blend: Literal["normal", "multiply", "overlay", "soft_light"] = "normal"
    asset: str = ""                                 # relative path under the job dir once generated

class ScrimSpec(BaseModel):
    kind: Literal["gradient_down", "gradient_up", "vignette", "panel"] = "panel"
    zone: Zone | None = None                        # None = derived from the protected TextSlot
    protects: Literal["title", "subtitle", "author", "series"] | None = None
    strength: float = 0.0                           # 0..1; composer may RAISE it (never lower) to pass contrast
    color_role: PaletteRole = PaletteRole.scrim

class LayerRef(BaseModel):
    kind: Literal["art", "scrim", "text"]
    ref: str                                        # ArtSlot.id / index / TextSlot.id

class CoverSpec(BaseModel):
    version: int = 1                                # bump on every revision; renders are named by it
    archetype: str                                  # archetype file stem
    concept_name: str                               # "Ash and Brass"
    rationale: str                                  # one sentence, shown on the card
    palette: Palette
    art: list[ArtSlot]
    scrims: list[ScrimSpec]
    text: list[TextSlot]
    layers: list[LayerRef]                          # explicit z-order, bottom first
    notes_log: list[str] = []                       # human notes applied so far, newest last

class RenderReport(BaseModel):
    contrast: dict[str, float]                      # text slot id -> achieved contrast ratio
    scrim_final: dict[int, float]                   # scrim index -> strength after escalation
    fitted_sizes: dict[str, float]                  # slot id -> chosen size (fraction)
    warnings: list[str]                             # "title at size_min and still 2 lines over", etc.
```

`Direction` = what the art-direction call returns per concept: `concept_name`, `rationale`, `archetype`, `palette`, `title_font`/`author_font` (validated against the font registry — constrain with `Literal[*FAMILY_NAMES]` built via `pydantic.create_model`, the same trick [docproof/prep/meta.py](../docproof/prep/meta.py) uses for subjects), `art_prompts` (per slot the archetype declares generatable), `texture: bool`. `build_spec(direction, brief, archetype) -> CoverSpec` merges the direction into the archetype's zones/slots and fills text content from the brief.

Canvas constants (module-level in `compose.py`): `EBOOK_W, EBOOK_H = 1600, 2560`; thumbnail width 100px (the Amazon-search-result test) and 300px (card display).

## 5. Archetypes (`config/cover/archetypes/*.yaml`)

Archetypes are **data**. Each declares: layer order, which art slots exist and whether they're generatable or procedural, text zones with fitting rules, default scrims, and a `composition_note` used to steer image prompts toward leaving negative space where the text will sit.

### 5.1 YAML shape

```yaml
name: full_bleed_art
describe: >-            # one line; injected into the art-direction prompt so the
  Full-bleed illustrated scene; title banded across the lower third on a scrim.
composition_note: >-    # appended to the background image prompt
  Keep the lower third of the image simple, dark, and uncluttered — open sky,
  ground, or shadow — so large text can sit there. No text anywhere.
art:
  - id: background
    generatable: true
    fit: cover
  - id: texture
    generatable: false            # procedural: film grain (§7.3)
    opacity: 0.06
    blend: overlay
scrims:
  - kind: gradient_down
    protects: title
    strength: 0.25
  - kind: gradient_down
    protects: author
    strength: 0.15
text:
  - id: title
    zone: {x: 0.08, y: 0.62, w: 0.84, h: 0.22}
    case: upper
    tracking: 60
    size_min: 0.035
    size_max: 0.10
    max_lines: 3
    shadow: {alpha: 0.45}
  - id: subtitle
    zone: {x: 0.10, y: 0.845, w: 0.80, h: 0.05}
    optional: true
    size_min: 0.016
    size_max: 0.026
  - id: author
    zone: {x: 0.10, y: 0.91, w: 0.80, h: 0.05}
    case: upper
    tracking: 140
    size_min: 0.018
    size_max: 0.030
layers: [background, texture, scrim:0, scrim:1, title, subtitle, author]
```

Loader (`archetypes.py`): read every `*.yaml` in `config/cover/archetypes` via `resource_root()`, validate into pydantic, expose `ARCHETYPES: dict[str, Archetype]` and `describe_archetypes()` (name + describe lines for the direction prompt — same pattern as `BookDesign.describe_subjects`). A malformed archetype file should fail loudly at import/first-use, not silently drop.

### 5.2 The three launch archetypes (DECIDED)

1. **`big_type`** — literary/memoir/nonfiction. Background is a *procedural* field (vertical two-stop gradient of `background`→slightly shifted lightness) or an optional generated abstract texture; the title is huge (size_max ~0.16, max_lines 4, stacked, left-aligned, upper), author bottom-left. Type IS the cover. No focal art slot. This archetype must look excellent with zero generated images — it is the $0 fallback and the print-safest design.
2. **`full_bleed_art`** — genre fiction default (YAML above): one generated scene, banded type on scrims.
3. **`cutout_sandwich`** — the showpiece: `background` (generated scene, `composition_note` asks for a relatively clear middle) → title (middle zone, large) → `focal` (generated **transparent-background** subject — a figure, object, or creature, `fit: contain`, zone-positioned to overlap the title's lower edge) → author. The focal element overlapping the title is the "a designer did this" effect no prompt-only generator produces. If the focal generation comes back non-transparent (§7.2 fallback), the composer degrades to rendering title ABOVE the focal layer and records a warning.

### 5.3 Template library (DECIDED 2026-08-28: 12–15 genre-tagged templates in v1)

Beyond the three launch archetypes, the library grows to 12–15 total, each grounded in a **current bestseller cover convention** (researched, not invented) and tagged by genre:

- Each archetype YAML gains an optional `genres:` list using the 10 subject keys (empty/absent = fits all genres — the three launch archetypes stay untagged).
- `describe_archetypes(genre: str | None = None)` filters to archetypes matching the genre plus the untagged ones; no match or `None` → all. The direction call passes `brief.genre` (normalized; free-text genres that aren't a subject key → no filter).
- Quality bar per template: loads/validates; `build_spec` produces a valid CoverSpec from it for every direction shape; a **procedural test render** (no API spend) passes the legibility autopilot with warnings only where expected; the 100px thumb of its test render is legible.
- Templates encode *convention*, not decoration: zone geometry, case/tracking/size ranges, scrim style, and a composition_note that steers generated art to leave the right negative space — each derived from named, current, real-shelf patterns per genre (e.g. the romantasy foiled-emblem center, the thriller big-type-over-texture stack, the romcom flat-vector full-bleed, the literary quiet-centered classic).

## 6. Model calls (`docproof/cover/direction.py`)

Both calls go through the existing `Provider` protocol exactly like [skin.py](../docproof/quest/skin.py): `provider.complete_structured(model=..., system=..., user=..., schema=strict_json_schema(Model), schema_name=..., max_tokens=8000)`, usage priced with `cost_of_usage`. Default model: `gpt-5.6-luna` (the site's existing cheap model; module constant, overridable per call). Effort "low" is set by the caller building the provider (mirror `_provider()` in [app/routes/quest.py:66](../app/routes/quest.py)).

### 6.1 Art direction: `run_directions(brief, provider, *, n) -> list[Direction]`

One call returns all N concepts (a `Directions` wrapper model with `concepts: list[Direction]`, min/max length enforced). System prompt requirements:

- Role: senior book-cover art director at a traditional press. Concepts must be **distinct from each other** (different archetype OR sharply different palette/imagery — say this explicitly).
- Genre conventions matter and should be honored by default (romance ≠ horror palettes), with the brief's `mood`/`avoid` overriding.
- Enumerate the archetypes with their `describe` lines; the model picks per concept. At least one concept must use `big_type` when `brief.concepts >= 3` (cheap, print-safe, and often the best one).
- Enumerate the font families with short character notes (from `fonts.py` role tags, §7.1) — the model picks `title_font`/`author_font` from that closed list (schema-enforced).
- Palette: five hexes by role. Require real contrast intent: `text` must be readable over `background`+`scrim` (the composer enforces it mechanically later, but ask for it).
- `art_prompts`: for each generatable slot the chosen archetype declares, a 1–3 sentence image prompt describing subject, style, lighting, era, and medium ("flat vector", "oil painting", "photographic", "paper-cutout collage" …). Rules to state verbatim in the system prompt: **never ask for text, letters, numbers, typography, book covers, mockups, borders, or frames; never name a living artist; describe a scene, not a cover.** The composition note is appended by code, not by the model.
- If `brief.pitch` is present, ground imagery in it; never spoil an ending on the cover.

User prompt: the brief fields, labeled — followed by a `MANUSCRIPT SAMPLE` section when the job has one (§8.1). **Manuscript upload is in scope (DECIDED 2026-08-28):** reuse `read_sample_source` and `sample_text` from [docproof/quest/skin.py](../docproof/quest/skin.py) (they already read .docx/.txt/.md and take an opening + middle slice so front matter doesn't get the only vote). When a sample is present, the system prompt additionally instructs: ground imagery, mood, and palette in the manuscript's actual text and era/setting; typed brief fields always win over the sample on any conflict (title, author, genre); never spoil an ending. Signature: `run_directions(brief, provider, *, n, manuscript_sample: str = "")`.

Failure: no `DEFAULT_DIRECTION` fallback — unlike skins, a junk direction wastes image dollars. Raise `DirectionError` with a human sentence; the route surfaces it and the job ends in state `error`. One deliberate exception: an art prompt for a slot the chosen archetype doesn't generate is dropped with a log line rather than raising — `build_spec` never reads it, dropping costs nothing, and a live run proved that failing a whole multi-concept job over one surplus prompt is the wrong trade. Only a fabricated archetype (or wrong concept count, schema mismatch, call failure) stays fatal.

### 6.2 Revision: `revise_spec(spec, notes, provider) -> CoverSpec`

Input: current `CoverSpec` (JSON-dumped into the user prompt) + the human's notes. Output schema: **a small list of patch edits, not the full document** — `SpecEdits{edits: [{path, value}]}` where `path` is dotted-with-indices (`palette.primary`, `text[1].zone.y`, `art[0].prompt`, `layers` for a whole-list replace) and `value` is the new value JSON-encoded as a string. Full-document echo is dead on arrival: Anthropic's structured-output grammar compiler rejects the full CoverSpec schema as too large (verified live, and collapsing enums wasn't enough), while the edits schema is tiny, cheaper on output tokens, and immune to copy-through drift. Code applies the edits to the spec dict (a list index equal to the list's length appends; guarded paths — `version`, `notes_log`, `art[*].asset` — and unresolvable paths are skipped and reported on `RevisionResult.skipped`), then re-validates with the real `CoverSpec` model, so wire looseness costs nothing. System prompt requirements:

- You are editing a design document with targeted patches, not rewriting it. Emit edits **only** for what the notes require; everything you don't touch stays exactly as shown. Three worked examples in the prompt (zone move, palette recolor, type resize) pin the path syntax.
- You may: move/resize zones, change palette hexes, swap fonts (closed list), change case/tracking/align, adjust scrim strengths and art transforms (scale/offset/anchor), rewrite an `art[n].prompt`, toggle texture.
- You may not: change `archetype` (unless the notes explicitly ask), invent new slots (express new imagery as prompt/treatment changes on existing slots), change text `content` unless the notes dictate new wording, or touch `version`/`notes_log`/`asset` (code owns those and skips such edits).
- Zero valid edits = an honest no-op: the pipeline's identical-spec early-stop ends the critique loop rather than looping on nothing.

Post-call, in code: `version += 1`; `notes_log.append(notes)`; **diff the art slots** — a slot whose `prompt` or `transparent` changed gets its `asset` cleared, which is the signal to regenerate that one image (and only that one). Validate the result; on validation failure raise `RevisionError` (the UI keeps the old version and shows the sentence).

### 6.3 Critique pass (DECIDED 2026-08-28: in v1; iterated, given two images, and given a bounded art-repaint escape hatch in the BRAIN wave / BRAIN v2.1, 2026-08-29)

After a concept first composes, and **before** it is marked `ready`, a vision model reviews the finished cover the way an art director reviews a proof — and, since the BRAIN wave, can loop this critique-then-revise cycle up to `MAX_CRITIQUE_ROUNDS` (4) times per concept: the owner's beta verdict was that a single fixed round left the judge merely reporting problems instead of getting them fixed. Module `docproof/cover/critique.py`:

- `@dataclass(frozen=True) CritiqueResult: passes: bool; tells: list[str]; notes: str; cost: float | None; art_defects: list[str] = []`
- `run_critique(png_bytes: bytes, thumb_bytes: bytes | None, spec: CoverSpec, brief: Brief, client, *, model=CRITIQUE_MODEL, composer_warnings: Sequence[str] = ()) -> CritiqueResult` — the `Provider` protocol is text-only, so this call uses the `anthropic` SDK directly (same "vendor SDK lives in its own module" precedent imaging.py sets for the `openai` SDK, just a different vendor; the actual request shape mirrors `docproof/providers/anthropic_provider.py`'s own structured-output call almost exactly). Sends **one or two images**: the composed render downscaled to ≤600px wide always, plus — BRAIN v2.1 — the job's own 100px shelf/search thumbnail (`save_renders`'s `_thumb100.png` companion file, read straight off disk next to the render) when one exists, each labeled so the judge can address either by name; a missing thumbnail (an old job, a caller that never rendered one) degrades to sending the one image and never fails the call over it. Plus a one-paragraph summary of brief + genre + this concept's generated art slots (id + prompt, so `art_defects` can name one) + any composer measurements (`composer_warnings` — the composer's own `RenderReport.warnings` for this exact render, clearly labeled, so the judge reasons with real measurements instead of re-deriving them by eye). Structured output. System prompt: you are reviewing a proof for a traditional press; would this pass on a trad-published shelf in this genre? Name concrete tells (type crowding, weak hierarchy, text fighting busy art, palette mismatch, AI-art artifacts, genre miscues, a large empty band with nothing designed in it, an element so low-contrast against its ground it disappears, a cover that reads as a blank field with words at thumbnail size). `passes=true` means ship it. `notes` = one actionable **design-only** revision instruction in the voice of §6.2 notes (it may NOT request new art — that's what `art_defects` is for). `art_defects` = the id of every art slot named in that same summary whose GENERATED IMAGE ITSELF is the problem — a visible generation artifact, anatomical or structural nonsense on an inanimate object, an illegible/warped subject — as opposed to a slot whose art is clean but simply mis-designed; that distinction is exactly what lets the pipeline tell "revise the design" apart from "repaint the art," below.
- Pipeline integration (`run_job`): compose → critique → if `passes` or the critique call fails (a critique failure must never block a cover — log, ledger note, proceed), mark `ready`; else run the §6.2 revision machinery with `critique.notes` and `allow_new_art=False`, recompose, and critique again — up to `MAX_CRITIQUE_ROUNDS` critique calls total (so at most `MAX_CRITIQUE_ROUNDS - 1` revisions), stopping early on a genuine pass, a revision that comes back byte-identical to its input (nothing left to give — unless a repaint also happened this round; see below), or a critique/revision call failure. A round that still fails on the very last permitted round ships with its tells as leftover `RenderReport.warnings` rather than buying a revision nothing will ever re-check. Ledger row `{kind: "critique", detail, usd}` per round; the FINAL verdict's leftover `tells` (if any) are appended to `RenderReport.warnings`; every applied note lands in `notes_log`, prefixed `[auto-critique r{n}]`, with a second, CODE-COMPUTED `[auto-critique r{n} changed] ...` entry right after it naming what the revision actually changed (`diff_spec_fields`, diffing the validated spec models themselves — never the model's own prose) — "revising did nothing" is visibly impossible this way.
- **Art-repaint escape hatch (BRAIN v2.1):** a design-only revision can never fix a defective GENERATED image (a surreal blob standing in for the intended object, an eye baked into a lighthouse lantern). When a failing verdict names `art_defects`, at least one more round remains, and this concept has not already used its repaint this job, the pipeline clears up to 2 of those flagged slots' `asset` fields and repaints them through the same `_generate_art_slot` path a fresh concept's own art uses — the same prompt, or the one this round's design revision just rewrote if it touched that slot (regeneration re-rolls the image either way) — then recomposes and continues the loop. Ledger row per repainted slot: `{kind: "image", detail: "concept N slot X repainted on judge's flag", usd: 0.0}`, alongside the normal costed image-generation row. **Hard bound: at most one repaint round per concept per job, and auto path only** — a human-triggered revision (§6.2) never repaints on its own initiative. Because a repaint changes real pixels on disk without changing the spec's `asset` string at all (the path is a deterministic function of concept index + slot id), the identical-spec early stop is blind to it by construction and is skipped for any round in which a repaint just happened.
- Revisions triggered by humans do NOT re-run critique (the human is the critic there), and never trigger a repaint on their own initiative.
- Model: default `claude-sonnet-5` (vision-capable) via a module constant; cost is a few tenths of a cent per critique call.

Also (same decision date): the §6.1 art-direction system prompt gains one rule — prefer illustrated, painterly, or graphic media for `art_prompts`; avoid photorealistic renders unless the brief explicitly calls for photography (stylized media hide generation artifacts; photoreal is the biggest "AI look" tell).

## 7. Rendering

### 7.1 Fonts (`docproof/cover/fonts.py`)

A small static registry — no fontTools dependency:

```python
@dataclass(frozen=True)
class CoverFont:
    family: str          # display name, the value model calls choose
    file: str            # filename under config/prep/fonts
    vibe: str            # one line for the direction prompt
    caps_friendly: bool  # good tracked-uppercase face
```

Populate all 10 families from `config/prep/fonts` (family names per its README: e.g. `IM FELL English` ↔ `IMFellEnglish-Regular.ttf`, Spectral for body/author lines, Playfair Display, Cormorant Garamond, EB Garamond, Lora, Orbitron, Pirata One, Quicksand, Special Elite). `FAMILIES: dict[str, Path]` resolves via `resource_root() / "config" / "prep" / "fonts"`. `describe_fonts()` feeds §6.1. Author-line default when a direction only picks a title font: `Spectral SemiBold`.

Expansion of the font library is out of scope; note it in the code as the obvious later win.

### 7.2 Imaging (`docproof/cover/imaging.py`)

**Engine (DECIDED 2026-08-28): `gpt-image-2`** via the `openai` SDK (already a dependency). It is the top instruction-following image model in blind-vote rankings as of August 2026, which is the property this architecture leans on hardest (composition-aware prompts: "leave the lower third uncluttered", "no text anywhere"). It generates at 1K/2K/4K with a native 2:3 aspect ratio, supports transparent backgrounds (preview, `background="transparent"`), and is cheaper than its predecessor (~$0.03/1K, $0.05/2K, $0.08/4K). Default: **2K, 2:3** (≈1365×2048; the composer's cover-fit upscale to the 1600×2560 canvas is mild). Midjourney still has no official API (enterprise-only, ToS bans automation) and is excluded; Google's Imagen line was deprecated in August 2026 (Gemini 3.x Image is the successor — the `google-genai` SDK is already installed if a second engine is ever wanted, but don't build it in v1).

- Key comes from the same path the skin call uses — `app.settings.get_api_key("openai")` falls back to the `OPENAI_API_KEY` env var, which is how the Fly box is configured.
- `generate(client, prompt, *, transparent=False, resolution="2K") -> bytes` (PNG bytes). `client.images.generate(model="gpt-image-2", ...)`, decode `b64_json`. **Verify the exact size/aspect parameter names against the current gpt-image-2 API reference at implementation time** (the resolution/aspect interface differs from gpt-image-1's `size`/`quality`); request 2:3, PNG. One retry on transient failure (rate limit / 5xx), then raise `ImagingError` with a human sentence.
- Prompt assembly (in `pipeline.py`, not here): `slot.prompt + " " + archetype.composition_note + NEGATIVE_SUFFIX` where `NEGATIVE_SUFFIX` is a module constant: `"Absolutely no text, no letters, no words, no numbers, no watermarks, no borders, no frames."`
- Transparent-cutout validation: after decoding, check the alpha channel actually varies (min alpha < 8 somewhere near the borders). If the model ignored transparency (the feature is in preview), keep the image but mark the slot opaque so the composer can degrade layer order (§5.2.3).
- Cost table: `IMAGE_COST: dict[str, float]` keyed by resolution. Seed with gpt-image-2 list prices — approximately `"1K": 0.03`, `"2K": 0.05`, `"4K": 0.08` — **verify against the current OpenAI pricing page during implementation** and cite the URL in a comment. Every generation appends `{slot, resolution, usd}` to the job's cost ledger. Image costs are tracked in the job ledger only; do not thread them into `Usage` (that plumbing exists for the main app — `Usage.sapling_cost` is the precedent — but the quest site doesn't roll up `Usage`, so keep it local).
- Keep the wrapper engine-shaped, not OpenAI-shaped (one `generate()` seam): the likely second engine is **Recraft V4.x vector** (native SVG output — resolution-independent focal elements and flat-illustration styles, ideal for the print path and clean cutouts). Stub comment only; don't build it.
- Mask-based inpainting (`images.edit`) is a later phase; leave a stub comment, don't build it.

### 7.3 Composer (`compose.py` + `typeset.py`)

Pillow only. **At import, check `PIL.features.check("raqm")`** — the manylinux/macOS wheels bundle libraqm (shaped text with real kerning); if it's absent, log one warning and continue with basic layout (covers still render, slightly worse kerning). Never hard-require raqm.

`compose(spec, job_dir, canvas=(1600, 2560)) -> tuple[Image, RenderReport]`, deterministic — no RNG anywhere (the grain texture uses a fixed seed). Walk `spec.layers` bottom-up:

**Art layers.** Load `job_dir / slot.asset` (or synthesize procedurally: `big_type` gradient field from the palette; `texture` = fixed-seed monochrome Gaussian noise at 25% scale, resized up, applied with the slot's blend/opacity). Fit `cover` = scale to fill + crop about `anchor`; `contain` = scale to fit zone. Apply `scale`/`offset`. Blend modes: `normal` = alpha composite; `multiply`/`overlay`/`soft_light` via `PIL.ImageChops` (soft_light exists; overlay via `ImageChops.overlay`).

**Scrims.** A scrim protecting a slot derives its zone from that slot's zone padded by 4% canvas on all sides. `panel` = solid rounded-nothing rectangle of scrim color at `strength` alpha. `gradient_down`/`up` = vertical alpha ramp (0 → strength) across the zone, extended to the nearest canvas edge below/above. `vignette` = radial. Draw onto an RGBA overlay, composite.

**Text slots** (in `typeset.py`):

1. Skip empty `optional` slots.
2. Apply `case`.
3. **Fit search:** binary-search the font size in `[size_min, size_max]` (converted to px via canvas height; 12 iterations) for the largest size whose best line-breaking fits the zone. Measure with `ImageFont.truetype(path, px).getlength()` plus `tracking` (tracking px = `tracking / 1000 * size_px`, added per inter-glyph gap).
4. **Balanced breaking:** for 1..max_lines, consider all word-boundary break placements (titles are short — brute force is fine, cap at 12 words considered); a candidate is valid if every line fits the zone width; score = variance of line widths; take the lowest-variance valid candidate with the fewest lines that permits the largest font. Never hyphenate. Line height = 1.08 × size for display slots.
5. If even `size_min` can't fit, render at `size_min` with the least-bad breaking and add a `RenderReport.warning`.
6. **Render:** each line drawn onto its own transparent layer. With tracking: draw glyph-by-glyph advancing `getlength(ch) + tracking_px` (tracked text is all-caps display type; per-glyph kerning loss is acceptable there). Without tracking: draw the whole line (raqm shapes it). Stroke via Pillow's `stroke_width`/`stroke_fill`. Shadow: draw the text alpha in shadow color, `GaussianBlur(blur_px)`, composite at `(dx, dy)` under the text at `alpha`. Align/valign within the zone.

**Legibility autopilot** (the step that makes output look designed, not generated):

- Before rendering each text slot, crop the currently-composited canvas under its zone; compute mean relative luminance (WCAG formula) and stddev (busyness).
- Compute the contrast ratio between the slot's palette color and the zone's mean luminance. Threshold: **4.5 for title/author, 3.0 for subtitle/series.**
- If under threshold: find the scrim protecting this slot and escalate `strength` by +0.15 steps (recompositing the scrim region) until the ratio passes or strength hits 0.85. Still failing at 0.85 → flip the text color to whichever of `#111111`/`#f5f1e8` contrasts better, re-measure, and record a warning. Record final ratios and strengths in `RenderReport`.
- If zone stddev > 0.22 (busy art under text) and the slot has no shadow, add the default `Shadow` automatically.

**Outputs** per render: `renders/v{version}_c{concept}.png` (full canvas RGBA flattened to RGB), `.jpg` (quality 90, sRGB), `_thumb300.png`, `_thumb100.png`, plus the `RenderReport` embedded in job state. The 100px thumb is displayed in the UI next to the full render — the "would you click it in a search result?" check as a first-class artifact.

### 7.4a Effects rack (DECIDED 2026-08-28: in v1, built after the template library lands)

Deterministic, $0 post-generation operations that push the output further from raw model pixels. All are Pillow point/mask ops in the composer; the critique pass judges the *treated* result, so bad combinations get caught automatically. Everything stays strict-schema-safe (no dicts, no tuples on the wire).

**Slot treatments** — `ArtSlot.treatment: Literal["none", "duotone", "silhouette", "posterize", "sticker"] = "none"`, applied after fit/placement, before compositing. Colors come from the palette by fixed rule (no per-slot color params in v1): duotone maps luminance onto a background→primary ramp; silhouette thresholds to a flat primary shape; posterize quantizes to 4 levels then snaps each to the nearest palette color; sticker dilates the alpha into a `text`-color outline (transparent slots only; on an opaque slot it is a no-op plus a report warning).

**Double exposure** — `ArtSlot.mask_from: str = ""`: after placement, this slot's pixels are kept only where the named slot's positioned alpha is opaque (art poured inside another slot's silhouette). Empty = off. Validation: the referenced slot must exist and precede it in `layers`; a dangling reference fails spec validation.

**More art slots** — widen the slot ids to `background | focal | focal2 | foreground | texture` (model, archetypes, prompts). Everything already generic (cutout suffix, alpha check, opaque degrade) applies per-slot unchanged. Depth-collage archetypes may now declare three generated layers; cost is linear per image.

**Knockout type** — `TextSlot.mode: Literal["fill", "knockout", "art_fill"] = "fill"`: `knockout` punches the glyphs out of a solid palette-`primary` panel covering the zone (padded 4%); `art_fill` fills the glyphs with the art+scrim ground beneath the panel's position (the title *is* a window into the art). Both skip the ink-color legibility loop (panel vs. ground contrast is measured instead, same thresholds).

**Mirrored corner frame** — `ArtSlot.corners: bool = False` (transparent slots): the generated ornament is placed in the top-left corner at its scale, then mirrored horizontally, vertically, and both, into the other three corners. Exact symmetry is the "a designer did this" tell generators can't produce; it is the romantasy-emblem convention's backbone.

**Motif scatter** — `ArtSlot.scatter: int = 0` (transparent slots, 2–12): the motif is stamped `scatter` times at a fixed-seed arrangement derived from the spec version (deterministic), sized down to ~14% canvas height each, never inside any text zone's padded rect.

**Direction vocabulary** — archetypes may preset any of these in YAML (the cozy graphic-stamp and nonfiction color-block conventions want duotone; the emblem wants corners), and the art-direction call may set `treatment` per art prompt (extend the ArtPrompt object with `treatment: Literal[...] = "none"`); revisions may change all of them (they're spec fields — the §6.2 machinery already covers it). The §6.1 system prompt gains one paragraph naming the rack and when each effect earns its place; the composition rules stay code-side.

**Tests** — every treatment: deterministic on a fixed synthetic image at small canvas, palette-coherence assertions (duotone output contains only ramp colors), sticker no-op warning path, mask_from validation failure, corner mirroring symmetry (pixel-compare flipped quadrants), scatter avoids text zones, knockout/art_fill contrast measurement path.

### 7.4 Concurrency & memory (512MB box)

- One `asyncio.Lock` around composition (Pillow buffers: 1600×2560×4 ≈ 16MB per layer; a compose touches ~5 — fine, but only one at a time).
- Image generations for one job run with `asyncio.gather` bounded by `Semaphore(2)`; they're network-bound, not memory-bound (SDK calls via `asyncio.to_thread`).
- Everything on disk immediately; nothing large held between requests.

## 8. Job store & pipeline (`docproof/cover/pipeline.py`)

Root: `COVER_DATA_PATH` env (Fly: `/data/cover`; local default: `cover_jobs/` under cwd). Layout:

```
<root>/<job_id>/            # job_id = UTC timestamp + 6 hex chars, e.g. 20260828-a1b2c3
  job.json                  # the single source of truth (JobState, atomic-rewritten)
  manuscript_sample.txt     # opening+middle sample, only when a manuscript was uploaded
  assets/c{n}_{slot}.png    # generated art, named by concept index + slot id
  renders/v{k}_c{n}.(png|jpg) + thumbs
```

### 8.1 Manuscript handling

When job creation includes a manuscript file: validate suffix/size exactly like `_read_upload` in [app/routes/quest.py](../app/routes/quest.py) (`.docx/.txt/.md`, 40MB cap, human-sentence errors), write it to a temp dir, run `read_sample_source` + `sample_text`, persist only the sample text to `manuscript_sample.txt` (never the full manuscript — the sample is all the direction call reads), and record `manuscript_name` and `word_count` on `JobState` (add both fields: `manuscript_name: str = ""`, `word_count: int = 0`). An unreadable file fails job creation with a 400 sentence — before any model spend. Revisions re-read the stored sample so grounding survives across versions.

`JobState` (pydantic, serialized to `job.json` after **every** step so a poll — or a machine restart — always sees truth):

```python
class ConceptState(BaseModel):
    spec: CoverSpec
    status: Literal["queued", "painting", "composing", "ready", "error"]
    error: str | None = None
    report: RenderReport | None = None
    renders: list[str] = []            # relative paths, all versions, newest last

class JobState(BaseModel):
    job_id: str
    brief: Brief
    status: Literal["directing", "working", "ready", "error"]
    error: str | None = None
    concepts: list[ConceptState] = []
    ledger: list[dict] = []            # {kind: "direction"|"image"|"revision", detail, usd}
    created: str                       # ISO UTC
```

Flow (`run_job`, invoked as an asyncio background task by the route):

1. `directing`: one `run_directions` call → build a `CoverSpec` per direction (`build_spec`) → concepts persisted as `queued`.
2. Per concept, independently (bounded gather): `painting` → generate each generatable art slot whose `asset` is empty → `composing` → `compose` → `ready`. A concept that fails (imaging error) lands in `error` with its sentence; **other concepts continue**.
3. Job `ready` when every concept is terminal.

`run_revision(job, concept_index, notes, allow_new_art: bool)`: `revise_spec` → if `allow_new_art`, regenerate cleared art slots, else restore prior `asset` paths even if prompts changed (note in ledger: "art change requested but art regen disabled") → recompose → append render. Revisions mutate the concept's spec in place (full history is recoverable from `notes_log` + renders; do not build a spec-version store in v1).

Scale-to-zero honesty: background tasks die if the machine stops. Polling keeps it alive during active work; if a poll finds a job stuck in a non-terminal state with no task running (track live tasks in a module-level dict), mark it `error: "interrupted — run it again"`. Do not build resumption.

## 9. HTTP API (`app/routes/cover.py`)

`register(app)` called from `quest_site.create_app()`. **Every** `/api/cover/*` endpoint first passes the key gate:

- `COVER_KEY` env unset → 503 `"Cover Studio is not enabled on this deployment."` (public deploys stay inert until Quinton sets the secret: `fly secrets set COVER_KEY=... -a spell-and-check`).
- Header `X-Cover-Key` missing/wrong → 401 with a polite sentence. Compare with `hmac.compare_digest`.

Rate limits (new `RateLimiter` instances in `quest_site.py`, keyed like the skin ones): jobs 10/IP/day and 40/site/day; revisions 60/IP/day. These cap worst-case spend at roughly $25/day sitewide.

| Endpoint | Contract |
|---|---|
| `POST /api/cover/jobs` | `multipart/form-data`: a `brief` form field holding the `Brief` JSON + an optional `manuscript` file (§8.1). Validates both, creates the job dir, spawns `run_job`, returns `{job_id}` 202. |
| `GET /api/cover/jobs/{job_id}` | Returns `JobState` JSON plus `total_usd` (ledger sum). Poll target; `Cache-Control: no-cache`. |
| `POST /api/cover/jobs/{job_id}/revise` | Body `{concept: int, notes: str, allow_new_art: bool}`. 409 if that concept isn't `ready`/`error`. Runs revision as a background task; 202. |
| `GET /api/cover/jobs/{job_id}/file/{name}` | Serves a file from the job's `renders/` (and `spec` via `?concept=n` returning the spec JSON as a download). **Reject any `name` containing `/` or `..`; resolve and require the result under the job dir.** |
| `GET /api/cover/jobs` | Last 20 jobs: `{job_id, title, status, created, total_usd}` — the "pick up where I left off" list. |

`python-multipart` is already installed (the quest endpoints upload files the same way).

## 10. Frontend (`app/static/sc-cover.html`)

`<body data-page="cover">`, shared chrome mounts normally. Follow the existing sc pages for structure: inline `<script>`, vanilla JS, `sc-shared.css` tokens, engraved-rule section frames, scrap-paper buttons, `aria-live="polite"` on the progress region, skip-link + `#main` (copy the pattern from `sc-quote.html`).

Views (one page, sections shown/hidden):

1. **Unlock** — shown when `sessionStorage['sc-cover-key']` is empty: one key field + scrap button; on submit, store and `GET /api/cover/jobs` to verify (401 → shake, clear). All fetches send `X-Cover-Key`.
2. **Brief form** — fields from §4 `Brief`; genre as a `<select>` of the 10 subject keys plus an "other…" free-text reveal; concepts as a 2/4/6 radio (default 4); an **optional manuscript drop zone** (.docx/.txt/.md — mirror the drag-drop affordance the /quote page already has) that shows a "grounded in *filename*" chip once chosen, with a remove control. Submit builds `FormData` (`brief` JSON + optional `manuscript` file) → create job, write `sc-cover-job` to sessionStorage, show progress (progress view shows the word count when a manuscript was read). Below the form: the recent-jobs list (title, date, status, cost) — click to reopen.
3. **Progress** — poll every 2.5s; per-concept status lines in Galley's quiet register ("mixing pigments… painting… setting type…" — keep it subtle; this is an internal tool, one voice line per state, no theatrics). Reload-safe: on boot, if `sc-cover-job` exists, resume polling.
4. **Contact sheet** — grid (`repeat(auto-fill, minmax(0, 260px))`) of concept cards: 300px thumb, the 100px thumb beside it at actual size (labeled "search-result size"), concept name, rationale, font + palette chips (five swatches), per-concept cost, warnings from `RenderReport` in small type. Click a card → **detail view**: full render (max-height 80vh), version strip if revised, download buttons (JPG / PNG / spec JSON — plain `<a href download>` to the file endpoint with the key as a query param is NOT allowed; fetch with the header and `URL.createObjectURL` instead), notes `<textarea>` + "allow new art" checkbox + Revise scrap-button. Revise → back to polling that concept.
5. **Errors** — every API sentence renders inline near its control; a concept card in `error` shows the sentence and a Retry (= revise with empty notes + allow_new_art).

Nav: in `sc-shared.js` `headerHTML()`, append `<a href="/cover">Cover studio</a>` only when the key is present in sessionStorage (try/catch the read; sessionStorage can throw). Footer unchanged.

## 11. Tests

No network, no OpenAI, no real image generation in tests. Follow the repo's existing test idioms (pytest, `httpx` TestClient for routes).

- **model:** spec/brief validation edges (bad hex, zone out of bounds, unknown font family rejected, `extra="forbid"`).
- **archetypes:** all shipped YAMLs load and validate; zone math px conversion; `layers` references resolve.
- **typeset:** with a bundled font (`config/prep/fonts/Spectral-Bold.ttf`): fit search monotonicity (longer titles → smaller-or-equal size), balanced breaks beat greedy on a known string, never exceeds zone, tracking widens measured width, size_min floor + warning path.
- **compose:** procedural-only spec (`big_type`, no assets) renders 1600×2560; determinism (two composes → identical bytes — allowed because inputs are procedural and seeded); legibility escalation: dark text on dark procedural background forces scrim strength up and the report shows ratio ≥ threshold; busy-art stddev path adds a shadow. Use tiny canvases (400×640) where speed matters — all geometry is fractional, so this is free.
- **imaging:** fake client object; prompt assembly includes composition note + negative suffix; transparent-flag plumbed; alpha-validation fallback flips the slot opaque; ledger entries written; retry-then-raise.
- **pipeline:** monkeypatched `run_directions` + imaging: full job reaches `ready`, per-concept isolation (one concept's ImagingError doesn't kill siblings), `job.json` written between steps (kill it mid-flight by making the second concept raise, then reload state from disk), revision bumps version + clears changed-art assets, interrupted-detection marks stale jobs.
- **routes:** 503 without `COVER_KEY` env; 401 wrong key; happy path create→poll→file; multipart create with a tiny `.txt` manuscript → sample persisted, word_count set, direction prompt received the sample (assert via the monkeypatched call); oversized/wrong-suffix manuscript → 400/413 sentence with no job dir left behind; path traversal on the file endpoint (`..%2f`, absolute, `name` with slash) → 400/404; rate limit 429; revise on a non-ready concept → 409.

Run the whole suite (`pytest`) before finishing; the repo has ~4,000 tests and they must stay green.

## 12. Print wrap — design constraint now, build later

Do not build. Do keep these invariants so the later phase is additive:

- All geometry is **fractional**; the composer takes an explicit canvas. A wrap render is the same spec applied per-panel (front panel = today's canvas; back and spine are new archetype panel definitions) on a canvas of `((2 × trim_w) + spine_w + 2 × bleed, trim_h + 2 × bleed)` at 300dpi.
- Spine width is a config table, per printer/paper: KDP ≈ 0.002252″/page (white) / 0.0025″ (cream); Ingram uses per-stock PPI divisors — the table ships with "verify against the printer's current calculator" comments.
- **Art resolution** is largely solved by the engine choice: gpt-image-2's 4K tier (in beta as of 2026-08) exceeds the 300dpi front-panel requirement (≈1875×2775+bleed), and `big_type`'s procedural fields render at any resolution. Residual risks for the wrap phase: 4K graduating from beta, and full-wrap panoramas (back+spine+front as one image) which may still want a Recraft-vector or tiled approach — decided then.
- PDF assembly (TrimBox/BleedBox, RGB for KDP first) will be one new dependency then (`pikepdf` or `reportlab`); do not add it now.

## 13. Acceptance checklist

1. `pip install -e ".[app,cover,dev]"` clean; `pytest` green including the new suites.
2. `spell-and-check` locally with `OPENAI_API_KEY` and `COVER_KEY=test` in env: `/cover` unlocks, a real brief ("The Lighthouse at Gull Point", literary, 4 concepts) reaches `ready` with 4 distinct concepts including one `big_type`; total cost visible and ≈ $0.15–0.40 at 2K.
3. Every render passes its contrast thresholds in `RenderReport` (or carries an explicit warning), and the 100px thumb of at least the `big_type` concept has a legible title.
4. A revision ("make the title bigger and the palette warmer, keep the art") completes **without** any image spend (ledger shows no new image rows) and produces a visibly changed v2.
5. A revision with "different imagery — make it a lighthouse at night" and allow_new_art regenerates exactly one image for that concept.
6. Public-visitor experience unchanged: no nav link without the key, `/api/cover/*` returns 503 when `COVER_KEY` is unset, existing pages byte-identical except `sc-shared.js`.
7. Package-data proven: `pip install .` into a scratch venv (no source checkout) and confirm archetype YAMLs and fonts resolve (`python -c "from docproof.cover.archetypes import ARCHETYPES; ..."`).
8. Version bumped in `docproof/__init__.py`; `Dockerfile.quest` installs `.[app,cover]`; `fly.quest.toml` carries `COVER_DATA_PATH`.
9. Do not deploy. Hand off with the two secrets Quinton must set (`COVER_KEY`; `OPENAI_API_KEY` already set) and the deploy command (`fly deploy -c fly.quest.toml`).

## 14. Out of scope (v1)

Inpainting/mask edits; upscaling; print wrap and PDF output; IDML export; layered-PNG bundle export; font-library expansion; Spell & Check consumer-facing skinning of this tool (an "Illuminator" party member is a later product decision); accounts, Stripe, email delivery; persistent spec-version history beyond the render strip.
